from __future__ import annotations

import collections
import logging
import time
from typing import Callable, Iterator

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


class UtteranceRecorder:
    """Capture mic audio and yield speech segments via energy + silence VAD."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        block_duration_ms: int = 30,
        silence_ms: int = 800,
        min_speech_ms: int = 400,
        max_utterance_s: float = 30.0,
        energy_threshold: float = 0.008,
        device: int | None = None,
        heartbeat_s: float = 2.0,
        on_speech_start: Callable[[float], None] | None = None,
        on_speech_end: Callable[[float], None] | None = None,
        on_peak: Callable[[float], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_duration_ms = block_duration_ms
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_s = max_utterance_s
        self.energy_threshold = energy_threshold
        self.device = device
        self.heartbeat_s = heartbeat_s
        self.on_speech_start = on_speech_start
        self.on_speech_end = on_speech_end
        self.on_peak = on_peak

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

    def utterances(self, should_stop: Callable[[], bool] | None = None) -> Iterator[np.ndarray]:
        """Yield float32 mono waveforms at sample_rate for each detected utterance."""
        stop = should_stop or (lambda: False)
        q: collections.deque[np.ndarray] = collections.deque()

        device = self.device
        if device is None:
            device = sd.default.device[0]

        info = sd.query_devices(device, "input")
        # Prefer native rate for capture reliability, then resample to Whisper 16 kHz.
        capture_rate = int(info.get("default_samplerate") or self.sample_rate)
        block_size = max(1, int(capture_rate * self.block_duration_ms / 1000))
        silence_blocks = max(1, int(self.silence_ms / self.block_duration_ms))
        min_speech_blocks = max(1, int(self.min_speech_ms / self.block_duration_ms))
        channels = min(self.channels, int(info["max_input_channels"]))

        log.info(
            "Mic device=%s name=%r capture_rate=%s channels=%s threshold=%.4f",
            device,
            info.get("name"),
            capture_rate,
            channels,
            self.energy_threshold,
        )

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                log.warning("PortAudio status: %s", status)
            q.append(indata.copy())

        with sd.InputStream(
            samplerate=capture_rate,
            channels=channels,
            dtype="float32",
            blocksize=block_size,
            device=device,
            callback=callback,
        ):
            speaking = False
            speech_blocks: list[np.ndarray] = []
            silent_run = 0
            started_at = 0.0
            peak_window = 0.0
            last_beat = time.monotonic()

            while not stop():
                if not q:
                    time.sleep(0.005)
                    now = time.monotonic()
                    if now - last_beat >= self.heartbeat_s:
                        log.info("Listening… peak_rms=%.4f (threshold=%.4f)", peak_window, self.energy_threshold)
                        if self.on_peak:
                            try:
                                self.on_peak(peak_window)
                            except Exception:
                                pass
                        peak_window = 0.0
                        last_beat = now
                    continue

                block = q.popleft()
                mono = block[:, 0] if block.ndim > 1 else block.reshape(-1)
                energy = self._rms(mono)
                if energy > peak_window:
                    peak_window = energy
                is_speech = energy >= self.energy_threshold

                if not speaking:
                    if is_speech:
                        speaking = True
                        speech_blocks = [mono]
                        silent_run = 0
                        started_at = time.monotonic()
                        log.info("Speech start (rms=%.4f)", energy)
                        if self.on_speech_start:
                            try:
                                self.on_speech_start(energy)
                            except Exception:
                                pass
                    continue

                speech_blocks.append(mono)
                if is_speech:
                    silent_run = 0
                else:
                    silent_run += 1

                timed_out = (time.monotonic() - started_at) >= self.max_utterance_s
                # Re-read so caller can stretch silence while session is armed
                cur_silence_blocks = max(1, int(self.silence_ms / self.block_duration_ms))
                ended = silent_run >= cur_silence_blocks or timed_out

                if ended:
                    n_blocks = len(speech_blocks)
                    if n_blocks >= min_speech_blocks:
                        raw = np.concatenate(speech_blocks)
                        audio = self._resample(raw, capture_rate)
                        dur = audio.size / float(self.sample_rate)
                        log.info("Speech end → %.2fs audio (%s blocks)", dur, n_blocks)
                        if self.on_speech_end:
                            try:
                                self.on_speech_end(dur)
                            except Exception:
                                pass
                        yield audio.astype(np.float32, copy=False)
                    else:
                        log.debug("Speech too short (%s blocks) — drop", n_blocks)
                    speaking = False
                    speech_blocks = []
                    silent_run = 0


def list_input_devices() -> list[str]:
    lines: list[str] = []
    for i, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) > 0:
            lines.append(f"{i}: {dev['name']} (in={dev['max_input_channels']})")
    return lines
