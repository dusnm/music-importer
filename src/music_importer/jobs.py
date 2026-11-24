"""Background jobs: everything that changes the library runs here, one job at a time.

Jobs live in DATA_DIR/jobs.db (SQLite), so the CLI can queue them and read their state
directly, and queued jobs survive a container restart. The server's worker thread runs them
in order; slskd webhooks and the inbox watcher queue import jobs the same way.

Code that does the work reports through progress(); it goes to the job's row while running
in the worker, or to the terminal when a command runs in the foreground (--fg).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

ACTIVE = ('queued', 'running')
FINISHED = ('done', 'failed', 'cancelled', 'interrupted')
KEEP_JOBS = 1000
LOG_LINES = 300
FLUSH_SECONDS = 1.0


class Cancelled(Exception):
    pass


# ---------- database ----------
def _db_path() -> Path:
    from .pipeline import Settings
    return Settings().data_dir / 'jobs.db'


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, title TEXT NOT NULL, params TEXT NOT NULL, source TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', cancel INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL, started REAL, finished REAL,
        progress TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        result TEXT, log TEXT NOT NULL DEFAULT '')''')
    con.execute('CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)')
    return con


@contextmanager
def db():
    """A connection that commits on success and is always closed."""
    con = connect()
    try:
        with con:
            yield con
    finally:
        con.close()


def submit(kind: str, title: str, params: dict, source: str = 'cli', *, dedupe: bool = True) -> tuple[int, bool]:
    """Queue a job. Returns (id, created); an identical queued/running job is reused."""
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False)
    with db() as con:
        if dedupe:
            row = con.execute('SELECT id FROM jobs WHERE kind=? AND params=? AND state IN (?, ?)',
                              (kind, blob, *ACTIVE)).fetchone()
            if row:
                return row['id'], False
        cur = con.execute('INSERT INTO jobs (kind, title, params, source, created) VALUES (?,?,?,?,?)',
                          (kind, title, blob, source, time.time()))
        return cur.lastrowid, True


def get(job_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()


def recent(limit: int = 20, *, active_only: bool = False) -> list[sqlite3.Row]:
    with db() as con:
        if active_only:
            return con.execute('SELECT * FROM jobs WHERE state IN (?, ?) ORDER BY id', ACTIVE).fetchall()
        rows = con.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return list(reversed(rows))


def cancel(job_id: int) -> str:
    with db() as con:
        row = con.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            return 'no such job'
        if row['state'] == 'queued':
            con.execute("UPDATE jobs SET state='cancelled', finished=? WHERE id=?", (time.time(), job_id))
            return 'cancelled'
        if row['state'] == 'running':
            con.execute('UPDATE jobs SET cancel=1 WHERE id=?', (job_id,))
            return 'stopping (it stops at the next safe point)'
        return f'already {row["state"]}'


def retry(job_id: int) -> tuple[int, bool] | None:
    row = get(job_id)
    if row is None:
        return None
    return submit(row['kind'], row['title'], json.loads(row['params']), 'retry')


# ---------- progress reporting ----------
class _Context:
    """The job the worker thread is running (or a terminal reporter for --fg)."""

    def __init__(self, job_id: int | None):
        self.job_id = job_id
        self.text = ''
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.dirty = False
        self.last_flush = 0.0
        self.last_cancel_check = 0.0

    def flush(self, force: bool = False) -> None:
        if self.job_id is None or not (self.dirty or force):
            return
        now = time.monotonic()
        if not force and now - self.last_flush < FLUSH_SECONDS:
            return
        with db() as con:
            con.execute('UPDATE jobs SET progress=?, log=? WHERE id=?',
                        (self.text, '\n'.join(self.lines), self.job_id))
        self.last_flush, self.dirty = now, False


_local = threading.local()
_console = False


def use_console() -> None:
    """Foreground mode: progress() writes to the terminal instead of a job row."""
    global _console
    _console = True


def _ctx() -> _Context | None:
    return getattr(_local, 'ctx', None)


def progress(text: str, *, force: bool = False) -> None:
    """Report what the current job is doing; cheap enough to call per chunk or per track."""
    ctx = _ctx()
    if ctx is not None:
        if text != ctx.text:
            ctx.text, ctx.dirty = text, True
        ctx.flush(force)
        check_cancel()
    elif _console:
        if sys.stderr.isatty():
            sys.stderr.write('\r\033[K' + text[:200])
            sys.stderr.flush()
        elif force:
            print(text, file=sys.stderr)


def console_done() -> None:
    if _console and sys.stderr.isatty():
        sys.stderr.write('\r\033[K')
        sys.stderr.flush()


def check_cancel() -> None:
    ctx = _ctx()
    if ctx is None or ctx.job_id is None:
        return
    now = time.monotonic()
    if now - ctx.last_cancel_check < 2:
        return
    ctx.last_cancel_check = now
    row = get(ctx.job_id)
    if row is not None and row['cancel']:
        raise Cancelled()


class _JobLogHandler(logging.Handler):
    """Copies log lines written while a job runs into that job's log."""

    def emit(self, record: logging.LogRecord) -> None:
        ctx = _ctx()
        if ctx is None or ctx.job_id is None:
            return
        try:
            ctx.lines.append(f'{time.strftime("%H:%M:%S", time.localtime(record.created))} '
                             f'{record.levelname[0]} {record.getMessage()}')
            ctx.dirty = True
        except Exception:
            pass


