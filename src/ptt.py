from __future__ import annotations

"""Push-to-talk: sounddevice (mic/WASAPI) + soundcard loopback (COM-safe)."""

import logging
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


def _com_init() -> bool:
    try:
        import pythoncom

        pythoncom.CoInitialize()
        return True
    except Exception:
        try:
            import ctypes

            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)
            return hr in (0, 1)
        except Exception:
            return False


def _com_uninit() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        try:
            import ctypes

            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def _hostapi_score(hostapi_index: int) -> int:
    try:
        name = str(sd.query_hostapis(hostapi_index).get("name") or "").lower()
    except Exception:
        return 50
    if "wasapi" in name:
        return 0
    if "directsound" in name:
        return 1
    if "mme" in name:
        return 2
    if "wdm" in name or "ks" in name:
        return 90
    return 40


class PushToTalkRecorder:
    """Record while armed; float32 mono @ sample_rate."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        block_duration_ms: int = 30,
        device_id: str | None = None,
        device_name: str | None = None,
        loopback: bool = False,
        max_seconds: float = 60.0,
        on_peak: Callable[[float], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_duration_ms = block_duration_ms
        self.device_id = device_id
        self.device_name = device_name
        self.loopback = loopback
        self.max_seconds = max_seconds
        self.on_peak = on_peak

        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._recording = False
        self._stop_flag = False
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._peak = 0.0
        self._last_peak = 0.0
        self._capture_rate = sample_rate
        self._error: BaseException | None = None
        self._ready = threading.Event()

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def last_peak(self) -> float:
        return self._last_peak

    def set_device(
        self,
        device_id: str | None,
        *,
        device_name: str | None = None,
        loopback: bool = False,
    ) -> None:
        if self._recording:
            raise RuntimeError("Cannot change device while recording")
        self.device_id = device_id
        self.device_name = device_name
        self.loopback = loopback

    def _rms(self, block: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))

    def _resample(self, mono: np.ndarray, src_rate: int) -> np.ndarray:
        if src_rate == self.sample_rate or mono.size == 0:
            return mono.astype(np.float32, copy=False)
        duration = mono.size / src_rate
        target_n = max(1, int(round(duration * self.sample_rate)))
        x_old = np.linspace(0.0, 1.0, num=mono.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_n, endpoint=False)
        return np.interp(x_new, x_old, mono).astype(np.float32)

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._chunks = []
            self._peak = 0.0
            self._last_peak = 0.0
            self._error = None
            self._stop_flag = False
            self._started_at = time.monotonic()
            self._recording = True
            self._ready.clear()

        self._thread = threading.Thread(target=self._capture_loop, name="ptt-capture", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout=4.0)
        if self._error is not None:
            self._stop_flag = True
            self._recording = False
            if self._thread:
                self._thread.join(timeout=1.5)
            raise RuntimeError(f"Mic open failed: {self._error}") from self._error
        if not ok:
            self._stop_flag = True
            self._recording = False
            if self._thread:
                self._thread.join(timeout=1.5)
            raise RuntimeError("Mic open timed out")
        log.info("PTT start loopback=%s name=%r", self.loopback, self.device_name)

    def _ingest(self, mono: np.ndarray) -> None:
        energy = self._rms(mono)
        with self._lock:
            if not self._recording:
                return
            self._chunks.append(mono.astype(np.float32, copy=False))
            if energy > self._peak:
                self._peak = energy
        if self.on_peak:
            try:
                self.on_peak(energy)
            except Exception:
                pass

    def _find_sounddevice_input(self) -> int:
        name = (self.device_name or "").strip().lower()
        candidates: list[tuple[int, int, int]] = []  # score, -channels, index
        for i, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels") or 0) <= 0:
                continue
            dname = str(dev.get("name") or "").strip().lower()
            if name:
                if not (dname == name or name in dname or dname in name):
                    # strip parenthetical brand match e.g. ugreen
                    core = name.split("(")[0].strip()
                    if core and core not in dname and "ugreen" not in dname:
                        # try last unique token
                        token = ""
                        for part in name.replace(")", " ").replace("(", " ").split():
                            if len(part) >= 4 and part.isascii():
                                token = part.lower()
                        if not token or token not in dname:
                            continue
            score = _hostapi_score(int(dev.get("hostapi") or 0))
            candidates.append((score, -int(dev["max_input_channels"]), i))
        if not candidates and name:
            # fallback: any input with shared brand token
            for i, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels") or 0) <= 0:
                    continue
                dname = str(dev.get("name") or "").lower()
                if "ugreen" in name and "ugreen" in dname:
                    score = _hostapi_score(int(dev.get("hostapi") or 0))
                    candidates.append((score, -int(dev["max_input_channels"]), i))
        if not candidates:
            default = sd.default.device[0]
            if default is None:
                raise RuntimeError("No default input device")
            return int(default)
        candidates.sort()
        return candidates[0][2]

    def _capture_mic_sounddevice(self) -> None:
        device = self._find_sounddevice_input()
        info = sd.query_devices(device, "input")
        self._capture_rate = int(info.get("default_samplerate") or self.sample_rate)
        block_size = max(1, int(self._capture_rate * self.block_duration_ms / 1000))
        channels = min(max(1, self.channels), int(info["max_input_channels"]))
        log.info(
            "sounddevice open idx=%s api_score=%s name=%r rate=%s",
            device,
            _hostapi_score(int(info.get("hostapi") or 0)),
            info.get("name"),
            self._capture_rate,
        )

        # Blocking read (no callback) — reliable from a worker thread on Windows.
        with sd.InputStream(
            samplerate=self._capture_rate,
            channels=channels,
            dtype="float32",
            blocksize=block_size,
            device=device,
        ) as stream:
            self._ready.set()
            while not self._stop_flag:
                data, _overflow = stream.read(block_size)
                mono = data[:, 0] if data.ndim > 1 else data.reshape(-1)
                self._ingest(mono.copy())

    def _capture_loopback_soundcard(self) -> None:
        import soundcard as sc

        if not self.device_id:
            raise RuntimeError("No loopback device_id")
        mic = sc.get_microphone(self.device_id, include_loopback=True)
        rate = 48000
        block = max(1, int(rate * self.block_duration_ms / 1000))
        self._capture_rate = rate
        # Loopback mix is usually stereo
        for channels in (2, 1):
            try:
                with mic.recorder(samplerate=rate, channels=channels) as rec:
                    self._ready.set()
                    while not self._stop_flag:
                        data = rec.record(numframes=block)
                        if data is None or getattr(data, "size", 0) == 0:
                            continue
                        arr = np.asarray(data, dtype=np.float32)
                        mono = arr.mean(axis=1) if arr.ndim > 1 else arr.reshape(-1)
                        self._ingest(mono)
                return
            except AssertionError:
                log.warning("soundcard channels=%s failed, retry", channels)
                continue
        raise RuntimeError("Loopback open failed for all channel layouts")

    def _capture_loop(self) -> None:
        com = False
        try:
            if self.loopback:
                com = _com_init()
                self._capture_loopback_soundcard()
            else:
                self._capture_mic_sounddevice()
        except BaseException as exc:
            self._error = exc
            log.exception("PTT capture error: %s", exc)
            self._ready.set()
        finally:
            with self._lock:
                self._recording = False
            if com:
                _com_uninit()

    def stop(self) -> np.ndarray:
        self._stop_flag = True
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.5)
        self._thread = None

        with self._lock:
            self._recording = False
            chunks = self._chunks
            self._chunks = []
            peak = self._peak
            self._last_peak = peak

        if self._error is not None and not chunks:
            err = self._error
            self._error = None
            raise RuntimeError(f"Capture failed: {err}") from err

        if not chunks:
            log.info("PTT stop — empty")
            return np.zeros(0, dtype=np.float32)

        raw = np.concatenate(chunks)
        audio = self._resample(raw, self._capture_rate)
        dur = audio.size / float(self.sample_rate)
        log.info("PTT stop → %.2fs audio peak_rms=%.4f", dur, peak)
        return audio.astype(np.float32, copy=False)

    def elapsed(self) -> float:
        if not self._started_at:
            return 0.0
        return time.monotonic() - self._started_at
