"""music-importer command line.

Work that changes the library runs as a background job in the server by default; --fg runs
it here instead and waits. On the host, `mi` wraps this command.

  music-importer serve                       run the server: webhook, inbox watcher, job worker
  music-importer import DIR [--mbid ID|--as-is] [--fg]   import a folder
  music-importer art DIR [--fg]              normalise cover art of an album folder in place
  music-importer lyrics [DIR] [--retry-all] [--fg]  fetch LRCLIB lyrics for the library (or DIR)
  music-importer bandcamp [...]              your Bandcamp collection (see bandcamp --help)
  music-importer index                       register the existing library in the beets DB
  music-importer jobs [-n N]                 queued, running and recent jobs
  music-importer job ID [--log]              one job in detail
  music-importer follow [ID] [--log]         watch a job (default: all active ones) live
  music-importer cancel ID...                cancel queued jobs / stop running ones
  music-importer retry ID...                 queue a finished job again
  music-importer clear [--failed]            remove finished jobs from the list (--failed: unsuccessful only)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import bandcamp, jobs, pipeline, server, tasks


def _queue(kind: str, title: str, params: dict) -> int:
    job_id, created = jobs.submit(kind, title, params)
    print(f'queued job #{job_id}: {kind} {title}' + ('' if created else ' (it was already queued)'))
    print(f'watch it with: mi follow {job_id}   (or mi jobs, mi job {job_id})')
    return 0


def _foreground(kind: str, params: dict) -> int:
    jobs.use_console()
    try:
        res = jobs.RUNNERS[kind](params, None)
    finally:
        jobs.console_done()
    print(res.get('_summary', 'done'))
    return 0 if res.get('_state', 'done') == 'done' else 1


def _run(a, kind: str, title: str, params: dict) -> int:
    return _foreground(kind, params) if a.fg else _queue(kind, title, params)


# ---------- job status ----------
def cmd_jobs(a) -> int:
    rows = jobs.recent(a.n)
    active = {r['id'] for r in jobs.recent(active_only=True)}
    rows += [r for r in jobs.recent(active_only=True) if r['id'] not in {x['id'] for x in rows}]
    if not rows:
        print('no jobs yet')
        return 0
    for r in sorted(rows, key=lambda r: r['id']):
        print(jobs.format_row(r))
    print(f'\n{len(active)} active' if active else '\nnothing queued or running')
    return 0


def _ts(t) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) if t else '-'


def cmd_job(a) -> int:
    r = jobs.get(a.id)
    if r is None:
        print(f'no job #{a.id}', file=sys.stderr)
        return 2
    print(jobs.format_row(r))
    print(f'\n  source:   {r["source"]}\n  params:   {r["params"]}')
    print(f'  queued:   {_ts(r["created"])}\n  started:  {_ts(r["started"])}\n  finished: {_ts(r["finished"])}')
    if r['state'] == 'running':
        print(f'  now:      {r["progress"]}')
    lines = r['log'].splitlines()
    if lines:
        shown = lines if a.log else lines[-25:]
        print(f'\n  log ({len(shown)} of {len(lines)} lines{"" if a.log else "; --log for all"}):')
        for line in shown:
            print('    ' + line)
    return 0


def _watch(job_id: int, show_log: bool) -> str:
    tty = sys.stdout.isatty()
    seen_lines, last_text, header = 0, None, False
    while True:
        r = jobs.get(job_id)
        if r is None:
            return 'gone'
        if not header and r['state'] != 'queued':
            print(f'▶ #{r["id"]} {r["kind"]} {r["title"]}')
            header = True
        if show_log:
            lines = r['log'].splitlines()
            new = lines[seen_lines:] if len(lines) >= seen_lines else lines
            for line in new:
                print(('\r\033[K' if tty else '') + '    ' + line)
            seen_lines = len(lines)
        if r['state'] in jobs.FINISHED:
            if tty:
                sys.stdout.write('\r\033[K')
            print(f'{jobs.MARK[r["state"]]} #{r["id"]} {r["state"]}: {r["summary"]}')
            return r['state']
        text = r['progress'] or r['state']
        if r['state'] == 'queued':
            ahead = sum(1 for x in jobs.recent(active_only=True) if x['id'] < job_id)
            text = f'#{job_id} queued ({ahead} job(s) ahead): {r["title"]}'
        if tty:
            sys.stdout.write('\r\033[K  ' + text[:150])
            sys.stdout.flush()
        elif text != last_text:
            print('  ' + text)
        last_text = text
        time.sleep(1)


def cmd_follow(a) -> int:
    try:
        if a.id:
            state = _watch(a.id, a.log)
            return 0 if state == 'done' else 1
        followed = set()
        while True:
            active = [r for r in jobs.recent(active_only=True) if r['id'] not in followed]
            if not active:
                if not followed:
                    print('nothing queued or running (mi jobs shows recent jobs)')
                return 0
            nxt = next((r for r in active if r['state'] == 'running'), active[0])
            followed.add(nxt['id'])
            _watch(nxt['id'], a.log)
    except KeyboardInterrupt:
        print('\nstopped watching; the job(s) keep running in the background')
        return 0


def cmd_cancel(a) -> int:
    for job_id in a.ids:
        print(f'#{job_id}: {jobs.cancel(job_id)}')
    return 0


def cmd_retry(a) -> int:
    for job_id in a.ids:
        res = jobs.retry(job_id)
        if res is None:
            print(f'#{job_id}: no such job')
        else:
            print(f'#{job_id}: queued again as #{res[0]}' + ('' if res[1] else ' (was already queued)'))
    return 0


def cmd_clear(a) -> int:
    counts = jobs.clear(failed_only=a.failed)
    if not counts:
        print('nothing to clear')
    else:
        print(f'cleared {sum(counts.values())} job(s): '
              + ', '.join(f'{n} {state}' for state, n in sorted(counts.items())))
    active = len(jobs.recent(active_only=True))
    if active:
        print(f'{active} queued/running job(s) kept')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='music-importer')
    ap.add_argument('-v', '--verbose', action='store_true', help='show detailed log output')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sv = sub.add_parser('serve')
    sv.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    fg = argparse.ArgumentParser(add_help=False)
    fg.add_argument('--fg', action='store_true', help='run here and wait instead of as a background job')
    im = sub.add_parser('import', parents=[fg], help='import a folder')
    im.add_argument('folder', type=Path)
    im.add_argument('--mbid', help='MusicBrainz release id to use instead of searching')
    im.add_argument('--as-is', action='store_true', default=None,
                    help='keep the files\' own tags (no MusicBrainz); also implied by "[as-is]" in DIR')
    sub.add_parser('index', help='register the existing library in the beets DB')
    ar = sub.add_parser('art', parents=[fg], help='normalise the cover art of an album folder')
    ar.add_argument('folder', type=Path)
    ly = sub.add_parser('lyrics', parents=[fg], help='fetch lyrics for the library or one folder')
    ly.add_argument('folder', type=Path, nargs='?')
    ly.add_argument('--retry-all', action='store_true', help='also retry tracks checked recently')
    bandcamp.add_parser(sub)
    js = sub.add_parser('jobs', help='queued, running and recent jobs')
    js.add_argument('-n', type=int, default=10, help='how many recent jobs (default 10)')
    jb = sub.add_parser('job', help='one job in detail')
    jb.add_argument('id', type=int)
    jb.add_argument('--log', action='store_true', help='the whole log, not just the end')
    fo = sub.add_parser('follow', help='watch a job live (default: all queued and running ones)')
    fo.add_argument('id', type=int, nargs='?')
    fo.add_argument('--log', action='store_true', help='also print log lines as they come')
    ca = sub.add_parser('cancel', help='cancel queued jobs, stop running ones')
    ca.add_argument('ids', type=int, nargs='+')
    rt = sub.add_parser('retry', help='queue finished jobs again')
    rt.add_argument('ids', type=int, nargs='+')
    cl = sub.add_parser('clear', help='remove finished jobs from the list (queued and running ones stay)')
    cl.add_argument('--failed', action='store_true',
                    help='only unsuccessful ones (failed, cancelled, interrupted); keep the done ones')
    a = ap.parse_args(argv)

    # The server logs everything; interactive commands only show warnings unless -v.
    quiet = a.cmd != 'serve' and not a.verbose
    logging.basicConfig(level=logging.WARNING if quiet else os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    if a.cmd == 'serve':
        server.serve(port=a.port)
    elif a.cmd == 'import':
        params = {'folder': str(a.folder.resolve()), 'source': 'cli'}
        if a.mbid:
            params['mbid'] = a.mbid
        if a.as_is:
            params['as_is'] = True
        return _run(a, 'import', a.folder.name, params)
    elif a.cmd == 'index':
        print(json.dumps(pipeline.index_library()))
    elif a.cmd == 'art':
        return _run(a, 'art', a.folder.name, {'folder': str(a.folder.resolve())})
    elif a.cmd == 'lyrics':
        folder = str(a.folder.resolve()) if a.folder else None
        return _run(a, 'lyrics', a.folder.name if a.folder else 'whole library',
                    {'folder': folder, 'retry_all': a.retry_all})
    elif a.cmd == 'bandcamp':
        return bandcamp.main(a)
    elif a.cmd == 'jobs':
        return cmd_jobs(a)
    elif a.cmd == 'job':
        return cmd_job(a)
    elif a.cmd == 'follow':
        return cmd_follow(a)
    elif a.cmd == 'cancel':
        return cmd_cancel(a)
    elif a.cmd == 'retry':
        return cmd_retry(a)
    elif a.cmd == 'clear':
        return cmd_clear(a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
