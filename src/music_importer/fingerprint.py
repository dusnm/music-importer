"""Identify a folder by AcoustID fingerprints and write provisional tags.

Used when beets cannot match a download from its tags (typically: no tags at all).
Every track is fingerprinted, the MusicBrainz release that contains the most of the
recordings is chosen, and its release id plus any *missing* basic tags are written to
the files. beets then matches the folder by that id on the next attempt.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

from . import jobs

import acoustid
import requests
from mutagen import File

log = logging.getLogger(__name__)

# The application key beets registered with AcoustID for lookups (not a user secret).
from beetsplug.chroma import API_KEY as ACOUSTID_KEY

MB = 'https://musicbrainz.org/ws/2'
UA = 'music-importer/0.1 ( no-contact )'
MIN_SCORE = 0.8
_last_mb = 0.0


def _mb_get(path: str, **params) -> dict:
    """GET from MusicBrainz at <= 1 request/s, backing off on 503 (its rate-limit reply)."""
    global _last_mb
    for attempt in range(5):
        wait = 1.1 - (time.monotonic() - _last_mb)
        if wait > 0:
            time.sleep(wait)
        _last_mb = time.monotonic()
        r = requests.get(f'{MB}/{path}', params={'fmt': 'json', **params},
                         headers={'User-Agent': UA}, timeout=30)
        if r.status_code not in (429, 502, 503, 504):
            break
        time.sleep(2 ** attempt * 2)
    r.raise_for_status()
    return r.json()


def _recordings(path: Path) -> tuple[list[str], list[str]]:
    """(recording ids, release ids) AcoustID knows for this file."""
    duration, fp = acoustid.fingerprint_file(str(path))
    res = acoustid.lookup(ACOUSTID_KEY, fp, duration, meta='recordings releaseids')
    recs, rels = [], []
    for r in res.get('results', []):
        if r.get('score', 0) < MIN_SCORE:
            continue
        for rec in r.get('recordings', []):
            recs.append(rec['id'])
            rels += [x['id'] for x in rec.get('releases', [])]
    return recs, rels


def identify(tracks: list[Path]) -> dict | None:
    """Fingerprint tracks and return {'release': mb release json, 'map': {path: track}}."""
    per_file: dict[Path, set[str]] = {}
    release_votes: Counter[str] = Counter()
    for n, t in enumerate(tracks, 1):
        jobs.progress(f'fingerprinting {n}/{len(tracks)}')
        try:
            recs, rels = _recordings(t)
        except Exception as e:
            log.warning('fingerprint failed for %s: %r', t.name, e)
            continue
        per_file[t] = set(recs)
        release_votes.update(set(rels))
        time.sleep(0.34)  # AcoustID: max 3 requests/s
    if not per_file or not release_votes:
        log.info('fingerprints: no AcoustID matches')
        return None

    best = None  # (matched files, exact track count, -track count delta, release, mapping)
    for rel_id, _votes in release_votes.most_common(8):
        try:
            rel = _mb_get(f'release/{rel_id}', inc='recordings artist-credits release-groups')
        except requests.RequestException as e:
            log.warning('MusicBrainz lookup of release %s failed: %r', rel_id, e)
            continue
        by_rec = {}
        for medium in rel.get('media', []):
            for tr in medium.get('tracks', []):
                by_rec[tr['recording']['id']] = (medium.get('position', 1), len(rel['media']), tr)
        mapping = {}
        for path, recs in per_file.items():
            hit = next((by_rec[r] for r in recs if r in by_rec), None)
            if hit:
                mapping[path] = hit
        total = sum(len(m.get('tracks', [])) for m in rel.get('media', []))
        key = (len(mapping), total == len(tracks), -abs(total - len(tracks)))
        if best is None or key > best[0]:
            best = (key, rel, mapping)
    if best is None:
        return None
    key, rel, mapping = best
    if key[0] * 2 < len(tracks):
        log.info('fingerprints: best release covers only %d of %d files', key[0], len(tracks))
        return None
    log.info('fingerprints: %s - %s (%s), %d/%d files', _artist(rel), rel['title'],
             rel['id'], key[0], len(tracks))
    return {'release': rel, 'map': mapping}


def _artist(obj: dict) -> str:
    return ''.join(c['name'] + c.get('joinphrase', '') for c in obj.get('artist-credit', []))


def write_provisional_tags(ident: dict) -> int:
    """Write the release id and fill empty basic tags. Returns number of files written."""
    rel = ident['release']
    n = 0
    for path, (disc, discs, tr) in ident['map'].items():
        f = File(path, easy=True)
        if f is None:
            continue
        if f.tags is None:
            f.add_tags()
        values = {
            'musicbrainz_albumid': rel['id'],
            'album': rel['title'],
            'albumartist': _artist(rel),
            'artist': _artist(tr) or _artist(tr['recording']) or _artist(rel),
            'title': tr['title'],
            'tracknumber': str(tr['position']),
            'discnumber': str(disc),
        }
        for k, v in values.items():
            if k == 'musicbrainz_albumid' or not any(str(x).strip() for x in f.tags.get(k, [])):
                try:
                    f.tags[k] = v
                except (KeyError, ValueError):  # tag not supported by this format's easy mode
                    pass
        f.save()
        n += 1
    return n
