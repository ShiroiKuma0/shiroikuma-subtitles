# shiroikuma-jisho-subtitles — working notes

Produces one-sentence-per-cue SRTs from an EPUB plus its audiobook, for the
sister app **shiroikuma-jisho** (`~/git/shiroikuma-jisho`). Pure Python CLI, no
Android side.

## Things that are load-bearing — do not "simplify" them

**`difflib.SequenceMatcher(..., autojunk=False)`** in `align.py`. The default
`autojunk=True` discards any token occurring in more than 1 % of the sequence,
which on a novel means every function word. Turning it on silently halves the
match rate.

**HTML-mode parsing plus explicit `rt`/`rp` removal** in `source.py`. EPUB
content is XHTML, so `BeautifulSoup(raw, "lxml-xml")` looks like the right call
— it is not. XML mode keeps ruby readings, turning `<ruby>舳先<rt>へさき</rt></ruby>`
into `舳先へさき`. HTML mode drops them, but only as a side effect of libxml2's
HTML5 handling, so the tags are decomposed by hand as well.

**`_drop_duplicate_docs`** in `source.py`. A 目次 inside the reading spine has no
`epub:type` to filter on and steals the match for every spoken chapter title.
The test is structural — most of the document's lines appear elsewhere in the
book — so it needs no metadata and works in any language.

**Everything in `srt.py`.** The app's `flattenSubtitles()` *rewrites* what it
loads: it drops empty cues, merges cues sharing a timestamp, merges adjacent
cues with identical text less than 500 ms apart, and drops the final cue when
`last.end < secondLast.start`. Read the rules table in the module docstring
before changing the writer, and run `jisho-subs lint` after.

**`ß → ss`** in `normalize.py`. Not cosmetic: it is the difference between 85 %
and 94 % on «Also sprach Zarathustra», whose EPUB mixes 1883 word forms with
modernised ß while Whisper writes modern German.

**`decode()` in `audio.py`, and every caller using it.** Never hand a path
straight to faster-whisper: it decodes with PyAV, and PyAV abandons files with
malformed embedded artwork without raising. «Nocni wędrowcy» carries a JPEG
cover labelled as PNG, and PyAV returned 106.97 s of a 2071.75 s file — the run
finished in 53 s for ten and three quarter hours and wrote 22 complete-looking
SRTs covering a twentieth of the book. `check_decode()` exists so the next such
file is a warning on the first file rather than a discovery after a full run;
do not remove it because it "never fires".

**`_reattach_orphans`** in `segment.py`.  Segmenters return the stray closing
quote or dash that dialogue formatting leaves behind; «Also sprach Zarathustra»
produced 352 of them.  Each became a subtitle showing a single punctuation mark,
timed by interpolation because it matched no audio, and the app stopped playback
on it.  Folding them back lifted that book from 91.7 % placed to 96.6 % and cut
interpolated cues from 363 to 66.

**`_snap_start`** in `refine.py`. Whisper emits words that are not there,
typically a few milliseconds *before* a pause rather than inside it, which is
why the test covers a fragment-then-pause case and not just "start is in
silence".

**Track numbers must count the book, not the batch.** `convert()` takes a
`positions` map built from every track in the folder; without it, converting one
leftover file of twenty-two tags it `1/1`. Tags already written cannot be
revised without re-converting, so a book done in two runs carries two different
totals — `_check_track_numbering()` reports that rather than leaving it to be
found on the phone.

**The conversion is a lossless remux, and `-f mp4` is what makes it possible.**
A `.m4b` extension selects ffmpeg's *ipod* muxer, which refuses MP3 outright —
"Could not find tag for codec mp3" — so without `-f mp4` the remux silently
becomes a failed conversion and falls through to the AAC path. Verified on the
phone: MP3-in-MP4 plays and seeks precisely in shiroikuma-jisho. Bit-identity is
covered by a test that decodes both and compares, not by assertion.

