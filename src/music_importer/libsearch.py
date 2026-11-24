"""Fuzzy search over the library's album folders (<music>/<Artist>/<Album (Year)>).

Every word of the query must match a word of "artist album year": exactly, as its start,
inside it, or with a small typo. Case, accents and punctuation are ignored. Used by
`find` and by the commands that take an album folder (art, lyrics, retag), so a search
like "plagues babylon" can stand in for the path.
"""
from __future__ import annotations

import difflib
import re
import sys
import unicodedata
from pathlib import Path

TYPO_RATIO = 0.8        # difflib similarity for a word to count as a typo match
CLEAR_LEAD = 0.15       # a best match this far ahead of the next is taken without asking


class SearchError(RuntimeError):
    pass


# Letters that are not "letter + accent", so NFKD alone would drop them.
_LETTERS = str.maketrans({'ø': 'o', 'Ø': 'O', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE', 'ß': 'ss',
                          'đ': 'dj', 'Đ': 'Dj', 'ð': 'd', 'Ð': 'D', 'ł': 'l', 'Ł': 'L', 'þ': 'th', 'Þ': 'Th',
                          'ı': 'i', '…': '...'})


def _norm(text: str) -> str:
    t = unicodedata.normalize('NFKD', text.translate(_LETTERS)).encode('ascii', 'ignore').decode().casefold()
    return re.sub(r'[\W_]+', ' ', t).strip()


def albums(music_dir: Path) -> list[Path]:
    out = []
    for artist in sorted(music_dir.iterdir()):
        if artist.is_dir() and not artist.name.startswith(('.', '_')):
            out += [a for a in sorted(artist.iterdir()) if a.is_dir() and not a.name.startswith('.')]
    return out


def _word_score(q: str, words: list[str]) -> float:
    best = 0.0
    for w in words:
        if w == q:
            return 1.0
        if w.startswith(q):
            best = max(best, 0.9)
        elif len(q) >= 3 and q in w:
            best = max(best, 0.8)
        elif len(q) >= 4:
            r = difflib.SequenceMatcher(None, q, w).ratio()
            if r >= TYPO_RATIO:
                best = max(best, 0.7 * r)
    return best


def score(query: str, album_dir: Path) -> float:
    q_words = _norm(query).split()
    text = _norm(f'{album_dir.parent.name} {album_dir.name}')
    words = text.split()
    if not q_words:
        return 0.0
    scores = [_word_score(q, words) for q in q_words]
    if min(scores) == 0:
        return 0.0                                  # every word must match
    s = sum(scores) / len(scores)
    if ' '.join(q_words) in text:
        s += 0.1                                    # the words in this order, side by side
    return round(s, 3)


def search(query: str, music_dir: Path, limit: int = 15) -> list[tuple[float, Path]]:
    hits = [(score(query, a), a) for a in albums(music_dir)]
    hits = [h for h in hits if h[0] > 0]
    hits.sort(key=lambda h: (-h[0], str(h[1]).casefold()))
    return hits[:limit]


def _label(path: Path, music_dir: Path) -> str:
    return str(path.relative_to(music_dir))


def resolve(arg: str, music_dir: Path) -> Path:
    """An existing path as is; otherwise the album folder a search picks (asking if unclear)."""
    p = Path(arg)
    if p.exists():
        return p.resolve()
    hits = search(arg, music_dir, limit=10)
    if not hits:
        raise SearchError(f'no such folder, and no album in the library matches "{arg}" (mi find ...)')
    if len(hits) == 1 or hits[0][0] - hits[1][0] >= CLEAR_LEAD:
        print(f'"{arg}" -> {_label(hits[0][1], music_dir)}', file=sys.stderr)
        return hits[0][1]
    choices = '\n'.join(f'  {n:2}  {_label(h[1], music_dir)}' for n, h in enumerate(hits, 1))
    if not sys.stdin.isatty():
        raise SearchError(f'"{arg}" matches several albums; be more specific:\n{choices}')
    print(f'"{arg}" matches several albums:\n{choices}', file=sys.stderr)
    while True:
        ans = input('which one? [number, q = quit]: ').strip().lower()
        if ans in ('q', ''):
            raise SearchError('nothing chosen')
        if ans.isdigit() and 1 <= int(ans) <= len(hits):
            return hits[int(ans) - 1][1]
        print('  not understood, try again', file=sys.stderr)
