"""German text normalisation, applied to the spoken text and nothing else.

German letter-to-sound is nearly solved: ß, umlauts, compound stress and even
1883 spelling come out right under rule-based G2P, and there is no reading
ambiguity remotely like kanji.  What German engines get wrong is not phonetics
but **convention**, and they get it wrong identically — measured on espeak-ng,
which is what Piper embeds:

===========================  ================================  ==================
input                        espeak-ng says                    should be
===========================  ================================  ==================
``am 1. Mai 1889``           eins Mai eintausendachthundert…   ersten Mai
                                                               achtzehnhundert…
``z. B.``, ``d. h.``         tsɛt beː, deː haː                 zum Beispiel, …
``27,50 €``                  komma fünf null Euro              siebenundzwanzig
                                                               Euro fünfzig
``im 19. Jahrhundert``       neunzehn Jahrhundert              neunzehnten
===========================  ================================  ==================

So the leverage in German is a pass we own, before the engine — and it is worth
more than the choice of engine, because every engine needs it.

This runs on the **spoken** copy of a sentence only.  The subtitle keeps the
author's text, for the same reason the alignment path keeps it: what 白い熊
reads must be the book, not our expansion of it.

*Ordinals are the one thing that cannot be got fully right here.*  German
ordinals inflect — *der 4. Versuch* is *vierte*, *mein 4. Problem* is *viertes*,
*am 4. Mai* is *vierten* — and choosing correctly needs the case, which needs a
parse.  What is done instead is narrow on purpose: an ordinal is only expanded
where a preceding article or contraction makes the ending predictable, or where
a month name follows.  Everywhere else the digits are left alone, because
espeak reading *vier* for *4.* is wrong in a small way and *viertes* for
*vierten* is wrong in a way a learner would copy.
"""

from __future__ import annotations

import re
from typing import Dict

from num2words import num2words

#: Abbreviations that are read letter by letter otherwise.  Spelled with the
#: spaces German typography actually uses, and matched with either spacing.
ABBREVIATIONS: Dict[str, str] = {
    "z. B.": "zum Beispiel",
    "z.B.": "zum Beispiel",
    "d. h.": "das heißt",
    "d.h.": "das heißt",
    "u. a.": "unter anderem",
    "u.a.": "unter anderem",
    "u. Ä.": "und Ähnliches",
    "s. o.": "siehe oben",
    "s. u.": "siehe unten",
    "v. a.": "vor allem",
    "v.a.": "vor allem",
    "u. U.": "unter Umständen",
    "z. T.": "zum Teil",
    "i. d. R.": "in der Regel",
    "bzw.": "beziehungsweise",
    "usw.": "und so weiter",
    "usf.": "und so fort",
    "etc.": "et cetera",
    "ca.": "circa",
    "vgl.": "vergleiche",
    "evtl.": "eventuell",
    "ggf.": "gegebenenfalls",
    "inkl.": "inklusive",
    "exkl.": "exklusive",
    "sog.": "sogenannte",
    "Nr.": "Nummer",
    "Abb.": "Abbildung",
    "Bd.": "Band",
    "Jh.": "Jahrhundert",
    "Jhd.": "Jahrhundert",
    "Kap.": "Kapitel",
    "Aufl.": "Auflage",
    "Hrsg.": "Herausgeber",
    "Dr.": "Doktor",
    "Prof.": "Professor",
    "St.": "Sankt",
}

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember",
          "Jänner", "Feber")

#: Words after which an ordinal takes the weak ``-en`` ending.  These are the
#: contractions and articles that mark dative or accusative, which is what a
#: date or a century reference almost always is.
_EN_TRIGGERS = ("am", "im", "vom", "zum", "beim", "dem", "den", "seit",
                "ab", "bis", "nach", "vor", "seinem", "ihrem", "diesem",
                "diesen", "meinem")
#: Words after which it takes ``-e`` — nominative with a definite article.
_E_TRIGGERS = ("der", "die", "das", "ein", "eine", "jeder", "jede", "jedes")

_ABBREV_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(ABBREVIATIONS, key=len, reverse=True)))