**MP3 conversion is not optional polish.** The app's own dialog
(`_showMp3SeekWarningDialog`) states that with MP3 "auto-pause will fire at the
wrong sentence boundaries" — so precisely-aligned cues are wasted on MP3. The
ffmpeg invocation in `convert.py` is copied from that dialog verbatim; keep the
two in step if the app changes it. Conversion writes the M4B **beside its MP3**, same
basename, and never touches the original — so one folder ends up holding both
copies of every track. `audio.prefer_seekable()` is what stops the tool
processing each track twice; the app has no such filter, so its chapter list
shows both until the MP3s go — which `-d` does, deleting each MP3 only after
`convert.verify_replacement()` confirms its M4B probes as audio of the same
length. That check is the whole safety story for the one irreversible thing this
tool does; do not weaken it to "the file exists". `-d` used to mean `--dir`, so
the prompt defaults to no.

Do not reuse MP3-derived SRTs against the M4B — AAC encoder delay shifts the
duration (238.315 s → 238.277 s on the first Lázár track), so the timings are
regenerated from the M4B itself.

**A stale SRT is worse than a missing one.** When a file yields no cues,
`write_for_files` must not simply skip it: an SRT from an earlier run would stay
on disk and keep pairing with that audio, carrying timings for a file that no
longer exists. It is moved aside to a timestamped `.bak` — not deleted, in case
it was hand-edited.

**`--dry-run` covers conversion and deletion too.** Conversion runs at stage 1,
long before the write step, so an early version re-encoded whole audiobooks —
and with `-d`, deleted the originals — while promising to write nothing. Dry
runs now skip conversion and align against the MP3s, saying so.

**`metadata.py` was derived from the library, not invented.** The rules come
from scanning all 127 books and 4,376 files in `~/〇/[197] オーディオブック`: only
82 books have a usable track title in their tags and 8 more in their filenames.
Two findings shaped it. Chapter markers must be **kept** — they name a real part
of the work, and dropping them cost 30 books their titles. And the language
bracket codes are used consistently on ebook files but appear on only 3 of 127
audiobook folders, so language is a bonus, never a source to depend on.

`demojibake()` guards itself by requiring the repaired string to come out ≥80 %
Cyrillic; without that it would mangle `Anéantir`, `Zoë Schiffer` and `Sněženka`.
Keep that check if you touch it, and keep the tests that assert those exact
strings survive.

**Language detection reads the name up to ` -- `, and nothing after it.**
Everything after the separator is 白い熊's tagging — bracket codes, the year, and
genre words written in Japanese (小説, 哲学, ルポルタージュ), which is why the
whole name cannot be used: a German book's folder ends in kanji. Everything
before it is the title and author as written, and carries subtitles and series
text that the `book`/`author` split discards. `before_tags()` does this for both
directory and file names.

Four traps, each with a test:
- Shared diacritics prove nothing — `á` in "Lázár" is Hungarian, not Czech.
- Publisher boilerplate is not evidence — "Opening Credits" and "Chapter" appear
  in German and Japanese audiobook tags.
- An initial is not a word — "Timothy **W.** Ryback" scored Polish and tied.
- Kana ranges include CJK *punctuation* — a German filename mangled into
  `Erw・ungen` carries U+30FB, which declared Der Zauberberg Japanese. The
  script test matches kana letters only.

When the rules find nothing, `dictionaries.py` looks the title's words up in
the system's hunspell dictionaries. That is what reaches «Beyond Order»,
«Greenlights», «Nineteen eighty-four» — plainly English, but holding no
diacritic, no distinctive script and no function word. Three details matter:
only the title and author are looked up (track names are numbers and
boilerplate), words must be four letters or more (three-letter tokens matched
everywhere — "MMA" scored Polish 1.00), and the candidate languages are
restricted to en/de/pl/cs/fr because with every installed dictionary in play
Dutch and Spanish score highly on English titles and turn a clear answer into a
tie. The German and Polish dictionaries are ISO-8859, not UTF-8.

