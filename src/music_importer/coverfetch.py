"""Find a cover online for an album folder that has none (no image file, nothing embedded).

Imports matched against MusicBrainz get their cover from beets' fetchart. This covers the
rest: albums imported with their own tags ([as-is], Bandcamp fallbacks) and `mi art` on a
folder without art. Sources, best first:

  1. Cover Art Archive, by the MusicBrainz release (or release group) id in the tags
  2. Cover Art Archive / fanart.tv, by a MusicBrainz search for artist + album (read-only:
     nothing is written to the tags)
  3. the iTunes catalogue, by artist + album (no API key needed)

The image is saved as cover.jpg and normalised like every other cover (art.fix_album_art).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import requests
from mutagen import File

from . import art, jobs
from .fingerprint import UA, _mb_get

log = logging.getLogger(__name__)

MIN_SIZE = 600            # pixels, shortest side; smaller finds are ignored (as fetchart minwidth)
TIMEOUT = 30


def _key(text: str) -> str:
    """Comparable form of a name: no accents, case, punctuation or "the"."""
    t = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().casefold()
    t = re.sub(r'^the\s+', '', t)
    return re.sub(r'[\W_]+', '', t)


def _album_key(text: str) -> str:
    # store suffixes that differ between catalogues: "Album - EP", "Album (Deluxe Edition)"
    t = re.sub(r'\s+-\s+(single|ep)$', '', text or '', flags=re.I)
    t = re.sub(r'\s*[\(\[][^)\]]*(edition|version|remaster(ed)?|deluxe|bonus)[^)\]]*[\)\]]', '', t, flags=re.I)
    return _key(t)


def _get_image(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=TIMEOUT)
    except requests.RequestException as e:
        log.debug('cover download %s failed: %r', url, e)
        return None
    if r.status_code != 200 or not r.headers.get('Content-Type', '').startswith('image/'):
        return None
    fmt, w, h = art.image_size(r.content)
    if not fmt or min(w, h) < MIN_SIZE:
        log.info('cover at %s ignored: %sx%s', url, w, h)
        return None
    return r.content


def _caa(kind: str, mbid: str) -> bytes | None:
    return _get_image(f'https://coverartarchive.org/{kind}/{mbid}/front')


def _fanart(rgid: str, key: str) -> bytes | None:
    if not key:
        return None
    try:
        r = requests.get(f'https://webservice.fanart.tv/v3/music/albums/{rgid}',
                         params={'api_key': key}, headers={'User-Agent': UA}, timeout=TIMEOUT)
        covers = r.json().get('albums', {}).get(rgid, {}).get('albumcover', []) if r.ok else []
    except (requests.RequestException, ValueError):
        return None
    for c in sorted(covers, key=lambda c: -int(c.get('likes') or 0)):
        data = _get_image(c['url'])
        if data:
            return data
    return None


def _mb_search(artist: str, album: str) -> list[str]:
    """Release-group ids whose title and artist match, best first."""
    q = lambda s: '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    try:
        res = _mb_get('release-group', query=f'releasegroup:{q(album)} AND artist:{q(artist)}', limit=10)
    except Exception as e:
        log.info('MusicBrainz search failed: %r', e)
        return []
    want_album, want_artist = _album_key(album), _key(artist)
    found = []
    for rg in res.get('release-groups', []):
        credit = ''.join(c.get('name', '') + c.get('joinphrase', '') for c in rg.get('artist-credit', []))
        if int(rg.get('score', 0)) >= 90 and _album_key(rg.get('title', '')) == want_album \
                and _key(credit) == want_artist:
            found.append(rg['id'])
    return found


def _itunes(artist: str, album: str) -> bytes | None:
    try:
        r = requests.get('https://itunes.apple.com/search',
                         params={'term': f'{artist} {album}', 'entity': 'album', 'media': 'music', 'limit': 25},
                         headers={'User-Agent': UA}, timeout=TIMEOUT)
        results = r.json().get('results', []) if r.ok else []
    except (requests.RequestException, ValueError):
        return None
    want_album, want_artist = _album_key(album), _key(artist)
    for res in results:
        if _key(res.get('artistName', '')) == want_artist and _album_key(res.get('collectionName', '')) == want_album:
            url = res.get('artworkUrl100', '')
            data = _get_image(re.sub(r'/\d+x\d+bb\.', '/3000x3000bb.', url)) if url else None
            if data:
                return data
    return None


def _tags(tracks: list[Path]) -> dict[str, str]:
    for t in tracks:
        f = File(t, easy=True)
        if f is None or not f.tags:
            continue
        get = lambda *keys: next((str(f[k][0]).strip() for k in keys if f.get(k) and str(f[k][0]).strip()), '')
        tags = {'artist': get('albumartist', 'artist'), 'album': get('album'),
                'release': get('musicbrainz_albumid'), 'group': get('musicbrainz_releasegroupid')}
        if tags['artist'] and tags['album']:
            return tags
    return {}


def find(tracks: list[Path], fanart_key: str = '') -> tuple[bytes, str] | None:
    """A cover for the album these tracks belong to, and where it came from; None if none."""
    tags = _tags(tracks)
    if not tags:
        return None
    if tags['release']:
        jobs.progress('cover art: Cover Art Archive (release in the tags)')
        data = _caa('release', tags['release'])
        if data:
            return data, f'Cover Art Archive (release {tags["release"]})'
    groups = [tags['group']] if tags['group'] else []
    if not groups:
        jobs.progress(f'cover art: searching MusicBrainz for {tags["artist"]} - {tags["album"]}')
        groups = _mb_search(tags['artist'], tags['album'])
    for rg in groups[:3]:
        jobs.progress('cover art: Cover Art Archive')
        data = _caa('release-group', rg)
        if data:
            return data, f'Cover Art Archive (release group {rg})'
        data = _fanart(rg, fanart_key)
        if data:
            return data, f'fanart.tv (release group {rg})'
    jobs.progress('cover art: iTunes catalogue')
    data = _itunes(tags['artist'], tags['album'])
    if data:
        return data, 'iTunes catalogue'
    return None


def fill(album_dir: Path, tracks: list[Path], report: dict, fanart_key: str = '') -> dict:
    """If fix_album_art found no cover, look one up online, save and normalise it."""
    if report.get('cover') is not None:
        return report
    found = find(tracks, fanart_key)
    if not found:
        report['actions'] = [a for a in report['actions'] if a != 'no cover art found'] + \
            ['no cover art found (folder, embedded, Cover Art Archive, fanart.tv, iTunes)']
        return report
    data, source = found
    (album_dir / 'cover.jpg').write_bytes(data)
    new = art.fix_album_art(album_dir, tracks)
    if new.get('cover'):
        new['cover']['source'] = source
        new['actions'] = [f'cover.jpg downloaded from {source} ({new["cover"]["how"]}, '
                          f'{new["cover"]["bytes"] // 1024} KB)'] + \
            [a for a in new['actions'] if not a.startswith('cover.jpg from ')]
    return new
