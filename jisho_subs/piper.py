"""Piper: the German voice, behind the same interface as the Japanese one.

``tts.py`` asks an engine for exactly two things — ``speak(text) -> WAV`` and
``reading_of(word)`` — so a second engine is a second client and nothing more.
Everything that makes the pipeline worth having (one sentence per request,
exact cues, chapters, resume) is engine-independent and untouched.

**Why Piper and not a paid service, when for Japanese the free option also
won.**  The reasoning does not carry over, and it is worth being clear about
that.  VOICEVOX won Japanese on *correctness*: OpenJTalk looks readings up in a
real accent dictionary, and beat every neural system on the kanji that defeat
them.  German has no equivalent — espeak-ng's ``de_list`` is 16 KB of
exceptions over letter-to-sound rules, and German orthography is shallow enough
that the rules carry most words unaided.  So no engine wins German on reading
accuracy, and Piper is chosen for being local, free, unlimited and good enough,
with Azure at roughly $9 a book as the paid alternative if the voice grates.

What Piper is: a 2023 VITS at 22.05 kHz.  ``de_DE-thorsten-medium`` is Thorsten
Müller's voice, released CC0, and he says on his own project page "i am no
professional voice talent".  白い熊 listened to medium and high side by side and
picked medium.

Two things about it that matter here:

*It is in-process, not a server.*  There is no port, no child, and nothing to
wedge — so ``restart_backend()`` is honestly False rather than pretending.  The
stall watchdog in ``tts.py`` simply never fires.

*Its front end is espeak-ng*, which means the pronunciation failures are
espeak's failures, and those are conventions rather than phonetics — dates,
abbreviations, ordinals, currency.  ``german.py`` fixes them before the text
arrives here, which is where the real leverage is.
"""

from __future__ import annotations

import io
import os
import threading
import wave
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from .voicevox import EngineError, Style, Wav, read_wav

#: Where downloaded voices live, beside the VOICEVOX engine.
VOICE_DIR = "~/.cache/jisho-subs/piper-voices"

#: One default per language.  Chosen by ear, not by benchmark — no German TTS
#: quality benchmark exists (voicearena's German table reads "No models yet for
#: this selection"), so listening was the only evidence available.
DEFAULT_VOICES: Dict[str, str] = {
    "de": "de_DE-thorsten-medium",
    "en": "en_US-lessac-medium",
    "fr": "fr_FR-siwis-medium",
    "es": "es_ES-davefx-medium",
    "it": "it_IT-riccardo-x_low",
    "nl": "nl_NL-mls-medium",
    "pl": "pl_PL-darkman-medium",
    "ru": "ru_RU-dmitri-medium",
}


def default_voice(lang: str) -> Optional[str]:
    return DEFAULT_VOICES.get((lang or "").lower()[:2])


def voice_dir(cache_dir: Optional[str] = None) -> str:
    if cache_dir:
        return os.path.join(cache_dir, "piper-voices")
    return os.path.expanduser(VOICE_DIR)


def installed_voices(cache_dir: Optional[str] = None) -> List[str]:
    d = voice_dir(cache_dir)
    try:
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".onnx"))
    except OSError:
        return []


def ensure_voice(name: str, cache_dir: Optional[str] = None, log=None) -> str:
    """Return the path to a voice model, downloading it once if need be."""
    log = log or (lambda *a: None)
    d = voice_dir(cache_dir)
    model = os.path.join(d, f"{name}.onnx")
    if os.path.isfile(model):
        return model
    os.makedirs(d, exist_ok=True)
    try:
        from piper.download_voices import download_voice
        from pathlib import Path
    except ImportError as exc:                    # pragma: no cover - install issue
        raise EngineError(
            "piper-tts is not installed.  Install it with:\n"
            "          pip install piper-tts") from exc
    log(f"downloading the voice {name} (about 60 MB, once)")
    try:
        download_voice(name, Path(d))
    except Exception as exc:
        raise EngineError(f"could not download the Piper voice {name!r}: "
                          f"{type(exc).__name__}: {exc}") from exc
    if not os.path.isfile(model):
        raise EngineError(f"the Piper voice {name!r} did not arrive at {model}")
    return model


#: VITS moves duration by less than ``length_scale`` asks for.  Measured on
#: de_DE-thorsten-medium, five runs per point: 0.87 gave 0.917x and 0.75 gave
#: 0.832x — about 65% of what was requested.  Corrected here so that
#: ``--tts-speed`` means the same thing on both engines instead of quietly
#: meaning less on this one.  Calibrated on one voice; a wildly different model
#: may want a different gain, which is why it is a named constant.
LENGTH_SCALE_GAIN = 0.65


