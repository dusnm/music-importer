"""Import releases from your own Bandcamp collection (purchases and claimed free downloads).

  music-importer bandcamp                             browse everything you own and pick
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

from . import jobs, pipeline

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
        jobs.progress('loading your Bandcamp collection ...')
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
    from bandcampsync.download import is_zip_file, temporary_download_path, unzip_file
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
                content_type = _stream(url, fh, bc.browser_profile, label(item), item.download_url)
        except (BandcampImportError, jobs.Cancelled):
            raise
        except Exception as e:  # network errors, corrupt responses ...
            raise BandcampImportError(f'download failed: {e}') from e
        if is_zip_file(tmp):
            unzip_file(tmp, dest)
        else:  # single-track purchases come as the bare audio file
            ext = {'audio/flac': '.flac', 'audio/x-flac': '.flac', 'audio/mpeg': '.mp3',
                   'audio/mp4': '.m4a', 'audio/ogg': '.ogg', 'audio/wav': '.wav'}.get(content_type, '')
            if not ext:
                raise BandcampImportError(f'unexpected download type {content_type!r}')
            track = dest / f'{item.item_title}{ext}'.replace('/', '_')
            shutil.move(tmp, track)
            _tag_single(track)
    for p in dest.rglob('*'):
        if p.is_file():
            p.chmod(0o644)
    return dest


def _stream(url: str, fh, profile: str, what: str, page: str = '') -> str:
    """Download url into fh, reporting progress; returns the content type."""
    from curl_cffi import requests
    from bandcampsync.download import _is_expired_download_page
    r = requests.get(url, stream=True, impersonate=profile, timeout=60)
    try:
        if r.status_code != 200:
            raise BandcampImportError(f'download failed: HTTP {r.status_code}')
        ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip()
        if ctype == 'text/html':
            body = b''.join(r.iter_content(chunk_size=64 * 1024)).decode('utf-8', 'replace')
            if _is_expired_download_page(body):
                raise BandcampImportError(
                    'the download link has expired and Bandcamp wants to e-mail you a new one: open '
                    f'{page or "the release in your collection"} in a browser, click to send the e-mail, '
                    'open the link in it, then retry this job (mi retry <job>)')
            raise BandcampImportError('Bandcamp returned a web page instead of the file')
        total = int(r.headers.get('Content-Length') or 0)
        done, t0 = 0, time.monotonic()
        for chunk in r.iter_content(chunk_size=256 * 1024):
            fh.write(chunk)
            done += len(chunk)
            rate = done / max(time.monotonic() - t0, 0.001)
            pct = f'{done * 100 // total}% ' if total else ''
            size = f'{done / 1e6:.0f}/{total / 1e6:.0f} MB' if total else f'{done / 1e6:.0f} MB'
            jobs.progress(f'downloading {what}: {pct}({size}, {rate / 1e6:.1f} MB/s)')
        if not done:
            raise BandcampImportError('download was empty')
        return ctype
    finally:
        r.close()


def _tag_single(track: Path) -> None:
    """A track bought on its own may carry no album tag: treat it as a single named after itself."""
    from mutagen import File
    f = File(track, easy=True)
    if f is None or f.tags is None or not f.get('title'):
        return
    changed = False
    if not (f.get('album') or [''])[0].strip():
        f['album'] = f['title'][0]
        changed = True
    if not (f.get('tracknumber') or [''])[0].strip():
        f['tracknumber'] = '1'
        changed = True
    if changed:
        f.save()


def import_item(bc, item, s: pipeline.Settings, *, fmt: str = 'flac', as_is: bool = False) -> dict:
    dest = s.data_dir / 'bandcamp' / _folder_name(item)
    log.info('downloading %s (%s)', label(item), fmt)
    download(bc, item, dest, fmt)
    # No fingerprint fallback: Bandcamp's own tags are the authority when MusicBrainz has no
    # confident match, and fingerprint guesses would leak MusicBrainz ids into them.
    res = pipeline.import_folder(dest, s, as_is=as_is, fingerprint_fallback=False)
    if res['status'].startswith('not imported') and not as_is:
        log.info('no confident MusicBrainz match: importing with Bandcamp\'s own tags')
        res = pipeline.import_folder(dest, s, as_is=True)
        res['fallback'] = 'as-is (Bandcamp tags)'
    if res['status'] != 'imported' and dest.exists():
        res['kept'] = str(dest)
    return res


def import_and_record(bc, item, s: pipeline.Settings, *, fmt: str, as_is: bool,
                      force: bool) -> tuple[bool, str, dict | None]:
    """Import one purchase and remember it. Returns (ok, one-line summary, import result)."""
    state = load_state(s)
    key = str(item.item_id)
    if key in state and not force:
        return True, f'already imported {state[key]["imported"]}, skipped (--force to redo)', None
    try:
        res = import_item(bc, item, s, fmt=fmt, as_is=as_is)
    except BandcampImportError as e:
        return False, str(e), None
    dirs = ', '.join(a['dir'] for a in res.get('albums', []))
    how = res.get('fallback') or res.get('match', '')
    summary = (res['status'] + (f' -> {dirs}' if dirs else '') + (f' ({how})' if dirs else '')
               + (f'; files kept in {res["kept"]}' if res.get('kept') else ''))
    ok = res['status'] == 'imported' or res['status'].startswith('duplicate')
    if ok:
        state = load_state(s)  # re-read: other jobs may have recorded meanwhile
        state[key] = {'artist': item.band_name, 'title': item.item_title, 'url': item_url(item),
                      'imported': time.strftime('%Y-%m-%d'), 'status': res['status'], 'dirs': dirs}
        save_state(s, state)
    return ok, summary, res


def run(items, bc, s: pipeline.Settings, *, fmt: str, as_is: bool, force: bool) -> int:
    """Foreground: import the items one after another, printing progress."""
    jobs.use_console()
    failures = 0
    for n, item in enumerate(items, 1):
        ok, summary, _ = import_and_record(bc, item, s, fmt=fmt, as_is=as_is, force=force)
        jobs.console_done()
        print(f'[{n}/{len(items)}] {label(item)}: {summary}')
        failures += not ok
    return 1 if failures else 0


def queue_items(items, *, fmt: str, as_is: bool, force: bool) -> int:
    """Background (default): one job per release."""
    ids = []
    for item in items:
        job_id, created = jobs.submit('bandcamp', label(item),
                                      {'item_id': item.item_id, 'format': fmt, 'as_is': as_is, 'force': force})
        ids.append(job_id)
        print(f'queued job #{job_id}: {label(item)}' + ('' if created else ' (was already queued)'))
    if ids:
        first = ids[0] if len(ids) == 1 else f'{ids[0]}-{ids[-1]}'
        print(f'\nrunning in the background; watch with: mi follow  (or mi jobs / mi job {ids[0]})'
              + (f'  [jobs {first}]' if len(ids) > 1 else ''))
    return 0


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
                                  'or pass --yes to take all of them (run "mi" in a terminal)')
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


def _go(selected, bc, s, a) -> int:
    if a.fg:
        print(f'importing {len(selected)} release(s) in the foreground ...')
        return run(selected, bc, s, fmt=a.format, as_is=a.as_is, force=a.force)
    return queue_items(selected, fmt=a.format, as_is=a.as_is, force=a.force)


def main(a) -> int:
    s = pipeline.Settings()
    jobs.use_console()
    try:
        bc = connect(s)
        jobs.console_done()
        items = [i for i in bc.purchases if not i.is_preorder]
        state = load_state(s)
        if a.bc_cmd is None:  # bare `bandcamp`: browse everything, grouped by artist
            selected = browse(bc, items, s, state, text=a.text)
            if not selected:
                print('nothing selected')
                return 0
            return _go(selected, bc, s, a)
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
                                              '(see "mi bandcamp list")')
                if len(hits) > 1:
                    print(f'{len(hits)} releases match {q!r}:')
                    hits = choose(hits, 'Import which?', assume_yes=a.yes, state=state)
                selected += [h for h in hits if h not in selected]
        else:  # artist
            hits = find_artist(items, a.query)
            if not hits:
                raise BandcampImportError(f'you own nothing from {a.query!r} on Bandcamp '
                                          '(see "mi bandcamp list")')
            artists = sorted({h.band_name for h in hits})
            hits.sort(key=lambda i: i.item_title.casefold())
            print(f'You own {len(hits)} release(s) from {", ".join(artists)}:')
            selected = choose(hits, 'Import which?', assume_yes=a.yes, state=state)
        if not selected:
            print('nothing selected')
            return 0
        return _go(selected, bc, s, a)
    except BandcampImportError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2


def add_parser(sub) -> None:
    bc = sub.add_parser('bandcamp', help='import releases from your Bandcamp collection')
    bc.add_argument('--text', action='store_true', help='numbered list instead of the full-screen picker')
    bsub = bc.add_subparsers(dest='bc_cmd', required=False,
                             description='without a subcommand: browse and pick from your whole collection')
    ls = bsub.add_parser('list', help='show the releases you own')
    ls.add_argument('filter', nargs='?', help='only show releases whose "Artist - Title" contains this')
    al = bsub.add_parser('album', help='import specific releases (album or track URL, or part of "Artist - Title")')
    al.add_argument('query', nargs='+')
    ar = bsub.add_parser('artist', help='import releases you own from one artist (artist URL or name)')
    ar.add_argument('query')
    for p in (bc, al, ar):
        p.add_argument('--yes', '-y', action='store_true', help='take every match without asking')
        p.add_argument('--format', default='flac', choices=FORMATS, help='download format (default flac)')
        p.add_argument('--as-is', action='store_true', help='skip MusicBrainz, keep Bandcamp\'s tags')
        p.add_argument('--force', action='store_true', help='re-import releases imported before')
        p.add_argument('--fg', action='store_true', help='run here and wait, instead of as background jobs')


# ---------- browsing the whole collection ----------
_DATE_FMT = '%d %b %Y %H:%M:%S %Z'


def _parse_date(raw: str | None) -> str:
    if not raw:
        return ''
    try:
        return time.strftime('%Y-%m-%d', time.strptime(raw.strip(), _DATE_FMT))
    except ValueError:
        m = re.search(r'\b(\d{4})\b', raw)
        return m.group(1) if m else ''


def release_dates(bc, items, s: pipeline.Settings) -> dict[str, str]:
    """item_id -> release date (YYYY-MM-DD, '' if unknown), from each release page; cached."""
    cache_file = s.data_dir / 'bandcamp-releases.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    todo = [i for i in items if str(i.item_id) not in cache]
    for n, item in enumerate(todo, 1):
        print(f'\rlooking up release dates {n}/{len(todo)} ...', end='', flush=True)
        date = ''
        try:
            html = bc._request('get', item_url(item), as_raw=True)
            m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
            if m:
                date = _parse_date(json.loads(m.group(1)).get('datePublished'))
            if not date:
                m = re.search(r'release_date&quot;:&quot;([^&]+)', html)
                date = _parse_date(m.group(1) if m else None)
        except Exception as e:  # a removed page etc.: show the release without a date
            log.debug('release date for %s: %r', label(item), e)
        cache[str(item.item_id)] = date
        if n % 10 == 0 or n == len(todo):
            cache_file.write_text(json.dumps(cache, indent=1))
        time.sleep(0.3)
    if todo:
        print()
    return {str(i.item_id): cache.get(str(i.item_id), '') for i in items}


def _key(text: str) -> str:
    return re.sub(r'\W+', '', (text or '').casefold())


def library_albums(s: pipeline.Settings) -> set[tuple[str, str]]:
    """(artist, album) keys of everything in the beets library, for "already there" marks."""
    if not s.db.exists():
        return set()
    from beets.library import Library
    lib = Library(str(s.db), directory=str(s.music_dir))
    return {(_key(a.albumartist), _key(a.album)) for a in lib.albums()}


class Row:
    """One line of the browser: an artist heading (item None) or a release."""

    def __init__(self, artist: str, item=None, date: str = '', status: str = ''):
        self.artist, self.item, self.date, self.status = artist, item, date, status


def build_rows(items, dates: dict, state: dict, library: set) -> list[Row]:
    groups: dict[str, list] = {}
    names: dict[str, list[str]] = {}
    for i in items:
        k = i.band_name.casefold()
        groups.setdefault(k, []).append(i)
        names.setdefault(k, []).append(i.band_name)
    rows = []
    for k in sorted(groups, key=lambda k: re.sub(r'^the\s+', '', k)):
        spelled = max(set(names[k]), key=names[k].count)  # "Spiritbox" over "spiritbox"
        rows.append(Row(spelled))
        releases = sorted(groups[k], key=lambda i: (dates.get(str(i.item_id)) or '9999',
                                                    i.item_title.casefold()))
        for i in releases:
            if str(i.item_id) in state:
                status = 'imported'
            elif (_key(i.band_name), _key(i.item_title)) in library:
                status = 'in library'
            else:
                status = ''
            rows.append(Row(spelled, i, dates.get(str(i.item_id), ''), status))
    return rows


STATUS_TEXT = {'imported': '✓ imported from Bandcamp', 'in library': '● already in library'}
HELP = ('↑↓ move  space select  a artist  n none  u all new  Enter import  q quit')


def _curses_pick(rows: list[Row]) -> list | None:
    import curses
    os.environ.setdefault('ESCDELAY', '25')

    def ui(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        chosen: set[int] = set()
        cur, top = 1 if len(rows) > 1 else 0, 0
        releases = [n for n, r in enumerate(rows) if r.item is not None]

        def artist_rows(n):
            a = rows[n].artist
            return [m for m in releases if rows[m].artist == a]

        while True:
            h, w = scr.getmaxyx()
            body = max(1, h - 3)
            top = min(max(top, cur - body + 1), cur)
            scr.erase()
            title = f' Bandcamp collection - {len(releases)} releases, {len(chosen)} selected '
            scr.addnstr(0, 0, title.ljust(w), w - 1, curses.A_REVERSE)
            for y, n in enumerate(range(top, min(len(rows), top + body)), 1):
                r = rows[n]
                attr = curses.A_REVERSE if n == cur else 0
                if r.item is None:
                    picked = artist_rows(n)
                    box = '[x]' if picked and all(m in chosen for m in picked) else (
                        '[-]' if any(m in chosen for m in picked) else '[ ]')
                    scr.addnstr(y, 0, f'{box} {r.artist}'.ljust(w), w - 1,
                                attr | curses.A_BOLD | curses.color_pair(3))
                    continue
                box = '[x]' if n in chosen else '[ ]'
                kind = ' (single)' if r.item._data.get('item_type') == 'track' else ''
                left = f'    {box} {r.date or "????-??-??"}  {r.item.item_title.strip()}{kind}'
                right = STATUS_TEXT.get(r.status, '')
                color = curses.color_pair(1 if r.status == 'imported' else 2) if r.status else 0
                line = left.ljust(max(len(left) + 2, w - len(right) - 2))
                scr.addnstr(y, 0, line, w - 1, attr | (curses.A_DIM if r.status else 0))
                if right and len(line) < w - 1:
                    scr.addnstr(y, len(line), right, w - 1 - len(line), attr | color)
            scr.addnstr(h - 1, 0, HELP.ljust(w), w - 1, curses.A_REVERSE)
            scr.refresh()
            k = scr.getch()
            if k in (curses.KEY_UP, ord('k')):
                cur = max(0, cur - 1)
            elif k in (curses.KEY_DOWN, ord('j')):
                cur = min(len(rows) - 1, cur + 1)
            elif k == curses.KEY_PPAGE:
                cur = max(0, cur - body)
            elif k == curses.KEY_NPAGE:
                cur = min(len(rows) - 1, cur + body)
            elif k == curses.KEY_HOME:
                cur = 0
            elif k == curses.KEY_END:
                cur = len(rows) - 1
            elif k == ord(' '):
                group = artist_rows(cur) if rows[cur].item is None else [cur]
                if all(m in chosen for m in group):
                    chosen.difference_update(group)
                else:
                    chosen.update(group)
                if rows[cur].item is not None:
                    cur = min(len(rows) - 1, cur + 1)
            elif k == ord('a'):
                group = artist_rows(cur)
                if all(m in chosen for m in group):
                    chosen.difference_update(group)
                else:
                    chosen.update(group)
            elif k == ord('n'):
                chosen.clear()
            elif k == ord('u'):
                chosen = {m for m in releases if not rows[m].status}
            elif k in (curses.KEY_ENTER, 10, 13):
                return [rows[m].item for m in sorted(chosen)]
            elif k in (ord('q'), ord('Q')):  # not Esc: arrow keys start with an Esc byte
                return None

    return curses.wrapper(ui)


def _text_pick(rows: list[Row]) -> list | None:
    numbered = []
    for r in rows:
        if r.item is None:
            print(f'\n{r.artist}')
            continue
        numbered.append(r.item)
        mark = f'   {STATUS_TEXT[r.status]}' if r.status else ''
        kind = ' (single)' if r.item._data.get('item_type') == 'track' else ''
        print(f'  {len(numbered):4d}  {r.date or "????-??-??"}  {r.item.item_title.strip()}{kind}{mark}')
    if not sys.stdin.isatty():
        return None
    while True:
        ans = input('\nImport which? (e.g. 1,3-5; q = quit): ').strip().lower()
        if ans in ('', 'q', 'n', 'none'):
            return None
        try:
            picked = []
            for part in ans.replace(' ', '').split(','):
                a, _, b = part.partition('-')
                picked += range(int(a), int(b or a) + 1)
            if all(1 <= x <= len(numbered) for x in picked):
                return [numbered[x - 1] for x in dict.fromkeys(picked)]
        except ValueError:
            pass
        print('  not understood, try again')


def browse(bc, items, s: pipeline.Settings, state: dict, *, text: bool = False) -> list | None:
    rows = build_rows(items, release_dates(bc, items, s), state, library_albums(s))
    if not text and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM', 'dumb') != 'dumb':
        try:
            return _curses_pick(rows)
        except Exception as e:  # tiny or odd terminal: fall back to the numbered list
            log.warning('full-screen picker unavailable (%s), using a numbered list', e)
    return _text_pick(rows)
