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
import threading
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
    "~/.cache/subtitles/vv-engine/run",
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
    cache = cache_dir or os.path.expanduser("~/.cache/subtitles")
    return extract_engine(app, os.path.join(cache, "vv-engine"), log=log)


# -- the client ----------------------------------------------------------

class Engine:
    """A thin client over the engine's HTTP API."""

    def __init__(self, base_url: str = f"http://127.0.0.1:{DEFAULT_PORT}",
                 timeout: float = 120.0, retries: int = 3, process=None):
        self.base_url = base_url.rstrip("/")
        #: Per-request read timeout.  It has to be finite: a wedged engine
        #: leaves the socket established and silent, and a client with no
        #: deadline waits on it for as long as the run lasts.
        self.timeout = timeout
        self.retries = max(1, retries)
        #: The child process, when this client started one.  Holding it is what
        #: lets a stalled request be broken by replacing the engine.
        self.process = process
        self._readings: Dict[tuple, str] = {}
        self._lock = threading.Lock()

    def restart_backend(self) -> bool:
        """Replace the engine process, if this client owns one."""
        if self.process is None:
            return False
        self.process.restart()
        self.base_url = self.process.base_url
        return True

    def cpu_seconds(self) -> Optional[float]:
        """CPU the engine has burned so far; None when this client cannot say.

        This is the wedge's tell.  A wedged engine is not *slow*, it is
        *stopped* — and every question the HTTP API can be asked says it is
        fine, because ``/version`` still answers in 5 ms.  Only the CPU
        disagrees, so it is read from the process rather than from the engine.
        """
        if self.process is None:
            return None
        return self.process.cpu_seconds()

    # -- transport --

    def _request(self, path: str, payload=None, method: Optional[str] = None,
                 raw: bool = False):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            # Rebuilt each time: the URL can move under us when the engine is
            # replaced, and a retry must not reuse the socket that hung.
            req = urllib.request.Request(
                self.base_url + path, data=data, headers=headers,
                method=method or ("POST" if data else "GET"))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                return body if raw else json.loads(body)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                # A 4xx/5xx is the engine's considered answer, not a hiccup.
                raise EngineError(f"{path} -> HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, ConnectionError, socket.timeout,
                    TimeoutError, OSError) as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2.0 * (attempt + 1), 5.0))
        raise EngineError(f"{path} -> {last}") from last

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


