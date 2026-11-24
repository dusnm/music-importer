"""What each kind of job does. Used by the background worker and by --fg runs alike.

Every runner takes (params, row) - row is the job's database row, or None in the
foreground - and returns a result dict; "_summary" becomes the one-line outcome.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import art, bandcamp, inbox, jobs, lyrics, pipeline

SETTLE_SECONDS = float(os.environ.get('SETTLE_SECONDS', '10'))
INBOX_DIR = os.environ.get('INBOX_DIR', '')


def _dirs(result: dict) -> str:
    return ', '.join(a['dir'] for a in result.get('albums', []))


def _import_summary(result: dict) -> str:
    dirs = _dirs(result)
    return result['status'] + (f' -> {dirs}' if dirs else '')


@jobs.runner('import')
def run_import(p: dict, row=None) -> dict:
    s = pipeline.Settings()
    folder = Path(p['folder'])
    source = p.get('source', 'cli')
    if source == 'slskd':
        end = time.monotonic() + SETTLE_SECONDS  # let slskd finish renaming/closing the last file
        while time.monotonic() < end:
            jobs.progress('waiting for slskd to finish writing')
            time.sleep(0.5)
    result: dict = {'status': 'failed with an error, see the job log'}
    try:
        result = pipeline.import_folder(folder, s, search_id=p.get('mbid'), as_is=p.get('as_is'))
    finally:
        if source == 'inbox' and INBOX_DIR and folder.exists():  # anything left is not imported
            parked = inbox.park(folder, Path(INBOX_DIR), result)
            if parked:
                result['parked'] = str(parked)
    summary = _import_summary(result)
    if result.get('parked'):
        summary += f'; left in {result["parked"]}'
    state = 'done' if result['status'] == 'imported' else 'failed'
    return {**result, '_summary': summary, '_state': state}


@jobs.runner('art')
def run_art(p: dict, row=None) -> dict:
    folder = Path(p['folder'])
    jobs.progress(f'cover art: {folder.name}')
    rep = art.fix_album_art(folder, pipeline.audio_files(folder))
    return {**rep, '_summary': '; '.join(rep.get('actions', [])) or 'nothing to do'}


@jobs.runner('lyrics')
def run_lyrics(p: dict, row=None) -> dict:
    s = pipeline.Settings()
    folder = Path(p['folder']) if p.get('folder') else s.music_dir
    jobs.progress('listing tracks')
    tracks = lyrics.library_tracks(folder)
    counts = lyrics.fetch(tracks, s.data_dir, retry_all=bool(p.get('retry_all')))
    return {'counts': counts, '_summary': f'{len(tracks)} tracks: ' + lyrics._fmt(counts)}


_bc = None
_bc_loaded = 0.0
BANDCAMP_SESSION_SECONDS = 900


def _bandcamp(s: pipeline.Settings, *, fresh: bool = False):
    """One logged-in Bandcamp session, reused by consecutive jobs for a while."""
    global _bc, _bc_loaded
    if fresh or _bc is None or time.time() - _bc_loaded > BANDCAMP_SESSION_SECONDS:
        jobs.progress('loading your Bandcamp collection')
        _bc, _bc_loaded = bandcamp.connect(s), time.time()
    return _bc


@jobs.runner('bandcamp')
def run_bandcamp(p: dict, row=None) -> dict:
    s = pipeline.Settings()
    want = str(p['item_id'])
    item = None
    for fresh in (False, True):  # a release bought after the session was loaded
        bc = _bandcamp(s, fresh=fresh)
        item = next((i for i in bc.purchases if str(i.item_id) == want), None)
        if item is not None:
            break
    if item is None:
        raise bandcamp.BandcampImportError(f'release {want} is no longer in your Bandcamp collection')
    ok, summary, res = bandcamp.import_and_record(bc, item, s, fmt=p.get('format', 'flac'),
                                                 as_is=bool(p.get('as_is')), force=bool(p.get('force')))
    return {**(res or {}), '_summary': summary, '_state': 'done' if ok else 'failed'}