#: ``1889`` standing alone.  Bounded to plausible years so that a page count or
#: a sum of money is not read as a date.  A *following* period is allowed — a
#: year ends sentences constantly — but not one that starts another digit
#: group, so ``1.000`` is left to the thousands rule that ran before this.
_YEAR_RE = re.compile(r"(?<![\d.,])(1[0-9]{3}|20[0-9]{2})(?![\d,])(?!\.\d)")

#: ``27,50 €`` or ``€ 27,50``, and bare ``12 Euro``.
_MONEY_RE = re.compile(
    r"(?<![\w])(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))?\s*(?:€|EUR\b|Euro\b)")

#: ``1.`` followed by a word — the shape of an ordinal in prose.
_ORDINAL_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(?=\s+\S)")

#: A decimal like ``3,5``: German writes the comma, engines read "komma".
_DECIMAL_RE = re.compile(r"(?<![\d.,])(\d+),(\d+)(?![\d])")

#: Digit-group separators: ``1.000`` is a thousand, not "eins Punkt null".
_THOUSANDS_RE = re.compile(r"(?<![\d.,])(\d{1,3})(?:\.(\d{3}))+(?![\d])")


def _cardinal(n: int) -> str:
    return num2words(n, lang="de")


def _ordinal(n: int, ending: str) -> str:
    """``num2words`` gives the ``-e`` form; swap the ending when the case wants it."""
    word = num2words(n, to="ordinal", lang="de")
    if ending == "en" and word.endswith("e"):
        return word + "n"
    return word


def expand_abbreviations(text: str) -> str:
    return _ABBREV_RE.sub(lambda m: ABBREVIATIONS[m.group(0)], text)


def expand_years(text: str) -> str:
    """``1889`` as *achtzehnhundertneunundachtzig*, not *eintausend…*."""
    return _YEAR_RE.sub(lambda m: num2words(int(m.group(1)), to="year", lang="de"),
                        text)


def expand_money(text: str) -> str:
    def one(m: re.Match) -> str:
        whole = int(m.group(1).replace(".", ""))
        cents = m.group(2)
        out = f"{_cardinal(whole)} Euro"
        if cents and int(cents):
            out += f" {_cardinal(int(cents))}"
        return out
    return _MONEY_RE.sub(one, text)


def expand_ordinals(text: str) -> str:
    """Only where the preceding word makes the ending predictable.

    Deliberately narrow.  ``am 1. Mai`` and ``im 19. Jahrhundert`` are the cases
    that actually occur in prose and both are unambiguous; a bare ``4.`` in an
    unknown case is left as digits rather than guessed at.
    """
    out = []
    last = 0
    for m in _ORDINAL_RE.finditer(text):
        number = int(m.group(1))
        before = text[:m.start()].rstrip()
        prev = re.split(r"[\s(„»\"']+", before)[-1].strip(".,;:!?") if before else ""
        after = text[m.end():].lstrip().split(" ", 1)[0].strip(".,;:!?")

        ending = None
        if prev.lower() in _EN_TRIGGERS:
            ending = "en"
        elif prev.lower() in _E_TRIGGERS:
            ending = "e"
        elif after in MONTHS:
            # "1. Mai 1889" with nothing in front: a date reads as dative.
            ending = "en"
        if ending is None:
            continue
        out.append(text[last:m.start()])
        out.append(_ordinal(number, ending))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def expand_numbers(text: str) -> str:
    """Thousands separators and decimal commas, which are read as punctuation."""
    text = _THOUSANDS_RE.sub(lambda m: _cardinal(int(m.group(0).replace(".", ""))),
                             text)
    return _DECIMAL_RE.sub(
        lambda m: f"{_cardinal(int(m.group(1)))} Komma "
                  f"{' '.join(_cardinal(int(d)) for d in m.group(2))}", text)


def normalise(text: str) -> str:
    """Everything, in the order the rules depend on each other.

    Abbreviations first, because they contain the periods that would otherwise
    look like ordinal markers.  Money before years, so ``1.000 €`` is not read
    as a date.  Ordinals before bare years, so ``am 1. Mai 1889`` gets both.
    """
    text = expand_abbreviations(text)
    text = expand_money(text)
    text = expand_ordinals(text)
    text = expand_numbers(text)
    text = expand_years(text)
    return text
