"""Furigana capture: the book's own readings, kept rather than discarded.

``source.py`` strips ``<rt>``/``<rp>`` because ruby spliced into the prose
poisons alignment — that is load-bearing and stays.  But the annotations it
throws away are an *author-supplied, publisher-aligned reading dictionary* for
exactly the words a grapheme-to-phoneme analyser gets wrong, and the validation
corpus is full of them: 62,780 ``<rt>`` in 葉隠, 16,484 in クリスマス・ピッグ.

So the readings are captured here in parallel with the stripping, as offsets
into the cleaned block text.  Nothing downstream of ``source.py`` changes; the
alignment path never looks at :attr:`Block.ruby`.

Why offsets and not a ``{word: reading}`` map.  A dictionary cannot say which
《橋》 is はし and which is キョウ, and a book contains both.  The whole value of
ruby over a lexicon is that it is positional, so the position is what is kept.

Two rules about *which* ruby is a reading at all:

* the annotation must be kana — a ``<rt>`` holding Latin text is a gloss, and
  「東京《とうきょう》」 is a reading while 「東京《Tokyo》」 is a translation;
* the base must contain a kanji — ruby on kana is 圏点 (emphasis dots) or a
  gloss, never a pronunciation, and substituting it would be a no-op at best.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from bs4 import NavigableString

#: Whitespace, including the ideographic space that indents Japanese paragraphs.
#: Single-character form: ``source._WS`` matches runs, this matches one.
_WS_CHAR = re.compile(r"[\s　 ]")

_KANA = re.compile(r"^[ぁ-ゖァ-ヺーゝゞヽヾ]+$")
_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002ffff]")

#: Where the reading is stashed on the ``<ruby>`` tag between the two passes.
#: An attribute rather than an ``id()`` map because bs4 ``Tag`` compares by
#: content, so tags cannot be used as dict keys.
YOMI_ATTR = "data-jisho-yomi"


@dataclass
class Ruby:
    """One furigana annotation, positioned in the cleaned block text."""

    start: int        #: offset into ``Block.text``
    end: int          #: exclusive
    base: str         #: the kanji it annotates, as it appears in the text
    reading: str      #: the ``<rt>`` content

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Ruby {self.start}:{self.end} {self.base}={self.reading}>"


def is_reading(base: str, reading: str) -> bool:
    """Is this ruby a pronunciation, rather than a gloss or emphasis?"""
    return bool(reading and _KANA.match(reading) and _KANJI.search(base or ""))


def to_katakana(text: str) -> str:
    """Hiragana to katakana, leaving everything else alone."""
    return "".join(
        chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text
    )


def stash_readings(soup) -> None:
    """Record each ``<ruby>``'s reading on the tag, before ``<rt>`` is dropped.

    Must run *before* the ``rt``/``rp`` decomposition in ``source.py``, since
    afterwards the reading is gone from the tree.
    """
    for ruby in soup.find_all("ruby"):
        parts = [rt.get_text() for rt in ruby.find_all("rt")]
        reading = "".join(parts).strip()
        if reading:
            ruby.attrs[YOMI_ATTR] = reading


def walk_text(el) -> Tuple[str, List[Tuple[int, int, str, str]]]:
    """Return the element's text and the raw span of every ruby base in it.

    The text is concatenated from the same strings ``get_text()`` would use, in
    the same order, so the offsets are offsets into that exact string.  It is
    built here rather than read back from ``get_text()`` because the two must
    not be able to drift; a test asserts they agree.
    """
    parts: List[str] = []
    pos = 0
    spans: List[Tuple[int, int, str, str]] = []
    for node in el.descendants:
        if type(node) is NavigableString:
            s = str(node)
            parts.append(s)
            pos += len(s)
        elif getattr(node, "name", None) == "ruby":
            reading = node.attrs.get(YOMI_ATTR)
            if reading:
                # The base is whatever this subtree still contributes — <rt> and
                # <rp> are already gone by now, so it is the kanji alone.
                base = node.get_text()
                if base:
                    spans.append((pos, pos + len(base), base, reading))
    return "".join(parts), spans


def clean_with_offsets(text: str) -> Tuple[str, List[int]]:
    """``source._clean``, plus a map from raw index to cleaned index.

    Whitespace runs collapse to one space and the ends are stripped, so an
    offset taken before cleaning does not survive it unaided.  ``pos_map[i]``
    is where raw character ``i`` ended up.
    """
    out: List[str] = []
    pos_map: List[int] = []
    prev_ws = False
    for ch in text:
        if _WS_CHAR.match(ch):
            if prev_ws and out:
                pos_map.append(len(out) - 1)
            else:
                pos_map.append(len(out))
                out.append(" ")
            prev_ws = True
        else:
            pos_map.append(len(out))
            out.append(ch)
            prev_ws = False

    collapsed = "".join(out)
    lead = len(collapsed) - len(collapsed.lstrip())
    cleaned = collapsed.strip()
    pos_map = [min(max(p - lead, 0), len(cleaned)) for p in pos_map]
    return cleaned, pos_map


def rubies_for(el) -> Tuple[str, List[Ruby]]:
    """The cleaned text of a block element and the ruby positioned inside it."""
    raw, spans = walk_text(el)
    cleaned, pos_map = clean_with_offsets(raw)

    out: List[Ruby] = []
    for start, end, base, reading in spans:
        if not is_reading(base, reading):
            continue
        if start >= len(pos_map) or end - 1 >= len(pos_map):
            continue
        s = pos_map[start]
        e = pos_map[end - 1] + 1
        if e <= s or e > len(cleaned):
            continue
        # Whitespace collapse can shave the span; trust the text, not the span.
        out.append(Ruby(s, e, cleaned[s:e], reading))
    return cleaned, out


def slice_ruby(rubies: List[Ruby], start: int, end: int) -> List[Ruby]:
    """Re-base the ruby falling wholly inside ``[start, end)`` onto that slice."""
    return [Ruby(r.start - start, r.end - start, r.base, r.reading)
            for r in rubies if r.start >= start and r.end <= end]


def apply(text: str, rubies: List[Ruby], accept=None) -> str:
    """Rewrite ``text`` with each accepted ruby base replaced by its reading.

    ``accept(ruby) -> bool`` decides one annotation at a time, which is how the
    "only where the engine disagrees" policy is applied without this module
    knowing anything about an engine.
    """
    if not rubies:
        return text
    out: List[str] = []
    cursor = 0
    for r in sorted(rubies, key=lambda r: r.start):
        if r.start < cursor or r.end > len(text):
            continue                      # overlapping or stale span
        if accept is not None and not accept(r):
            continue
        out.append(text[cursor:r.start])
        out.append(r.reading)
        cursor = r.end
    out.append(text[cursor:])
    return "".join(out)
