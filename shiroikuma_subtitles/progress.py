"""In-place progress reporting for the stages that take minutes.

Transcribing a 21-hour audiobook is a quarter of an hour of work.  Printing one
line per file floods the terminal with a hundred and eleven near-identical
lines; printing every tenth file leaves five-minute silences where nothing at
all appears.  Neither tells 白い熊 what the tool is actually doing right now.

So: a single line, rewritten in place, naming the file being worked on and
carrying enough numbers to predict the finish.  When output is not a terminal —
a log file, a pipe — it degrades to one plain line at intervals, because
carriage returns in a log are worse than useless.

The counter counts *items*, which is the right unit for a hundred and eleven
MP3s and no unit at all for one M4B holding the whole book — see ``within()``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from typing import Optional

BLOCKS = "▏▎▍▌▋▊▉█"

#: A filename shorter than this identifies nothing, so it is not worth showing.
_MIN_NAME = 14
#: Columns kept for the bar before the filename is allowed to claim space.
_BAR_RESERVE = 16
#: Sparsest stats line the layout will fall back to — counter only.
_LAST_TIER = 4

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Colour codes occupy no columns; they must not count toward the width."""
    return _ANSI.sub("", text)


def _hms(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds in (float("inf"),):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _mb(rate: float) -> str:
    """Bytes per second, readably — a decimal only where it carries meaning."""
    mb = rate / 1e6
    if mb >= 100:
        return f"{mb:.0f} MB/s"
    if mb >= 10:
        return f"{mb:.0f} MB/s"
    return f"{mb:.1f} MB/s"


def _bar(fraction: float, width: int) -> str:
    """A bar with sub-character resolution, so short runs still visibly move."""
    fraction = min(1.0, max(0.0, fraction))
    filled = fraction * width
    whole = int(filled)
    out = "█" * whole
    if whole < width:
        part = filled - whole
        out += BLOCKS[int(part * len(BLOCKS))] if part > 0.02 else " "
        out += " " * (width - whole - 1)
    return out


def shorten(text: str, room: int) -> str:
    """Trim from the middle, keeping both ends.

    Audiobook filenames carry the track number at the front and little else of
    interest after it — `001_111_9783732422098_DEXN82531555.mp3`.  Cutting the
    head to fit leaves `…N82531555.mp3`, which identifies nothing; cutting the
    middle leaves `001_111_97…31555.mp3`, which says which track is running.
    """
    if room <= 1 or len(text) <= room:
        return text
    if room < 8:
        return text[:room - 1] + "…"
    head = (room - 1) * 2 // 3
    tail = room - 1 - head
    return f"{text[:head]}…{text[-tail:]}" if tail else text[:head] + "…"


class Progress:
    """One rewritten line for a stage with a known number of steps."""

    def __init__(self, total: int, label: str, tag: str = "",
                 stream=None, enabled: Optional[bool] = None,
                 min_interval: float = 0.08, rate_unit: str = "×"):
        self.total = max(1, total)
        self.label = label
        self.tag = tag
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        #: "×" for work measured against audio duration; "MB/s" for a stream
        #: copy, which is bound by the disk and not by how long the audio runs.
        self.rate_unit = rate_unit
        self.done = 0
        self.weight = 0.0          # e.g. audio seconds completed
        #: Position inside the item in flight — see within().  Held apart from
        #: `done` and `weight` so the counter never claims a file that is still
        #: running, while the bar, the estimate and the throughput can.
        self._partial = 0.0
        self._partial_weight = 0.0
        self._position = ""
        self._phase = ""
        self.started = time.time()
        self._last_draw = 0.0
        self._last_line_len = 0
        self._plain_marker = 0
        if enabled is None:
            enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self.enabled = enabled
        # Draw straight away, so the line appears at 0/N rather than materialising
        # part-finished after the first item completes.
        if self.enabled:
            self._draw("", force=True)

    # -- drawing ---------------------------------------------------------

    def _compose(self, detail: str, width: int) -> str:
        """Lay the line out by priority, not by fixed shares.

        What matters most is which file is being worked on — that is the whole
        point of the line — so it is placed before the bar, and the numbers are
        abbreviated rather than the filename dropped.  On a narrow terminal the
        line degrades in this order: speed, elapsed, position, bar, time-left
        — except that a stage with a single item drops the unchanging filename
        first, since there the position is the only thing that moves.
        """
        units = self.done + self._partial
        frac = units / self.total
        elapsed = time.time() - self.started
        pace = units / elapsed if elapsed > 0 else 0
        remaining = (self.total - units) / pace if pace > 0 else float("inf")
        speed = ""
        weight = self.weight + self._partial_weight
        if weight and elapsed > 0:
            rate = weight / elapsed
            speed = (_mb(rate) if self.rate_unit == "MB/s" else f"{rate:.0f}×")

        head = f"{self.tag}{self.label}"
        head_len = len(self.label) + len(_strip_ansi(self.tag))
        # "1/1" carries no information; with a single item the position inside
        # it is the whole of what there is to say.
        counts = f"{frac * 100:3.0f}%"
        if self.total > 1:
            counts += f" · {self.done}/{self.total}"
        # …and for the same reason the one file's name is not news either: it
        # is the same name for the whole stage, while the position is the only
        # thing that moves.  So there the position outlives every other field,
        # and the name goes when the line finally runs out of room.  With many
        # files the reverse holds, and the position gives way first.
        keep_position = self.total == 1 and bool(self._position)

        for tier in range(_LAST_TIER + 1):
            parts = [counts]
            if self._position and (keep_position or tier <= 2):
                parts.append(self._position)
            if tier <= 3 and pace > 0:
                parts.append(f"{_hms(remaining)} left")
            if tier <= 1:
                parts.append(f"{_hms(elapsed)} elapsed")
            if tier == 0 and speed:
                parts.append(speed)
            stats = " · ".join(parts)

            room = width - head_len - len(stats) - 3
            if room < 6:
                continue
            name = ""
            if detail:
                # Reserve a usable bar before the name takes the rest.
                want = min(len(detail) + 3, max(0, room - _BAR_RESERVE))
                if want >= _MIN_NAME + 3:
                    name = " · " + shorten(detail, want - 3)
                    room -= len(name)
                elif tier < _LAST_TIER:
                    # Rather than drop the filename, spend a less detailed
                    # stats line on it — knowing which file is running beats
                    # knowing the elapsed time to the second.
                    continue
            bar_width = min(30, room - 2)
            if bar_width < 6:
                if tier < _LAST_TIER:
                    continue
                return f"{head} {stats}{name}"
            return f"{head} ▕{_bar(frac, bar_width)}▏ {stats}{name}"

        # Even the bare fallback keeps the position of a single item, as long
        # as it fits: dropping it leaves a line that never changes again.
        line = f"{head} {counts}"
        if keep_position and (head_len + len(counts) + len(self._position)
                              + 4 <= width):
            line += f" · {self._position}"
        return line

    def _draw(self, detail: str, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_draw < self.min_interval:
            return
        self._last_draw = now
        width = shutil.get_terminal_size((100, 24)).columns
        line = self._compose(detail, width - 1)
        pad = " " * max(0, self._last_line_len - len(line))
        self._last_line_len = len(line)
        self.stream.write("\r" + line + pad)
        self.stream.flush()

    def _draw_plain(self, detail: str) -> None:
        """Non-terminal output: a line per decile, never a carriage return."""
        decile = int(10 * (self.done + self._partial) / self.total)
        if decile == self._plain_marker and self.done != self.total:
            return
        self._plain_marker = decile
        elapsed = time.time() - self.started
        speed = ""
        weight = self.weight + self._partial_weight
        if weight and elapsed:
            rate = weight / elapsed
            speed = (f", {_mb(rate)}" if self.rate_unit == "MB/s"
                     else f", {rate:.0f}x realtime")
        position = f" {self._position}" if self._position else ""
        self.stream.write(f"{self.tag}{self.label} {self.done}/{self.total}"
                          f"{position} ({_hms(elapsed)} elapsed{speed})\n")
        self.stream.flush()

    # -- api -------------------------------------------------------------

    def advance(self, detail: str = "", amount: int = 1,
                weight: float = 0.0) -> None:
        self.done = min(self.total, self.done + amount)
        self.weight += weight
        # The item is now counted whole; anything within() reported about it
        # would otherwise be added a second time.
        self._partial = 0.0
        self._partial_weight = 0.0
        self._position = ""
        self._phase = ""
        if self.enabled:
            self._draw(detail, force=self.done == self.total)
        else:
            self._draw_plain(detail)

    def within(self, detail: str, done: Optional[float], total: float,
               phase: str = "") -> None:
        """Report position *inside* the item currently being worked on.

        Counting items is the right unit for a hundred and eleven MP3s and no
        unit at all for one M4B holding the whole book: fourteen hours of
        «Поколение «М»» is a bar reading `0/1` from the first second to the
        last — no movement, no throughput, no estimate, and indistinguishable
        from a hung process, which is how it was read.  Feeding the position in
        makes the bar, the estimate and the ×realtime figure describe the file,
        while the counter goes on meaning files finished.

        ``done`` and ``total`` are in the stage's own unit — audio seconds
        where the rate is ×realtime, bytes where it is MB/s — so the position
        doubles as the weight covered so far.  It never moves backwards: the
        batched Whisper pipeline yields a batch at a time, and a bar that
        retreats reads as a bug.

        A named ``phase`` marks *preparatory* work — decoding the file, finding
        the speech in it — which is shown but claims none of the bar: it is not
        the item's own progress, and its speed says nothing about how long the
        item will take.  Naming it is what keeps a number moving under
        "transcribing" from being mistaken for transcription.  A phase with no
        position to report stands on its own.
        """
        if done is None or total <= 0:
            if not phase:
                return
            self._position = phase
        else:
            pair = (f"{_hms(done)}/{_hms(total)}" if self.rate_unit == "×"
                    else f"{done / 1e6:.0f}/{total / 1e6:.0f} MB")
            if phase:
                self._position = f"{phase} {pair}"
            else:
                fraction = min(1.0, max(0.0, done / total))
                if fraction < self._partial:
                    return
                self._partial = fraction
                self._partial_weight = min(max(0.0, done), total)
                self._position = pair
        # A phase can end within the redraw interval of the one before it, and
        # nothing would redraw it afterwards: decoding finished 40 ms before
        # Silero started, so the line sat on "decoding" for the whole of the
        # speech detection.  A change of phase is therefore always drawn.
        changed, self._phase = phase != self._phase, phase
        if self.enabled:
            self._draw(detail, force=changed)
        else:
            self._draw_plain(detail)

    def note(self, detail: str) -> None:
        """Redraw with a new current-item label but no progress."""
        if self.enabled:
            self._draw(detail)

    def close(self, summary: str = "") -> None:
        elapsed = time.time() - self.started
        if self.enabled:
            self.stream.write("\r" + " " * self._last_line_len + "\r")
            self.stream.flush()
        line = f"{self.tag}{self.label} — {self.done}/{self.total} in {_hms(elapsed)}"
        if self.weight and elapsed > 0:
            rate = self.weight / elapsed
            line += (f" ({_mb(rate)})" if self.rate_unit == "MB/s"
                     else f" ({rate:.0f}× realtime)")
        if summary:
            line += f"  {summary}"
        self.stream.write(line + "\n")
        self.stream.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Spinner:
    """An animated line for a blocking call with no measurable progress.

    The whole-book alignment is one ``difflib`` call that offers no hook to
    report from — forty-five seconds of nothing on the Japanese book.  A
    spinner at least distinguishes "working" from "hung".
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, tag: str = "", stream=None,
                 enabled: Optional[bool] = None):
        self.label, self.tag = label, tag
        self.stream = stream or sys.stderr
        if enabled is None:
            enabled = self.stream.isatty() and not os.environ.get("NO_COLOR")
        self.enabled = enabled
        self._stop = None
        self._thread = None
        self.started = 0.0

    def __enter__(self):
        self.started = time.time()
        if not self.enabled:
            self.stream.write(f"{self.tag}{self.label}…\n")
            self.stream.flush()
            return self
        import threading

        self._stop = threading.Event()

        def run():
            i = 0
            while not self._stop.wait(0.1):
                frame = self.FRAMES[i % len(self.FRAMES)]
                elapsed = _hms(time.time() - self.started)
                self.stream.write(f"\r{self.tag}{frame} {self.label} · {elapsed}\033[K")
                self.stream.flush()
                i += 1

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self.stream.write("\r\033[K")
            self.stream.flush()
        return False
