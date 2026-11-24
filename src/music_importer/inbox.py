"""Drop-folder watcher: album folders copied into INBOX_DIR are imported once the copy is done.

There is no "copy finished" event for files arriving over SFTP/SSHFS, so a folder is
considered complete when nothing inside it has changed for STABLE_SECONDS. Changes are
detected with ctime (updated by every write and rename, and not settable by the client,
unlike mtime which file managers copy from the source) plus file count and total size.

Folders that are not imported (no confident match, duplicate) are moved to
INBOX_DIR/_not-imported/ with a <name>.txt note, so the inbox only shows pending work.
Moving a folder back into the inbox retries it.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

NOT_IMPORTED = '_not-imported'
POLL_SECONDS = float(os.environ.get('INBOX_POLL_SECONDS', '10'))
STABLE_SECONDS = float(os.environ.get('INBOX_STABLE_SECONDS', '60'))


@dataclass(frozen=True)
class Snapshot:
    files: int
    size: int
    newest_ctime: float


def snapshot(folder: Path) -> Snapshot:
    files = size = 0
    newest = folder.stat().st_ctime
    for dirpath, dirnames, filenames in os.walk(folder):
        for d in dirnames:
            newest = max(newest, os.lstat(os.path.join(dirpath, d)).st_ctime)
        for f in filenames:
            st = os.lstat(os.path.join(dirpath, f))
            files += 1
            size += st.st_size
            newest = max(newest, st.st_ctime)
    return Snapshot(files, size, newest)


def candidates(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.iterdir()
                  if p.is_dir() and not p.name.startswith(('.', '_')))


def park(folder: Path, inbox: Path, result: dict) -> Path | None:
    """Move a folder that was not (fully) imported to _not-imported with a note."""
    if not folder.exists():
        return None
    dest_dir = inbox / NOT_IMPORTED
    dest_dir.mkdir(exist_ok=True)
    dest, n = dest_dir / folder.name, 2
    while dest.exists() or dest.with_name(dest.name + '.txt').exists():
        dest, n = dest_dir / f'{folder.name} ({n})', n + 1
    shutil.move(str(folder), str(dest))
    note = [
        f"{result.get('status', 'not imported')}",
        f"processed: {result.get('finished', time.strftime('%Y-%m-%dT%H:%M:%S'))}",
        '',
    ]
    if result.get('albums'):
        note.append('Imported from this folder: ' + ', '.join(a['dir'] for a in result['albums']))
        note.append('The files left here were not imported.')
        note.append('')
    note += [
        'To retry, move the folder back into the inbox (fix tags first), or force a',
        'MusicBrainz release on the server:',
        '  cd ~/.local/share/docker/music-importer && docker-compose exec music-importer \\',
        f'    music-importer import "/inbox/{NOT_IMPORTED}/{dest.name}" --mbid <release id>',
        '',
        'For a custom album (fan edit, own compilation) add "[as-is]" to the folder name',
        'before moving it back: it is then imported with its own tags, without MusicBrainz.',
    ]
    dest.with_name(dest.name + '.txt').write_text('\n'.join(note) + '\n')
    log.info('moved %s to %s', folder, dest)
    return dest


def watch(inbox: Path, enqueue: Callable[[Path, str], bool], stop: threading.Event) -> None:
    """Poll the inbox and enqueue folders whose contents have settled."""
    inbox.mkdir(parents=True, exist_ok=True)
    log.info('watching inbox %s (ready after %.0f s without changes)', inbox, STABLE_SECONDS)
    last: dict[Path, Snapshot] = {}
    queued: set[Path] = set()
    warned_loose: set[str] = set()
    while not stop.is_set():
        try:
            present = set(candidates(inbox))
            for loose in inbox.iterdir():
                if loose.is_file() and not loose.name.startswith('.') and loose.name not in warned_loose:
                    warned_loose.add(loose.name)
                    log.warning('inbox: ignoring loose file %s (put albums in a folder)', loose.name)
            for folder in present:
                try:
                    snap = snapshot(folder)
                except FileNotFoundError:
                    continue  # renamed or removed while scanning
                prev = last.get(folder)
                last[folder] = snap
                if folder in queued:
                    if prev is not None and snap != prev:
                        queued.discard(folder)  # changed after queuing: wait for it again
                    continue
                settled = prev == snap and time.time() - snap.newest_ctime >= STABLE_SECONDS
                if settled and snap.files:
                    if enqueue(folder, 'inbox'):
                        queued.add(folder)
                        log.info('inbox: %s is complete (%d files, %.0f MB), queued',
                                 folder.name, snap.files, snap.size / 1e6)
            for gone in set(last) - present:
                last.pop(gone, None)
                queued.discard(gone)
        except Exception:
            log.exception('inbox scan failed')
        stop.wait(POLL_SECONDS)
