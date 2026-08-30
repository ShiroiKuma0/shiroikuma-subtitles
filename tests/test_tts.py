"""Tests for the text-to-speech path: ruby capture, reading policy, cue timing.

Nothing here needs VOICEVOX installed.  The engine is faked, because what is
worth testing is the arithmetic and the policy — that a cue's start is the audio
before it, that furigana lands on the right characters, and that a resumed run
does not respeak what it already said.  Whether the voice is any good is not a
question a test can answer.
"""

from __future__ import annotations

import io
import json
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from jisho_subs import ruby as ruby_mod
from jisho_subs import tts as tts_mod
from jisho_subs.ruby import Ruby
from jisho_subs.segment import Sentence, segment
from jisho_subs.source import Block, _blocks_from_html


# -- offsets survive whitespace collapse ---------------------------------

def test_clean_with_offsets_matches_the_cleaner_it_mirrors():
    from jisho_subs.source import _clean

    for raw in ["  Am  Rand ", "　日本語　の　文章　", "a\n\nb", "", "   ",
                "私は　猫である"]:
        cleaned, pos_map = ruby_mod.clean_with_offsets(raw)
        assert cleaned == _clean(raw), raw
        assert len(pos_map) == len(raw)


def test_an_offset_taken_before_cleaning_still_points_at_its_word():
    raw = "　　むかし  むかし、舳先に  立って"
    cleaned, pos_map = ruby_mod.clean_with_offsets(raw)
    start = raw.index("舳先")
    assert cleaned[pos_map[start]:pos_map[start + 1] + 1] == "舳先"


def test_walk_text_agrees_with_get_text():
    """The offsets are into this string, so it must be that string."""
    html = ('<p><span class="initiale">A</span>m Rand '
            '<ruby>舳先<rt>へさき</rt></ruby> stand er.</p>')
    soup = BeautifulSoup(html, "lxml")
    el = soup.find("p")
    walked, _ = ruby_mod.walk_text(el)
    assert walked == el.get_text()


# -- what counts as a reading --------------------------------------------

@pytest.mark.parametrize("base, reading, expected", [
    ("舳先", "へさき", True),
    ("東京", "トウキョウ", True),
    ("東京", "Tokyo", False),        # a gloss, not a reading
    ("東京", "the capital", False),
    ("とても", "・・・", False),        # emphasis dots on kana
    ("ゆっくり", "つよく", False),      # ruby on kana is not a pronunciation
    ("舳先", "", False),
])
def test_only_kana_on_kanji_counts_as_a_reading(base, reading, expected):
    assert ruby_mod.is_reading(base, reading) is expected


def test_ruby_is_captured_with_the_text_it_annotates():
    html = ("<p>運転手は、まるで<ruby>舳先<rt>へさき</rt></ruby>に立って"
            "<ruby>不吉<rt>ふきつ</rt></ruby>な。</p>")
    blocks = _blocks_from_html(html, "x.xhtml")
    assert len(blocks) == 1
    block = blocks[0]
    assert "へさき" not in block.text, "the reading must stay out of the prose"
    assert len(block.ruby) == 2
    for r in block.ruby:
        assert block.text[r.start:r.end] == r.base
    assert [(r.base, r.reading) for r in block.ruby] == \
        [("舳先", "へさき"), ("不吉", "ふきつ")]


def test_a_latin_gloss_is_not_captured_as_a_reading():
    html = "<p>彼は<ruby>東京<rt>Tokyo</rt></ruby>へ行った。</p>"
    block = _blocks_from_html(html, "x.xhtml")[0]
    assert block.ruby == []
    assert block.text == "彼は東京へ行った。"


# -- rewriting -----------------------------------------------------------

