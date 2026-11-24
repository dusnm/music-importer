"""Re-tag an album that is already in the library: match it against MusicBrainz again and
rewrite its tags and file names (beets `import -L`).

Useful for albums imported [as-is] or from Bandcamp's own tags, or after MusicBrainz data
improved. Without a release id the match is only applied when beets is confident (as for
imports); with --mbid that release is used. beets moves the audio files and the cover; this
module carries the lyrics sidecars (.lrc/.txt) and any other files of the album folder along,
keeps lyrics.db in step, and reports what changed.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import art, coverfetch, jobs, lyrics, pipeline

log = logging.getLogger(__name__)

SIDECARS = ('.lrc', '.txt')
FIELDS = ('albumartist', 'album', 'year', 'mb_albumid')


class RetagError(RuntimeError):
    pass


def _library(s: pipeline.Settings):
    from beets.library import Library
    return Library(str(s.db), directory=str(s.music_dir))


def _album_in(lib, folder: Path):
    """The one library album whose tracks are in folder (or its disc sub-folders)."""
    prefix = str(folder).rstrip('/') + '/'
    items = [i for i in lib.items() if os.fsdecode(i.path).startswith(prefix)]
    if not items:
        raise RetagError(f'no library tracks in {folder} (is it in the library? `mi index` registers it)')
    album_ids = {i.album_id for i in items}
    if len(album_ids) != 1 or None in album_ids:
        raise RetagError(f'{folder} holds {len(album_ids)} albums/singletons: name one album folder')
    return lib.get_album(album_ids.pop())


def _root(paths: list[Path]) -> Path:
    root = Path(os.path.commonpath([p.parent for p in paths]))
    return root.parent if root.name.startswith('CD') and root.name[2:].isdigit() else root


def retag(folder: Path, s: pipeline.Settings | None = None, *, mbid: str | None = None) -> dict:
    s = s or pipeline.Settings()
    folder = folder.resolve()
    lib = _library(s)
    album = _album_in(lib, folder)
    before = {f: album.get(f) for f in FIELDS}
    old_items = {i.id: Path(os.fsdecode(i.path)) for i in album.items()}
    titles_before = {i.id: i.title for i in album.items()}
    old_root = _root(list(old_items.values()))
    by_inode = {p.stat().st_ino: p for p in old_items.values()}
    result: dict = {'folder': str(old_root), 'started': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'match': f'retag {mbid}' if mbid else 'retag'}

    jobs.progress('matching with MusicBrainz (beets)', force=True)
    args = ['import', '-L', '-q']
    if mbid:
        args += ['--search-id', mbid]
    p, outcome = pipeline._import(s, args + [f'album_id:{album.id}'], old_root, trusted_release=bool(mbid))
    result['beets_exit'] = p.returncode
    if p.returncode != 0:
        raise RetagError(f'beets exited with {p.returncode} (see the job log)')

    lib = _library(s)
    new_album = lib.get_album(album.id) or next(
        (a for a in lib.albums() if any(Path(os.fsdecode(i.path)).stat().st_ino in by_inode
                                        for i in a.items() if os.path.exists(i.path))), None)
    if new_album is None:
        raise RetagError('the album is no longer in the library after beets ran (see the job log)')
    after = {f: new_album.get(f) for f in FIELDS}
    new_items = {i.id: Path(os.fsdecode(i.path)) for i in new_album.items()}
    if after == before and set(new_items.values()) == set(old_items.values()) and outcome == 'skip':
        result['status'] = 'unchanged: no confident match (retry with --mbid)'
        result['albums'] = []
        return pipeline._record(s, result)

    # Map every moved audio file by inode (a rename keeps it) and carry its sidecars along.
    moved: dict[Path, Path] = {}
    for new in new_items.values():
        old = by_inode.get(new.stat().st_ino)
        if old is not None and old != new:
            moved[old] = new
    new_root = _root(list(new_items.values()))
    carried = 0
    for old, new in moved.items():
        for ext in SIDECARS:
            src = old.with_suffix(ext)
            if src.exists():
                dst = new.with_suffix(ext)
                if dst.exists():
                    dst.unlink()
                shutil.move(str(src), str(dst))
                carried += 1
    # Whatever else was in the old album folder (booklets, notes) follows the album.
    extras = 0
    if new_root != old_root and old_root.exists():
        for f in sorted(old_root.rglob('*')):
            if f.is_file() and f.suffix.lower() not in art.AUDIO:
                dst = new_root / f.relative_to(old_root)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dst))
                    extras += 1
        for d in sorted((d for d in old_root.rglob('*') if d.is_dir()), key=lambda d: -len(d.parts)):
            if not any(d.iterdir()):
                d.rmdir()
        if not any(old_root.iterdir()):
            old_root.rmdir()
            parent = old_root.parent
            if parent != s.music_dir and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()   # the artist folder, when this was its only album

    if moved:
        con = lyrics._db(s.data_dir)   # creates the table in a library without lyrics yet
        con.executemany('UPDATE tracks SET path=? WHERE path=?', [(str(n), str(o)) for o, n in moved.items()])
        con.commit()
        con.close()

    jobs.progress(f'cover art: {new_root.name}', force=True)
    tracks = list(new_items.values())
    rep = coverfetch.fill(new_root, tracks, art.fix_album_art(new_root, tracks), s.fanarttv_key)
    cover = new_root / 'cover.jpg'
    pipeline.set_artpath(s, new_album.id, cover if cover.exists() else None)

    changed = [f'{f}: {before[f] or "-"} -> {after[f] or "-"}' for f in FIELDS if before[f] != after[f]]
    titles = sum(1 for i in new_album.items() if titles_before.get(i.id, i.title) != i.title)
    if titles:
        changed.append(f'{titles} title(s) changed')
    if new_root != old_root:
        changed.append(f'moved to {new_root}')
    if carried or extras:
        changed.append(f'carried along {carried} lyrics file(s) and {extras} other file(s)')
    rep['actions'] = changed + rep.get('actions', [])
    rep['tracks'] = len(tracks)
    result['albums'] = [rep]
    result['status'] = 'retagged' if (after != before or moved or titles) else 'unchanged: MusicBrainz agrees with the tags'
    result['download'] = 'gone'
    log.info('retagged %s: %s', new_root, '; '.join(rep['actions']))
    return pipeline._record(s, result)
