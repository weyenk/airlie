"""Supabase bearer token for lhr-office authoring API calls.

Stored under observability.state_dir/lhr/supabase_token (mode 0600). Override with
LHR_SUPABASE_ACCESS_TOKEN for tests or one-off use."""

from __future__ import annotations

import os
from pathlib import Path


class LhrSessionStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir.expanduser() / "lhr"
        self._path = self._dir / "supabase_token"

    def token(self) -> str | None:
        env = os.environ.get("LHR_SUPABASE_ACCESS_TOKEN", "").strip()
        if env:
            return env
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return raw or None

    def set_token(self, value: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(value.strip(), encoding="utf-8")
        self._path.chmod(0o600)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