def test_apply_substitutes_only_what_is_accepted():
    text = "可成候間、堅火中可仕由"
    rubies = [Ruby(0, 2, "可成", "なるべく"), Ruby(5, 6, "堅", "かたく")]
    assert ruby_mod.apply(text, rubies) == "なるべく候間、かたく火中可仕由"
    assert ruby_mod.apply(text, rubies, accept=lambda r: r.base == "堅") == \
        "可成候間、かたく火中可仕由"
    assert ruby_mod.apply(text, rubies, accept=lambda r: False) == text


def test_slice_ruby_keeps_only_what_lies_wholly_inside():
    rubies = [Ruby(0, 2, "可成", "なるべく"), Ruby(10, 12, "火中", "かちゅう")]
    assert [r.base for r in ruby_mod.slice_ruby(rubies, 8, 20)] == ["火中"]
    assert ruby_mod.slice_ruby(rubies, 8, 20)[0].start == 2


def test_segmentation_rebases_ruby_onto_each_sentence():
    block = Block("舳先に立った。不吉な潮目だ。", "d", False, 0,
                  [Ruby(0, 2, "舳先", "へさき"), Ruby(7, 9, "不吉", "ふきつ")])
    sentences = segment([block], "ja")
    assert len(sentences) == 2
    for s in sentences:
        for r in s.ruby:
            assert s.text[r.start:r.end] == r.base
    assert [r.base for r in sentences[0].ruby] == ["舳先"]
    assert [r.base for r in sentences[1].ruby] == ["不吉"]


# -- the reading policy --------------------------------------------------

class FakeEngine:
    """An engine that reads a fixed handful of words and nothing else."""

    def __init__(self, readings=None, rate=24000):
        self.readings = readings or {}
        self.rate = rate
        self.asked = []
        self.spoken = []

    def reading_of(self, word, speaker):
        self.asked.append(word)
        return self.readings.get(word, "")

    def speak(self, text, speaker, **kw):
        self.spoken.append(text)
        return _wav_of(len(text) * 0.1, self.rate)


def _wav_of(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(round(seconds * rate)))
    return buf.getvalue()


def test_differing_leaves_a_reading_the_engine_already_gets_right():
    """クリスマス・ピッグ has 11,330 annotations on words a dictionary knows.

    Substituting them all would replace the book with kana and cost OpenJTalk
    the word identity it takes accent from.
    """
    engine = FakeEngine({"舳先": "ヘサキ"})
    policy = tts_mod.ReadingPolicy(engine, 31, "differing")
    s = Sentence("舳先に立った。", "d", False, 0, [Ruby(0, 2, "舳先", "へさき")])
    assert policy.spoken(s) == "舳先に立った。"
    assert policy.agreed == 1 and policy.applied == 0


def test_differing_applies_a_reading_the_engine_gets_wrong():
    """葉隠 is classical: 之 is の, and a modern dictionary says これ."""
    engine = FakeEngine({"之": "コレ"})
    policy = tts_mod.ReadingPolicy(engine, 31, "differing")
    s = Sentence("夜陰之閑談", "d", False, 0, [Ruby(2, 3, "之", "の")])
    assert policy.spoken(s) == "夜陰の閑談"
    assert policy.applied == 1 and policy.agreed == 0


def test_each_distinct_word_is_probed_once_however_often_it_occurs():
    """葉隠 carries 41,635 annotations over far fewer distinct words."""
    engine = FakeEngine({"之": "コレ"})
    # The cache lives in the real Engine, so emulate it the way the real one does.
    seen = {}

    def reading_of(word, speaker):
        seen[word] = seen.get(word, 0) + 1
        return "コレ"

    engine.reading_of = reading_of
    policy = tts_mod.ReadingPolicy(engine, 31, "differing")
    for _ in range(5):
        policy.spoken(Sentence("之", "d", False, 0, [Ruby(0, 1, "之", "の")]))
    assert policy.applied == 5
    assert sum(seen.values()) == 5, "the real cache lives in Engine.reading_of"


