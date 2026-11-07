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


# 2.6.1 — what 2.6.0 cut apart that one speaker said

def test_a_sentence_that_plainly_runs_on_is_one_speaker(film_py):
    def ws(t, s): return [word(t, s, s + 0.5)]
    for a, b in [("この", "標識みたいな"), ("骨を固定したら", "病院へ連れて行くよ"), ("ブレイスウェイトと", "いいます"),
                 ("失礼", "します"), ("もう", "一度"), ("確かに", "困難だけど"), ("相手を間違えたな、", "ふざけやがって")]:
        assert film_py.runs_on(ws(a, 0.0), ws(b, 0.5)), a
    for a, b in [("何をしたの", "2ヶ月後に"), ("いいえ長官", "ダメだ"), ("気が散るんだけど", "僕は"), ("どうします", "女を殺せ"),
                 ("飛び込め", "ああ、もう"), ("一緒に来て", "これから")]:
        assert not film_py.runs_on(ws(a, 0.0), ws(b, 0.5)), a
    # は runs on into the next word, but after a pause it is a question: 人数は? / ワンサといるよ
    assert film_py.runs_on(ws("ガードは", 0.0), ws("万全だ", 0.5))
    assert not film_py.runs_on(ws("人数は", 0.0), ws("ワンサといるよ", 1.2))


def test_a_cue_that_runs_on_gets_no_dashes_whatever_the_check_track_says(film_py):
    cfg = film_py.lang_cfg("ja")
    g = [word("この", 165.4, 165.7, seg=0, phr=0, spk=1), word("標識", 165.7, 166.1, seg=0, phr=1, spk=2),
         word("みたい", 166.1, 166.5, seg=0, phr=1, spk=2), word("な", 166.5, 166.6, seg=0, phr=1, spk=2)]
    dlg = [(165.0, 167.0, True)]
    assert not film_py.two_speakers(g, ["この", "標識みたいな"], cfg, dlg, 165.4, 166.6)


def test_a_whisper_segment_edge_may_start_a_new_voice_and_the_grammar_keeps_a_sentence_whole(film_py):
    # Fallout, 34:26: 祈るなんて作戦があるか | 新入り — two speakers with no pause, only a segment edge between them
    edge = [word("あるか", 10.0, 10.4, seg=0, phr=0), word("新入り", 10.4, 10.8, seg=1, phr=0)]
    assert film_py.legal_boundary(edge, 1, [])
    inside = [word("あるか", 10.0, 10.4, seg=0, phr=0), word("新入り", 10.4, 10.8, seg=0, phr=0)]
    assert not film_py.legal_boundary(inside, 1, [])
    # Batman Begins, 10:29: the same edge inside one speaker's sentence is kept whole by the grammar
    assert film_py.runs_on([word("骨を固定したら", 10.0, 10.4)], [word("病院へ連れて行くよ", 10.4, 10.8)])


def test_endings_that_close_a_sentence_stay_open_for_a_new_speaker(film_py):
    def ws(t, s): return [word(t, s, s + 0.5)]
    # each of these lost a real turn in the first cut of 2.6.1
    for a, b in [("ウィドウと会わないと", "会うのはあいつだろ"), ("別にデジャブって言っただけ", "何を見たの"),
                 ("行かなきゃ", "モーフィアスは自分の命を")]:
        assert not film_py.runs_on(ws(a, 0.0), ws(b, 0.5)), a
    # the concessive after a volitional form, and あの read as a filler before a noun, still run on
    for a, b in [("外の世界がどうなろうと", "生き延びてどうする気だ"), ("何が違うあの", "人は絵を持ってた")]:
        assert film_py.runs_on(ws(a, 0.0), ws(b, 0.5)), a


def test_a_line_the_vad_missed_does_not_join_the_next_phrase(film_py, tmp_path):
    # Mission: Impossible II, 1:10:07 — もうじきエアフィルターが起動する時間だ, too quiet for the VAD;
    # its last word touches the next phrase once shifted by Whisper's lean
    quiet = [word(t, 4207.66 + 0.2*k, 4207.86 + 0.2*k) for k, t in enumerate("もう じき エア フィルター が 起動 する 時間".split())]
    quiet.append(word("だ", 4209.82, 4210.06))
    nxt = [word("これ", 4210.06, 4210.41), word("から", 4210.41, 4210.62)]
    segs = [dict(words=quiet), dict(words=nxt)]
    lean = [[100.0 + 3*k, 101.5 + 3*k] for k in range(30)]          # phrases enough to measure a lean on
    words = [dict(words=[word("あ", s - 0.3, s + 0.2), word("い", e - 0.6, e - 0.3)]) for s, e in lean]
    cache = tmp_path / "vad_fine.json"
    cache.write_text(json.dumps(lean + [[4210.37, 4212.73]]))
    film_py.make_phrases(None, None, words + segs, str(cache))
    assert quiet[0]["phr"] is None                      # far from any phrase: no phrase, so the cue keeps its own start
    assert nxt[0]["phr"] == len(lean)


def test_the_original_ends_a_sentence_under_any_tag(film_py):
    check = [(0, 1, '<font face="sans-serif" size="71">This is very pleasant.</font>'), (1, 2, "{\\an8}Mr. Lee..."),
             (2, 3, "<i>Remote control.</i>")]
    assert film_py.sentence_ends(check) == [1, 3]
