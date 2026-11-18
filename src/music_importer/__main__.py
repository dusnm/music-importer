"""music-importer command line.

  music-importer serve                       run the webhook server (container default)
  music-importer import DIR [--mbid ID]      import one folder now (DIR inside /downloads)
  music-importer index                       register the existing library in the beets DB
  music-importer art DIR                     normalise cover art of an album folder in place
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import art, pipeline, server


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
    sub.add_parser('index')
    ar = sub.add_parser('art')
    ar.add_argument('folder', type=Path)
    a = ap.parse_args(argv)

    if a.cmd == 'serve':
        server.serve(port=a.port)
    elif a.cmd == 'import':
        res = pipeline.import_folder(a.folder, search_id=a.mbid)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res['status'] == 'imported' else 1
    elif a.cmd == 'index':
        print(json.dumps(pipeline.index_library()))
    elif a.cmd == 'art':
        rep = art.fix_album_art(a.folder, pipeline.audio_files(a.folder))
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
