"""beets plugin: "newer wins" duplicate replacement that keeps the old copy for a while.

With `import.duplicate_action: remove`, beets deletes the files of an album already in
the library with the same album artist + album before moving the new one in. This
plugin replaces that deletion: the old album's whole folder (including booklets and
other extra files) is moved to `musicimporter.replaced_dir`, and a JSON line is
appended to `musicimporter.replaced_log`. music_importer.pipeline purges old entries.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from beets.dbcore.query import PathQuery
from beets.importer import tasks
from beets.plugins import BeetsPlugin

_original_remove_duplicates = tasks.ImportTask.remove_duplicates


def _unique(dest: Path) -> Path:
    n = 2
    candidate = dest
    while candidate.exists():
        candidate = dest.with_name(f'{dest.name} ({n})')
        n += 1
    return candidate


def _prune(path: Path, stop: Path) -> None:
    """Remove empty directories from path up to (not including) stop."""
    while path != stop and stop in path.parents:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


class MusicImporterPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({'replaced_dir': '/replaced', 'replaced_log': '/data/replaced.jsonl'})
        plugin = self

        def remove_duplicates(task, lib):
            if getattr(task, '_upgrade_superseded', None) is not None:
                return _original_remove_duplicates(task, lib)
            for album in task.find_duplicates(lib):
                plugin.set_aside(lib, album, task)

        tasks.ImportTask.remove_duplicates = remove_duplicates

    def set_aside(self, lib, album, task) -> None:
        libdir = Path(os.fsdecode(lib.directory))
        items = list(album.items())
        paths = [Path(os.fsdecode(i.path)) for i in items]
        replaced = Path(self.config['replaced_dir'].as_filename())
        stamp = time.strftime('%Y-%m-%d %H%M%S')
        label = f'{album.albumartist} - {album.album}'.replace('/', '_')
        dest = _unique(replaced / f'{stamp} {label}')

        root = Path(os.path.commonpath([p.parent for p in paths])) if paths else None
        if root is not None and re.fullmatch(r'CD\s*\d+', root.name, re.I):
            root = root.parent
        ids = {i.id for i in items}
        # Move the folder as a whole only if it is a proper Artist/Album folder that holds
        # nothing else from the library; otherwise move just this album's files.
        whole = (
            root is not None and libdir in root.parents and len(root.relative_to(libdir).parts) >= 2
            and all(i.id in ids for i in lib.items(PathQuery('path', os.fsencode(root))))
            and not any(Path(os.fsdecode(i.path)).is_relative_to(root) for i in task.items)
        )
        replaced.mkdir(parents=True, exist_ok=True)
        if whole:
            shutil.move(str(root), str(dest))
            moved = str(root.relative_to(libdir))
        else:
            for p in paths:
                if p.exists() and libdir in p.parents:
                    target = dest / p.relative_to(libdir)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(target))
            moved = 'files of ' + label
        for item in items:
            item.remove(with_album=False)
        album.remove(with_items=False)
        if root is not None:
            _prune(root if not whole else root.parent, libdir)
        entry = {'time': stamp, 'albumartist': album.albumartist, 'album': album.album,
                 'year': album.year, 'tracks': len(items), 'from': moved, 'to': str(dest)}
        with open(self.config['replaced_log'].as_filename(), 'a') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self._log.info('replaced older copy {} -> {}', moved, dest)
