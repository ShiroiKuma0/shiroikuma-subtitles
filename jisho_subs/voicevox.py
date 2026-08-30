"""VOICEVOX engine: HTTP client, and the supervisor that starts one.

Why VOICEVOX and not a neural service.  Its front end is OpenJTalk with a real
accent dictionary, so it *looks readings up* rather than inferring them from
kanji.  Measured against the six words the Sarashina2.2-TTS paper reports every
end-to-end system failing 12–15 times out of 15 — 好事家, 出納, 生い茂る, 従容,
六十路, 坂路 — this engine reads all six correctly, and 舳先 with them.  That is
the whole argument; the voice is the price paid for it.

Two contracts this module relies on, both verified against 0.25.2:

*The engine is started as a child process.*  Not merely convenient — a server
started elsewhere is not necessarily reachable, and the tool must not be
breakable by being invoked without one already running.  Same reasoning as
``cuda.py``.  An engine that is *already* answering is used as it stands.

*Duration is exact.*  ``/audio_query`` returns per-mora ``consonant_length`` and
``vowel_length``, and the frames they imply match the synthesised WAV to the
sample: measured 2.4747 s against 2.4747 s, a delta of 0.0 ms.  So cue
boundaries are arithmetic, not alignment — no transcription, no VAD, no
interpolation.  The WAV itself is still used as the source of truth, because it
cannot drift from itself.

The engine writes a user dictionary under ``XDG_DATA_HOME``; it exits at startup
if that path is read-only, so the supervisor always points it somewhere it owns.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional

DEFAULT_PORT = 50021

#: Where the engine is looked for, in order, before falling back to extraction.
ENGINE_PATHS = [
    "~/.cache/jisho-subs/vv-engine/run",
    "~/voicevox/vv-engine/run",
    "~/.local/share/voicevox/vv-engine/run",
    "/opt/voicevox/vv-engine/run",
]

#: The GUI ships the engine inside its AppImage; this is where it hides.
APPIMAGE_PATHS = ["~/voicevox/VOICEVOX.AppImage", "~/Applications/VOICEVOX.AppImage"]
APPIMAGE_MEMBER = "vv-engine/*"


class EngineError(RuntimeError):
    pass


@dataclass
class Style:
    """One selectable voice: a speaker's named style."""

    id: int
    speaker: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.speaker} / {self.name}"


# -- locating and unpacking ----------------------------------------------

def find_engine() -> Optional[str]:
    for p in ENGINE_PATHS:
        full = os.path.expanduser(p)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def find_appimage() -> Optional[str]:
    for p in APPIMAGE_PATHS:
        full = os.path.expanduser(p)
        if os.path.isfile(full):
            return full
    return None


def extract_engine(appimage: str, dest: str, log=None) -> str:
    """Unpack ``vv-engine`` out of the GUI AppImage, once.

    The AppImage cannot be mounted without FUSE, which a headless machine will
    not have, so the payload is extracted instead.  It lands as
    ``squashfs-root/vv-engine`` in the working directory, which is why this
    runs in a temporary directory and moves the result into place.
    """
    log = log or (lambda *a: None)
    dest = os.path.expanduser(dest)
    run = os.path.join(dest, "run")
    if os.path.isfile(run):
        return run

    parent = os.path.dirname(dest.rstrip(os.sep))
    os.makedirs(parent, exist_ok=True)
    staging = dest + ".unpacking"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    log(f"unpacking the engine from {os.path.basename(appimage)} (about 4 GB, once)")
    proc = subprocess.run([appimage, "--appimage-extract", APPIMAGE_MEMBER],
                          cwd=staging, capture_output=True, text=True)
    unpacked = os.path.join(staging, "squashfs-root", "vv-engine")
    if proc.returncode != 0 or not os.path.isfile(os.path.join(unpacked, "run")):
        shutil.rmtree(staging, ignore_errors=True)
        raise EngineError(
            f"could not unpack the engine from {appimage}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}")

    shutil.rmtree(dest, ignore_errors=True)
    os.replace(unpacked, dest)
    shutil.rmtree(staging, ignore_errors=True)
    os.chmod(run, 0o755)
    return run


def ensure_engine(explicit: Optional[str] = None, cache_dir: Optional[str] = None,
                  log=None) -> str:
    """Return a path to an engine binary, unpacking one if that is what it takes."""
    if explicit:
        full = os.path.expanduser(explicit)
        if os.path.isdir(full):
            full = os.path.join(full, "run")
        if not os.path.isfile(full):
            raise EngineError(f"no VOICEVOX engine at {explicit}")
        return full
    found = find_engine()
    if found:
        return found
    app = find_appimage()
    if not app:
        raise EngineError(
            "no VOICEVOX engine found.  Install VOICEVOX (its AppImage is enough — "
            "put it at ~/voicevox/VOICEVOX.AppImage), or point --tts-engine at an "
            "unpacked vv-engine directory.")
    cache = cache_dir or os.path.expanduser("~/.cache/jisho-subs")
    return extract_engine(app, os.path.join(cache, "vv-engine"), log=log)


# -- the client ----------------------------------------------------------

