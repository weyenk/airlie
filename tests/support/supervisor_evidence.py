"""Shared test helpers for tests.support.supervisor_evidence."""

from __future__ import annotations

import json
from pathlib import Path


def write_step(
    folder: Path, n: int, reasoning: str = "", text: str = "", tool: str | None = None, obs: str = ""
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"step-{n:03d}.json").write_text(
        json.dumps(
            {
                "u_t": {"name": tool or "", "arguments": {"path": "a.py"} if tool else {}},
                "o_t": obs,
                "ok": True,
                "assistant_text": text,
                "reasoning": reasoning,
            }
        )
    )
