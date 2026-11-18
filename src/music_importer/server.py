"""HTTP endpoint for slskd webhooks; imports completed download folders one at a time."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from . import inbox
from .pipeline import Settings, import_folder

log = logging.getLogger(__name__)

# slskd reports paths as seen inside its own container.
SLSKD_DOWNLOADS = PurePosixPath(os.environ.get('SLSKD_DOWNLOADS_DIR', '/app/downloads'))
SETTLE_SECONDS = float(os.environ.get('SETTLE_SECONDS', '10'))

INBOX_DIR = os.environ.get('INBOX_DIR', '')

_jobs: queue.Queue[tuple[Path, str]] = queue.Queue()
_pending: set[Path] = set()  # queued or being imported
_lock = threading.Lock()


def _find_dir(payload) -> str | None:
    """Return the local directory from a DownloadDirectoryComplete event (any casing/nesting)."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k.lower() == 'localdirectoryname' and isinstance(v, str):
                return v
        for v in payload.values():
            found = _find_dir(v)
            if found:
                return found
    elif isinstance(payload, list):
        for v in payload:
            found = _find_dir(v)
            if found:
                return found
    return None


def map_path(slskd_path: str, s: Settings) -> Path | None:
    """Translate a slskd path into ours; refuse anything outside the downloads folder."""
    p = PurePosixPath(slskd_path.replace('\\', '/'))
    try:
        rel = p.relative_to(SLSKD_DOWNLOADS)
    except ValueError:
        return None
    if not rel.parts or '..' in rel.parts:
        return None
    local = (s.downloads_dir / rel).resolve()
    if s.downloads_dir.resolve() not in local.parents:
        return None
    return local


def enqueue(folder: Path, source: str = 'slskd') -> bool:
    with _lock:
        if folder in _pending:
            return False
        _pending.add(folder)
    _jobs.put((folder, source))
    return True


def worker(s: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            folder, source = _jobs.get(timeout=1)
        except queue.Empty:
            continue
        if source == 'slskd':
            stop.wait(SETTLE_SECONDS)  # let slskd finish renaming/closing the last file
        result: dict = {'status': 'failed with an error, see the container log'}
        try:
            result = import_folder(folder, s)
        except Exception:
            log.exception('import of %s failed', folder)
        finally:
            try:
                if source == 'inbox' and folder.exists():  # anything left is not imported
                    inbox.park(folder, Path(INBOX_DIR), result)
            except Exception:
                log.exception('could not move %s out of the inbox', folder)
            with _lock:
                _pending.discard(folder)
            _jobs.task_done()


class Handler(BaseHTTPRequestHandler):
    settings: Settings

    def log_message(self, fmt, *args):  # route http.server logging through logging
        log.debug('%s %s', self.address_string(), fmt % args)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            self._reply(200, {'ok': True, 'queued': _jobs.qsize()})
        else:
            self._reply(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/webhook':
            return self._reply(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 1_000_000:
                return self._reply(413, {'error': 'too large'})
            payload = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {'error': 'invalid json'})
        event = payload.get('type') or payload.get('Type') if isinstance(payload, dict) else None
        raw = _find_dir(payload)
        if not raw:
            log.warning('webhook without localDirectoryName (type=%s): %s', event, str(payload)[:500])
            return self._reply(202, {'ignored': 'no directory in payload'})
        folder = map_path(raw, self.settings)
        if folder is None:
            log.warning('refusing path outside downloads: %s', raw)
            return self._reply(400, {'error': 'path outside downloads'})
        queued = enqueue(folder)
        log.info('webhook %s: %s (%s)', event, folder, 'queued' if queued else 'already queued')
        self._reply(202, {'queued': queued, 'folder': str(folder)})


def serve(host: str = '0.0.0.0', port: int = 8080) -> None:
    s = Settings()
    stop = threading.Event()
    threading.Thread(target=worker, args=(s, stop), daemon=True, name='importer').start()
    if INBOX_DIR:
        threading.Thread(target=inbox.watch, args=(Path(INBOX_DIR), enqueue, stop),
                         daemon=True, name='inbox').start()
    Handler.settings = s
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info('listening on %s:%d; downloads %s -> %s; library %s; fanart.tv %s',
             host, port, SLSKD_DOWNLOADS, s.downloads_dir, s.music_dir,
             'enabled' if s.fanarttv_key else 'disabled (no FANARTTV_API_KEY)')
    try:
        httpd.serve_forever()
    finally:
        stop.set()
