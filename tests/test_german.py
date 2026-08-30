"""German normalisation: the conventions every engine gets wrong.

German letter-to-sound is nearly solved, so what is tested here is not
phonetics but dates, abbreviations, ordinals and money — measured failures of
espeak-ng, which is what Piper embeds.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jisho_subs.german import (expand_abbreviations, expand_money,
                               expand_numbers, expand_ordinals, expand_years,
                               normalise)


@pytest.mark.parametrize("raw, expected", [
    ("z. B. Wien", "zum Beispiel Wien"),
    ("z.B. Wien", "zum Beispiel Wien"),
    ("d. h. so", "das heißt so"),
    ("bzw. auch", "beziehungsweise auch"),
    ("usw.", "und so weiter"),
    ("Nr. 7", "Nummer 7"),
    ("im 19. Jh.", "im 19. Jahrhundert"),
    ("Prof. Dr. Müller", "Professor Doktor Müller"),
])
def test_abbreviations_are_words_not_letters(raw, expected):
    """espeak reads `z. B.` as "tsɛt beː"."""
    assert expand_abbreviations(raw) == expected


def test_years_are_read_as_years():
    """1889 is achtzehnhundert…, not eintausendachthundert…"""
    assert expand_years("1889") == "achtzehnhundertneunundachtzig"
    assert expand_years("Er starb 1976.") == \
        "Er starb neunzehnhundertsechsundsiebzig."
    assert expand_years("2026") == "zweitausendsechsundzwanzig"


def test_a_thousands_separator_is_not_a_year():
    """1.000 is a thousand; the year rule must not reach into it."""
    assert normalise("1.000 Menschen") == "eintausend Menschen"


def test_money_is_amount_then_currency():
    assert expand_money("27,50 €") == "siebenundzwanzig Euro fünfzig"
    assert expand_money("12 Euro") == "zwölf Euro"
    assert expand_money("5,00 €") == "fünf Euro"


def test_a_decimal_comma_is_spoken_as_komma():
    assert expand_numbers("3,5 Meter") == "drei Komma fünf Meter"


@pytest.mark.parametrize("raw, expected", [
    ("am 1. Mai", "am ersten Mai"),
    ("im 19. Jahrhundert", "im neunzehnten Jahrhundert"),
    ("Der 3. Oktober", "Der dritte Oktober"),
    ("vom 2. Januar", "vom zweiten Januar"),
])
def test_ordinals_are_expanded_where_the_ending_is_predictable(raw, expected):
    assert expand_ordinals(raw) == expected


def test_an_ordinal_in_an_unknown_case_is_left_alone():
    """German ordinals inflect and choosing needs a parse.  `vier` for `4.` is
    wrong in a small way; `viertes` for `vierten` teaches a wrong form."""
    assert expand_ordinals("Das ist mein 4. Problem.") == "Das ist mein 4. Problem."


def test_the_whole_pass_on_one_hard_sentence():
    got = normalise("Am 1. Mai 1889 kostete es 27,50 €, d. h. viel.")
    assert got == ("Am ersten Mai achtzehnhundertneunundachtzig kostete es "
                   "siebenundzwanzig Euro fünfzig, das heißt viel.")


def test_ordinary_prose_is_untouched():
    """The pass must be invisible where nothing needs fixing."""
    for line in ["Also sprach Zarathustra, ein Buch für Alle und Keinen.",
                 "Er maß die Straße mit großen Schritten.",
                 "Die Brücke über den Fluss war schön."]:
        assert normalise(line) == line


def test_1883_orthography_is_left_to_the_engine():
    """Archaic German is largely self-correcting under rule-based G2P — ⟨th⟩ is
    /t/, ⟨ie⟩ is /iː/ — so this pass must not meddle with it."""
    line = "Thut es, giebt es einen Theil des Muthes."
    assert normalise(line) == line