Dictionaries are entirely optional; without them the tool falls back to its own
word lists. With them, 124 of 127 books resolve.

**`und` cannot be removed, only replaced.** MP4's `mdhd` box carries a
mandatory 16-bit packed language field; a file with no language reads `und`
(0x55c4), and so do 白い熊's untouched publisher M4Bs. The answer is therefore
never to omit the tag but to know the language — the EPUB states it, `-l` states
it, `BookInfo.with_language()` fills it in — and to say plainly when it is
genuinely unknown.

MP4 keeps `language` on the **stream**, not the container. A format-level
`-metadata language=…` is accepted by ffmpeg and silently dropped, leaving the
track marked `und`.

**`plan.py` classifies, it does not touch anything.** `inspect()` deliberately
lists rather than probes — surveying the 127-book library would otherwise cost
one ffprobe per file across 4,376 of them; as it stands the survey is instant.
The full `discover()` runs only when a book is actually processed.

**A folder of SRTs is resynced, not re-aligned.** The remux is lossless — the
decoded audio is bit-identical and only the origin moves, by the encoder delay
(25 ms measured on Lázár) — so there is no drift to rediscover and transcribing
hundreds of hours would produce nothing. `_resync_from_srt()` reads the existing
cues, snaps them to the M4B's own pauses (absolute, so the offset corrects
itself) and rewrites. `--realign` runs the full pipeline with the cues as the
reference text, for an SRT that is actually wrong rather than merely moved.
This is the difference between a one-hour library run and an eight-hour one.

## Reading a book aloud (`--tts`, VOICEVOX)

**The engine was chosen for its dictionary, not its voice.** VOICEVOX's front
end is OpenJTalk with a real accent dictionary, so it looks readings up instead
of inferring them. Measured here against the six words the Sarashina2.2-TTS
paper reports every end-to-end neural system failing 12–15 times out of 15 —
好事家 コオズカ, 出納 スイトオ, 生い茂る オイシゲル, 従容 ショオヨオ, 六十路
ムソジ, 坂路 サカジ — this engine reads all six correctly, and 舳先 ヘサキ with
them. Any replacement must be held to that test before it is considered.

**Ruby capture is parallel to ruby stripping, never instead of it.** `source.py`
still decomposes `rt`/`rp`, and `Block.text` must never contain a reading —
that is what poisons alignment. `ruby.py` stashes the reading on the `<ruby>`
tag *before* the decomposition and records it as an offset into the cleaned
text afterwards. Offsets, not a `{word: reading}` map: a map cannot say which
橋 is はし and which is キョウ. `clean_with_offsets()` mirrors `_clean()` and a
test asserts they agree; `walk_text()` must equal `get_text()` for the same
reason. Verified on the corpus: 53,063 annotations captured, zero misplaced.

**Furigana is applied only where the engine disagrees** (`--tts-ruby
differing`). クリスマス・ピッグ is a children's book with 11,330 usable
annotations over 150,720 characters, on *ordinary* words. Substituting all of
them replaces most of the prose with kana, and kana costs OpenJTalk the word
identity it derives accent from — flat, mis-accented speech in exchange for
corrections that were not needed. Measured: 0 of 2 applied on 1Q84, 12 of 13 on
葉隠, where 之 = の, 而 = て and 可成候間、堅火中可仕由 becomes
なるべく候間、かたく火中つかまつるべき由. The probe is per distinct word and in
isolation — 41,635 annotations over far fewer words — so a word whose isolated
reading differs but whose in-context reading was right gets corrected
unnecessarily. That is the safe direction: the correction is the book's own.

**One sentence per request is what makes the cues exact.** A cue's start is the
audio written before it; its end is that plus the clip. No Whisper, no VAD, no
interpolation. Two independent checks: `/audio_query`'s per-mora lengths imply a
duration matching the synthesised WAV to 0.0 ms, and the last cue of each track
ends exactly at the encoded M4B's duration, also 0.0 ms, on all three tracks of
the first book. **Note this contradicts the MP3→M4B note above**: a *re-encode*
from WAV to AAC drifted by nothing measurable, because ffmpeg's edit list
compensates. Do not generalise either result to the other path.

