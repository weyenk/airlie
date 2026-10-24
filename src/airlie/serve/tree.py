"""A run as the topology page draws it: its agents as nodes shaped by the topology that ran it (studio: the
chief, its specialists, the reviewer and the synthesis; fork: the agent that got stuck and its forks; teams:
the parallel teams and the judge; single: one agent), who is working now, and the hand-offs between them as
edges that say what went across. Derived from the run's events.jsonl alone, so a finished run reads the same
after a restart, and a run logged before `handoff` events existed gets its edges from what it did log."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from airlie.serve.jobs import read_events
from airlie.topology.studio import HANDOFF_CHARS

__all__ = ["HANDOFF_CHARS", "RunNotFound", "build_tree", "run_tree"]

RECENT = 5
"""How many of a node's latest events its detail lists."""

_SAFE = re.compile(r"[A-Za-z0-9._-]+")
_BUSY = frozenset({"active", "idle"})
"""A subtask in either state has its chief waiting on it."""
_STUDIO = frozenset(
    {
        "plan",
        "route",
        "skill",
        "subtask_start",
        "handoff",
        "review",
        "fix_round",
        "follow_up",
        "subtask_done",
        "synthesis",
    }
)
"""The events the studio's own nodes and edges come from."""


class RunNotFound(LookupError):
    """No run directory of that id, or (airlie.serve.run_flow) no stage or job of that id in its flow."""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class _Tree:
    def __init__(self, models: Mapping[str, str]) -> None:
        self.models = models
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.topology: str | None = None
        self.mode = "run"
        self.goal: str | None = None
        self.repo: str | None = None
        self.run_id: str | None = None
        self.started: float | None = None
        self.done: dict[str, Any] | None = None
        self.planned = False
        self.handoffs = False  # the log has `handoff` events: they are the edges, nothing is derived
        self.forks = 0
        self.teams = 0
        self.k = 1

    # ---- nodes and edges ----
    def node(
        self, nid: str, label: str | None = None, kind: str = "role", role: str | None = None
    ) -> dict[str, Any]:
        if nid not in self.nodes:
            self.nodes[nid] = {
                "id": nid,
                "label": label or nid,
                "kind": kind,
                "role": role,
                "model": None,
                "skill": None,
                "state": "waiting",
                "steps": 0,
                "tokens": {"prompt": 0, "completion": 0},
                "current": None,
                "recent": [],
                "stop_reason": None,
                "fix_rounds": 0,
                "won": False,
            }
        return self.nodes[nid]

    def role_node(self, nid: str, role: str | None) -> dict[str, Any]:
        n = self.node(nid, role=role)
        if role:
            n["role"] = role
            n["label"] = role.replace("_", " ").capitalize()
        return n

    def reviewer(self, role: str | None = None) -> dict[str, Any]:
        n = self.node("reviewer", "Reviewer", "reviewer", role or "reviewer")
        if role:
            n["role"] = role
        return n

    def note(self, nid: str, text: str) -> None:
        if nid in self.nodes:
            recent = self.nodes[nid]["recent"]
            recent.append(text)
            del recent[:-RECENT]

    def edge(self, src: str, dst: str, what: str, text: str | None, ts: Any) -> None:
        self.edges.append(
            {
                "src": src,
                "dst": dst,
                "what": what,
                "text": text[:HANDOFF_CHARS] if isinstance(text, str) else None,
                "ts": ts,
                "active": False,
            }
        )

    def finish(self, n: dict[str, Any], e: dict[str, Any]) -> None:
        n["state"] = "done" if e.get("ok") else "failed"
        n["stop_reason"] = e.get("stop_reason")
        if isinstance(e.get("steps"), int):
            n["steps"] = e["steps"]
        n["current"] = None
        steps = _plural(n["steps"], "step")
        self.note(n["id"], f"{e.get('stop_reason') or ('done' if e.get('ok') else 'failed')} after {steps}")

    # ---- events ----
    def feed(self, e: dict[str, Any]) -> None:
        kind = e.get("kind")
        if self.run_id is None and isinstance(e.get("run_id"), str):
            self.run_id = e["run_id"]
        if self.started is None and isinstance(e.get("ts"), int | float):
            self.started = float(e["ts"])
        if kind == "run_start":
            self.start(e)
        elif kind == "run_done":
            self.done = {"ok": e.get("ok"), "wall_ms": e.get("wall_ms")}
        elif kind == "step":
            self.step(e)
        elif kind == "steer" and e.get("team_id") in self.nodes:
            self.note(e["team_id"], f"steer {e.get('id')}")
        elif kind in _STUDIO:
            self.studio(kind, e)
        elif kind in ("branch_start", "branch_done", "fork_point", "fork_resolved"):
            self.fork(kind, e)
        elif kind in ("team_start", "team_done", "aggregate"):
            self.parallel(kind, e)

    def start(self, e: dict[str, Any]) -> None:
        self.goal, self.repo = e.get("goal"), e.get("repo")
        k = e.get("k")
        self.k = k if isinstance(k, int) else 1
        if e.get("mode") == "plan":
            self.mode = "plan"
        if e.get("orchestrator") == "studio":
            self.topology = "studio"
            chief = self.node("chief", "Chief", "chief", "chief")
            chief["state"] = "active"
        elif e.get("topology") == "fork":
            self.topology = "fork"
        else:
            self.topology = "teams" if self.k > 1 else "single"

    def step(self, e: dict[str, Any]) -> None:
        tid = e.get("team_id")
        if tid not in self.nodes:
            return
        n = self.nodes[tid]
        n["steps"] += 1
        n["tokens"]["prompt"] += int(e.get("prompt_tokens") or 0)
        n["tokens"]["completion"] += int(e.get("completion_tokens") or 0)
        n["model"] = e.get("model") or n["model"]
        n["role"] = n["role"] or e.get("role")
        ok = e.get("ok") is not False
        n["current"] = {"step": e.get("step"), "tool": e.get("tool"), "ok": ok}
        self.note(tid, f"step {e.get('step')} · {e.get('tool')}" + ("" if ok else " (error)"))

    def studio(self, kind: str, e: dict[str, Any]) -> None:
        self.topology = self.topology or "studio"
        self.node("chief", "Chief", "chief", "chief")
        sid = e.get("subtask")
        if kind == "plan":
            self.planned = True
            roles = list(e.get("roles") or [])
            for i, nid in enumerate(e.get("subtasks") or []):
                self.role_node(nid, roles[i] if i < len(roles) else None)
            self.note("chief", f"planned {_plural(len(e.get('subtasks') or []), 'subtask')}")
        elif kind == "route" and sid:
            self.role_node(sid, e.get("chosen"))
        elif kind == "skill" and sid:
            self.node(sid)["skill"] = e.get("chosen")
        elif kind == "subtask_start" and sid:
            self.planned = True
            n = self.role_node(sid, e.get("role"))
            n["state"], n["skill"] = "active", e.get("skill") or n["skill"]
            self.note(sid, "started")
            if not self.handoffs:
                self.edge("chief", sid, "brief", None, e.get("ts"))
        elif kind == "handoff":
            self.handoff(e)
        elif kind == "review" and sid:
            rv = self.reviewer()
            rv["state"] = "idle"
            verdict, found, blocking = (
                e.get("verdict"),
                int(e.get("findings") or 0),
                int(e.get("blocking") or 0),
            )
            self.note("reviewer", f"{sid}: {verdict}, {_plural(found, 'finding')} (round {e.get('round')})")
            self.note(sid, f"review: {verdict}")
            if not self.handoffs:
                self.edge(sid, "reviewer", "diff", None, e.get("ts"))
                text = f"{verdict}: {_plural(found, 'finding')}, {blocking} blocking"
                self.edge("reviewer", sid, "findings", text, e.get("ts"))
        elif kind == "fix_round" and sid:
            n = self.node(sid)
            n["state"], n["fix_rounds"] = "active", e.get("round") or n["fix_rounds"] + 1
            self.note(sid, f"fix round {n['fix_rounds']} ({e.get('blocking', 0)} blocking)")
        elif kind == "follow_up" and sid:
            self.node(sid)["state"] = "active"
            self.note(sid, "sent back once more")
        elif kind == "subtask_done" and sid:
            n = self.node(sid)
            if isinstance(e.get("fix_rounds"), int):
                n["fix_rounds"] = max(n["fix_rounds"], e["fix_rounds"])
            self.finish(n, e)
            if not self.handoffs:
                text = f"{e.get('stop_reason') or 'done'} after {_plural(n['steps'], 'step')}"
                self.edge(sid, "chief", "result", text, e.get("ts"))
        elif kind == "synthesis":
            s = self.node("synthesis", "Synthesis", "synthesis", "chief")
            s["state"], s["model"] = "done", self.models.get("chief")
            opened = e.get("open_questions")
            count = len(opened) if isinstance(opened, list) else int(opened or 0)
            self.note("synthesis", f"{e.get('chars')} characters, {_plural(count, 'open question')}")
            if not self.handoffs:
                text = f"{e.get('chars')} characters, {_plural(count, 'open question')}"
                self.edge("chief", "synthesis", "answer", text, e.get("ts"))

    def handoff(self, e: dict[str, Any]) -> None:
        self.handoffs = True
        src, dst = str(e.get("src")), str(e.get("dst"))
        for end in (src, dst):
            if end == "reviewer":
                self.reviewer(e.get("role"))
            elif end == "synthesis":
                self.node("synthesis", "Synthesis", "synthesis", "chief")
            else:
                self.node(end)
        if dst == "reviewer":
            self.nodes["reviewer"]["state"] = "active"
            if self.nodes[src]["state"] == "active":
                self.nodes[src]["state"] = "idle"  # waits on its review
        self.edge(src, dst, str(e.get("what")), e.get("text"), e.get("ts"))

    def fork(self, kind: str, e: dict[str, Any]) -> None:
        self.topology = self.topology or "fork"
        bid = e.get("branch")
        if kind == "branch_start" and bid:
            if bid not in self.nodes:
                child = bool(e.get("child"))
                if child:
                    self.forks += 1
                self.node(bid, f"Fork {self.forks}" if child else "Chief", "branch")
            self.nodes[bid]["state"] = "active"
            self.note(bid, "started")
        elif kind == "branch_done" and bid in self.nodes:
            self.finish(self.nodes[bid], e)
        elif kind == "fork_point":
            parent = str(e.get("parent"))
            self.node(parent, kind="branch")["state"] = "stuck"
            self.note(parent, f"stuck ({e.get('reason')}): forked")
            hyps = list(e.get("hypotheses") or [])
            for i, child in enumerate(e.get("children") or []):
                if child not in self.nodes:
                    self.forks += 1
                    self.node(child, f"Fork {self.forks}", "branch")
                self.edge(parent, child, "hypothesis", hyps[i] if i < len(hyps) else None, e.get("ts"))
        elif kind == "fork_resolved" and e.get("winner") in self.nodes:
            self.nodes[e["winner"]]["won"] = True
            self.note(e["winner"], f"won round {e.get('round')} ({e.get('how')})")

    def parallel(self, kind: str, e: dict[str, Any]) -> None:
        tid = e.get("team_id")
        if kind == "team_start" and tid:
            if tid not in self.nodes:
                self.teams += 1
                single = self.topology == "single"
                self.node(tid, "Agent" if single else f"Team {self.teams}", "team")
            self.nodes[tid]["state"] = "active"
        elif kind == "team_done" and tid in self.nodes:
            self.finish(self.nodes[tid], e)
        elif kind == "aggregate":
            judge = self.node("aggregate", "Judge", "aggregate")
            judge["state"] = "done"
            self.note("aggregate", str(e.get("reason") or ""))
            for team, score in (e.get("scores") or {}).items():
                self.edge(str(team), "aggregate", "score", f"score {score}", e.get("ts"))
            if e.get("winner") in self.nodes:
                self.nodes[e["winner"]]["won"] = True

    # ---- the tree ----
    def tree(self) -> dict[str, Any]:
        running = self.done is None
        if self.topology == "studio":
            self.settle_studio(running)
        if not running:
            for n in self.nodes.values():
                if n["state"] in _BUSY:
                    n["state"], n["current"] = "done", None
        for n in self.nodes.values():
            if n["model"] is None and n["role"]:
                n["model"] = self.models.get(n["role"])
        busy = {nid for nid, n in self.nodes.items() if n["state"] == "active"}
        for nid in busy:
            into = [e for e in self.edges if e["dst"] == nid]
            if into:
                into[-1]["active"] = True
        return {
            "run_id": self.run_id,
            "topology": self.topology,
            "mode": self.mode,
            "goal": self.goal,
            "repo": self.repo,
            "started": self.started,
            "state": "running" if running else "done",
            "ok": None if running else self.done.get("ok") if self.done else None,
            "wall_ms": self.done.get("wall_ms") if self.done else None,
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
        }

    def settle_studio(self, running: bool) -> None:
        """The chief and reviewer, whose state follows the others': the chief plans, waits on its specialists,
        then works again (synthesis, the gate); the reviewer is shown unless the run ended without one."""
        subtasks = [n for n in self.nodes.values() if n["kind"] == "role"]
        if self.planned and self.mode != "plan" and running:
            self.reviewer()
        if not running and "reviewer" in self.nodes and self.nodes["reviewer"]["state"] == "waiting":
            del self.nodes["reviewer"]
        chief = self.nodes["chief"]
        reviewing = self.nodes.get("reviewer", {}).get("state") == "active"
        if not running:
            chief["state"] = "done"
        elif not self.planned:
            chief["state"] = "active"
        elif reviewing or any(n["state"] in _BUSY for n in subtasks):
            chief["state"] = "idle"
        else:
            chief["state"] = "active"
        if "reviewer" in self.nodes:  # last, after the subtasks, as the page lays it out
            self.nodes["reviewer"] = self.nodes.pop("reviewer")
        if "synthesis" in self.nodes:
            self.nodes["synthesis"] = self.nodes.pop("synthesis")


def build_tree(
    events: Iterable[dict[str, Any]], *, models: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The tree of a run from its events, in order. `models` maps a role to the model it runs on, for a node
    whose events do not name one (the chief and the reviewer take no steps)."""
    t = _Tree(models or {})
    for e in events:
        if isinstance(e, dict):
            t.feed(e)
    return t.tree()


def run_tree(state_dir: Path, run_id: str, *, models: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The tree of run `run_id` under `state_dir/runs`, with `next`: its event log's line count, which a
    follower passes back as `since`. RunNotFound for an id that names no run directory."""
    if not _SAFE.fullmatch(run_id) or run_id in (".", ".."):
        raise RunNotFound(f"no run '{run_id}'")
    d = state_dir / "runs" / run_id
    if not d.is_dir():
        raise RunNotFound(f"no run '{run_id}'")
    events, upto = read_events(d / "events.jsonl", 0)
    tree = build_tree(events, models=models)
    tree["run_id"], tree["next"] = run_id, upto
    return tree
