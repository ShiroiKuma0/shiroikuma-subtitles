"""`subtitles fix-mkv`: a corrected copy of the film's own track goes back into it.

Each test builds a small Matroska film — a few seconds of test picture and
tone, an English SRT, a publisher's Japanese SRT and the tool's track (named
日本語, tagged SHIROIKUMA_SUBTITLES) — and runs the tangled script on it."""

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from tests.test_pipeline import SCRIPT

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("ffmpeg", "mkvmerge", "mkvextract", "mkvpropedit")),
    reason="needs ffmpeg and mkvtoolnix")

TOOL = ["今日は晴れ", "カイルはどこだ", "急げ", "行こう"]
PUBLISHER = ["今日は晴れだ", "カイルはどこ？", "急げ！", "行こう"]
ENGLISH = ["It's sunny today", "Where's Kyle?", "Hurry!", "Let's go"]


def srt(lines, step=2.0):
    out = []
    for i, t in enumerate(lines):
        a, b = 1 + i * step, 1 + i * step + 1.5
        f = lambda s: "%02d:%02d:%02d,%03d" % (s // 3600, s % 3600 // 60, s % 60, round(s * 1000) % 1000)
        out.append("%d\n%s --> %s\n%s\n" % (i + 1, f(a), f(b), t))
    return "\n".join(out)


def tracks(film):
    d = json.loads(subprocess.run(["mkvmerge", "-J", str(film)], capture_output=True, text=True, check=True).stdout)
    return [t for t in d["tracks"] if t["type"] == "subtitles"]


def extract(film, tid, out):
    subprocess.run(["mkvextract", "tracks", str(film), "%d:%s" % (tid, out)], capture_output=True, check=True)
    return open(out, encoding="utf-8-sig").read()


@pytest.fixture
def film(tmp_path):
    base = tmp_path / "av.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "10", "-c:v", "libx264", "-c:a", "aac", str(base)], check=True)
    for name, lines in (("en", ENGLISH), ("pub", PUBLISHER), ("tool", TOOL)):
        (tmp_path / (name + ".srt")).write_text(srt(lines), encoding="utf-8")
    tag = tmp_path / "tag.xml"
    tag.write_text('<?xml version="1.0"?>\n<Tags><Tag><Targets/><Simple><Name>SHIROIKUMA_SUBTITLES</Name>'
                   '<String>2.4.0</String></Simple></Tag></Tags>\n', encoding="utf-8")
    f = tmp_path / "Film -- [7][227] (2026).mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(f), str(base),
                    "--language", "0:eng", str(tmp_path / "en.srt"),
                    "--language", "0:jpn", "--track-name", "0:日本語", "--tags", "0:" + str(tag), str(tmp_path / "tool.srt"),
                    "--language", "0:jpn", str(tmp_path / "pub.srt")], check=True)
    return f


def working(tmp_path, film, lines):
    tid = next(t["id"] for t in tracks(film) if t["properties"].get("tag_shiroikuma_subtitles"))
    raw = tmp_path / "raw.srt"
    extract(film, tid, raw)
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    w = film.with_name(film.stem + ".ja-whisper-working.srt")
    w.write_text("\ufeff" + srt(lines), encoding="utf-8")      # as extracted by mkvextract, and kept by Yosuga's editor
    return w, sha


def fix(*args, env=None):
    return subprocess.run(["bash", SCRIPT(), "fix-mkv", *map(str, args)], capture_output=True, text=True, env=env)