**The engine is a child process on purpose.** A server started in one process
is not necessarily reachable from another, and the tool must not be breakable by
being invoked without one running — the same reasoning as `cuda.py`. An engine
already answering is used as it stands. Two traps: the engine writes its user
dictionary under `XDG_DATA_HOME` and **exits at startup** if that path is
read-only, so the supervisor always points it at a directory it owns; and the
GUI AppImage cannot be mounted without FUSE, so `vv-engine` is extracted (4.2 GB,
once) into the cache directory instead.

**`open_track()` must not reset the sentence counter.** It did, after the loop
had already counted the sentence that opens the track, so every track's
manifest count was one short and a resumed run respoke the last sentence of
every track into a duplicate. `test_the_manifest_covers_every_sentence_exactly_once`
is that bug.

**A killed run is cleaned up on the way in, not on the way out.** SIGTERM does
not unwind Python, so the `finally` never runs and the half-written track stays
behind under a track's name with no SRT — which the app would list as a chapter
and play the fraction of. `sweep_partials()` sets aside any WAV the manifest
does not account for. It is moved to `.partial`, never deleted.

**A chapter is not a spine document, and neither signal works alone.** One M4B
per chapter is the default, and `plan_chapters()` decides where they start
*before* anything is synthesised. Measured across the corpus: 1Q84 BOOK1 (28
docs) and クリスマス・ピッグ (65 docs) carry **no headings at all**, so headings
alone would give one track per book; 自省録 is **688 spine documents with a
median length of thirty characters**, so documents alone would give 688 tracks
of a few seconds. Headings where the book has them, documents where it does
not, fits all six books — 1Q84 plans to exactly its true **24** chapters, 自省録
29, クリスマス・ピッグ 62, 葉隠 42, 人魚のひいさま 1.

Two rules that are not optional. Chapter one always starts at sentence zero, so
front matter ahead of the first heading joins it — 人魚のひいさま opens with a
seven-character spine document that shipped as its own track until this was
added. And **chapter one folds the next chapter into itself** when it is too
short, because unlike every other chapter it has nothing behind it to merge
into; without that, `min_chapter_chars` silently does not apply to the one
chapter most likely to be a half-title.

`--tts-split time` keeps the older duration-driven behaviour (fill the target,
then run on to the end of the document); it returns `None` from
`plan_chapters()` because it depends on durations that do not exist yet.

**Default speed is 1.15**, chosen by ear from rendered samples at 1.00 / 1.15 /
1.30 / 1.45. Note that much of what reads as "slow" is `--tts-gap` between
sentences rather than the rate, so try the gap before the speed.

Track titles come from the chapter heading when there is one; the *filename*
stays `Book NNN` so it sorts and pairs with its SRT, since the app finds
subtitles by identical basename.

**Do not write a TTS test with the same sentence twice.** `srt._prepare` merges
identical adjacent cues less than 500 ms apart — the app's own contract — so a
track built from one repeated sentence comes out as a single cue and the test
measures the merge rather than what it meant to. `_distinct()` in
`tests/test_tts.py` exists for this.

**The GPU is tried, verified, and fallen back from.** `--tts-gpu` is now the
default, but requesting it is not enough: the failure mode is a **500 from
`/synthesis` while `/version` and `/audio_query` answer perfectly**, because the
model loads and the OpenJTalk front end runs and only the neural decode fails.
So `_gpu_works()` synthesises one utterance at startup — eight seconds to learn
it, rather than four hundred sentences.