def test_never_and_always_do_what_they_say():
    engine = FakeEngine({"之": "コレ"})
    s = Sentence("夜陰之閑談", "d", False, 0, [Ruby(2, 3, "之", "の")])
    assert tts_mod.ReadingPolicy(engine, 31, "never").spoken(s) == "夜陰之閑談"
    assert tts_mod.ReadingPolicy(engine, 31, "always").spoken(s) == "夜陰の閑談"


def test_a_failing_probe_keeps_the_book_s_own_reading():
    class Broken(FakeEngine):
        def reading_of(self, word, speaker):
            raise RuntimeError("engine went away")

    policy = tts_mod.ReadingPolicy(Broken(), 31, "differing")
    s = Sentence("夜陰之閑談", "d", False, 0, [Ruby(2, 3, "之", "の")])
    assert policy.spoken(s) == "夜陰の閑談"


# -- cue arithmetic ------------------------------------------------------

def _sentences(texts, doc="c1.xhtml"):
    return [Sentence(t, doc, False, i) for i, t in enumerate(texts)]


def _distinct(n, doc="c1.xhtml", first=0):
    """`n` five-character sentences, no two alike.

    Equal length so each is the same number of seconds, distinct because
    ``srt._prepare`` merges identical adjacent cues under 500 ms apart — a test
    built on one repeated sentence measures that merge instead of what it meant
    to measure.
    """
    return [Sentence("あいうえ" + chr(0x3042 + (first + i) % 80), doc, False,
                     first + i) for i in range(n)]


def test_a_cue_starts_where_the_audio_before_it_ends(tmp_path):
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.5, paragraph_gap=0.5, fmt="wav",
                              track_seconds=10_000, jobs=1)
    sentences = _sentences(["あいうえお", "かきくけこ", "さしすせそ"])
    tts_mod.speak_book(engine, sentences, str(tmp_path), "book", opts)

    srt = (tmp_path / "book 001.srt").read_text(encoding="utf-8")
    stamps = [l for l in srt.splitlines() if "-->" in l]
    assert len(stamps) == 3
    # 5 characters x 0.1 s = 0.5 s each, 0.5 s of silence between.
    assert stamps[0].startswith("00:00:00,000 --> 00:00:00,500")
    assert stamps[1].startswith("00:00:01,000 --> 00:00:01,500")
    assert stamps[2].startswith("00:00:02,000 --> 00:00:02,500")


def test_the_written_audio_is_as_long_as_the_cues_say(tmp_path):
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.25, paragraph_gap=0.25, fmt="wav",
                              track_seconds=10_000, jobs=1)
    tts_mod.speak_book(engine, _distinct(2), str(tmp_path), "book", opts)
    with wave.open(str(tmp_path / "book 001.wav")) as w:
        duration = w.getnframes() / w.getframerate()
    assert duration == pytest.approx(0.5 + 0.25 + 0.5, abs=1e-6)


def test_with_split_time_a_track_runs_on_to_the_document_boundary(tmp_path):
    """A track starting mid-chapter makes the app's chapter list useless."""
    engine = FakeEngine()
    # Chapter one runs to 1.5 s against a 1.2 s target — over, but under the
    # 1.8 s at which a single document is split internally.  So the only thing
    # that can end track one is the document boundary.
    opts = tts_mod.TtsOptions(gap=0.0, paragraph_gap=0.0, fmt="wav",
                              split="time", track_seconds=1.2, jobs=1)
    sentences = (_distinct(3, doc="one.xhtml")
                 + _distinct(2, doc="two.xhtml", first=40))
    stats = tts_mod.speak_book(engine, sentences, str(tmp_path), "book", opts)
    assert stats.tracks == 2
    first = (tmp_path / "book 001.srt").read_text(encoding="utf-8")
    assert first.count("-->") == 3, "it broke mid-chapter"
    assert "00:00:01,500" in first, "the target did not give way to the chapter"


def test_with_split_time_a_single_long_document_is_still_split(tmp_path):
    """Otherwise 葉隠's longest section becomes one four-hour file."""
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.0, paragraph_gap=0.0, fmt="wav",
                              split="time", track_seconds=1.0, jobs=1)
    stats = tts_mod.speak_book(engine, _distinct(12), str(tmp_path), "book", opts)
    assert stats.tracks > 1


