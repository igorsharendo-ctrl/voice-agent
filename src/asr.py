from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


def _ensure_cuda_libs_on_path() -> None:
    """Prepend Ollama-bundled CUDA 12 DLLs (cublas/cudart) if present."""
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Programs" / "Ollama" / "lib" / "ollama" / "cuda_v12",
        Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6\bin"),
        Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin"),
        Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.1\bin"),
    ]
    for folder in candidates:
        dll = folder / "cublas64_12.dll"
        if dll.is_file():
            path = str(folder)
            cur = os.environ.get("PATH", "")
            if path.lower() not in cur.lower():
                os.environ["PATH"] = path + os.pathsep + cur
                log.info("Added CUDA libs to PATH: %s", path)
            return
    log.debug("No bundled cublas64_12.dll found on PATH candidates")


class AsrEngine:
    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "ru",
    ) -> None:
        self.language = language
        self._model_name = model
        if device == "cuda":
            _ensure_cuda_libs_on_path()
        log.info("Loading Whisper model=%s device=%s compute_type=%s", model, device, compute_type)
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._model = WhisperModel(model, device=device, compute_type=compute_type)
                self._device = device
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                downloadish = any(
                    s in msg
                    for s in ("download", "cdn", "cas client", "reconstruction", "connection", "timeout", "http")
                )
                if downloadish and attempt < 3:
                    log.warning("Model download/load attempt %s failed: %s; retrying…", attempt, exc)
                    continue
                if device == "cuda":
                    log.warning("CUDA load failed (%s); falling back to CPU int8", exc)
                    self._model = WhisperModel(model, device="cpu", compute_type="int8")
                    self._device = "cpu"
                    last_exc = None
                    break
                raise
        if last_exc is not None:
            raise last_exc

    def _fallback_cpu(self) -> None:
        log.warning("Reloading ASR on CPU int8")
        self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        self._device = "cpu"

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        initial_prompt: str | None = None,
    ) -> str:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        prompt = initial_prompt  # None = no bias; never default to «ИА. Прием.»

        def _run() -> tuple[str, Any]:
            kwargs: dict[str, Any] = dict(
                language=self.language,
                vad_filter=False,
                beam_size=1,
                best_of=1,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            if prompt:
                kwargs["initial_prompt"] = prompt
            segments, info = self._model.transcribe(audio, **kwargs)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text, info

        try:
            text, info = _run()
        except RuntimeError as exc:
            msg = str(exc).lower()
            if self._device == "cuda" and ("cublas" in msg or "cuda" in msg):
                log.warning("GPU encode failed (%s)", exc)
                self._fallback_cpu()
                text, info = _run()
            else:
                raise

        log.debug("ASR lang=%s prob=%.2f text=%r", info.language, info.language_probability, text)
        return text