# ---------- worker ----------
RUNNERS: dict = {}


def runner(kind: str):
    def register(fn):
        RUNNERS[kind] = fn
        return fn
    return register


def _next_job(con) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()


def recover() -> None:
    """At startup: jobs that were running when the container stopped did not finish."""
    with db() as con:
        n = con.execute("UPDATE jobs SET state='interrupted', finished=?, "
                        "summary='container stopped while running (mi retry ID)' WHERE state='running'",
                        (time.time(),)).rowcount
        con.execute('DELETE FROM jobs WHERE id <= (SELECT MAX(id) FROM jobs) - ? AND state NOT IN (?, ?)',
                    (KEEP_JOBS, *ACTIVE))
    if n:
        log.warning('%d job(s) were interrupted by a restart', n)


def work(stop: threading.Event) -> None:
    logging.getLogger().addHandler(_JobLogHandler())
    while not stop.is_set():
        with db() as con:
            row = _next_job(con)
            if row is not None:
                claimed = con.execute("UPDATE jobs SET state='running', started=? WHERE id=? AND state='queued'",
                                      (time.time(), row['id'])).rowcount
        if row is None:
            stop.wait(1)
            continue
        if not claimed:  # cancelled in between
            continue
        _run(row)


def _run(row: sqlite3.Row) -> None:
    ctx = _Context(row['id'])
    _local.ctx = ctx
    params = json.loads(row['params'])
    log.info('job %d started: %s %s', row['id'], row['kind'], row['title'])
    state, result, summary = 'failed', None, ''
    try:
        fn = RUNNERS.get(row['kind'])
        if fn is None:
            raise RuntimeError(f'unknown job kind {row["kind"]!r}')
        result = fn(params, row) or {}
        summary = result.pop('_summary', '') if isinstance(result, dict) else ''
        state = result.pop('_state', 'done') if isinstance(result, dict) else 'done'
    except Cancelled:
        state, summary = 'cancelled', 'cancelled while running'
        log.info('job %d cancelled', row['id'])
    except Exception as e:
        log.exception('job %d (%s) failed', row['id'], row['title'])
        summary = f'error: {e}'
    finally:
        ctx.text = summary or state
        ctx.flush(force=True)
        with db() as con:
            con.execute('UPDATE jobs SET state=?, finished=?, summary=?, result=? WHERE id=?',
                        (state, time.time(), summary,
                         json.dumps(result, ensure_ascii=False) if result is not None else None, row['id']))
        _local.ctx = None
        log.info('job %d %s: %s', row['id'], state, summary)


# ---------- formatting (CLI) ----------
def _ago(ts: float | None) -> str:
    if not ts:
        return ''
    d = time.time() - ts
    if d < 90:
        return f'{d:.0f}s ago'
    if d < 5400:
        return f'{d / 60:.0f}m ago'
    if d < 86400 * 2:
        return f'{d / 3600:.0f}h ago'
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def _took(row) -> str:
    if not row['started']:
        return ''
    end = row['finished'] or time.time()
    d = end - row['started']
    return f'{d:.0f}s' if d < 120 else f'{d / 60:.0f}m'


MARK = {'queued': '…', 'running': '▶', 'done': '✓', 'failed': '✗', 'cancelled': '-', 'interrupted': '!'}


def format_row(row, width: int = 100) -> str:
    detail = row['progress'] if row['state'] == 'running' else (row['summary'] or '')
    when = _ago(row['finished'] or row['started'] or row['created'])
    took = _took(row)
    head = f"{MARK.get(row['state'], '?')} #{row['id']:<4} {row['state']:<11} {row['kind']:<8} {row['title']}"
    tail = '  '.join(x for x in (detail, took and f'({took})', when) if x)
    return f'{head}\n      {tail}' if tail else head