# -- resuming ------------------------------------------------------------

def test_a_resumed_run_speaks_nothing_it_has_already_spoken(tmp_path):
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.0, fmt="wav", split="time",
                              track_seconds=1.0, jobs=1)
    sentences = _distinct(8)

    first = tts_mod.speak_book(engine, sentences, str(tmp_path), "book", opts)
    spoken_first = len(engine.spoken)
    assert first.sentences == 8

    again = tts_mod.speak_book(engine, sentences, str(tmp_path), "book", opts)
    assert again.sentences == 0
    assert again.skipped_tracks == first.tracks
    assert len(engine.spoken) == spoken_first, "it respoke something"


def test_the_manifest_covers_every_sentence_exactly_once(tmp_path):
    """The counter bug this catches respoke the last sentence of every track."""
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.0, fmt="wav", split="time",
                              track_seconds=1.0, jobs=1)
    tts_mod.speak_book(engine, _distinct(9), str(tmp_path), "book", opts)
    data = json.loads((tmp_path / tts_mod.MANIFEST).read_text(encoding="utf-8"))
    cursor = 0
    for rec in data["tracks"]:
        assert rec["first"] == cursor
        cursor += rec["count"]
    assert cursor == 9


def test_changing_the_voice_starts_the_book_again(tmp_path):
    engine = FakeEngine()
    base = dict(gap=0.0, fmt="wav", split="time", track_seconds=1.0, jobs=1)
    tts_mod.speak_book(engine, _distinct(4), str(tmp_path),
                       "book", tts_mod.TtsOptions(speaker=31, **base))
    again = tts_mod.speak_book(engine, _distinct(4), str(tmp_path),
                               "book", tts_mod.TtsOptions(speaker=13, **base))
    assert again.sentences == 4, "a different voice cannot reuse the old tracks"


def test_a_wav_the_manifest_does_not_know_about_is_set_aside(tmp_path):
    """A killed run never unwinds, so the half-written track is found on the
    way in rather than left to appear in the app as a truncated chapter."""
    stray = tmp_path / "book 007.wav"
    stray.write_bytes(_wav_of(0.1))
    moved = tts_mod.sweep_partials(str(tmp_path), [])
    assert moved == ["book 007.wav"]
    assert not stray.exists()
    assert (tmp_path / "book 007.wav.partial").exists()


def test_the_sweep_leaves_real_tracks_alone(tmp_path):
    keep = tmp_path / "book 001.wav"
    keep.write_bytes(_wav_of(0.1))
    assert tts_mod.sweep_partials(str(tmp_path), [{"name": "book 001.wav"}]) == []
    assert keep.exists()


# -- tagging -------------------------------------------------------------

def test_the_language_tag_is_a_three_letter_code_or_und():
    assert tts_mod._iso3("ja") == "jpn"
    assert tts_mod._iso3("de") == "deu"
    # `und` cannot be removed, only replaced — so an unknown language says so.
    assert tts_mod._iso3("xx") == "und"
    assert tts_mod._iso3("") == "und"


def test_one_bad_sentence_does_not_lose_the_book(tmp_path):
    class Flaky(FakeEngine):
        def speak(self, text, speaker, **kw):
            if text == "かきくけこ":
                raise RuntimeError("engine hiccup")
            return super().speak(text, speaker, **kw)

    engine = Flaky()
    opts = tts_mod.TtsOptions(gap=0.0, fmt="wav", track_seconds=10_000, jobs=1)
    stats = tts_mod.speak_book(engine, _sentences(["あいうえお", "かきくけこ", "さしすせそ"]),
                               str(tmp_path), "book", opts)
    assert stats.sentences == 2
    assert len(stats.failures) == 1
    assert (tmp_path / "book 001.srt").exists()


# -- chapter planning ----------------------------------------------------

