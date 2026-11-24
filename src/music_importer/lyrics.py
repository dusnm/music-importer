"""Lyrics from LRCLIB (https://lrclib.net) as sidecar files next to each track.

  01 Title.flac  ->  01 Title.lrc   time-synced lyrics (preferred)
                     01 Title.txt   plain lyrics when no synced version exists

Navidrome reads these by default (LyricsPriority ".lrc,.txt,embedded"). Audio files are
never modified. Tracks that already have a sidecar or embedded lyrics are skipped.
Results are remembered in DATA_DIR/lyrics.db so a re-run only retries tracks that were
not found more than RETRY_DAYS ago.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import requests
from mutagen import File

from . import art, jobs

log = logging.getLogger(__name__)

API = 'https://lrclib.net/api'
UA = 'music-importer/0.4 ( no-contact )'
DURATION_TOLERANCE = 2.0      # seconds; LRCLIB's own matching uses the same
REQUEST_INTERVAL = float(os.environ.get('LYRICS_REQUEST_INTERVAL', '0.5'))
RETRY_DAYS = float(os.environ.get('LYRICS_RETRY_DAYS', '30'))
EMBEDDED_KEYS = ('lyrics', 'unsyncedlyrics', 'USLT', 'SYLT', '©lyr')

_session = requests.Session()
_session.headers['User-Agent'] = UA
_rate_lock = threading.Lock()
_last = 0.0


# ---------- state ----------
def _db(data_dir: Path) -> sqlite3.Connection:
    con = sqlite3.connect(data_dir / 'lyrics.db', timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE IF NOT EXISTS tracks (path TEXT PRIMARY KEY, status TEXT, checked REAL)')
    return con


# ---------- LRCLIB ----------
def _get(endpoint: str, params: dict) -> requests.Response | None:
    global _last
    for attempt in range(5):
        with _rate_lock:
            wait = REQUEST_INTERVAL - (time.monotonic() - _last)
            if wait > 0:
                time.sleep(wait)
            _last = time.monotonic()
        try:
            r = _session.get(f'{API}/{endpoint}', params=params, timeout=30)
        except requests.RequestException as e:
            log.debug('lrclib %s failed: %r', endpoint, e)
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 2)
            continue
        return r
    return None


def lookup(artist: str, title: str, album: str, duration: float) -> dict | None:
    """Best LRCLIB record for a track, preferring synced lyrics; None if not found.

    Raises RuntimeError when LRCLIB could not be reached (so the track is retried later)."""
    r = _get('get', {'artist_name': artist, 'track_name': title, 'album_name': album,
                     'duration': round(duration)})
    if r is None:
        raise RuntimeError('LRCLIB unreachable')
    exact = r.json() if r.status_code == 200 else None
    if exact and (exact.get('syncedLyrics') or exact.get('instrumental')):
        return exact
    # Album names differ a lot between sources: search by artist + title, match on duration.
    r = _get('search', {'artist_name': artist, 'track_name': title})
    if r is None:
        raise RuntimeError('LRCLIB unreachable')
    hits = [h for h in (r.json() if r.status_code == 200 else [])
            if h.get('duration') and abs(h['duration'] - duration) <= DURATION_TOLERANCE]
    hits.sort(key=lambda h: (not h.get('syncedLyrics'), not h.get('plainLyrics'),
                             abs(h['duration'] - duration)))
    best = hits[0] if hits else None
    if exact and (not best or not best.get('syncedLyrics')):
        return exact  # plain lyrics from the exact album match beat plain from a search hit
    return best


# ---------- tracks ----------
def _sidecars(track: Path) -> tuple[Path, Path]:
    return track.with_suffix('.lrc'), track.with_suffix('.txt')


def _has_embedded(f) -> bool:
    tags = getattr(f, 'tags', None)
    if not tags:
        return False
    for key in tags.keys():
        k = str(key)
        if any(k == e or k.lower() == e.lower() or k.startswith(e + ':') for e in EMBEDDED_KEYS):
            values = tags[key] if isinstance(tags[key], list) else [tags[key]]
            if any(str(getattr(v, 'text', v)).strip() for v in values):  # ignore empty lyrics tags
                return True
    return False


def _tag(f, *keys) -> str:
    for k in keys:
        v = (f.tags or {}).get(k) if f is not None else None
        if v:
            v = v[0] if isinstance(v, list) else v
            if str(v).strip():
                return str(v).strip()
    return ''


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.lyrics-tmp-')
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text.rstrip('\n') + '\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def fetch_track(track: Path) -> str:
    """Fetch lyrics for one track. Returns a status: synced, plain, embedded,
    instrumental, not-found, no-tags or error. Existing lyrics are reported, not refetched."""
    lrc, txt = _sidecars(track)
    if lrc.exists():
        return 'synced'
    if txt.exists():
        return 'plain'
    raw = File(track)
    if raw is not None and _has_embedded(raw):
        return 'embedded'
    f = File(track, easy=True)
    if f is None or not getattr(f, 'info', None):
        return 'no-tags'
    artist = _tag(f, 'artist', 'albumartist')
    title = _tag(f, 'title')
    album = _tag(f, 'album')
    if not (artist and title):
        return 'no-tags'
    try:
        rec = lookup(artist, title, album, f.info.length)
    except (RuntimeError, ValueError) as e:
        log.warning('lyrics lookup failed for %s: %s', track.name, e)
        return 'error'
    if rec is None:
        return 'not-found'
    if rec.get('instrumental'):
        return 'instrumental'
    if rec.get('syncedLyrics'):
        _write(lrc, rec['syncedLyrics'])
        return 'synced'
    if rec.get('plainLyrics'):
        _write(txt, rec['plainLyrics'])
        return 'plain'
    return 'not-found'


def fetch(tracks: list[Path], data_dir: Path, *, retry_all: bool = False,
          progress_every: int = 0) -> dict[str, int]:
    """Fetch lyrics for tracks, skipping ones checked recently. Returns counts per status."""
    con = _db(data_dir)
    known = {} if retry_all else dict(con.execute('SELECT path, checked FROM tracks WHERE status IN '
                                                  "('not-found','instrumental','no-tags')"))
    cutoff = time.time() - RETRY_DAYS * 86400
    counts: dict[str, int] = {}
    for n, t in enumerate(tracks, 1):
        jobs.progress(f'lyrics {n}/{len(tracks)}')
        key = str(t)
        if key in known and known[key] > cutoff:
            status = 'skipped (checked recently)'
        else:
            try:
                status = fetch_track(t)
            except Exception as e:  # unreadable file etc.: note it and carry on
                log.warning('lyrics: %s: %r', t, e)
                status = 'error'
            if status != 'error':
                con.execute('INSERT OR REPLACE INTO tracks VALUES (?, ?, ?)', (key, status, time.time()))
                con.commit()
        counts[status] = counts.get(status, 0) + 1
        if progress_every and n % progress_every == 0:
            log.info('lyrics: %d/%d %s', n, len(tracks), _fmt(counts))
    con.close()
    return counts


def library_tracks(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in art.AUDIO)


def _fmt(counts: dict[str, int]) -> str:
    return ', '.join(f'{k} {v}' for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