**On this machine the GPU cannot work, and no configuration fixes it.** The
bundled ONNX Runtime's CUDA provider carries kernels for **sm_30, 70, 75, 80,
86, 89** and no forward-compilable PTX; the RTX 5090 is **sm_120**. Swapping in
the venv's cuDNN 9.24 does nothing — the provider links `libcudnn.so.8`, and
there are no kernels to run regardless. VOICEVOX CORE 0.17.0 (2026-08-13) moved
to ONNX Runtime 1.23.2, which is where Blackwell support arrives, but **no
VOICEVOX engine release carries it yet** (latest engine 0.25.2, 2026-04-30).
Re-check that before concluding the GPU is broken again.

**A wedged engine is replaced, never waited on.** Measured failure, not a
hypothetical: on オバアチャンノゴメンネリスト (8,460 sentences) the engine stopped
answering four in-flight `/synthesis` calls after 1,214 sentences. It stayed
*healthy* — `/version` in 5 ms, zero CPU — while those four sockets sat
`ESTAB` with empty queues both ways, the workers blocked in `recv`, the main
thread in `futex_do_wait`, and the run hung 35 minutes until killed. Three
things came out of it, and all three matter:

* `Engine` now has a finite timeout (120 s) and retries transport errors on a
  **fresh request**, since the URL moves when the engine is replaced. An HTTP
  4xx/5xx is *not* retried — it is the engine's considered answer, and that is
  how the GPU probe learns the GPU cannot synthesise.
* **Replacing the engine process is the only thing that makes a wedged socket
  read fail**; nothing in the client can break it from this side. So
  `render_stream` calls `engine.restart_backend()` and the sentence is retried
  rather than lost.
* The pool is shut down with `wait=False, cancel_futures=True`. With
  `wait=True` — which `with ThreadPoolExecutor(...)` does — the exit blocks on
  the stuck worker, which is why Ctrl-C did nothing and the run needed
  `kill -9`.

**The wedge is found by watching the engine, not the clock.** The first version
waited out `--tts-stall`, and a *second* run of the same book showed what that
costs: four more wedges, each burning the full five minutes before anything
moved — **22 of an 86-minute run** spent waiting for a timeout to expire on a
process that was doing nothing. It is not slow, it is *stopped*, and nothing
the HTTP API can be asked will say so — `/version` answers in 5 ms throughout.

So `drain()` polls instead of blocking, and asks `cpu_seconds()`: an engine
that has burned under `_IDLE_CORES` (0.05) of one core for `--tts-idle` seconds
while sentences are outstanding is wedged. Measured on a real engine, **0.002
cores idle against 11.27 synthesising**, so the threshold is nowhere near a
close call and no plausible work reads as a wedge. `--tts-stall` stays as the
backstop, and is the *only* detector where CPU cannot be read: Piper is
in-process and a `--tts-url` engine has no pid here, so both answer `None` and
keep the old behaviour exactly.

Two details that are easy to get wrong. `/proc/<pid>/stat` puts `comm` in
parentheses and it may contain more of them, so the fixed-width fields start
after the **last** `)` — and a test name ending `))` hides the bug, because the
wrong split lands on the same token by luck. And the replacement process counts
its CPU from zero, so the idle window must restart with it or the drop reads as
a fresh stall.

**`--jobs` defaults to one per core** (`TtsOptions.jobs = 0` means auto) and is
no longer passed to the engine as `--cpu_num_threads`: it meant two different
things at once, so 24 workers each asked the engine for 24 threads.

**The hours estimate is per language, and measured.** `CHARS_PER_MINUTE` comes
from 白い熊's own recordings beside their EPUBs — Japanese 1Q84 270,
クリスマス・ピッグ 308, 自省録 341; German Der Vorleser 853, Genealogie der Moral
680. The single hardcoded `chars / 400` was wrong for both, in opposite
directions: it understated Japanese by a quarter and overstated German by more
than half.

Throughput measured on 24 CPU cores: **9.6× realtime single-stream**, ~10× at
`--jobs 12`.

## German (`--tts` on a German book, Piper)

**The engine follows the language**: `ja` → VOICEVOX, everything else → Piper.
`tts.py` asks an engine for `speak(text) -> WAV` and `reading_of(word)` and
nothing else, so a second engine is a second client — chapters, exact cues,
resume and the stall watchdog are all untouched.

