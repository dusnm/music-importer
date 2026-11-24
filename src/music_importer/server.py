"""HTTP endpoint for slskd webhooks plus the background job worker and inbox watcher."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from . import inbox, jobs, tasks  # noqa: F401  (tasks registers the job runners)
from .pipeline import Settings

log = logging.getLogger(__name__)

# slskd reports paths as seen inside its own container.
SLSKD_DOWNLOADS = PurePosixPath(os.environ.get('SLSKD_DOWNLOADS_DIR', '/app/downloads'))
INBOX_DIR = os.environ.get('INBOX_DIR', '')


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
    """Queue an import job (slskd webhook, inbox watcher). False if it is already queued."""
    _, created = jobs.submit('import', folder.name, {'folder': str(folder), 'source': source}, source)
    return created


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
            self._reply(200, {'ok': True, 'active': len(jobs.recent(active_only=True))})
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
    jobs.recover()
    worker = threading.Thread(target=jobs.work, args=(stop,), daemon=True, name='importer')
    worker.start()
    if INBOX_DIR:
        threading.Thread(target=inbox.watch, args=(Path(INBOX_DIR), enqueue, stop),
                         daemon=True, name='inbox').start()
    Handler.settings = s
    httpd = ThreadingHTTPServer((host, port), Handler)

    def shutdown(signum, _frame):
        # docker stop: take no new jobs, let the running one finish within the grace period
        log.info('received signal %d: finishing the current job, then stopping', signum)
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info('listening on %s:%d; downloads %s -> %s; library %s; fanart.tv %s',
             host, port, SLSKD_DOWNLOADS, s.downloads_dir, s.music_dir,
             'enabled' if s.fanarttv_key else 'disabled (no FANARTTV_API_KEY)')
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        worker.join()  # docker kills us at the end of stop_grace_period if it takes longer
        log.info('stopped')
