"""Search Soulseek through slskd and queue album downloads (slskd's REST API).

  music-importer slskd search [--lossless] [-n N] [--pick 1,3] WORDS...
  music-importer slskd downloads

Results are grouped into folders (one remote folder = one album, usually), sorted by
free upload slot first, then upload speed, then the user's queue length. Picking a folder
queues all of its files (the full folder is listed from the user, so covers, logs and
tracks that did not match the search come along). When slskd finishes a folder, its
webhook hands it to the importer as usual.

Needs SLSKD_URL and SLSKD_API_KEY (a readwrite key from slskd.yml web.authentication.api_keys).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field

import requests

AUDIO = {'flac', 'mp3', 'm4a', 'aac', 'ogg', 'opus', 'wav', 'aiff', 'aif', 'alac', 'ape', 'wv', 'wma'}
LOSSLESS = {'flac', 'wav', 'aiff', 'aif', 'alac', 'ape', 'wv'}
SEARCH_LIMIT = float(os.environ.get('SLSKD_SEARCH_SECONDS', '45'))


class SlskdError(RuntimeError):
    pass


def _api() -> tuple[str, dict]:
    url = os.environ.get('SLSKD_URL', 'http://slskd:5030').rstrip('/')
    key = os.environ.get('SLSKD_API_KEY', '')
    if not key:
        raise SlskdError('SLSKD_API_KEY is not set: add a readwrite key to slskd.yml '
                         '(web.authentication.api_keys) and to the importer\'s .env')
    return f'{url}/api/v0', {'X-API-Key': key}


def _call(method: str, path: str, **kw):
    base, headers = _api()
    try:
        r = requests.request(method, base + path, headers=headers, timeout=kw.pop('timeout', 30), **kw)
    except requests.RequestException as e:
        raise SlskdError(f'cannot reach slskd at {base}: {e}') from e
    if r.status_code in (401, 403):
        raise SlskdError(f'slskd refused the API key ({r.status_code}): check SLSKD_API_KEY and its role/cidr')
    if not r.ok:
        raise SlskdError(f'slskd {method} {path}: HTTP {r.status_code} {r.text[:200]}')
    return r.json() if r.content else None


@dataclass
class Folder:
    username: str
    path: str                      # remote folder, backslash-separated
    slot: bool
    speed: int                     # bytes/s, as the user reports it
    queue: int
    files: list = field(default_factory=list)   # matching files from the search

    @property
    def audio(self) -> list:
        return [f for f in self.files if _ext(f['filename']) in AUDIO]

    @property
    def size(self) -> int:
        return sum(f.get('size', 0) for f in self.files)

    @property
    def quality(self) -> str:
        exts = {_ext(f['filename']) for f in self.audio}
        if not exts:
            return '-'
        if len(exts) > 1:
            return '/'.join(sorted(e.upper() for e in exts))
        ext = exts.pop()
        if ext in LOSSLESS:
            depth = {f.get('bitDepth') for f in self.audio if f.get('bitDepth')}
            rate = {f.get('sampleRate') for f in self.audio if f.get('sampleRate')}
            spec = f' {max(depth)}/{max(rate) / 1000:g}' if depth and rate else ''
            return ext.upper() + spec
        rates = sorted({f.get('bitRate') for f in self.audio if f.get('bitRate')})
        vbr = any(f.get('isVariableBitRate') for f in self.audio)
        return ext.upper() + (f' {"V" if vbr else ""}{rates[-1]}' if rates else '')

    @property
    def lossless(self) -> bool:
        return bool(self.audio) and all(_ext(f['filename']) in LOSSLESS for f in self.audio)


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def search(text: str, *, progress=print) -> list[dict]:
    sid = str(uuid.uuid4())
    _call('POST', '/searches', json={'id': sid, 'searchText': text})
    t0 = time.monotonic()
    state = {}
    while time.monotonic() - t0 < SEARCH_LIMIT:
        state = _call('GET', f'/searches/{sid}')
        progress(f'searching "{text}": {state.get("responseCount", 0)} users, {state.get("fileCount", 0)} files '
                 f'({time.monotonic() - t0:.0f}s)')
        if state.get('isComplete'):
            break
        time.sleep(1)
    else:
        _call('PUT', f'/searches/{sid}')   # stop it: enough answers by now
    full = _call('GET', f'/searches/{sid}', params={'includeResponses': 'true'}, timeout=60)
    return full.get('responses') or []


def folders(responses: list[dict], *, lossless_only: bool = False) -> list[Folder]:
    out: dict[tuple, Folder] = {}
    for r in responses:
        for f in r.get('files', []):
            path = f['filename'].rsplit('\\', 1)[0]
            key = (r['username'], path)
            if key not in out:
                out[key] = Folder(r['username'], path, bool(r.get('hasFreeUploadSlot')),
                                  int(r.get('uploadSpeed') or 0), int(r.get('queueLength') or 0))
            out[key].files.append(f)
    result = [x for x in out.values() if x.audio and (x.lossless or not lossless_only)]
    result.sort(key=lambda x: (not x.slot, -x.speed, x.queue, -len(x.audio)))
    return result


def _fmt_size(n: int) -> str:
    return f'{n / 1e9:.1f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def _short(path: str, width: int = 60) -> str:
    parts = path.split('\\')
    text = '\\'.join(parts[-2:]) if len(parts) > 1 else path
    return text if len(text) <= width else '…' + text[-(width - 1):]


def print_table(rows: list[Folder]) -> None:
    print(f'{"#":>3}  {"slot":<6} {"speed":>9}  {"quality":<13} {"tracks":>6} {"size":>7}  user / folder')
    for n, x in enumerate(rows, 1):
        slot = 'free' if x.slot else f'q {x.queue}'
        print(f'{n:>3}  {slot:<6} {x.speed / 1e6:>6.1f} MB/s  {x.quality:<13} {len(x.audio):>6} '
              f'{_fmt_size(x.size):>7}  {x.username} / {_short(x.path)}')


def parse_picks(text: str, count: int) -> list[int]:
    picked: list[int] = []
    for part in text.replace(' ', '').split(','):
        a, _, b = part.partition('-')
        picked += range(int(a), int(b or a) + 1)
    if not picked or not all(1 <= p <= count for p in picked):
        raise ValueError(text)
    return list(dict.fromkeys(picked))


def ask(rows: list[Folder]) -> list[Folder]:
    while True:
        ans = input('\nDownload which? [e.g. 1 or 1,3-4; Enter = none]: ').strip().lower()
        if ans in ('', 'q', 'n', 'none'):
            return []
        try:
            return [rows[p - 1] for p in parse_picks(ans, len(rows))]
        except ValueError:
            print('  not understood, try again')


def folder_files(x: Folder) -> list[dict]:
    """Every file of the remote folder (not just the ones that matched the search)."""
    listing = _call('POST', f'/users/{requests.utils.quote(x.username, safe="")}/directory',
                    json={'directory': x.path}, timeout=90)
    entry = listing[0] if isinstance(listing, list) and listing else listing or {}
    files = entry.get('files') or []
    if not files:   # the user did not answer the listing: fall back to what the search found
        return [{'filename': f['filename'], 'size': f['size']} for f in x.files]
    return [{'filename': x.path + '\\' + f['filename'].rsplit('\\', 1)[-1], 'size': f['size']} for f in files]


def enqueue(x: Folder) -> int:
    files = folder_files(x)
    _call('POST', f'/transfers/downloads/{requests.utils.quote(x.username, safe="")}', json=files, timeout=60)
    return len(files)


def _state(state: str) -> str:
    """slskd's "Completed, Succeeded" / "Queued, Remotely" / "InProgress" as short words."""
    parts = [p.strip() for p in state.split(',')]
    if parts[0] == 'Completed' and len(parts) > 1:
        return parts[-1].lower()                       # succeeded, cancelled, errored, rejected, ...
    return ' '.join(re.sub(r'(?<!^)([A-Z])', r' \1', p).lower() for p in parts)   # queued remotely, in progress


