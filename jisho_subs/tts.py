"""Reading a book aloud: EPUB in, audiobook and sentence-exact SRT out.

This is the other half of the tool.  The alignment path takes a recording and
works out where the sentences fall in it; this path has no recording, so it
*makes* one — and because it makes it one sentence at a time, it knows where
every sentence falls without looking.  There is no transcription here, no
voice-activity detection and no interpolation: a cue's start is how much audio
came before it and its end is that plus the length of the clip.  Exact by
construction.

Three things in here are load-bearing.

**Ruby is applied only where the engine disagrees.**  The obvious design — read
the furigana, substitute it everywhere — is wrong, and クリスマス・ピッグ is why:
it is a children's book carrying 11,330 usable annotations over 150,720
characters, ruby on ordinary words a reader might not know yet.  Substituting
all of them replaces most of the book with kana, and kana costs OpenJTalk the
word identity it derives accent from, so the prose comes out flat and
mis-accented in exchange for corrections it did not need.  Asking the engine
what it would say first, and correcting only the differences, keeps the
dictionary's accent everywhere it was already right.  On 葉隠 that still leaves
most annotations in force — classical 漢文訓読 (之 = の, 而 = て, 被申 = もうされ)
is exactly what a modern dictionary gets wrong — and on 1Q84 it leaves almost
none, which is the correct answer for a book with 98 of them.

**The reading probe is a per-word query, and per-word is a compromise.**  The
engine is asked for its reading of the annotated word *in isolation*, because
asking in context would mean a query per occurrence rather than per distinct
word — 41,635 against 3,000 on 葉隠.  A word whose isolated reading differs but
whose in-context reading would have been right is therefore corrected
unnecessarily.  That is the safe direction to err: the correction is the book's
own furigana.

**Tracks break on document boundaries.**  A track that starts mid-chapter makes
the app's chapter list useless, so the target length is a target, not a rule:
the writer keeps going until the spine document changes.  Only a single
document longer than half again the target is split internally, which is what
stops 葉隠's longest section from becoming one four-hour file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from . import ruby as ruby_mod
from .align import Cue
from .audio import AudioFile, probe
from .ruby import Ruby
from .segment import Sentence
from .voicevox import Engine, Wav, WavWriter, read_wav, silence

#: Written beside the audio so a resumed run knows which sentences are already
#: spoken.  The cues themselves are not in here — they are in the SRTs.
MANIFEST = ".jisho-tts.json"

_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: Characters of book text per minute of *synthesised* audio at speed 1.0, gaps
#: included — this predicts what we are about to make, so it is the engine's
#: rate that matters and not a narrator's.  Measured on finished runs: VOICEVOX
#: Japanese 人魚のひいさま, 10,599 characters in 0.61 h = 290/min; Piper German
#: Briefe an einen jungen Dichter, 67,670 in 0.96 h = 1175/min.
#:
#: For context the human narrators of the same library read Japanese at 270–341
#: and German at 680–853, so VOICEVOX paces about like a narrator while Thorsten
#: at 1.0 is already brisk — which is part of why German does not inherit the
#: 1.15 chosen for Japanese.  One figure for both languages is wrong by a factor
#: of four; the old hardcoded 400 was wrong in both directions at once.
CHARS_PER_MINUTE = {"ja": 290.0, "de": 1175.0}

#: Speaking rate 白い熊 chose by ear, per language.  1.15 was picked against
#: VOICEVOX on Japanese; the German samples that were approved were Piper's own
#: default, so German stays at 1.0 rather than inheriting a number chosen for a
#: different engine and a different language.
DEFAULT_SPEED = {"ja": 1.15, "de": 1.0}


def default_speed(lang: str) -> float:
    return DEFAULT_SPEED.get((lang or "").lower()[:2], 1.0)
DEFAULT_CHARS_PER_MINUTE = 900.0


def estimate_hours(chars: int, lang: str, speed: float = 1.0) -> float:
    """Roughly how long `chars` of this language will take to read aloud."""
    rate = CHARS_PER_MINUTE.get((lang or "").lower()[:2], DEFAULT_CHARS_PER_MINUTE)
    return chars / (rate * max(speed, 0.01)) / 60.0


@dataclass
class TtsOptions:
    speaker: int = 31                 #: No.7 / 読み聞かせ — the read-aloud style
    speed: float = 1.15               #: chosen by ear against 1.0/1.15/1.3/1.45
    pitch: float = 0.0
    intonation: float = 1.0
    gap: float = 0.25                 #: silence between sentences
    paragraph_gap: float = 0.55       #: extra silence at a paragraph or heading
    split: str = "chapter"            #: chapter | document | time
    track_seconds: float = 1800.0     #: target track length, for split=time
    min_chapter_chars: int = 400      #: below this a chapter joins the one before
    ruby: str = "differing"           #: differing | always | never
    fmt: str = "m4b"                  #: m4b | wav
    bitrate: str = "64k"
    jobs: int = 0                     #: 0 = one worker per core
    #: How long one sentence may go unanswered before the engine is presumed
    #: wedged.  Generous — the slowest real sentence measured is a couple of
    #: seconds, and a needless restart costs about a minute.
    stall_seconds: float = 300.0
    language: str = "ja"

    @property
    def workers(self) -> int:
        return self.jobs if self.jobs > 0 else (os.cpu_count() or 4)


@dataclass
class TtsStats:
    sentences: int = 0
    tracks: int = 0
    audio_seconds: float = 0.0
    ruby_seen: int = 0
    ruby_applied: int = 0
    ruby_agreed: int = 0
    skipped_tracks: int = 0
    failures: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def hours(self) -> float:
        return self.audio_seconds / 3600.0


# -- reading correction --------------------------------------------------

class ReadingPolicy:
    """Decides, per annotation, whether the book's furigana overrides the engine.

    Holds the per-word cache that makes ``differing`` affordable: one query per
    distinct annotated word for the whole book, not one per occurrence.
    """

    def __init__(self, engine: Engine, speaker: int, mode: str = "differing",
                 normalise=None):
        self.engine = engine
        self.speaker = speaker
        self.mode = mode
        #: Applied to the spoken copy after ruby, if the language has a
        #: normaliser.  German needs one and Japanese does not: what German
        #: engines get wrong is dates, abbreviations and ordinals rather than
        #: readings.  See ``german.py``.
        self.normalise = normalise
        self.seen = 0
        self.applied = 0
        self.agreed = 0

    def accept(self, r: Ruby) -> bool:
        self.seen += 1
        if self.mode == "never":
            return False
        if self.mode == "always":
            self.applied += 1
            return True
        try:
            theirs = self.engine.reading_of(r.base, self.speaker)
        except Exception:
            # A probe that fails should not lose the book's own reading.
            self.applied += 1
            return True
        if theirs and theirs == ruby_mod.to_katakana(r.reading):
            self.agreed += 1
            return False
        self.applied += 1
        return True

    def spoken(self, sentence: Sentence) -> str:
        """The text to hand the engine: the sentence, corrected and normalised.

        The subtitle keeps ``sentence.text`` untouched — what 白い熊 reads is
        the book, never our expansion of it.
        """
        text = sentence.text
        if sentence.ruby and self.mode != "never":
            text = ruby_mod.apply(text, sentence.ruby, accept=self.accept)
        if self.normalise is not None:
            text = self.normalise(text)
        return text



# -- chapter planning ----------------------------------------------------

@dataclass
class Chapter:
    """One output track: where it starts, and what to call it."""

    first: int                        #: index of its first sentence
    title: Optional[str] = None       #: the heading it began at, if any


def plan_chapters(sentences: List[Sentence], opts: TtsOptions) -> Optional[List[Chapter]]:
    """Work out where the tracks begin, before a word is synthesised.

    Returns ``None`` for ``split=time``, which cannot be planned because it
    depends on durations that do not exist yet.

    **A chapter is not a spine document.**  Measured across the corpus, neither
    signal works alone: 1Q84 BOOK1 and クリスマス・ピッグ carry *no headings at
    all*, so headings would give one track for the whole book; 自省録 is 688
    spine documents with a median length of thirty characters, so documents
    would give 688 tracks averaging a few seconds.  Headings where the book has
    them and documents where it does not fits every book in the corpus.

    Two rules keep the result sane.  The first chapter always starts at sentence
    zero, so front matter ahead of the first heading joins chapter one instead
    of becoming a track of its own — 人魚のひいさま opens with a seven-character
    document that would otherwise be a track.  And a chapter shorter than
    ``min_chapter_chars`` joins the one before it, which is what stops a stray
    half-title or section divider from becoming a four-second M4B.
    """
    if not sentences or opts.split == "time":
        return None

    use_headings = (opts.split == "chapter"
                    and any(s.is_heading for s in sentences))

    starts: List[Chapter] = [Chapter(0, None)]
    for i, s in enumerate(sentences):
        if i == 0:
            continue
        if use_headings:
            if s.is_heading and not sentences[i - 1].is_heading:
                starts.append(Chapter(i, s.text))
        elif s.doc != sentences[i - 1].doc:
            starts.append(Chapter(i, s.text if s.is_heading else None))

    # Name chapter one after its own heading when it has one.
    if starts[0].title is None and sentences[0].is_heading:
        starts[0].title = sentences[0].text

    # Fold away anything too short to be a chapter.
    lengths = [len(s.text) for s in sentences]

    def chars(a: int, b: int) -> int:
        return sum(lengths[a:b])

    kept: List[Chapter] = []
    for n, ch in enumerate(starts):
        end = starts[n + 1].first if n + 1 < len(starts) else len(sentences)
        if kept and chars(ch.first, end) < opts.min_chapter_chars:
            continue                      # too small to stand alone
        kept.append(ch)

    # Chapter one has nothing behind it to fold into, so it swallows the next
    # instead — 人魚のひいさま opens with a seven-character document, and 1Q84
    # with a half-title, both of which would otherwise ship as their own track.
    while len(kept) > 1 and chars(kept[0].first, kept[1].first) < opts.min_chapter_chars:
        title = kept[0].title or kept[1].title
        kept.pop(1)
        kept[0] = Chapter(kept[0].first, title)
    return kept


# -- synthesis -----------------------------------------------------------

@dataclass
class Clip:
    sentence: Sentence
    index: int
    wav: Optional[Wav]
    error: Optional[str] = None


def _render(engine: Engine, policy: ReadingPolicy, sentence: Sentence,
            index: int, opts: TtsOptions) -> Clip:
    try:
        text = policy.spoken(sentence)
        data = engine.speak(text, opts.speaker, speed=opts.speed,
                            pitch=opts.pitch, intonation=opts.intonation,
                            pre=0.0, post=0.0)
        return Clip(sentence, index, read_wav(data))
    except Exception as exc:                      # one bad sentence, not a lost book
        return Clip(sentence, index, None, f"{type(exc).__name__}: {exc}")


def render_stream(engine: Engine, policy: ReadingPolicy,
                  sentences: List[Sentence], opts: TtsOptions,
                  first: int = 0, log=None) -> Iterator[Clip]:
    """Synthesise in parallel, hand back in order, and survive a wedged engine.

    The engine answers concurrent requests happily, but a book read out of
    order is not a book, so a bounded window of futures is drained in the order
    they were submitted rather than as they finish.

    The stall handling is not defensive programming — it is a bug that
    happened.  On an 8,460-sentence book the engine stopped answering four
    in-flight ``/synthesis`` calls after 1,214 sentences.  It stayed healthy:
    ``/version`` answered in 5 ms and it used no CPU at all.  But those four
    sockets sat established and silent with empty queues on both sides, the
    workers blocked in ``recv``, the main thread waited on the first future,
    and the run hung for 35 minutes until it was killed.  **Replacing the
    engine is the only thing that makes those reads fail**, so that is what
    happens here.
    """
    log = log or (lambda *a: None)
    workers = opts.workers
    window = max(2, workers * 3)

    def drain(index: int, fut) -> Clip:
        try:
            return fut.result(timeout=opts.stall_seconds)
        except FutureTimeout:
            log(f"  nothing back for {opts.stall_seconds:.0f}s — replacing the engine")
            if engine.restart_backend():
                try:
                    return fut.result(timeout=opts.stall_seconds)
                except FutureTimeout:
                    pass
            return Clip(sentences[index], index, None,
                        "the engine stopped answering")

    pool = ThreadPoolExecutor(max_workers=workers)
    pending: deque = deque()
    try:
        for i in range(first, len(sentences)):
            pending.append((i, pool.submit(_render, engine, policy,
                                           sentences[i], i, opts)))
            if len(pending) >= window:
                yield drain(*pending.popleft())
        while pending:
            yield drain(*pending.popleft())
    finally:
        # Never wait here.  A wedged worker is blocked in a socket read that
        # only an engine restart can break, and shutdown(wait=True) hangs the
        # exit on it — which is what made Ctrl-C useless the first time.
        pool.shutdown(wait=False, cancel_futures=True)


# -- track assembly ------------------------------------------------------

def _safe(name: str) -> str:
    return _SAFE.sub("_", name).strip() or "track"


def _read_manifest(out_dir: str, opts: TtsOptions) -> Tuple[int, List[dict]]:
    """Return (sentences already spoken, the track records that cover them)."""
    path = os.path.join(out_dir, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0, []
    if data.get("options") != _option_key(opts):
        return 0, []                              # different voice: start over
    done: List[dict] = []
    cursor = 0
    for rec in data.get("tracks", []):
        audio = os.path.join(out_dir, rec["name"])
        subs = os.path.splitext(audio)[0] + ".srt"
        if rec.get("first") != cursor or not (os.path.exists(audio)
                                              and os.path.exists(subs)):
            break                                 # first gap ends the resume
        done.append(rec)
        cursor += rec["count"]
    return cursor, done


def _option_key(opts: TtsOptions) -> dict:
    """What a resumed run must match for its existing tracks to still be valid."""
    return {"speaker": opts.speaker, "speed": opts.speed, "pitch": opts.pitch,
            "intonation": opts.intonation, "gap": opts.gap,
            "paragraph_gap": opts.paragraph_gap, "split": opts.split,
            "track_seconds": opts.track_seconds,
            "min_chapter_chars": opts.min_chapter_chars, "ruby": opts.ruby,
            "fmt": opts.fmt}


def sweep_partials(out_dir: str, tracks: List[dict], log=None) -> List[str]:
    """Set aside WAVs the manifest does not account for.

    A run that is killed rather than interrupted — SIGTERM, or the machine
    going down — never unwinds, so the track being written stays behind under
    a track's name with no SRT beside it.  The app would list it as a chapter
    and play the fraction that got written.  Cleaning up on the way *in* covers
    that, where a handler on the way out cannot.
    """
    log = log or (lambda *a: None)
    known = {r["name"] for r in tracks}
    moved = []
    try:
        entries = sorted(os.listdir(out_dir))
    except OSError:
        return moved
    for name in entries:
        if not name.lower().endswith(".wav") or name in known:
            continue
        src = os.path.join(out_dir, name)
        dest = src + ".partial"
        try:
            os.replace(src, dest)
        except OSError:                           # pragma: no cover - racing writer
            continue
        moved.append(name)
        log(f"  set aside {name}.partial (left by a run that did not finish)")
    return moved


def _write_manifest(out_dir: str, opts: TtsOptions, tracks: List[dict]) -> None:
    tmp = os.path.join(out_dir, MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "options": _option_key(opts), "tracks": tracks},
                  fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(out_dir, MANIFEST))


def encode(wav_path: str, out_path: str, opts: TtsOptions, tags: dict) -> None:
    """WAV to M4B.

    ``-f mp4`` for the reason ``convert.py`` gives, and the language goes on the
    stream: MP4 keeps it in the track's ``mdhd`` box, and a format-level
    ``-metadata language=`` is accepted and silently dropped, leaving ``und``.
    """
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", wav_path,
           "-c:a", "aac", "-b:a", opts.bitrate, "-movflags", "+faststart"]
    for key, value in tags.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-metadata:s:a:0", f"language={_iso3(opts.language)}", "-f", "mp4", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-300:]}")


_ISO3 = {"ja": "jpn", "de": "deu", "en": "eng", "pl": "pol", "ru": "rus",
         "cs": "ces", "fr": "fra", "es": "spa", "it": "ita", "nl": "nld"}


def _iso3(lang: str) -> str:
    return _ISO3.get((lang or "").lower()[:2], "und")


# -- the run -------------------------------------------------------------

def _retag_totals(paths: List[str], log=None) -> None:
    """Rewrite ``track=n/total`` once the total is finally known.

    The count cannot be written during the run because it is not known until
    the last track exists, and a tag already written cannot be revised without
    rewriting the file — the mismatched-totals trap ``_check_track_numbering``
    exists to catch.  This is a stream copy, so the whole book retags in
    seconds.
    """
    log = log or (lambda *a: None)
    total = len(paths)
    for i, path in enumerate(paths, 1):
        if not path.lower().endswith(".m4b"):
            continue
        tmp = path + ".retag"
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", path, "-map", "0", "-c", "copy",
             "-metadata", f"track={i}/{total}", "-f", "mp4", tmp],
            capture_output=True, text=True)
        if proc.returncode == 0 and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            log(f"  could not set the track total on {os.path.basename(path)}")


def speak_book(engine: Engine, sentences: List[Sentence], out_dir: str,
               stem: str, opts: TtsOptions, meta: Optional[dict] = None,
               log=None, progress=None, force: bool = False,
               normalise=None) -> TtsStats:
    """Read `sentences` aloud into `out_dir`, writing an SRT beside each track."""
    from . import srt as srt_mod

    log = log or (lambda *a: None)
    meta = meta or {}
    os.makedirs(out_dir, exist_ok=True)
    stats = TtsStats()

    cursor, tracks = (0, []) if force else _read_manifest(out_dir, opts)
    stats.skipped_tracks = len(tracks)
    sweep_partials(out_dir, tracks, log=log)
    if cursor:
        log(f"resuming: {len(tracks)} track(s) and {cursor} sentences already spoken")
        if progress is not None:
            progress.advance("(already done)", amount=cursor)

    chapters = plan_chapters(sentences, opts)
    starts = {c.first: c for c in chapters} if chapters is not None else None
    policy = ReadingPolicy(engine, opts.speaker, opts.ruby,
                           normalise=normalise)
    written: List[str] = [os.path.join(out_dir, r["name"]) for r in tracks]

    state = {"writer": None, "cues": [], "first": cursor, "consumed": 0,
             "doc": None, "block": None, "title": None}

    def open_track(sample: Wav, title: Optional[str] = None) -> None:
        path = os.path.join(out_dir, f"{_safe(stem)} {len(tracks) + 1:03d}.wav")
        state["writer"] = WavWriter(path, sample.rate, sample.channels, sample.width)
        state["cues"] = []
        state["title"] = title
        # `consumed` is deliberately not reset here: the loop has already
        # counted the sentence that is opening this track.  Zeroing it lost one
        # sentence per track, and a resumed run then respoke the last sentence
        # of every track into a duplicate one.

    def close_track() -> None:
        writer: WavWriter = state["writer"]
        if writer is None:
            return
        duration = writer.close()
        number = len(tracks) + 1
        target = writer.path
        if opts.fmt == "m4b":
            target = os.path.splitext(writer.path)[0] + ".m4b"
            encode(writer.path, target, opts, {
                "title": state["title"] or f"{meta.get('title') or stem} {number:03d}",
                "album": meta.get("title") or stem,
                "artist": meta.get("creator") or "",
                "track": str(number),
                "comment": "synthesised with VOICEVOX",
            })
            os.remove(writer.path)
        audio = probe(target) or AudioFile(target, duration, "pcm_s16le", "wav")
        srt_mod.write_for_files(state["cues"], [audio], out_dir)
        stats.tracks += 1
        stats.audio_seconds += duration
        written.append(target)
        tracks.append({"name": os.path.basename(target), "first": state["first"],
                       "count": state["consumed"]})
        _write_manifest(out_dir, opts, tracks)
        state["first"] += state["consumed"]
        state["writer"] = None

    try:
        for clip in render_stream(engine, policy, sentences, opts,
                                  first=cursor, log=log):
            state["consumed"] += 1
            if progress is not None:
                progress.advance(clip.sentence.text[:48],
                                 weight=clip.wav.duration if clip.wav else 0.0)
            if clip.wav is None:
                stats.failures.append((clip.sentence.text[:60], clip.error or ""))
                continue

            sentence = clip.sentence
            chapter = starts.get(clip.index) if starts is not None else None
            if state["writer"] is None:
                open_track(clip.wav, chapter.title if chapter else None)
            else:
                writer: WavWriter = state["writer"]
                new_doc = sentence.doc != state["doc"]
                if starts is not None:
                    # Planned: the track ends exactly where the chapter does.
                    breaking = chapter is not None
                else:
                    # split=time: run to the target, then on to the end of the
                    # spine document, unless one document alone runs to half
                    # again the target.
                    breaking = ((writer.duration >= opts.track_seconds and new_doc)
                                or writer.duration >= opts.track_seconds * 1.5)
                if breaking:
                    consumed_here = state["consumed"]
                    state["consumed"] = consumed_here - 1
                    close_track()
                    open_track(clip.wav, chapter.title if chapter else None)
                    state["consumed"] = 1
                else:
                    paragraph = new_doc or sentence.is_heading \
                        or sentence.block_index != state["block"]
                    writer.append(silence(
                        opts.paragraph_gap if paragraph else opts.gap, clip.wav))

            writer = state["writer"]
            start = writer.duration
            writer.append(clip.wav.frames)
            state["cues"].append(Cue(sentence, 0, start, writer.duration, 1.0, False))
            state["doc"] = sentence.doc
            state["block"] = sentence.block_index
            stats.sentences += 1
        close_track()
    finally:
        writer = state["writer"]
        if writer is not None:
            # Interrupted mid-track.  The half-written WAV is real audio but
            # covers only part of its track and has no SRT, so leaving it named
            # like a track would put a silent-from-here file in the app's
            # chapter list.  It is moved aside rather than deleted — the same
            # reasoning as the retired SRTs in ``srt.py``.
            try:
                writer.close()
                os.replace(writer.path, writer.path + ".partial")
                log(f"  set aside {os.path.basename(writer.path)}.partial "
                    f"(interrupted mid-track)")
            except Exception:
                pass

    if opts.fmt == "m4b" and written:
        _retag_totals(written, log=log)

    stats.ruby_seen = policy.seen
    stats.ruby_applied = policy.applied
    stats.ruby_agreed = policy.agreed
    return stats
