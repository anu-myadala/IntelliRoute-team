"""Minimal .env loader for local IntelliRoute development."""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False

_DOTENV_SKIP_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if value and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if " #" in value and not value.startswith("#"):
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def load_dotenv_if_present() -> None:
    global _LOADED
    if _LOADED:
        return
    # Subprocess / test isolation: do not merge project .env (e.g. CI or spawned routers).
    if os.environ.get("INTELLIROUTE_SKIP_DOTENV", "").strip().lower() in _DOTENV_SKIP_TRUTHY:
        _LOADED = True
        return
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(raw)
            if parsed is None:
                continue
            key, value = parsed
            os.environ.setdefault(key, value)
    _LOADED = True
