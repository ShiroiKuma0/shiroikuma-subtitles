"""`subtitles mkv`: where one speaker stops and the next begins.

Terminator 2, 1:08:16 — Sarah asks 何をしたの ("Why him?") and the T-800
answers 2ヶ月後に画期的なマイクロプロセッサーを開発する a second later.  The
voices were told apart correctly, and the two still came out as one
sentence; each test below is one of the three faults behind that."""

import importlib.util
import json
import subprocess

import pytest

from tests.test_pipeline import SCRIPT


@pytest.fixture(scope="module")
def film_py(tmp_path_factory):
    path = tmp_path_factory.mktemp("py") / "film.py"
    out = subprocess.run(["bash", "-c", "source <(sed '/^case \"${1:-}\" in$/,$d' %s); mkv_whisper_py" % SCRIPT()],
                         capture_output=True, text=True, check=True).stdout
    path.write_text(out, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("film", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def word(w, s, e, **k):
    return dict(w=w, start=s, end=e, **k)


def test_a_word_stamped_across_the_pause_joins_the_phrase_it_is_spoken_in(film_py, tmp_path):
    # Whisper's times as they came: the "2" runs from the end of の over a second of silence
    segs = [dict(words=[word("何", 4096.28, 4096.56), word("を", 4096.56, 4096.78), word("した", 4096.78, 4096.92),
                        word("の", 4096.92, 4097.08), word(" 2", 4097.08, 4098.36), word("ヶ", 4098.36, 4098.72),
                        word("月", 4098.72, 4098.90)])]
    cache = tmp_path / "vad_fine.json"
    cache.write_text(json.dumps([[4096.45, 4097.374], [4098.37, 4101.566]]))
    film_py.make_phrases(None, None, segs, str(cache))
    phr = [w["phr"] for w in segs[0]["words"]]
    assert phr == [0, 0, 0, 0, 1, 1, 1]


def test_a_pause_between_two_kanji_is_no_word(film_py):
    # 誰 | 直接 a second apart: a change of voice there is legal; joined, it is inside a compound
    apart = [word("誰", 4083.08, 4083.28, seg=0, phr=0), word("直接", 4084.24, 4084.56, seg=1, phr=1)]
    assert film_py.legal_boundary(apart, 1, [])
    joined = [word("誰", 4083.08, 4083.28, seg=0, phr=0), word("直接", 4083.28, 4083.56, seg=0, phr=0)]
    assert not film_py.legal_boundary(joined, 1, [])
    # two VAD phrases do not make a compound two words
    split = [word("一", 10.0, 10.2, seg=0, phr=0), word("月", 10.2, 10.4, seg=0, phr=1)]
    assert not film_py.legal_boundary(split, 1, [])


def test_the_original_ends_a_sentence_at_a_stop_not_at_an_ellipsis(film_py):
    check = [(0, 1, "Why him?"), (1, 2, "<i>first name Sarah...</i>"), (2, 3, "<i>Remote control.</i>"),
             (3, 4, "I'm going to find her, whoever she is,"), (4, 5, '"Stop!"')]
    assert film_py.sentence_ends(check) == [1, 3, 5]


def test_a_verified_turn_between_two_cues_ends_the_sentence(film_py):
    cfg = film_py.lang_cfg("ja")
    a = [word("何", 4096.28, 4096.56), word("を", 4096.56, 4096.78), word("した", 4096.78, 4096.92), word("の", 4096.92, 4097.08)]
    b = [word(" 2", 4098.12, 4098.36), word("ヶ", 4098.36, 4098.72), word("月", 4098.72, 4098.90), word("後", 4098.90, 4099.02),
         word("に", 4099.02, 4099.14), word("開発", 4099.14, 4099.50), word("する", 4099.50, 4099.70)]
    segs = [dict(start=4096.28, words=a + b)]
    out = [(4096.35, 4097.53, "何をしたの", 0.9, False), (4098.27, 4099.90, "2ヶ月後に開発する", 0.9, False)]
    assert [s["text"] for s in film_py.whole_sentences(out, segs, cfg)] == ["何をしたの2ヶ月後に開発する"]
    a[-1]["turn_after"] = True
    assert [s["text"] for s in film_py.whole_sentences(out, segs, cfg)] == ["何をしたの", "2ヶ月後に開発する"]