class EngineProcess:
    """Owns the child engine, and can put a new one in its place.

    Restarting is not housekeeping — it is the only lever that frees a wedged
    request.  A synthesis that never answers leaves the client blocked in
    ``recv`` on a healthy-looking socket; killing the process at the other end
    makes that read fail, which is what lets the run carry on.
    """

    def __init__(self, run: str, use_gpu: bool = False,
                 threads: Optional[int] = None, data_home: Optional[str] = None,
                 startup: float = 180.0, log=None):
        self.run = run
        self.use_gpu = use_gpu
        self.threads = threads
        self.data_home = data_home or os.path.expanduser(
            "~/.cache/subtitles/voicevox-data")
        self.startup = startup
        self.log = log or (lambda *a: None)
        self.port = free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.restarts = 0
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cpu_seconds(self) -> Optional[float]:
        """utime + stime of the child, in seconds, or None if unreadable.

        Linux-only deliberately: ``/proc`` is where the answer is, and where
        there is no ``/proc`` the caller falls back to its timeout rather than
        pretending to know.
        """
        proc = self.proc
        if proc is None:
            return None
        try:
            with open(f"/proc/{proc.pid}/stat", "rb") as fh:
                raw = fh.read()
            # comm is parenthesised and may itself contain spaces and
            # parentheses, so the fixed-width fields start after the *last* ')'
            # — splitting on the first one reads a process named "vv (run)"
            # wrong.  From there: state, ppid, ... utime is [11], stime [12].
            fields = raw[raw.rindex(b")") + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            return None
        return ticks / os.sysconf("SC_CLK_TCK")

    def start(self) -> None:
        os.makedirs(self.data_home, exist_ok=True)
        cmd = [self.run, "--host", "127.0.0.1", "--port", str(self.port),
               "--output_log_utf8"]
        if self.use_gpu:
            cmd.append("--use_gpu")
        if self.threads:
            cmd += ["--cpu_num_threads", str(self.threads)]
        env = {**os.environ, "XDG_DATA_HOME": self.data_home}
        self.proc = subprocess.Popen(cmd, cwd=os.path.dirname(self.run), env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        probe = Engine(self.base_url, timeout=10.0, retries=1)
        deadline = time.monotonic() + self.startup
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                raise EngineError(f"the engine exited at startup: {err.strip()[-400:]}")
            if probe.alive():
                return
            time.sleep(0.5)
        self.stop()
        raise EngineError(f"the engine did not answer within {self.startup:.0f}s")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:      # pragma: no cover - stubborn child
            proc.kill()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def restart(self) -> None:
        """Replace the engine.  Safe to call from several threads at once."""
        with self._lock:
            self.stop()
            self.port = free_port()            # the old one may still be in TIME_WAIT
            self.restarts += 1
            self.log(f"  restarting the engine (restart #{self.restarts})")
            self.start()


def _gpu_works(engine: "Engine", speaker: int = 0) -> Optional[str]:
    """Synthesise one utterance; return None if it worked, else why it did not.

    Asked at startup rather than discovered later, because the GPU failure mode
    is a 500 from ``/synthesis`` while ``/version`` and ``/audio_query`` answer
    perfectly well — the model loads, the CPU-side front end runs, and only the
    neural decode fails.  A run that finds that out on sentence 400 has wasted
    the wait.
    """
    try:
        engine.speak("テスト", speaker, pre=0.0, post=0.0)
        return None
    except EngineError as exc:
        return str(exc)[:200]


@contextmanager
def running_engine(engine_path: Optional[str] = None, url: Optional[str] = None,
                   use_gpu=True, threads: Optional[int] = None,
                   cache_dir: Optional[str] = None, log=None,
                   startup: float = 180.0, timeout: float = 120.0,
                   retries: int = 3, speaker: int = 0):
    """Yield a live :class:`Engine`, starting one only if none is answering.

    ``use_gpu`` may be True (try the GPU, fall back to the CPU if it cannot
    actually synthesise), False, or "force" to fail rather than fall back.
    """
    log = log or (lambda *a: None)

    candidate = Engine(url or f"http://127.0.0.1:{DEFAULT_PORT}",
                       timeout=timeout, retries=retries)
    if candidate.alive():
        log(f"using the VOICEVOX engine already at {candidate.base_url}")
        yield candidate
        return
    if url:
        raise EngineError(f"nothing answering at {url}")

    run = ensure_engine(engine_path, cache_dir=cache_dir, log=log)
    data_home = os.path.join(cache_dir or os.path.expanduser("~/.cache/subtitles"),
                             "voicevox-data")

    process = EngineProcess(run, use_gpu=bool(use_gpu), threads=threads,
                            data_home=data_home, startup=startup, log=log)
    process.start()
    engine = Engine(process.base_url, timeout=timeout, retries=retries,
                    process=process)
    try:
        if use_gpu:
            why = _gpu_works(engine, speaker)
            if why is None:
                log(f"engine {engine.version()} on the GPU")
            elif use_gpu == "force":
                raise EngineError(f"the GPU cannot synthesise: {why}")
            else:
                log(f"  {C_WARN}the GPU cannot synthesise; using the CPU{C_RESET}")
                log(f"  ({why})")
                process.stop()
                process.use_gpu = False
                process.port = free_port()
                process.start()
                engine.base_url = process.base_url
                log(f"engine {engine.version()} on the CPU")
        else:
            log(f"engine {engine.version()} on the CPU")
        yield engine
    finally:
        process.stop()


#: Plain-text markers; the CLI owns colour, this module must not import it.
C_WARN = ""
C_RESET = ""
