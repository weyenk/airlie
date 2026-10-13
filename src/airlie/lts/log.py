"""admission.jsonl + retrieval.jsonl: the exact rows M5's dataset builder consumes (spec 03 §3 needs per-step
decisions, which keys were later used by peers, and the episode reward)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from airlie.engine.types import ToolCall


class AdmissionLog:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.admission_path = self.dir / "admission.jsonl"
        self.retrieval_path = self.dir / "retrieval.jsonl"

    def admission(
        self,
        *,
        run_id: str,
        team_id: str,
        step: int,
        query: str,
        memory_keys: list[str],
        u_t: ToolCall,
        s_t: str,
        o_t: str,
        decision: bool,
        p_yes: float,
        reason: str,
        entry_id: str | None,
        controller: str,
    ) -> None:
        rec: dict[str, Any] = {
            "ts": time.time(),
            "run_id": run_id,
            "team_id": team_id,
            "step": step,
            "query": query,
            "memory_keys": list(memory_keys),
            "u_t": {"name": u_t.name, "arguments": u_t.arguments},
            "s_t": s_t,
            "o_t": o_t,
            "decision": decision,
            "p_yes": p_yes,
            "reason": reason,
            "entry_id": entry_id,
            "controller": controller,
            "used_later": None,
            "episode_reward": None,
        }
        self._append(self.admission_path, rec)

    def retrieval(self, *, run_id: str, team_id: str, step: int, entry_id: str, found: bool) -> None:
        self._append(
            self.retrieval_path,
            {
                "ts": time.time(),
                "run_id": run_id,
                "team_id": team_id,
                "step": step,
                "entry_id": entry_id,
                "found": found,
            },
        )

    def finalize(self, episode_reward: dict[str, float]) -> None:
        """Post-run pass: mark admissions whose entry a peer team retrieved; attach each team's reward."""
        used: set[str] = set()
        if self.retrieval_path.exists():
            for line in self.retrieval_path.read_text().splitlines():
                r = json.loads(line)
                if r.get("found"):
                    used.add(f"{r['entry_id']}|{r['team_id']}")
        if not self.admission_path.exists():
            return
        rows = [json.loads(x) for x in self.admission_path.read_text().splitlines()]
        for row in rows:
            eid = row.get("entry_id")
            row["used_later"] = bool(eid) and any(
                k.startswith(f"{eid}|") and not k.endswith(f"|{row['team_id']}") for k in used
            )
            row["episode_reward"] = episode_reward.get(row["team_id"])
        self.admission_path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))

    @staticmethod
    def _append(path: Path, rec: dict[str, Any]) -> None:
        with path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
