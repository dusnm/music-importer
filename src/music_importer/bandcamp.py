"""Import releases from your own Bandcamp collection (purchases and claimed free downloads).

  music-importer bandcamp list [FILTER]               what you own, optionally filtered
  music-importer bandcamp album URL|TEXT [...]        import specific releases
  music-importer bandcamp artist URL|NAME [--yes]     import everything you own from an artist

Talking to Bandcamp (login cookie, its bot challenge, the collection API, download links)
is done by bandcampsync. Each release is downloaded as FLAC into DATA_DIR/bandcamp/<name>,
unpacked and run through the normal import. Bandcamp releases from smaller artists are often
missing from MusicBrainz: when there is no confident match, the release is imported with
Bandcamp's own tags instead (as with an "[as-is]" folder). Imported purchases are remembered
in DATA_DIR/bandcamp.json, so running `artist` again only fetches new ones.

Authentication: BANDCAMP_COOKIES in the environment - the value of the `identity` cookie of a
logged-in bandcamp.com browser session (either just the value or "identity=<value>"), or a
Netscape cookies.txt export in DATA_DIR/bandcamp-cookies.txt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import pipeline

log = logging.getLogger(__name__)

FORMATS = ('flac', 'alac', 'wav', 'aiff-lossless', 'mp3-320', 'mp3-v0', 'aac-hi', 'vorbis')


class BandcampImportError(RuntimeError):
    pass


# ---------- connection & collection ----------
def _cookies(s: pipeline.Settings) -> str:
    raw = os.environ.get('BANDCAMP_COOKIES', '').strip()
    if not raw:
        f = s.data_dir / 'bandcamp-cookies.txt'
        if f.exists():
            return f.read_text()
        raise BandcampImportError(
            'No Bandcamp login: set BANDCAMP_COOKIES in .env to the value of the "identity" '
            'cookie from a logged-in bandcamp.com browser session, then recreate the container.')
    return raw if '=' in raw.split(';')[0] else f'identity={raw}'


def connect(s: pipeline.Settings):
    from bandcampsync.bandcamp import Bandcamp, BandcampError  # heavy imports, lazily
    for name in ('bandcamp', 'download', 'sync'):  # bandcampsync logs every collection item
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
    try:
        bc = Bandcamp(cookies=_cookies(s))
        bc.verify_authentication()
        if not bc.is_authenticated:
            raise BandcampImportError('Bandcamp did not accept the login cookie (expired? log in '
                                      'again in the browser and copy a fresh "identity" cookie)')
        log.info('loading your Bandcamp collection ...')
        bc.load_purchases()
    except BandcampError as e:
        raise BandcampImportError(str(e)) from e
    return bc


def item_url(item) -> str:
    d = item._data
    if d.get('item_url'):
        return d['item_url']
    h = d.get('url_hints') or {}
    host = h.get('custom_domain') or (f"{h['subdomain']}.bandcamp.com" if h.get('subdomain') else '')
    kind = 'track' if (h.get('item_type') == 't' or d.get('tralbum_type') == 't') else 'album'
    return f'https://{host}/{kind}/{h["slug"]}' if host and h.get('slug') else ''


def _host(url: str) -> str:
    return urlsplit(url if '://' in url else f'https://{url}').netloc.lower()


def _norm_url(url: str) -> str:
    u = urlsplit(url if '://' in url else f'https://{url}')
    return f'{u.netloc.lower()}{u.path.rstrip("/")}'


def label(item) -> str:
    kind = 'track' if (item._data.get('item_type') == 'track' or item._data.get('tralbum_type') == 't') else 'album'
    return f'{item.band_name} - {item.item_title}' + (' [single track]' if kind == 'track' else '')


def find_albums(items, query: str) -> list:
    if re.match(r'^(https?://)?[^/\s]+\.[^/\s]+/(album|track)/', query):
        want = _norm_url(query)
        return [i for i in items if _norm_url(item_url(i)) == want]
    q = query.casefold()
    return [i for i in items if q in f'{i.band_name} - {i.item_title}'.casefold()]


def find_artist(items, query: str) -> list:
    if '.' in query and ' ' not in query:  # an artist URL / domain
        host = _host(query)
        return [i for i in items if _host(item_url(i)) == host]
    q = query.casefold()
    exact = [i for i in items if (i.band_name or '').casefold() == q]
    return exact or [i for i in items if q in (i.band_name or '').casefold()]


# ---------- state ----------
def _state_path(s: pipeline.Settings) -> Path:
    return s.data_dir / 'bandcamp.json'


def load_state(s: pipeline.Settings) -> dict:
    p = _state_path(s)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(s: pipeline.Settings, state: dict) -> None:
    p = _state_path(s)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(p)


# ---------- download & import ----------
def _folder_name(item) -> str:
    name = f'{item.band_name} - {item.item_title}'
    name = re.sub(r'[/\\\x00-\x1f]', '_', name).strip().lstrip('.')
    return name[:150] or str(item.item_id)


def download(bc, item, dest: Path, fmt: str) -> Path:
    from bandcampsync.download import (DownloadExpired, download_file, is_zip_file,
                                       temporary_download_path, unzip_file)
    from bandcampsync.bandcamp import BandcampError
    try:
        url = bc.get_download_file_url(item, encoding=fmt)
        url = bc.check_download_stat(item, url)
    except BandcampError as e:
        raise BandcampImportError(f'no {fmt} download: {e}') from e
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with temporary_download_path(dest.parent) as tmp:
        try:
            with open(tmp, 'wb') as fh:
                content_type = download_file(url, fh, bc.browser_profile, logevery=25)
        except DownloadExpired as e:
            raise BandcampImportError('Bandcamp wants an e-mail confirmation for this download: '
                                      'open it once in the browser, then retry') from e
        except Exception as e:  # network errors, corrupt responses ...
            raise BandcampImportError(f'download failed: {e}') from e
        if is_zip_file(tmp):
            unzip_file(tmp, dest)
        else:  # single-track purchases come as the bare audio file
            ext = {'audio/flac': '.flac', 'audio/x-flac': '.flac', 'audio/mpeg': '.mp3',
                   'audio/mp4': '.m4a', 'audio/ogg': '.ogg', 'audio/wav': '.wav'}.get(content_type, '')
            if not ext:
                raise BandcampImportError(f'unexpected download type {content_type!r}')
            shutil.move(tmp, dest / f'{item.item_title}{ext}'.replace('/', '_'))
    for p in dest.rglob('*'):
        if p.is_file():
            p.chmod(0o644)
    return dest


def import_item(bc, item, s: pipeline.Settings, *, fmt: str = 'flac', as_is: bool = False) -> dict:
    dest = s.data_dir / 'bandcamp' / _folder_name(item)
    log.info('downloading %s (%s)', label(item), fmt)
    download(bc, item, dest, fmt)
    res = pipeline.import_folder(dest, s, as_is=as_is)
    if res['status'].startswith('not imported') and not as_is:
        log.info('no confident MusicBrainz match: importing with Bandcamp\'s own tags')
        res = pipeline.import_folder(dest, s, as_is=True)
        res['fallback'] = 'as-is (Bandcamp tags)'
    if res['status'] != 'imported' and dest.exists():
        res['kept'] = str(dest)
    return res


def run(items, bc, s: pipeline.Settings, *, fmt: str, as_is: bool, force: bool) -> int:
    state = load_state(s)
    failures = 0
    for n, item in enumerate(items, 1):
        key = str(item.item_id)
        prefix = f'[{n}/{len(items)}] {label(item)}'
        if key in state and not force:
            print(f'{prefix}: already imported {state[key]["imported"]}, skipped (--force to redo)')
            continue
        try:
            res = import_item(bc, item, s, fmt=fmt, as_is=as_is)
        except BandcampImportError as e:
            failures += 1
            print(f'{prefix}: FAILED - {e}')
            continue
        dirs = ', '.join(a['dir'] for a in res.get('albums', []))
        how = res.get('fallback') or res.get('match', '')
        print(f'{prefix}: {res["status"]}' + (f' -> {dirs}' if dirs else '') + (f' ({how})' if dirs else '')
              + (f'; files kept in {res["kept"]}' if res.get('kept') else ''))
        if res['status'] == 'imported' or res['status'].startswith('duplicate'):
            state[key] = {'artist': item.band_name, 'title': item.item_title, 'url': item_url(item),
                          'imported': time.strftime('%Y-%m-%d'), 'status': res['status'], 'dirs': dirs}
            save_state(s, state)
        else:
            failures += 1
    return 1 if failures else 0


# ---------- interaction ----------
def _print_items(items, state: dict | None = None) -> None:
    for n, i in enumerate(items, 1):
        mark = '  (imported)' if state and str(i.item_id) in state else ''
        print(f'{n:4d}  {label(i)}{mark}\n      {item_url(i)}')


def choose(items, prompt: str, *, assume_yes: bool, state: dict) -> list:
    """Let the user pick from a numbered list: Enter/'a' = all, '1,3-5' = some, 'q' = none."""
    _print_items(items, state)
    if assume_yes:
        return items
    if not sys.stdin.isatty():
        raise BandcampImportError('several matches and no terminal to ask on: be more specific, '
                                  'or pass --yes to take all of them (run with "docker-compose exec")')
    while True:
        ans = input(f'{prompt} [Enter = all, e.g. 1,3-5, q = quit]: ').strip().lower()
        if ans in ('', 'a', 'all', 'y', 'yes'):
            return items
        if ans in ('q', 'n', 'no', 'none'):
            return []
        try:
            picked: list[int] = []
            for part in ans.replace(' ', '').split(','):
                a, _, b = part.partition('-')
                picked += range(int(a), int(b or a) + 1)
            if all(1 <= x <= len(items) for x in picked):
                return [items[x - 1] for x in dict.fromkeys(picked)]
        except ValueError:
            pass
        print('  not understood, try again')


def main(a) -> int:
    s = pipeline.Settings()
    try:
        bc = connect(s)
        items = [i for i in bc.purchases if not i.is_preorder]
        state = load_state(s)
        if a.bc_cmd == 'list':
            shown = find_albums(items, a.filter) if a.filter else items
            _print_items(sorted(shown, key=lambda i: (i.band_name.casefold(), i.item_title.casefold())), state)
            print(f'\n{len(shown)} of {len(items)} downloadable releases')
            return 0
        if a.bc_cmd == 'album':
            selected = []
            for q in a.query:
                hits = find_albums(items, q)
                if not hits:
                    raise BandcampImportError(f'nothing in your collection matches {q!r} '
                                              '(see "music-importer bandcamp list")')
                if len(hits) > 1:
                    print(f'{len(hits)} releases match {q!r}:')
                    hits = choose(hits, 'Import which?', assume_yes=a.yes, state=state)
                selected += [h for h in hits if h not in selected]
        else:  # artist
            hits = find_artist(items, a.query)
            if not hits:
                raise BandcampImportError(f'you own nothing from {a.query!r} on Bandcamp '
                                          '(see "music-importer bandcamp list")')
            artists = sorted({h.band_name for h in hits})
            hits.sort(key=lambda i: i.item_title.casefold())
            print(f'You own {len(hits)} release(s) from {", ".join(artists)}:')
            selected = choose(hits, 'Import which?', assume_yes=a.yes, state=state)
        if not selected:
            print('nothing selected')
            return 0
        return run(selected, bc, s, fmt=a.format, as_is=a.as_is, force=a.force)
    except BandcampImportError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2


def add_parser(sub) -> None:
    bc = sub.add_parser('bandcamp', help='import releases from your Bandcamp collection')
    bsub = bc.add_subparsers(dest='bc_cmd', required=True)
    ls = bsub.add_parser('list', help='show the releases you own')
    ls.add_argument('filter', nargs='?', help='only show releases whose "Artist - Title" contains this')
    al = bsub.add_parser('album', help='import specific releases (album or track URL, or part of "Artist - Title")')
    al.add_argument('query', nargs='+')
    ar = bsub.add_parser('artist', help='import releases you own from one artist (artist URL or name)')
    ar.add_argument('query')
    for p in (al, ar):
        p.add_argument('--yes', '-y', action='store_true', help='take every match without asking')
        p.add_argument('--format', default='flac', choices=FORMATS, help='download format (default flac)')
        p.add_argument('--as-is', action='store_true', help='skip MusicBrainz, keep Bandcamp\'s tags')
        p.add_argument('--force', action='store_true', help='re-import releases imported before')
