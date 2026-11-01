"""Sentence segmentation of the reference text.

The book's own text is segmented, never the ASR transcript.  That is the whole
point: the book has correct spelling and correct punctuation, so segmentation is
an easy problem here and a hard one on a transcript.  It also means the subtitle
text 白い熊 reads is the author's, not Whisper's.

pysbd is used because it ships hand-built rule modules for all four target
languages — German with a 115-entry abbreviation list, Japanese with 「」 and
（） protection so dialogue is not cut mid-quote — rather than a generic
punctuation fallback.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import List

import pysbd

from . import ruby as ruby_mod
from .ruby import Ruby
from .source import Block

#: pysbd language codes for the languages we support.
SUPPORTED = {"de", "ja", "pl", "ru", "en", "fr", "es", "it", "nl", "zh"}

_ALIASES = {"cz": "cs", "jp": "ja", "gr": "el"}


@dataclass
class Sentence:
    """One subtitle's worth of reference text."""

    text: str
    doc: str
    is_heading: bool
    block_index: int

    #: The furigana lying inside this sentence, re-based onto :attr:`text`.
    #: Empty everywhere except the TTS path, which is the only caller that
    #: wants the book's own readings — see ``ruby.py``.
    ruby: List[Ruby] = field(default_factory=list)


@functools.lru_cache(maxsize=None)
def _segmenter(lang: str):
    return pysbd.Segmenter(language=lang, clean=False)


def normalise_lang(lang: str) -> str:
    lang = lang.lower()
    lang = _ALIASES.get(lang, lang)
    if lang not in SUPPORTED:
        # pysbd falls back to English rules for anything it does not know, which
        # is a reasonable default for Latin-script languages.
        return "en"
    return lang


#: A fragment with none of these is punctuation, not a sentence.
_HAS_WORD = re.compile(r"\w", re.UNICODE)


def _reattach_orphans(pieces: List[str]) -> List[str]:
    """Fold punctuation-only fragments back into their neighbour.

    Segmenters hand back the stray closing quote or dash that dialogue
    formatting leaves behind — «Also sprach Zarathustra» yields 352 of them,
    pieces like `”` and `-`.  Each would become a subtitle showing a single
    punctuation mark, timed by interpolation because it matches no audio, and
    the app would dutifully stop playback on it.

    They are reattached rather than discarded: the mark belongs to the sentence
    it was split from, and the cue text has to stay faithful to the book.
    """
    out: List[str] = []
    for piece in pieces:
        if not _HAS_WORD.search(piece):
            if out:
                out[-1] = f"{out[-1]} {piece}".strip()
                continue
            # Nothing to attach to yet; hold it for the next real sentence.
            out.append(piece)
            continue
        if out and not _HAS_WORD.search(out[-1]):
            out[-1] = f"{out[-1]} {piece}".strip()
        else:
            out.append(piece)
    return [p for p in out if _HAS_WORD.search(p)]


#: Past this many characters a "sentence" is certainly several.  At German
#: narration speed 400 characters is about half a minute of unbroken audio,
#: which is no longer a subtitle.
OVERLONG = 400

#: A sentence end that a segmenter missed: terminal punctuation, optional
#: closing quotes or brackets, then whitespace and something that starts a
#: sentence.  German capitalises its nouns, so the capital alone is weak
#: evidence — the guard is the word *before* the stop.
_MISSED_BREAK = re.compile(r'(?<=[.!?])(["»«”』」）)\]]*)\s+(?=[«»„“"\'(\[–—]?[A-ZÄÖÜ])')

#: An abbreviation or an initial, where the period is not a sentence end.
_NOT_A_STOP = re.compile(r'(?:^|[\s(])(?:[A-Za-zÄÖÜäöü]{1,2}|[A-ZÄÖÜ])\.$')


def _split_overlong(pieces: List[str], limit: int = OVERLONG) -> List[str]:
    """Break pieces the segmenter left far too long.

    pysbd protects quoted spans from splitting, which is right for dialogue and
    catastrophic when the quotes do not balance: «Zur Genealogie der Moral»
    has a block with 28 ``»`` and 27 ``«``, so one unmatched mark swallowed
    everything after it — 8,481 characters holding 22 sentence endings, which
    would have become a single half-hour cue.

    Only over-long pieces are touched, so ordinary dialogue keeps the
    protection it is given.  A split is refused when the word before the stop
    looks like an abbreviation or an initial, because "Prof. Dr. Müller" is
    worth more than one extra break.
    """
    out: List[str] = []
    for piece in pieces:
        if len(piece) <= limit:
            out.append(piece)
            continue
        cuts, last = [], 0
        for m in _MISSED_BREAK.finditer(piece):
            head = piece[last:m.end(1)]
            if _NOT_A_STOP.search(head.rstrip()):
                continue
            if len(head.strip()) < 2:
                continue
            cuts.append(head.strip())
            last = m.end()
        tail = piece[last:].strip()
        if tail:
            cuts.append(tail)
        out.extend(cuts if len(cuts) > 1 else [piece])
    return out


def split_block(text: str, lang: str) -> List[str]:
    try:
        parts = _segmenter(lang).segment(text)
    except Exception:
        # pysbd occasionally trips on pathological input; a whole block is a
        # far better fallback than losing it.
        parts = [text]
    pieces = _reattach_orphans([p.strip() for p in parts if p and p.strip()])
    return _split_overlong(pieces)


def segment(blocks: List[Block], lang: str) -> List[Sentence]:
    """Turn reference blocks into one-sentence units.

    A heading is kept whole even when it has no terminal punctuation — «Das
    Glaskind» is one spoken unit, and splitting it would produce cues the
    narrator never pauses between.
    """
    lang = normalise_lang(lang)
    out: List[Sentence] = []
    for i, b in enumerate(blocks):
        pieces = [b.text] if b.is_heading else split_block(b.text, lang)
        # Ruby is positioned in the *block*, so each piece has to be found
        # again to re-base it.  A piece that cannot be located simply carries
        # no ruby: _reattach_orphans may have joined two fragments with a
        # space that was never in the block, and a missing reading is a far
        # smaller problem than a misplaced one.
        cursor = 0
        for piece in pieces:
            rubies: List[Ruby] = []
            if b.ruby:
                at = b.text.find(piece, cursor)
                if at >= 0:
                    rubies = ruby_mod.slice_ruby(b.ruby, at, at + len(piece))
                    cursor = at + len(piece)
            out.append(Sentence(piece, b.doc, b.is_heading, i, rubies))
    return out
