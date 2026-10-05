from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    outbox = data.get("outbox_path", "outbox/commands.jsonl")
    outbox_path = Path(outbox)
    if not outbox_path.is_absolute():
        outbox_path = ROOT / outbox_path
    data["outbox_path"] = outbox_path

    model = data.get("model")
    if isinstance(model, str) and model and not Path(model).is_absolute():
        data["model"] = str((ROOT / model).resolve())
    return data
