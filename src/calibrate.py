from __future__ import annotations

"""
Enroll personal «ИА» / «прием» voiceprints from the microphone.

Usage:
  .\\.venv\\Scripts\\python.exe -m src.calibrate
  .\\.venv\\Scripts\\python.exe -m src.calibrate --device 8 --count 6
"""

import argparse
import sys
import time
import winsound

import numpy as np
import sounddevice as sd

from .audio import list_input_devices
from .voiceprint import DEFAULT_DIR, build_bank_from_wavs, write_wav


def _beep(ok: bool = True) -> None:
    try:
        winsound.Beep(880 if ok else 440, 120 if ok else 200)
    except Exception:
        print("\a", end="", flush=True)


def _record_one(device: int | None, sr: int, max_s: float, silence_s: float, threshold: float) -> np.ndarray:
    """Record until speech then silence (or max_s)."""
    block = int(sr * 0.03)
    buf: list[np.ndarray] = []
    speaking = False
    silent_run = 0
    need_silent = max(1, int(silence_s / 0.03))
    started = time.monotonic()
    q: list[np.ndarray] = []

    def cb(indata, frames, t, status):  # noqa: ARG001
        q.append(indata.copy())

    with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block, device=device, callback=cb):
        while True:
            if time.monotonic() - started > max_s:
                break
            if not q:
                time.sleep(0.005)
                continue
            mono = q.pop(0).reshape(-1)
            rms = float(np.sqrt(np.mean(mono**2)))
            if not speaking:
                if rms >= threshold:
                    speaking = True
                    buf = [mono]
                    silent_run = 0
                continue
            buf.append(mono)
            if rms < threshold:
                silent_run += 1
            else:
                silent_run = 0
            if silent_run >= need_silent:
                break

    if not buf:
        return np.zeros(int(sr * 0.3), dtype=np.float32)
    return np.concatenate(buf).astype(np.float32)


def _enroll_label(
    label: str,
    folder_name: str,
    count: int,
    device: int | None,
    sr: int,
    threshold: float,
) -> list:
    out_dir = DEFAULT_DIR / "wavs" / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    print(f"\n=== Эталон «{label}» — {count} записей ===")
    print("Говори чётко и одинаково. Перед каждой записью — короткий сигнал.")
    i = 1
    while i <= count:
        input(f"[{i}/{count}] Нажми Enter и сразу скажи «{label}»… ")
        _beep(True)
        time.sleep(0.15)
        audio = _record_one(device, sr, max_s=2.5, silence_s=0.45, threshold=threshold)
        dur = audio.size / sr
        rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
        if dur < 0.15 or rms < threshold * 0.5:
            print(f"  слабо ({dur:.2f}s rms={rms:.4f}) — повтори")
            _beep(False)
            continue
        path = out_dir / f"{folder_name}_{i:02d}.wav"
        write_wav(path, audio, sr=sr)
        paths.append(path)
        print(f"  OK → {path.name} ({dur:.2f}s)")
        _beep(True)
        i += 1
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Калибровка голосовых эталонов ИА / прием")
    parser.add_argument("--device", type=int, default=None, help="Индекс микрофона")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--count", type=int, default=5, help="Сколько образцов на слово")
    parser.add_argument("--sr", type=int, default=16000)
    parser.add_argument("--threshold", type=float, default=0.01, help="RMS старт речи при записи")
    args = parser.parse_args(argv)

    if args.list_devices:
        for line in list_input_devices():
            print(line)
        return 0

    print("Голосовая калибровка Voice-IA")
    print(f"Каталог: {DEFAULT_DIR}")
    print("Микрофоны: python -m src.calibrate --list-devices")
    if args.device is not None:
        print(f"Устройство: {args.device}")

    ia_paths = _enroll_label("ИА", "ia", args.count, args.device, args.sr, args.threshold)
    priem_paths = _enroll_label("прием", "priem", args.count, args.device, args.sr, args.threshold)

    bank = build_bank_from_wavs(ia_paths, priem_paths, sr=args.sr)
    bank.root = DEFAULT_DIR
    bank.save(DEFAULT_DIR)

    print("\nГотово.")
    print(f"  ИА    threshold={bank.ia.threshold:.3f}  samples={bank.ia.embeddings.shape[0]}")
    print(f"  прием threshold={bank.priem.threshold:.3f}  samples={bank.priem.embeddings.shape[0]}")
    print("Перезапусти агент: start-voice-ia-hidden.bat")
    print("Проверка: скажи «ИА проверка один два прием»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
