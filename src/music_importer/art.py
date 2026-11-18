"""Cover art normalisation, identical to the rules used for the existing library.

One cover.jpg per album folder: the largest available source image, at most 1200 px on
the long side (never upscaled), JPEG q85 4:2:0, progressive, mozjpeg-optimised. A JPEG
that is already small enough is only losslessly optimised unless re-encoding saves >= 15 %.
Embedded pictures are removed from the tracks.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import tempfile
from pathlib import Path

import mozjpeg_lossless_optimization as mozj
from mutagen import File
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_PX = 1200
QUALITY = 85
REENCODE_MIN_SAVING = 0.15
AUDIO = {'.flac', '.mp3', '.m4a', '.ogg', '.opus', '.wav', '.aiff', '.wv', '.ape'}
IMG = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
# Front-cover candidates; everything else (back, booklet scans, ...) is left alone.
COVER_NAMES = re.compile(r'^(cover|folder|front|albumart|album|art|00[ _.-]*cover.*)$', re.I)
JUNK = re.compile(r'^(albumartsmall|albumart_\{.*\}_(large|small))$', re.I)


# ---------- images ----------
def _load_rgb(data: bytes) -> Image.Image:
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if im.mode in ('RGBA', 'LA', 'P', 'PA'):
        im = im.convert('RGBA')
        bg = Image.new('RGB', im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im if im.mode == 'RGB' else im.convert('RGB')


def image_size(data: bytes) -> tuple[str | None, int, int]:
    try:
        im = Image.open(io.BytesIO(data))
        return im.format, im.size[0], im.size[1]
    except Exception:
        return None, 0, 0


def normalise(data: bytes) -> tuple[bytes, str]:
    """Return (jpeg bytes, description of what was done)."""
    fmt, w, h = image_size(data)
    im = _load_rgb(data)
    scale = MAX_PX / max(im.size)
    if scale < 1:
        im = im.resize((round(im.size[0] * scale), round(im.size[1] * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=QUALITY, subsampling=2, progressive=True, optimize=True)
    enc = mozj.optimize(buf.getvalue())
    if fmt == 'JPEG' and scale >= 1:
        try:
            lossless = mozj.optimize(data)
        except Exception:
            lossless = data
        if len(enc) > (1 - REENCODE_MIN_SAVING) * len(lossless):
            return lossless, f'lossless {w}x{h}'
    return enc, f'reencode {w}x{h}->{im.size[0]}x{im.size[1]}'


# ---------- embedded art ----------
def embedded_pictures(path: Path) -> list[tuple[int, bytes]]:
    f = File(path)
    if f is None:
        return []
    if isinstance(f, FLAC):
        return [(p.type, p.data) for p in f.pictures]
    if isinstance(f.tags, ID3):
        return [(f.tags[k].type, f.tags[k].data) for k in f.tags.keys() if k.startswith('APIC')]
    if isinstance(f, MP4):
        return [(3, bytes(c)) for c in (f.tags or {}).get('covr', [])]
    if f.tags is not None and 'metadata_block_picture' in f.tags:
        pics = [Picture(base64.b64decode(b)) for b in f.tags['metadata_block_picture']]
        return [(p.type, p.data) for p in pics]
    return []


def strip_pictures(path: Path) -> None:
    f = File(path)
    if f is None:
        return
    if isinstance(f, FLAC):
        f.clear_pictures()
    elif isinstance(f.tags, ID3):
        f.tags.delall('APIC')
    elif isinstance(f, MP4):
        f.tags.pop('covr', None)
    elif f.tags is not None:
        f.tags.pop('metadata_block_picture', None)
    f.save()


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.cover-tmp-')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---------- per album ----------
def fix_album_art(album_dir: Path, tracks: list[Path]) -> dict:
    """Write album_dir/cover.jpg from the best source and strip embedded art.

    Cover candidates are looked for in album_dir and in the folders holding the tracks
    (disc sub-folders); the redundant copies are removed afterwards.
    """
    report: dict = {'dir': str(album_dir), 'actions': []}
    folders = sorted({album_dir, *(t.parent for t in tracks)})
    images: list[Path] = []
    junk: list[Path] = []
    for d in folders:
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in IMG:
                continue
            if JUNK.match(p.stem):
                junk.append(p)
            elif COVER_NAMES.match(p.stem) or (d == album_dir and p.stem == album_dir.name):
                images.append(p)

    best = None  # (area, prefer_file, label, data)
    for p in images:
        data = p.read_bytes()
        fmt, w, h = image_size(data)
        if fmt and (best is None or (w * h, 1) > best[:2]):
            best = (w * h, 1, str(p.relative_to(album_dir)), data)
    with_art: list[Path] = []
    for t in tracks:
        try:
            pics = embedded_pictures(t)
        except Exception as e:  # unreadable tag block: leave the file alone
            report['actions'].append(f'cannot read embedded art in {t.name}: {e!r}')
            continue
        if not pics:
            continue
        with_art.append(t)
        front = [p for p in pics if p[0] == 3] or pics
        data = max(front, key=lambda p: len(p[1]))[1]
        fmt, w, h = image_size(data)
        if fmt and (best is None or (w * h, 0) > best[:2]):
            best = (w * h, 0, f'embedded:{t.name}', data)

    if best is None:
        report['cover'] = None
        report['actions'].append('no cover art found')
    else:
        jpeg, how = normalise(best[3])
        target = album_dir / 'cover.jpg'
        _atomic_write(target, jpeg)
        _, w, h = image_size(jpeg)
        report['cover'] = {'source': best[2], 'how': how, 'bytes': len(jpeg), 'size': f'{w}x{h}'}
        report['actions'].append(f'cover.jpg from {best[2]} ({how}, {len(jpeg) // 1024} KB)')
        for p in images:
            if p != target and p.exists():
                p.unlink()
                report['actions'].append(f'removed {p.relative_to(album_dir)}')
    for p in junk:
        p.unlink()
        report['actions'].append(f'removed {p.relative_to(album_dir)}')
    for t in with_art:
        strip_pictures(t)
    if with_art:
        report['actions'].append(f'stripped embedded art from {len(with_art)} track(s)')
    return report
