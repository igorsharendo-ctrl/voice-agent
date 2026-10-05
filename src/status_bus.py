from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import ROOT

STATUS_PATH = ROOT / "outbox" / "status.json"
INBOX_PATH = ROOT / "outbox" / "inbox.json"
INBOX_QUEUE_PATH = ROOT / "outbox" / "inbox_queue.jsonl"


@dataclass
class AgentStatus:
    state: str = "boot"  # boot|listening|speech|delivering|delivered|ignore|error|offline
    label: str = "Запуск…"
    heard: str = ""
    command: str = ""
    peak_rms: float = 0.0
    armed: bool = False
    # Overlay button: start | stop | busy
    btn: str = "start"
    device_key: str = ""
    device_label: str = ""
    train_phase: str = ""  # "" | ia | priem | save | done
    train_n: int = 0
    train_total: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentStatus":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class StatusWriter:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATUS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._status = AgentStatus()

    @property
    def status(self) -> AgentStatus:
        return self._status

    def update(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self._status, k):
                setattr(self._status, k, v)
        self._status.ts = time.time()
        self.flush()

    def set_state(self, state: str, label: str, **kwargs: Any) -> None:
        self.update(state=state, label=label, **kwargs)

    def flush(self) -> None:
        payload = json.dumps(self._status.to_dict(), ensure_ascii=False)
        last: Exception | None = None
        for attempt in range(10):
            try:
                self.path.write_text(payload, encoding="utf-8")
                return
            except OSError as exc:
                last = exc
                time.sleep(0.02 * (attempt + 1))
        if last:
            pass


def read_status(path: Path | None = None) -> AgentStatus:
    p = path or STATUS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return AgentStatus.from_dict(data)
    except Exception:
        return AgentStatus(state="offline", label="Агент не запущен")


def write_inbox(cmd: str, **extra: Any) -> None:
    """Append command to queue (does not overwrite pending commands)."""
    INBOX_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cmd": cmd, "ts": time.time(), **extra}
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    for attempt in range(8):
        try:
            with INBOX_QUEUE_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            time.sleep(0.02 * (attempt + 1))
    # Legacy single-file fallback
    INBOX_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def pop_inbox() -> dict[str, Any] | None:
    """Pop next queued command (FIFO). Also drains legacy inbox.json once."""
    # Migrate legacy single inbox into queue
    if INBOX_PATH.exists():
        try:
            data = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("cmd"):
                cmd = str(data.pop("cmd"))
                write_inbox(cmd, **data)
        except Exception:
            pass
        try:
            INBOX_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    if not INBOX_QUEUE_PATH.exists():
        return None
    try:
        lines = INBOX_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    if not lines:
        return None

    first, rest = lines[0], lines[1:]
    try:
        INBOX_QUEUE_PATH.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
    except OSError:
        pass
    try:
        data = json.loads(first)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_inbox() -> None:
    for p in (INBOX_PATH, INBOX_QUEUE_PATH):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def peek_inbox() -> dict[str, Any] | None:
    if INBOX_QUEUE_PATH.exists():
        try:
            lines = INBOX_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
            if lines:
                data = json.loads(lines[0])
                return data if isinstance(data, dict) else None
        except Exception:
            pass
    if not INBOX_PATH.exists():
        return None
    try:
        data = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
