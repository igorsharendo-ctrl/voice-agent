from __future__ import annotations

"""Capture device list + prefs (mic + WASAPI loopback via soundcard)."""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import soundcard as sc

from .config import ROOT

log = logging.getLogger(__name__)

PREFS_PATH = ROOT / "outbox" / "prefs.json"
DEVICES_PATH = ROOT / "outbox" / "devices.json"


@dataclass
class CaptureSource:
    key: str
    device_id: str
    name: str
    label: str
    loopback: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_capture_sources() -> list[CaptureSource]:
    sources: list[CaptureSource] = []
    seen: set[str] = set()
    com = False
    try:
        try:
            import pythoncom

            pythoncom.CoInitialize()
            com = True
        except Exception:
            try:
                import ctypes

                hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)
                com = hr in (0, 1)
            except Exception:
                pass
        try:
            mics = sc.all_microphones(include_loopback=True)
        except Exception as exc:
            log.exception("list microphones failed: %s", exc)
            return sources

        for m in mics:
            try:
                mid = str(m.id)
                name = str(m.name)
                loop = bool(getattr(m, "isloopback", False))
            except Exception:
                continue
            key = f"{'loop' if loop else 'mic'}:{mid}"
            if key in seen:
                continue
            seen.add(key)
            kind = "Система" if loop else "Микрофон"
            sources.append(
                CaptureSource(
                    key=key,
                    device_id=mid,
                    name=name,
                    label=f"{kind}: {name}",
                    loopback=loop,
                )
            )
        sources.sort(key=lambda s: (s.loopback, s.name.lower()))
        return sources
    finally:
        if com:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                try:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass


def write_devices_file(sources: list[CaptureSource] | None = None) -> list[CaptureSource]:
    sources = sources if sources is not None else list_capture_sources()
    DEVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEVICES_PATH.write_text(
        json.dumps([s.to_dict() for s in sources], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sources


def load_prefs() -> dict[str, Any]:
    if not PREFS_PATH.exists():
        return {}
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(**kwargs: Any) -> None:
    data = load_prefs()
    data.update(kwargs)
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_source(
    sources: list[CaptureSource] | None = None,
    *,
    preferred_key: str | None = None,
    preferred_name: str | None = None,
    preferred_id: str | None = None,
) -> CaptureSource | None:
    """Pick a live device: prefs name/id/key, else first non-loopback mic."""
    sources = sources if sources is not None else list_capture_sources()
    if not sources:
        return None

    prefs = load_prefs()
    key = preferred_key or prefs.get("device_key")
    name = preferred_name or prefs.get("device_name")
    did = preferred_id or prefs.get("device_id")

    if key:
        for s in sources:
            if s.key == key:
                return s
    if did:
        for s in sources:
            if s.device_id == did:
                return s
    if name:
        name_l = str(name).strip().lower()
        for s in sources:
            if s.name.strip().lower() == name_l:
                return s
        # partial match (Windows sometimes adds trailing spaces / suffixes)
        for s in sources:
            if name_l in s.name.lower() or s.name.lower() in name_l:
                return s

    for s in sources:
        if not s.loopback:
            return s
    return sources[0]


def get_microphone(source: CaptureSource):
    """Return soundcard Microphone for capture (loopback OK)."""
    return sc.get_microphone(source.device_id, include_loopback=True)