def length_scale_for(speed: float) -> float:
    """Piper's duration multiplier for a requested speed-up."""
    wanted = 1.0 / max(speed, 0.01)             # the duration ratio we want
    scale = 1.0 - (1.0 - wanted) / LENGTH_SCALE_GAIN
    return min(max(scale, 0.25), 4.0)


def _quieten_onnxruntime() -> None:
    """Stop ONNX Runtime writing a telemetry warning into the progress line.

    It prints "Failed to persist telemetry device ID" on the first session and
    drops a stray ``:memory:.ses`` in the working directory while doing it —
    neither of which belongs in the middle of a book being read aloud.
    """
    try:
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)   # errors only
    except Exception:                                # pragma: no cover
        pass


class Engine:
    """A Piper voice, wearing the same interface as :class:`voicevox.Engine`."""

    def __init__(self, voice: str, cache_dir: Optional[str] = None, log=None):
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise EngineError(
                "piper-tts is not installed.  Install it with:\n"
                "          pip install piper-tts") from exc
        _quieten_onnxruntime()
        model = ensure_voice(voice, cache_dir=cache_dir, log=log)
        self.name = voice
        self._voice = PiperVoice.load(model)
        self.rate = self._voice.config.sample_rate
        # espeak-ng carries global state.  Measured safe under 8 threads here,
        # but the cost of a lock on a step that takes microseconds is nothing
        # next to the cost of being wrong about it once, mid-book.
        self._phon_lock = threading.Lock()
        self._readings: Dict[tuple, str] = {}

    # -- the interface tts.py uses --

    def version(self) -> str:
        return f"piper {self.name}"

    def alive(self) -> bool:
        return True

    def restart_backend(self) -> bool:
        """Nothing to restart: the model runs in this process."""
        return False

    def styles(self) -> List[Style]:
        speakers = getattr(self._voice.config, "num_speakers", 1) or 1
        if speakers <= 1:
            return [Style(0, self.name, "default")]
        return [Style(i, self.name, f"speaker {i}") for i in range(speakers)]

    def reading_of(self, word: str, speaker: int = 0) -> str:
        """espeak's phonemes for a word — the nearest thing to a reading.

        Only the Japanese ruby policy asks for this, and German books carry no
        ruby, so in practice it is never called on this engine.  It exists so
        the two engines are interchangeable rather than nearly so.
        """
        key = (word, speaker)
        hit = self._readings.get(key)
        if hit is None:
            with self._phon_lock:
                hit = "".join("".join(s) for s in self._voice.phonemize(word))
            self._readings[key] = hit
        return hit

    def speak(self, text: str, speaker: int = 0, speed: float = 1.0,
              pitch: float = 0.0, intonation: float = 1.0,
              pre: Optional[float] = None, post: Optional[float] = None) -> bytes:
        """Synthesise one utterance and return WAV bytes.

        ``speed`` becomes Piper's ``length_scale`` through
        :func:`length_scale_for`, which corrects for VITS moving duration by
        less than it is asked to.  Note that the duration predictor is
        stochastic: the same sentence resynthesised varies by several per cent,
        which costs nothing here because every cue is timed from the audio that
        was actually produced.  ``pitch`` and
        ``intonation`` have no equivalent and are accepted so that the two
        engines take the same call; a non-default value is ignored rather than
        silently doing something else.
        """
        from piper import SynthesisConfig

        cfg = SynthesisConfig(length_scale=length_scale_for(speed))
        with self._phon_lock:
            sentences = self._voice.phonemize(text)

        parts: List[np.ndarray] = []
        for phonemes in sentences:
            ids = self._voice.phonemes_to_ids(phonemes)
            parts.append(np.asarray(self._voice.phoneme_ids_to_audio(ids, cfg)))
        if not parts:
            audio = np.zeros(0, dtype=np.float32)
        else:
            audio = np.concatenate(parts)

        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")

        pad = lambda sec: np.zeros(int(round((sec or 0.0) * self.rate)), dtype="<i2")
        pcm = np.concatenate([pad(pre), pcm, pad(post)])

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()


def running_engine(voice: Optional[str] = None, lang: str = "de",
                   cache_dir: Optional[str] = None, log=None, **_ignored):
    """Context-manager shaped like ``voicevox.running_engine``, for symmetry.

    Piper has no process to supervise, so this only loads the model — but the
    caller should not have to know which engine it asked for.
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        log_ = log or (lambda *a: None)
        name = voice or default_voice(lang) or DEFAULT_VOICES["de"]
        engine = Engine(name, cache_dir=cache_dir, log=log_)
        log_(f"{engine.version()} at {engine.rate} Hz")
        yield engine

    return _cm()
