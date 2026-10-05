from __future__ import annotations

"""Live voiceprint enrollment driven by the agent + HUD."""

import logging
import time
import winsound
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import UtteranceRecorder
from .status_bus import StatusWriter, pop_inbox
from .voiceprint import DEFAULT_DIR, VoiceprintBank, build_bank_from_wavs, write_wav

log = logging.getLogger(__name__)


def _beep(ok: bool = True) -> None:
    try:
        winsound.Beep(880 if ok else 440, 100 if ok else 180)
    except Exception:
        pass


def _clip_ok(audio: np.ndarray, sr: int, min_s: float = 0.12, min_rms: float = 0.004) -> bool:
    if audio.size < int(sr * min_s):
        return False
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return rms >= min_rms


def run_live_enrollment(
    *,
    status: StatusWriter,
    recorder: UtteranceRecorder,
    count: int = 40,
    sample_rate: int = 16000,
    should_abort: Callable[[], bool] | None = None,
) -> VoiceprintBank | None:
    """
    Capture `count` clips of «ИА», then `count` of «прием».
    Progress goes to status bus; cancel via inbox cmd=cancel_train.
    """
    abort = should_abort or (lambda: False)
    count = max(1, int(count))
    ia_dir = DEFAULT_DIR / "wavs" / "ia"
    pr_dir = DEFAULT_DIR / "wavs" / "priem"
    ia_dir.mkdir(parents=True, exist_ok=True)
    pr_dir.mkdir(parents=True, exist_ok=True)

    for d in (ia_dir, pr_dir):
        for old in d.glob("*.wav"):
            try:
                old.unlink()
            except OSError:
                pass

    phases: list[tuple[str, str, Path]] = [
        ("ia", "ИА", ia_dir),
        ("priem", "прием", pr_dir),
    ]
    collected: dict[str, list[Path]] = {"ia": [], "priem": []}

    prev_silence = recorder.silence_ms
    recorder.silence_ms = 450

    try:
        for phase_key, label, out_dir in phases:
            n = 0
            cancelled = False
            overall_total = count * 2
            base = 0 if phase_key == "ia" else count
            status.set_state(
                "train",
                f"Скажи «{label}»  ({n}/{count})",
                train_phase=phase_key,
                train_n=base,
                train_total=overall_total,
                heard="",
                command="",
                armed=False,
            )
            _beep(True)

            def should_stop() -> bool:
                nonlocal cancelled
                if abort() or cancelled:
                    return True
                msg = pop_inbox()
                if msg and msg.get("cmd") == "cancel_train":
                    log.info("Train cancelled via inbox")
                    cancelled = True
                    return True
                return False

            for audio in recorder.utterances(should_stop=should_stop):
                if cancelled:
                    break
                if not _clip_ok(audio, sample_rate):
                    _beep(False)
                    status.set_state(
                        "train",
                        f"Слабо — ещё раз «{label}»  ({n}/{count})",
                        train_phase=phase_key,
                        train_n=base + n,
                        train_total=overall_total,
                        heard="тише / короче",
                    )
                    continue
                n += 1
                path = out_dir / f"{phase_key}_{n:02d}.wav"
                write_wav(path, audio, sr=sample_rate)
                collected[phase_key].append(path)
                _beep(True)
                # Overall progress across both words (for HUD bar)
                overall_n = (0 if phase_key == "ia" else count) + n
                overall_total = count * 2
                status.set_state(
                    "train",
                    f"Скажи «{label}»  ({n}/{count})",
                    train_phase=phase_key,
                    train_n=overall_n,
                    train_total=overall_total,
                    heard=f"OK {path.name}",
                )
                log.info("Enroll %s %s/%s → %s", phase_key, n, count, path.name)
                if n >= count:
                    break

            if cancelled:
                status.set_state(
                    "listening",
                    "Обучение отменено",
                    train_phase="",
                    train_n=0,
                    train_total=0,
                    heard="",
                )
                return None
            if len(collected[phase_key]) < count:
                status.set_state(
                    "error",
                    "Обучение прервано",
                    train_phase="",
                    train_n=0,
                    train_total=0,
                )
                return None

        status.set_state("train", "Считаю эталоны…", train_phase="save", train_n=count * 2, train_total=count * 2)
        bank = build_bank_from_wavs(collected["ia"], collected["priem"], sr=sample_rate)
        bank.root = DEFAULT_DIR
        bank.save(DEFAULT_DIR)
        status.set_state(
            "delivered",
            f"Эталон готов ({count}+{count})",
            train_phase="done",
            train_n=count * 2,
            train_total=count * 2,
            heard=f"ia≥{bank.ia.threshold:.2f} priem≥{bank.priem.threshold:.2f}",
            command="",
        )
        time.sleep(1.2)
        status.set_state(
            "listening",
            "Слушаю…",
            train_phase="",
            train_n=0,
            train_total=0,
            heard="",
        )
        return bank
    finally:
        recorder.silence_ms = prev_silence
