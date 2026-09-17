# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A **literate-programming project**: the single tracked source `subtitles.org` is a GNU Emacs Org-mode
document that tangles into two files in `~/0/bin` (which is on PATH):

- `subtitles.sh` — the Bash driver (options, the Whisper venv, ffmpeg decode, optional mkvmerge step);
- `subtitles-whisper.py` — the Python pipeline (VAD, speech regions, two Whisper passes, merge,
  hallucination filters, subtitle building, review report).

`subtitles.sh` transcribes a film's **dubbed** soundtrack (Japanese by default) into subtitles of what is
actually said, using a local Whisper large-v3 on the GPU, guided by an existing subtitle file whose
*timings* are right but whose *text* is not (a translation track, an OCR of a bitmap track).

**`subtitles.org` is the source of truth — never hand-edit the tangled files.** They are overwritten on
the next tangle.

## Build (tangle)

- In Emacs: open `subtitles.org`, `C-c C-v C-t`. The `Local Variables` block also tangles on every save.
- Batch: `emacs --batch -l org --eval '(progn (setq org-confirm-babel-evaluate nil) (org-babel-tangle-file "subtitles.org"))'`

Each tangled file is one top-level heading (`* subtitles-whisper.py`, `* subtitles`) whose
`:header-args:` property sets the target and shebang; the `#+begin_src` blocks under it are concatenated
in document order. Blocks elsewhere (usage examples) are `#+begin_example` and are not tangled.

## Conventions

- A line starting with `*` inside a source block is written `,*` in the Org file (Org's escape); it
  tangles back to `*` — e.g. the `,* ) help ;;` fall-through in the option parser.
- Bump the version in the top headline and prepend a `** x.y.z` / `*** <UTC stamp> (曜日)` / `**** entry`
  block to **Changelog** for user-facing changes.
- Tunables live in the `Parameters` block of the Python half (padding, merge gap, snapping, line lengths,
  per-language hallucination phrases). Add a language by adding a `LANG[...]` entry.
- After editing, re-tangle and syntax-check: `bash -n ~/0/bin/subtitles.sh` and
  `~/.venvs/whisper/bin/python -m py_compile ~/0/bin/subtitles-whisper.py`.

## Runtime

- Needs a system PyTorch with CUDA; the venv `$KXWHISPERVENV` (default `~/.venvs/whisper`) is created on
  first run on top of it (`openai-whisper`, `silero-vad`, `scipy`).
- Work dir `/tmp/subtitles/<basename>` caches the VAD and both Whisper passes, so the subtitle-building
  parameters can be re-tuned without re-transcribing (`-f` forces a fresh transcription).
- The reference SRT and the audio must be on the same timeline — verify the first cue against the first
  spoken line before a long run.