class Engine:
    """A thin client over the engine's HTTP API."""

    def __init__(self, base_url: str = f"http://127.0.0.1:{DEFAULT_PORT}",
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._readings: Dict[tuple, str] = {}

    # -- transport --

    def _request(self, path: str, payload=None, method: Optional[str] = None,
                 raw: bool = False):
        url = self.base_url + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise EngineError(f"{path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, socket.timeout) as exc:
            raise EngineError(f"{path} -> {exc}") from exc
        return body if raw else json.loads(body)

    # -- endpoints --

    def version(self) -> str:
        return self._request("/version")

    def alive(self) -> bool:
        try:
            self.version()
            return True
        except EngineError:
            return False

    def styles(self) -> List[Style]:
        """Every selectable talk voice, in the engine's own order."""
        out: List[Style] = []
        for sp in self._request("/speakers"):
            for st in sp.get("styles", []):
                if st.get("type", "talk") == "talk":
                    out.append(Style(st["id"], sp["name"], st["name"]))
        return out

    def audio_query(self, text: str, speaker: int) -> dict:
        q = urllib.parse.urlencode({"text": text, "speaker": speaker})
        return self._request(f"/audio_query?{q}", payload={})

    def accent_phrases(self, text: str, speaker: int, is_kana: bool = False):
        q = urllib.parse.urlencode({"text": text, "speaker": speaker,
                                    "is_kana": str(bool(is_kana)).lower()})
        return self._request(f"/accent_phrases?{q}", payload={})

    def synthesis(self, query: dict, speaker: int) -> bytes:
        q = urllib.parse.urlencode({"speaker": speaker})
        return self._request(f"/synthesis?{q}", payload=query, raw=True)

    # -- convenience --

    def reading_of(self, word: str, speaker: int) -> str:
        """The engine's own katakana reading of a word, with accent marks gone.

        Cached: the "correct ruby only where the engine disagrees" policy asks
        this once per distinct annotated word, and a book repeats its words.
        """
        key = (word, speaker)
        hit = self._readings.get(key)
        if hit is None:
            kana = self.audio_query(word, speaker).get("kana", "")
            hit = _bare_kana(kana)
            self._readings[key] = hit
        return hit

    def speak(self, text: str, speaker: int, speed: float = 1.0,
              pitch: float = 0.0, intonation: float = 1.0,
              pre: Optional[float] = None, post: Optional[float] = None) -> bytes:
        """Synthesise one utterance and return WAV bytes."""
        query = self.audio_query(text, speaker)
        query["speedScale"] = speed
        query["pitchScale"] = pitch
        query["intonationScale"] = intonation
        if pre is not None:
            query["prePhonemeLength"] = pre
        if post is not None:
            query["postPhonemeLength"] = post
        return self.synthesis(query, speaker)


def _bare_kana(kana: str) -> str:
    """Strip AquesTalk-style accent notation, leaving the reading itself."""
    return "".join(c for c in (kana or "") if c not in "'/_、")


# -- WAV handling --------------------------------------------------------

@dataclass
class Wav:
    """A decoded mono PCM buffer, kept as frames so joins stay sample-exact."""

    frames: bytes
    rate: int
    channels: int
    width: int

    @property
    def duration(self) -> float:
        return len(self.frames) / (self.rate * self.channels * self.width)


def read_wav(data: bytes) -> Wav:
    with wave.open(io.BytesIO(data)) as w:
        return Wav(w.readframes(w.getnframes()), w.getframerate(),
                   w.getnchannels(), w.getsampwidth())


def silence(seconds: float, like: Wav) -> bytes:
    n = int(round(seconds * like.rate)) * like.channels * like.width
    return b"\0" * n


class WavWriter:
    """Append-only WAV file, tracking exactly how much audio has gone in.

    The running total is what the cue timings are built from, so it has to be
    the file's own length rather than a sum of estimates.
    """

    def __init__(self, path: str, rate: int, channels: int, width: int):
        self.path = path
        self.rate, self.channels, self.width = rate, channels, width
        self._fh = wave.open(path, "wb")
        self._fh.setnchannels(channels)
        self._fh.setsampwidth(width)
        self._fh.setframerate(rate)
        self.frames = 0

    @property
    def duration(self) -> float:
        return self.frames / self.rate

    def append(self, data: bytes) -> None:
        self._fh.writeframes(data)
        self.frames += len(data) // (self.channels * self.width)

    def close(self) -> float:
        self._fh.close()
        return self.duration

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._fh.close()
        except Exception:                      # pragma: no cover - already shut
            pass


# -- the supervisor ------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_engine(engine_path: Optional[str] = None, url: Optional[str] = None,
                   use_gpu: bool = False, threads: Optional[int] = None,
                   cache_dir: Optional[str] = None, log=None, startup: float = 180.0):
    """Yield a live :class:`Engine`, starting one only if none is answering."""
    log = log or (lambda *a: None)

    candidate = Engine(url or f"http://127.0.0.1:{DEFAULT_PORT}")
    if candidate.alive():
        log(f"using the VOICEVOX engine already at {candidate.base_url}")
        yield candidate
        return
    if url:
        raise EngineError(f"nothing answering at {url}")

    run = ensure_engine(engine_path, cache_dir=cache_dir, log=log)
    port = free_port()
    data_home = os.path.join(cache_dir or os.path.expanduser("~/.cache/jisho-subs"),
                             "voicevox-data")
    os.makedirs(data_home, exist_ok=True)

    cmd = [run, "--host", "127.0.0.1", "--port", str(port), "--output_log_utf8"]
    if use_gpu:
        cmd.append("--use_gpu")
    if threads:
        cmd += ["--cpu_num_threads", str(threads)]

    env = {**os.environ, "XDG_DATA_HOME": data_home}
    proc = subprocess.Popen(cmd, cwd=os.path.dirname(run), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    engine = Engine(f"http://127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + startup
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                raise EngineError(f"the engine exited at startup: {err.strip()[-400:]}")
            if engine.alive():
                break
            time.sleep(0.5)
        else:
            raise EngineError(f"the engine did not answer within {startup:.0f}s")
        log(f"engine {engine.version()} on port {port}"
            f"{' (GPU)' if use_gpu else ''}")
        yield engine
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:      # pragma: no cover - stubborn child
            proc.kill()
