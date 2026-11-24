"""Import one downloaded folder: beets match/tag/move, then cover art clean-up."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import art, fingerprint, lyrics

log = logging.getLogger(__name__)

TEMPLATE = Path(os.environ.get('BEETS_TEMPLATE', '/app/config/beets.yaml'))
# What may be left behind in a download folder once its audio has been imported.
# A folder named with this marker is imported with its own tags, without MusicBrainz
# matching (custom albums, fan edits): e.g. "Metallica - ...And Justice for All (with Bass) [as-is]".
AS_IS_MARKER = re.compile(r'\[\s*as[\s_-]?is\s*\]', re.IGNORECASE)
LEFTOVER_OK = art.IMG | {'.txt', '.nfo', '.log', '.cue', '.m3u', '.m3u8', '.sfv', '.md5',
                         '.accurip', '.url', '.db', '.ini', '.pdf', '.lrc'}


@dataclass
class Settings:
    music_dir: Path = Path(os.environ.get('MUSIC_DIR', '/music'))
    downloads_dir: Path = Path(os.environ.get('DOWNLOADS_DIR', '/downloads'))
    data_dir: Path = Path(os.environ.get('DATA_DIR', '/data'))
    fanarttv_key: str = field(default_factory=lambda: os.environ.get('FANARTTV_API_KEY', ''))
    remove_leftovers: bool = os.environ.get('REMOVE_LEFTOVERS', '1') == '1'
    fetch_lyrics: bool = os.environ.get('LYRICS', '1') == '1'
    replaced_dir: Path = Path(os.environ.get('REPLACED_DIR', '/replaced'))
    replaced_keep_days: float = float(os.environ.get('REPLACED_KEEP_DAYS', '30'))

    @property
    def db(self) -> Path:
        return self.data_dir / 'library.db'


def beets_config(s: Settings, *, index_only: bool = False) -> Path:
    """Write the runtime beets config (outside the data volume: it holds the API key)."""
    cfg = yaml.safe_load(TEMPLATE.read_text())
    cfg['directory'] = str(s.music_dir)
    cfg['library'] = str(s.db)
    cfg['import']['log'] = str(s.data_dir / 'beets-import.log')
    cfg['fetchart']['fanarttv_key'] = s.fanarttv_key
    cfg['musicimporter'] = {'replaced_dir': str(s.replaced_dir),
                            'replaced_log': str(s.data_dir / 'replaced.jsonl')}
    if not s.fanarttv_key:
        cfg['fetchart']['sources'] = [x for x in cfg['fetchart']['sources'] if x != 'fanarttv']
    if index_only:
        # Register files where they are: no matching, moving, writing or plugins.
        cfg['plugins'] = []
        cfg['import'].update(move=False, copy=False, write=False, autotag=False,
                             duplicate_action='keep', log=None)
    path = Path('/tmp') / ('beets-index.yaml' if index_only else 'beets.yaml')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)  # replace: order matters
    return path


def run_beet(s: Settings, args: list[str], *, index_only: bool = False) -> subprocess.CompletedProcess:
    cfg = beets_config(s, index_only=index_only)
    env = {**os.environ, 'BEETSDIR': str(s.data_dir / 'beets')}
    verbose = ['-v'] if os.environ.get('BEETS_VERBOSE') == '1' else []
    cmd = ['beet', *verbose, '-c', str(cfg), *args]
    log.info('running beet %s', ' '.join(args))
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            log.info('beets: %s', line)
    return p


def audio_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob('*') if p.is_file() and p.suffix.lower() in art.AUDIO)


def albums_added_since(s: Settings, t0: float) -> list[tuple[int, Path, list[Path]]]:
    """(album id, album root, tracks) for albums beets added after t0."""
    if not s.db.exists():
        return []
    from beets.library import Library  # imported lazily: beets is heavy

    lib = Library(str(s.db), directory=str(s.music_dir))
    out = []
    for a in lib.albums():
        if a.added < t0:
            continue
        tracks = [Path(os.fsdecode(i.path)) for i in a.items()]
        if not tracks:
            continue
        root = Path(os.path.commonpath([t.parent for t in tracks]))
        if re.fullmatch(r'CD\d+', root.name):
            root = root.parent  # only one disc of a multi-disc release
        out.append((a.id, root, tracks))
    return out


def set_artpath(s: Settings, album_id: int, cover: Path | None) -> None:
    from beets.library import Library

    lib = Library(str(s.db), directory=str(s.music_dir))
    a = lib.get_album(album_id)
    if a is not None:
        a.artpath = os.fsencode(cover) if cover else None
        a.store()


def clean_download(folder: Path) -> str:
    """Remove a download folder whose audio has all been imported."""
    if not folder.exists():
        return 'gone'
    if audio_files(folder):
        return 'kept (audio left: not imported)'
    other = [p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() not in LEFTOVER_OK]
    if other:
        return 'kept (unexpected files: ' + ', '.join(p.name for p in other[:5]) + ')'
    shutil.rmtree(folder)
    return 'removed'


def _import(s: Settings, args: list[str], folder: Path) -> tuple[subprocess.CompletedProcess, str]:
    """Run beet import; return the process and beets' logged outcome for the folder
    ('skip', 'duplicate-skip', ... or '' when nothing was logged, i.e. imported)."""
    logfile = s.data_dir / 'beets-import.log'
    start = logfile.stat().st_size if logfile.exists() else 0
    p = run_beet(s, [*args, str(folder)])
    outcome = ''
    if logfile.exists():
        with open(logfile, 'rb') as fh:
            fh.seek(start)
            for line in fh.read().decode('utf-8', 'replace').splitlines():
                verb, _, path = line.partition(' ')
                if path.startswith(str(folder)) and verb != 'import':
                    outcome = verb
    return p, outcome


def is_as_is(folder: Path) -> bool:
    return bool(AS_IS_MARKER.search(folder.name))


def import_folder(folder: Path, s: Settings | None = None, *, search_id: str | None = None,
                  as_is: bool | None = None) -> dict:
    s = s or Settings()
    folder = folder.resolve()
    if as_is is None:
        as_is = is_as_is(folder)
    result: dict = {'folder': str(folder), 'started': time.strftime('%Y-%m-%dT%H:%M:%S')}
    tracks = audio_files(folder) if folder.is_dir() else []
    if not tracks:
        result['status'] = 'no audio files'
        return _record(s, result)

    t0 = time.time() - 1
    replaced_mark = _size(s.data_dir / 'replaced.jsonl')
    args = ['import', '-q']
    if as_is:
        args += ['-A']  # keep the files' own tags; beets still renames/moves them
    elif search_id:
        args += ['--search-id', search_id]
    p, outcome = _import(s, args, folder)
    result['match'] = 'as-is (own tags)' if as_is else 'tags'
    albums = albums_added_since(s, t0)
    if not albums and outcome == 'skip' and p.returncode == 0 and not search_id and not as_is:
        log.info('no confident match from tags; identifying by audio fingerprints')
        ident = fingerprint.identify(tracks)
        if ident:
            fingerprint.write_provisional_tags(ident)
            result['match'] = f"fingerprint -> {ident['release']['id']}"
            p, outcome = _import(s, args, folder)
            albums = albums_added_since(s, t0)
    result['beets_exit'] = p.returncode
    if not albums:
        if outcome.startswith('duplicate'):
            result['status'] = 'duplicate: already in the library'
        else:
            result['status'] = 'not imported: no confident match (retry with --mbid)'
        result['download'] = 'kept'
        return _record(s, result)

    result['albums'] = []
    for album_id, root, album_tracks in albums:
        rep = art.fix_album_art(root, album_tracks)
        cover = root / 'cover.jpg'
        set_artpath(s, album_id, cover if cover.exists() else None)
        rep['tracks'] = len(album_tracks)
        if s.fetch_lyrics:
            rep['lyrics'] = lyrics.fetch(album_tracks, s.data_dir)
            rep['actions'].append('lyrics: ' + lyrics._fmt(rep['lyrics']))
        result['albums'].append(rep)
        log.info('imported %s (%d tracks): %s', root, len(album_tracks), '; '.join(rep['actions']))
    replaced = _new_lines(s.data_dir / 'replaced.jsonl', replaced_mark)
    if replaced:
        result['replaced'] = replaced
        for r in replaced:
            log.info('replaced older copy %s (kept in %s for %g days)', r['from'], r['to'],
                     s.replaced_keep_days)
    purge_replaced(s)
    result['status'] = 'imported'
    result['download'] = clean_download(folder) if s.remove_leftovers else 'kept'
    return _record(s, result)


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _new_lines(path: Path, start: int) -> list[dict]:
    if not path.exists():
        return []
    with open(path, 'rb') as fh:
        fh.seek(start)
        return [json.loads(line) for line in fh.read().decode().splitlines() if line.strip()]


def purge_replaced(s: Settings) -> None:
    """Delete set-aside album copies older than REPLACED_KEEP_DAYS."""
    if not s.replaced_dir.is_dir():
        return
    cutoff = time.time() - s.replaced_keep_days * 86400
    for entry in s.replaced_dir.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                log.info('purged replaced copy %s', entry.name)
        except OSError as e:
            log.warning('could not purge %s: %r', entry, e)


def index_library(s: Settings | None = None) -> dict:
    """Register the existing library in the beets DB (read-only) so duplicates are skipped."""
    s = s or Settings()
    p = run_beet(s, ['import', '-q', '-A', '-C', '-W', str(s.music_dir)], index_only=True)
    return {'index_exit': p.returncode}


def _record(s: Settings, result: dict) -> dict:
    result['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    s.data_dir.mkdir(parents=True, exist_ok=True)
    with open(s.data_dir / 'imports.jsonl', 'a') as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + '\n')
    log.info('%s: %s', result['folder'], result['status'])
    return result