def _mixed(specs):
    """specs: (text, doc, is_heading) triples."""
    return [Sentence(t, d, h, i) for i, (t, d, h) in enumerate(specs)]


def test_headings_mark_the_chapters_when_the_book_has_them():
    """自省録 is 688 spine documents with a median length of 30 characters and
    30 headings; the headings are the chapters."""
    body = "あ" * 500
    sents = _mixed([("第一巻", "a.xhtml", True), (body, "b.xhtml", False),
                    (body, "c.xhtml", False), ("第二巻", "d.xhtml", True),
                    (body, "e.xhtml", False)])
    chs = tts_mod.plan_chapters(sents, tts_mod.TtsOptions())
    assert [c.first for c in chs] == [0, 3]
    assert [c.title for c in chs] == ["第一巻", "第二巻"]


def test_documents_mark_the_chapters_when_there_are_no_headings():
    """1Q84 BOOK1 and クリスマス・ピッグ carry no headings at all."""
    body = "あ" * 500
    sents = _mixed([(body, "a.xhtml", False), (body, "b.xhtml", False),
                    (body, "c.xhtml", False)])
    chs = tts_mod.plan_chapters(sents, tts_mod.TtsOptions())
    assert [c.first for c in chs] == [0, 1, 2]
    assert all(c.title is None for c in chs)


def test_front_matter_joins_chapter_one_rather_than_shipping_alone():
    """人魚のひいさま opens with a seven-character spine document."""
    sents = _mixed([("表題", "cover.xhtml", False),
                    ("DEN LILLE HAVFRUE", "body.xhtml", True),
                    ("あ" * 900, "body.xhtml", False)])
    chs = tts_mod.plan_chapters(sents, tts_mod.TtsOptions())
    assert len(chs) == 1
    assert chs[0].first == 0
    assert chs[0].title == "DEN LILLE HAVFRUE", "it should take the real title"


def test_a_chapter_too_short_to_stand_alone_joins_the_one_before():
    body = "あ" * 900
    sents = _mixed([("章一", "a.xhtml", True), (body, "a.xhtml", False),
                    ("挿話", "b.xhtml", True), ("短い", "b.xhtml", False),
                    ("章二", "c.xhtml", True), (body, "c.xhtml", False)])
    chs = tts_mod.plan_chapters(sents, tts_mod.TtsOptions(min_chapter_chars=400))
    assert [c.title for c in chs] == ["章一", "章二"]


def test_split_document_ignores_headings():
    body = "あ" * 500
    sents = _mixed([("章一", "a.xhtml", True), (body, "a.xhtml", False),
                    ("節", "a.xhtml", True), (body, "a.xhtml", False),
                    (body, "b.xhtml", False)])
    chs = tts_mod.plan_chapters(sents, tts_mod.TtsOptions(split="document"))
    assert [c.first for c in chs] == [0, 4]


def test_split_time_cannot_be_planned_ahead():
    """It depends on durations that do not exist until synthesis."""
    sents = _mixed([("あ" * 500, "a.xhtml", False)])
    assert tts_mod.plan_chapters(sents, tts_mod.TtsOptions(split="time")) is None


def test_one_track_is_written_per_planned_chapter(tmp_path):
    engine = FakeEngine()
    opts = tts_mod.TtsOptions(gap=0.0, paragraph_gap=0.0, fmt="wav",
                              min_chapter_chars=1, jobs=1)
    sents = _mixed([("いち", "a.xhtml", True), ("あいうえお", "a.xhtml", False),
                    ("にい", "b.xhtml", True), ("かきくけこ", "b.xhtml", False)])
    stats = tts_mod.speak_book(engine, sents, str(tmp_path), "book", opts)
    assert stats.tracks == 2
    assert (tmp_path / "book 001.wav").exists()
    assert (tmp_path / "book 002.wav").exists()


def test_the_default_speed_is_the_one_chosen_by_ear():
    assert tts_mod.TtsOptions().speed == 1.15