**The Japanese argument does not transfer, and pretending otherwise would be
wrong.** VOICEVOX won Japanese on *correctness*. German has no equivalent:
espeak-ng's `de_list` is 16 KB of exceptions over letter-to-sound rules, and
German orthography is shallow enough that the rules carry most words. No engine
wins German on reading accuracy — so Piper is chosen for being local, free and
good enough, and Azure at roughly $9 a book is the paid alternative. There is
also **no German TTS quality benchmark at all**: voicearena's German table reads
"No models yet for this selection", so 白い熊 chose thorsten-medium by ear from
samples, which was the only evidence available.

**What German gets wrong is convention, not phonetics** — and every engine gets
it wrong identically, so `german.py` fixes it before the engine. Measured on
espeak-ng: `am 1. Mai 1889` → "eins Mai eintausendachthundert…", `z. B.` spelled
letter by letter, `27,50 €` → "komma fünf null Euro". Nineteenth-century
spelling needs **nothing** — ⟨th⟩ is /t/ and ⟨ie⟩ is /iː/ already, so `Thut`,
`giebt`, `Theil` come out right and the pass must not meddle with them.

**Ordinals are expanded only where the ending is certain.** German ordinals
inflect (*vierte / viertes / vierten*) and choosing needs a case parse. A bare
`4.` is left as digits: espeak saying *vier* is wrong in a small way, *viertes*
for *vierten* is wrong in a way a learner would copy.

**Piper's `length_scale` moves duration by about 65% of what is asked.**
Measured on de_DE-thorsten-medium over five runs: 0.87 gave 0.917x, 0.75 gave
0.832x. `length_scale_for()` corrects it so `--tts-speed` means the same thing
on both engines. The duration predictor is also *stochastic* — the same
sentence varies several per cent between runs — which costs nothing because
every cue is timed from the audio actually produced.

**Default speed is per language**: 1.15 Japanese, 1.0 German. 1.15 was chosen
by ear against VOICEVOX; the German samples 白い熊 approved were Piper's own
default, and Piper at 1.0 is *already* brisk — 1175 chars/min against a human
narrator's 680–853.

**pysbd protects quoted spans, and German quotes do not always balance.** «Zur
Genealogie der Moral» has a block with 28 `»` and 27 `«`, so one unmatched mark
swallowed everything after it: a single "sentence" of 8,481 characters holding
22 sentence endings, which would have been one half-hour cue.
`_split_overlong()` breaks only pieces past 400 characters, so ordinary
dialogue keeps its protection, and refuses a split where the word before the
stop looks like an abbreviation or an initial ("Prof. Dr. Müller"). That book
went from 747 sentences (max 8,480) to 1,350 (max 2,216) — and the 2,216 one
contains no sentence break at all, so it is genuinely one Nietzsche sentence.

Measured: **68x realtime** on 24 cores, so a German book is minutes rather than
hours — Rilke's Briefe, 67,670 characters, took 50 seconds. Piper is in-process,
so `restart_backend()` is honestly False and the stall watchdog never fires.

`piper-tts` is an optional extra (`pip install -e '.[piper]'`), because Japanese
needs no package at all.

## Architecture note

There is no chapter matching, and adding it would be a regression. Global
alignment is only expensive at character level; at token level the whole book
matches in about a second (measured: 70 000 × 69 000 tokens, 0.8 s; the Japanese
worst case, 318 000 × 306 000 characters, 44.9 s, 40 MB peak). Chapter matching
is exactly what makes subplz fail when audio files and text chapters do not
correspond — which is the normal case.

**A parallel stage must report the file it *finished*, not the one it started.**
With sixteen workers, `on_start` fires for everything almost at once, so the bar
read "5/20" beside the name of file 20 — which looks like a stuck bar that then
jumps. Parallel stages (converting, snapping, writing) advance on completion
only; the sequential one (transcribing) still shows what is in flight.