def test_two_corrections_go_into_the_tool_track_and_nowhere_else(tmp_path, film, monkeypatch):
    env = dict(os.environ, XDG_DATA_HOME=str(tmp_path / "share"))
    before = tracks(film)
    w, sha = working(tmp_path, film, ["今日は晴れ", "カイルはどこだ", "急げ!急げ!", "行こうぜ"])
    review = film.with_name(film.stem + ".possible-ja-CORRECTIONS-review.srt")
    review.write_text(srt(["[?] 急げ"]), encoding="utf-8")
    r = fix(film, w, "--review", review, "--expect-sha256", sha.upper(), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout.splitlines()
    assert "CHANGED 2" in out and out[-1] == "VERIFIED"
    assert any(l.startswith("PROGRESS ") for l in out)
    after = tracks(film)
    assert [(t["codec"], t["properties"].get("language"), t["properties"].get("track_name")) for t in after] == \
           [(t["codec"], t["properties"].get("language"), t["properties"].get("track_name")) for t in before]
    own = [t for t in after if t["properties"].get("tag_shiroikuma_subtitles")]
    assert len(own) == 1 and own[0]["properties"]["track_name"] == "日本語"
    assert "急げ!急げ!" in extract(film, own[0]["id"], tmp_path / "new.srt")
    publisher = [t for t in after if t["properties"].get("language") == "jpn" and t is not own[0]][0]
    assert extract(film, publisher["id"], tmp_path / "pub-after.srt").count("急げ！") == 1
    assert not w.exists() and not review.exists()
    fixes = (tmp_path / "share" / "subtitles" / "fixes" / (film.stem + ".tsv")).read_text(encoding="utf-8").splitlines()
    assert fixes == ["LINE\t急げ\t急げ!急げ!\t0:00:05.000", "LINE\t行こう\t行こうぜ\t0:00:07.000"]


def test_a_track_changed_since_extraction_is_refused_and_the_film_untouched(tmp_path, film):
    w, sha = working(tmp_path, film, ["今日は晴れ", "カイルはどこだ", "急げ", "行こうぜ"])
    data = film.read_bytes()
    r = fix(film, w, "--expect-sha256", "0" * 64)
    assert r.returncode == 7 and film.read_bytes() == data and w.exists()


def test_a_film_without_the_tagged_track_is_refused(tmp_path, film):
    tid = next(k for k, t in enumerate(tracks(film)) if t["properties"].get("tag_shiroikuma_subtitles"))
    w, _ = working(tmp_path, film, TOOL)
    subprocess.run(["mkvpropedit", "-q", str(film), "--tags", "track:s%d:" % (tid + 1)], check=True)
    data = film.read_bytes()
    r = fix(film, w)
    assert r.returncode == 3 and film.read_bytes() == data


def test_a_working_copy_failing_lint_is_refused(tmp_path, film):
    w, _ = working(tmp_path, film, ["今日は晴れ", "今日は晴れ", "急げ", "行こう"])
    w.write_text(srt(["今日は晴れ", "今日は晴れ"], step=0.0), encoding="utf-8")   # two cues at one time: the app would merge them
    data = film.read_bytes()
    r = fix(film, w)
    assert r.returncode == 4 and film.read_bytes() == data


def test_nothing_changed_means_no_remux(tmp_path, film):
    w, sha = working(tmp_path, film, TOOL)
    data = film.read_bytes()
    r = fix(film, w, "--expect-sha256", sha)
    assert r.returncode == 0 and r.stdout.splitlines()[-2:] == ["CHANGED 0", "VERIFIED"]
    assert film.read_bytes() == data


# --- the corrections file's whole-line entries, applied to finished cues ---

@pytest.fixture(scope="module")
def film_py(tmp_path_factory):
    import importlib.util
    path = tmp_path_factory.mktemp("py") / "film.py"
    out = subprocess.run(["bash", "-c", "source <(sed '/^case \"${1:-}\" in$/,$d' %s); mkv_whisper_py" % SCRIPT()],
                         capture_output=True, text=True, check=True).stdout
    path.write_text(out, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("film", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_a_line_correction_changes_only_the_cue_at_its_time(film_py):
    cues = [(707.6, 708.6, "ナイスシュート", 0, False), (720.0, 721.0, "ナイスシュート", 0, False)]
    out, done = film_py.apply_line_fixes(cues, [("ナイスシュート", "ナイス", 707.6)])
    assert [c[2] for c in out] == ["ナイス", "ナイスシュート"] and done[0][1]


def test_a_line_correction_ignores_line_breaks_and_keeps_its_own(film_py):
    cues = [(10.0, 12.0, "じゃあ少佐\n気をつけて", 0, False)]
    out, _ = film_py.apply_line_fixes(cues, [("じゃあ少佐気をつけて", "じゃあ少佐、\n気をつけて", 10.4)])
    assert out[0][2] == "じゃあ少佐、\n気をつけて"


def test_an_emptied_line_drops_the_cue_and_a_missing_one_is_reported(film_py):
    cues = [(1.0, 2.0, "えっ", 0, False), (5.0, 6.0, "行こう", 0, False)]
    out, done = film_py.apply_line_fixes(cues, [("えっ", "", 1.0), ("帰る", "カイル", 5.0)])
    assert [c[2] for c in out] == ["行こう"] and [ok for _, ok in done] == [True, False]


def test_line_entries_and_fragments_are_read_apart(film_py, tmp_path):
    f = tmp_path / "fixes.tsv"
    f.write_text("帰る\tカイル\t1:30:55\nLINE\t急げ\t急げ!\\n急げ!\t0:00:05.000\n", encoding="utf-8")
    assert [x[:2] for x in film_py.read_fixes(str(f))] == [("帰る", "カイル")]
    assert film_py.read_line_fixes(str(f)) == [("急げ", "急げ!\n急げ!", 5.0)]
