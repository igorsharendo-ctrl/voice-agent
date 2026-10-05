from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .asr import AsrEngine
from .audio import list_input_devices
from .config import ROOT, load_config
from .deliver import append_outbox, set_clipboard
from .devices import (
    list_capture_sources,
    resolve_source,
    save_prefs,
    write_devices_file,
)
from .ptt import PushToTalkRecorder
from .singleton import acquire_single_instance
from .status_bus import StatusWriter, clear_inbox, pop_inbox
from .wake import extract_ptt_command, is_empty_or_filler

log = logging.getLogger("voice_ia")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_path = ROOT / "outbox" / "agent-utf8.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(-1)
    except Exception:
        pass


def run(config_path: Path | None = None, list_devices: bool = False) -> int:
    if list_devices:
        for s in list_capture_sources():
            print(f"{s.key}\t{s.label}")
        # legacy PortAudio list too
        print("--- sounddevice (legacy) ---")
        for line in list_input_devices():
            print(line)
        return 0

    if not acquire_single_instance():
        return 2

    cfg = load_config(config_path)
    status = StatusWriter()
    status.set_state("boot", "Загрузка модели…", btn="busy", armed=False)
    clear_inbox()

    log.info("Config loaded from %s", config_path or ROOT / "config.yaml")
    log.info("Mode: START/STOP → clipboard")
    log.info("Outbox: %s", cfg["outbox_path"])

    sources = write_devices_file()
    # Prefer saved prefs; optional legacy hint from config device_name
    source = resolve_source(
        sources,
        preferred_name=str(cfg.get("device_name") or "") or None,
    )
    if source is None:
        status.set_state("error", "Нет устройств ввода", btn="busy")
        log.error("No capture sources")
        return 1

    save_prefs(
        device_key=source.key,
        device_id=source.device_id,
        device_name=source.name,
        loopback=source.loopback,
    )
    log.info("Capture: %s", source.label)

    asr = AsrEngine(
        model=cfg.get("model", "large-v3-turbo"),
        device=cfg.get("device", "cuda"),
        compute_type=cfg.get("compute_type", "float16"),
        language=cfg.get("language", "ru"),
    )
    log.info("ASR ready — press START on overlay")
    status.set_state(
        "listening",
        "Готов",
        btn="start",
        armed=False,
        device_key=source.key,
        device_label=source.label,
    )

    sample_rate = int(cfg.get("sample_rate", 16000))
    max_s = float(cfg.get("max_utterance_s", 60))
    min_ms = int(cfg.get("min_speech_ms", 250))
    energy_threshold = float(cfg.get("energy_threshold", 0.0025))
    wake_phrases = cfg.get("wake_phrases") or ["иа", "и а", "ia"]
    end_tokens = cfg.get("end_tokens") or ["прием", "приём", "прем"]
    do_beep = bool(cfg.get("beep_on_command", True))
    outbox = Path(cfg["outbox_path"])

    def on_peak(rms: float) -> None:
        status.update(peak_rms=rms)

    ptt = PushToTalkRecorder(
        sample_rate=sample_rate,
        channels=int(cfg.get("channels", 1)),
        block_duration_ms=int(cfg.get("block_duration_ms", 30)),
        device_id=source.device_id,
        device_name=source.name,
        loopback=source.loopback,
        max_seconds=max_s,
        on_peak=on_peak,
    )

    recording = False
    recording_since = 0.0
    min_record_s = 0.9  # ignore accidental STOP right after START
    last_heartbeat = 0.0
    heartbeat_s = 2.0

    def apply_source(new_key: str) -> None:
        nonlocal source
        sources_now = write_devices_file()
        match = next((s for s in sources_now if s.key == new_key), None)
        if match is None:
            # try by name field in inbox handled by caller
            status.set_state("error", "Устройство не найдено", btn="start", device_label=source.label)
            time.sleep(0.8)
            status.set_state(
                "listening",
                "Готов",
                btn="start",
                device_key=source.key,
                device_label=source.label,
            )
            return
        source = match
        ptt.set_device(
            source.device_id,
            device_name=source.name,
            loopback=source.loopback,
        )
        save_prefs(
            device_key=source.key,
            device_id=source.device_id,
            device_name=source.name,
            loopback=source.loopback,
        )
        log.info("Capture switched: %s", source.label)
        status.set_state(
            "listening",
            "Готов",
            btn="start",
            device_key=source.key,
            device_label=source.label,
        )

    def finish_recording() -> None:
        nonlocal recording
        recording = False
        clear_inbox()  # drop any queued start that would auto-restart
        audio = ptt.stop()
        peak = ptt.last_peak
        status.set_state(
            "speech",
            "Распознаю…",
            btn="busy",
            armed=False,
            device_key=source.key,
            device_label=source.label,
        )

        dur_ms = (audio.size / sample_rate) * 1000.0 if audio.size else 0.0
        if audio.size == 0 or dur_ms < min_ms:
            log.info("Recording too short (%.0f ms) — ignore", dur_ms)
            status.set_state("ignore", "Слишком коротко", btn="start")
            time.sleep(0.5)
            status.set_state("listening", "Готов", btn="start")
            return

        if peak < energy_threshold:
            log.info("Silence (peak_rms=%.5f < %.5f) — no clipboard", peak, energy_threshold)
            status.set_state("ignore", "Тишина — буфер не трогаю", btn="start")
            time.sleep(0.7)
            status.set_state("listening", "Готов", btn="start")
            return

        t0 = time.perf_counter()
        transcript = asr.transcribe(audio, sample_rate=sample_rate)
        dt = time.perf_counter() - t0
        log.info("Heard (%.2fs): %s", dt, transcript)

        if is_empty_or_filler(transcript, wake_phrases, end_tokens):
            log.info("Empty/filler — skip clipboard (%r)", transcript)
            status.set_state("ignore", "Ничего не распознано", btn="start")
            time.sleep(0.7)
            status.set_state("listening", "Готов", btn="start")
            return

        text = extract_ptt_command(transcript, wake_phrases, end_tokens)
        log.info("Clipboard: %s", text)
        status.set_state("delivering", "В буфер…", btn="busy")
        try:
            set_clipboard(text)
            append_outbox(outbox, text, transcript)
            if do_beep:
                _beep()
            status.set_state("delivered", "В буфере — Ctrl+V", btn="start")
        except Exception as exc:
            log.exception("Clipboard failed: %s", exc)
            status.set_state("error", f"Буфер: {exc}", btn="start")
        time.sleep(1.2)
        status.set_state("listening", "Готов", btn="start")

    try:
        while True:
            now = time.monotonic()
            msg = pop_inbox()
            cmd = (msg or {}).get("cmd") if msg else None

            if cmd == "set_device" and not recording:
                key = str((msg or {}).get("device_key") or "")
                if key:
                    apply_source(key)
                last_heartbeat = time.monotonic()
                continue

            if cmd == "refresh_devices" and not recording:
                write_devices_file()
                # re-resolve current if still present
                again = resolve_source(
                    preferred_key=source.key,
                    preferred_name=source.name,
                    preferred_id=source.device_id,
                )
                if again:
                    source = again
                    ptt.set_device(
                        source.device_id,
                        device_name=source.name,
                        loopback=source.loopback,
                    )
                status.set_state(
                    "listening",
                    "Готов",
                    btn="start",
                    device_key=source.key,
                    device_label=source.label,
                )
                last_heartbeat = time.monotonic()
                continue

            if cmd:
                log.info("Inbox cmd=%s", cmd)

            if cmd == "shutdown":
                log.info("Shutdown requested by HUD")
                if recording or ptt.recording:
                    try:
                        ptt.stop()
                    except Exception:
                        pass
                    recording = False
                clear_inbox()
                status.set_state("offline", "Остановлен", btn="busy", armed=False)
                return 0

            if recording and cmd == "ptt_stop":
                # Debounce: ignore STOP in the first second (accidental double-action)
                if (time.monotonic() - recording_since) < min_record_s:
                    log.info("Ignore early STOP (%.0f ms after START)", (time.monotonic() - recording_since) * 1000)
                    last_heartbeat = time.monotonic()
                    continue
                log.info("Stop requested by UI")
                finish_recording()
                last_heartbeat = time.monotonic()
                continue

            if recording and ptt.elapsed() >= max_s:
                log.info("Max length %.0fs — auto stop", max_s)
                finish_recording()
                last_heartbeat = time.monotonic()
                continue

            # Capture thread died unexpectedly
            if recording and not ptt.recording and ptt.elapsed() > 1.0:
                log.warning("Capture thread ended early — finishing")
                finish_recording()
                last_heartbeat = time.monotonic()
                continue

            if not recording and cmd == "ptt_start":
                clear_inbox()  # only this start; drop duplicates
                try:
                    ptt.start()
                except Exception as exc:
                    log.exception("Mic start failed: %s", exc)
                    status.set_state("error", f"Микрофон: {exc}", btn="start")
                    time.sleep(0.8)
                    status.set_state("listening", "Готов", btn="start")
                    continue
                recording = True
                recording_since = time.monotonic()
                status.set_state("speech", "Запись… 0с", btn="stop", armed=True)
                last_heartbeat = now
                continue

            if cmd == "ptt_stop" and not recording:
                log.info("Ignore STOP while idle")
                continue

            if cmd == "ptt_start" and recording:
                log.info("Ignore START while already recording")
                continue

            if recording:
                status.update(
                    label=f"Запись… {ptt.elapsed():.0f}с",
                    btn="stop",
                    armed=True,
                    state="speech",
                    device_key=source.key,
                    device_label=source.label,
                )
                last_heartbeat = now
                time.sleep(0.05)
            else:
                if now - last_heartbeat >= heartbeat_s:
                    status.set_state(
                        "listening",
                        "Готов",
                        btn="start",
                        armed=False,
                        device_key=source.key,
                        device_label=source.label,
                    )
                    last_heartbeat = now
                time.sleep(0.03)

    except KeyboardInterrupt:
        log.info("Stopped by user")
        if ptt.recording:
            ptt.stop()
        status.set_state("offline", "Остановлен", btn="busy")
        return 0
    except Exception as exc:
        log.exception("Agent crashed: %s", exc)
        status.set_state("error", f"Ошибка: {exc}", btn="start")
        raise
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local voice agent: START/STOP → clipboard")
    parser.add_argument("-c", "--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--list-devices", action="store_true", help="List capture devices and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    return run(config_path=args.config, list_devices=args.list_devices)


if __name__ == "__main__":
    sys.exit(main())