**A counter of files is no unit at all when the book is one file.** «Поколение
«М»» arrived as a single 14 h 15 m M4B, so `transcribing` read `0/1` with an
empty bar for the whole twelve-minute stage — no movement, no throughput, no
estimate, and indistinguishable from a hung process, which is how it was read.
`Progress.within()` takes the position inside the item in flight and lets the
bar, the estimate and the ×realtime figure describe the file while the counter
goes on meaning files finished. Two consequences worth keeping: with a single
item the `1/1` and the never-changing filename are both dropped, because the
position is the only thing on the line that moves; and the work before the
first Whisper segment — 67 s of ffmpeg, then Silero over all fourteen hours —
is reported under its own name and claims none of the bar, since counting it as
transcription would put a number under "transcribing" that is not transcription.

**A phase that ends inside the redraw interval is never drawn again.** Decoding
finished 40 ms before speech detection started, the 0.08 s throttle swallowed
the frame, and nothing redraws until the first segment — so the line sat on
`decoding` for the whole of the VAD pass. Phase changes force a draw.

**Do not measure a stream copy in xrealtime.** The remux is bound by disk
throughput, not audio duration — 330 MB/s, 0.03 s per file — so the library's
30 GB converts in about ninety seconds. Reasoning from a xrealtime figure gave
an estimate of an hour, forty times too high.

**`refine()` runs across files in parallel**, because it is the only stage that
reads all the audio and is therefore the whole cost of a resync run: 187x
realtime sequential, 3700x across the cores.

## Known cost

The whole-book alignment is fast for word languages (~1 s) but takes **several
minutes on Japanese**, far more than the synthetic benchmark suggested. Real
Japanese is dominated by a handful of very frequent characters, and with
`autojunk=False` — which is mandatory — `difflib` builds huge occurrence lists
for them. It is a once-per-book cost, cached afterwards, and the spinner makes
it visible. If it ever needs fixing, run the anchor pass over character 4-grams
rather than single characters: same sequence length, far more distinctive
tokens.

## Validation corpus

`~/tmp/subtitles/{1..5}` — German (111 MP3), Polish (23 MP3), Japanese (24 M4B,
vertical + ruby), German 1883 orthography (175 M4A, first track is a publisher
intro with no book text), Russian (31 MP3). Between them they cover every edge
case the code guards against. Re-check against all five after touching
`source.py`, `normalize.py` or `align.py`.

## Running it

```
shiroikuma-jisho-subtitles ~/tmp/subtitles/1        # ~/0/bin, wraps the venv
~/venv.shiroikuma-jisho-subtitles/bin/python -m pytest -q
```

`bin/shiroikuma-jisho-subtitles` carries the full `-h`. Two tests enforce that
every subcommand and every option appears in it, so a new flag fails the suite
until it is documented — a flag nobody can discover may as well not exist.

`-s` (setup/verify) lives in the wrapper rather than in argparse because it has
to run on a machine with no virtualenv — it cannot go through the venv's Python.
Two bash traps to respect there: the file runs under `set -e`, so every bare
conditional used for its side effect needs `|| true`; and `$?` inside an
`if ! cmd; then` branch is the negation's status, not the command's, which once
made a broken environment report itself ready.

Transcripts and VAD results cache in `~/.cache/jisho-subs`; runs after the first
skip the GPU work entirely. Use `--force` to re-transcribe.

`cuda.py` re-execs once to put the venv's own cuDNN/cuBLAS on `LD_LIBRARY_PATH`;
without it CTranslate2 dies in a native abort with no traceback. That is the same
failure the neighbouring `~/0/bin/subplz` wrapper exists to prevent — doing it in
Python means the tool cannot be broken by being invoked some other way.

## House rules

Artefacts 白い熊 looks at go in `~/tmp` with a `yyyy-MM-dd_HH-mm-ss` stamp; the
tool's own scratch goes in `.scratch/`. Never delete a generated SRT or an old
build. No `Co-Authored-By` trailers in commits.