def downloads() -> list[tuple[str, str, int, int, float, str]]:
    """(user, folder, files done, files, percent, state summary) per downloading folder."""
    out = []
    for user in _call('GET', '/transfers/downloads') or []:
        for d in user.get('directories', []):
            files = d.get('files', [])
            total = sum(f.get('size', 0) for f in files) or 1
            done_bytes = sum(f.get('bytesTransferred', 0) for f in files)
            states: dict[str, int] = {}
            for f in files:
                s = _state(f.get('state', '?'))
                states[s] = states.get(s, 0) + 1
            done = states.get('succeeded', 0)
            summary = ', '.join(f'{n} {s}' for s, n in sorted(states.items(), key=lambda kv: -kv[1]))
            out.append((user['username'], d['directory'], done, len(files), 100 * done_bytes / total, summary))
    return out


def main(a) -> int:
    try:
        if a.slskd_cmd == 'downloads':
            rows = downloads()
            if not rows:
                print('nothing downloading (finished folders are handed to the importer: mi jobs)')
            for user, folder, done, n, pct, summary in rows:
                print(f'{pct:5.1f}%  {done}/{n} files  {user} / {_short(folder)}\n        {summary}')
            return 0
        text = ' '.join(a.words)
        tty = sys.stderr.isatty()
        last = [0.0]

        def progress(msg: str) -> None:
            if tty:
                sys.stderr.write('\r\033[K' + msg)
                sys.stderr.flush()
            elif time.monotonic() - last[0] > 10:
                print(msg, file=sys.stderr)
                last[0] = time.monotonic()

        responses = search(text, progress=progress)
        if tty:
            sys.stderr.write('\r\033[K')
        rows = folders(responses, lossless_only=a.lossless)
        if not rows:
            print(f'no {"lossless " if a.lossless else ""}album folders found for "{text}"')
            return 1
        shown = rows[:a.n]
        print(f'{len(rows)} folders from {len(responses)} users, best {len(shown)} shown '
              f'(free slot first, then speed, then queue):')
        print_table(shown)
        if a.pick:
            chosen = [shown[p - 1] for p in parse_picks(a.pick, len(shown))]
        elif sys.stdin.isatty():
            chosen = ask(shown)
        else:
            print('\n(no terminal: pass --pick N to queue a download)')
            return 0
        for x in chosen:
            n = enqueue(x)
            print(f'queued {n} file(s) from {x.username}: {_short(x.path)}')
        if chosen:
            print('\nprogress: mi slskd downloads; when a folder completes, the importer takes it (mi jobs)')
        return 0
    except SlskdError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except ValueError as e:
        print(f'error: cannot read the pick "{e}" (e.g. 1 or 1,3-4)', file=sys.stderr)
        return 2


def add_parser(sub) -> None:
    sk = sub.add_parser('slskd', help='search Soulseek and queue album downloads (via slskd)')
    ssub = sk.add_subparsers(dest='slskd_cmd', required=True)
    se = ssub.add_parser('search', help='search, list album folders by free slot and speed, queue picks')
    se.add_argument('--lossless', action='store_true',
                    help='only folders whose audio is all lossless (FLAC, WAV, AIFF, ALAC, APE, WavPack)')
    se.add_argument('--flac', dest='lossless', action='store_true', help=argparse.SUPPRESS)   # old name
    se.add_argument('-n', type=int, default=25, help='list at most this many folders (default 25)')
    se.add_argument('--pick', help='queue these list numbers without asking, e.g. 1 or 1,3')
    se.add_argument('words', nargs='+', help='what to search for, e.g. artist and album')
    ssub.add_parser('downloads', help='what slskd is downloading')
