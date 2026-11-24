"""music-importer command line.

  music-importer serve                       run the webhook server (container default)
  music-importer import DIR [--mbid ID|--as-is]  import one folder now (--as-is: keep own tags)
  music-importer index                       register the existing library in the beets DB
  music-importer art DIR                     normalise cover art of an album folder in place
  music-importer lyrics [DIR] [--retry-all]  fetch LRCLIB lyrics for the library (or DIR)
  music-importer bandcamp list [FILTER]      releases in your Bandcamp collection
  music-importer bandcamp album URL|TEXT...  import releases you own
  music-importer bandcamp artist URL|NAME    import (a choice of) everything you own from an artist
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import art, bandcamp, lyrics, pipeline, server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    ap = argparse.ArgumentParser(prog='music-importer')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sv = sub.add_parser('serve')
    sv.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    im = sub.add_parser('import')
    im.add_argument('folder', type=Path)
    im.add_argument('--mbid', help='MusicBrainz release id to use instead of searching')
    im.add_argument('--as-is', action='store_true', default=None,
                    help='keep the files\' own tags (no MusicBrainz); also implied by "[as-is]" in DIR')
    sub.add_parser('index')
    ar = sub.add_parser('art')
    ar.add_argument('folder', type=Path)
    ly = sub.add_parser('lyrics')
    ly.add_argument('folder', type=Path, nargs='?')
    ly.add_argument('--retry-all', action='store_true', help='also retry tracks checked recently')
    bandcamp.add_parser(sub)
    a = ap.parse_args(argv)

    if a.cmd == 'serve':
        server.serve(port=a.port)
    elif a.cmd == 'import':
        res = pipeline.import_folder(a.folder, search_id=a.mbid, as_is=a.as_is)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res['status'] == 'imported' else 1
    elif a.cmd == 'index':
        print(json.dumps(pipeline.index_library()))
    elif a.cmd == 'art':
        rep = art.fix_album_art(a.folder, pipeline.audio_files(a.folder))
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif a.cmd == 'lyrics':
        s = pipeline.Settings()
        tracks = lyrics.library_tracks(a.folder or s.music_dir)
        logging.info('lyrics: %d tracks', len(tracks))
        counts = lyrics.fetch(tracks, s.data_dir, retry_all=a.retry_all, progress_every=100)
        print(json.dumps(counts, indent=2))
    elif a.cmd == 'bandcamp':
        return bandcamp.main(a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
