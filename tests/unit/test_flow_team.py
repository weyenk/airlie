"""The team node (`team_runner`): the supervisor delegates, the workers hand back, the supervisor assembles as
each finishes (nodes-are-config design, "The team node", D9, D22), through the real asker on the fake
engine."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.engine.protocol import OnToken
from airlie.engine.types import GenRequest, GenResult
from airlie.flow.composer import Registry
from airlie.flow.spec import WorkflowSpec, check_workflows
from airlie.flow.store import MemoryStore
from airlie.flow.team import team_runner
from airlie.story.board import text_card
from airlie.story.flow import story_runners
from tests.unit.git_helpers import git
from tests.unit.test_story_flow import bare_state, briefs_to, run_context, v2
from tests.unit.test_story_pipeline import fenced, pipeline, repo_with_marker_tests
from tests.unit.test_studio_orchestrator import APPROVE


class TeamEngine(FakeEngine):
    """The fake engine with the workers' replies kept by the assignment id their brief names (`[id]`). A
    worker's first reply waits until the worker before it in `order` has handed back to the supervisor, so
    they finish in that order while the rest are still working."""

    def __init__(
        self,
        scripts: dict[str, list[FakeReply]],
        *,
        worker: str,
        workers: dict[str, list[FakeReply]],
        order: list[str],
        supervisor: str = "architect",
    ) -> None:
        super().__init__(scripts)
        self.worker, self.supervisor, self.order = worker, supervisor, order
        self.by_id = {k: deque(v) for k, v in workers.items()}
        self.gates = {k: asyncio.Event() for k in workers}
        self.gates[order[0]].set()

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        said = "\n".join(m.content for m in req.messages)
        if req.role == self.supervisor:
            for before, after in zip(self.order, self.order[1:], strict=False):
                if f"Worker {before} has handed back" in said:
                    self.gates[after].set()
        if req.role == self.worker:
            id = next(k for k in self.by_id if f"[{k}]" in said)
            await self.gates[id].wait()
            self._scripts[self.worker] = deque([self.by_id[id].popleft()])
        return await super().generate(req, on_token)


def assign(*ids: str, task: str = "write {id}.txt") -> FakeReply:
    return fenced({"assignments": [{"id": i, "task": task.format(id=i), "files": [f"{i}.txt"]} for i in ids]})


def writes(path: str, content: str, account: str) -> list[FakeReply]:
    """A writing worker's turn: one file written, then its account."""
    call = ToolCall("w", "write_file", json.dumps({"path": path, "content": content}))
    return [FakeReply(tool_calls=[call]), FakeReply(text=account)]


SUMMARY = {"summary": {"format": "json", "fields": {"summary": "text"}}}
WORKER = "Do {assignment.task} [{assignment.id}]."


def team(worker: str, *, tools: list[str] | None = None, workers: int | None = None) -> Any:
    return v2(
        type="team",
        role={"supervisor": "architect", "worker": worker},
        prompt="Split this: {card}.",
        worker=WORKER,
        inputs=["card"],
        outputs={"results": "list", **SUMMARY, "branch": "ref:branch"},
        tools=tools,
        workers=workers,
        steps=4,
    )


def handbacks(engine: FakeEngine) -> list[str]:
    """Each hand-back the supervisor was sent, in the order it was sent (its last request has them all)."""
    last = [r for r in engine.requests if r.role == "architect"][-1]
    return [m.content for m in last.messages if "has handed back" in m.content]


def handed(engine: FakeEngine) -> list[str]:
    return [re.search(r"Worker (\w+) has handed back", h).group(1) for h in handbacks(engine)]  # type: ignore[union-attr]


async def test_three_workers_hand_back_in_finish_order_and_the_synthesis_is_stored(tmp_path: Path) -> None:
    """Three assignments, three writing workers at once, each on its own checkout seeing only its chunk; the
    supervisor gets a turn as each hands back, in the order they finish (the others still working), each
    diff committed on its checkout; its reply to the last is the synthesis, and the assembled work is a
    branch."""
    repo = repo_with_marker_tests(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    engine = TeamEngine(
        {
            "architect": [
                assign("w1", "w2", "w3"),
                FakeReply(text="w2 in"),
                FakeReply(text="w3 in"),
                fenced({"summary": "all three"}),
            ],
            "reviewer": [APPROVE] * 12,
        },
        worker="coder",
        workers={i: writes(f"{i}.txt", f"{i}\n", f"wrote {i}") for i in ("w1", "w2", "w3")},
        order=["w2", "w3", "w1"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "three files"}), "split")
    out = await team_runner(team("coder", tools=["write_file"]), ctx, asker=pipe.asker, st=st)
    assert out["results"] == [
        {"id": "w2", "ok": True, "text": "wrote w2"},
        {"id": "w3", "ok": True, "text": "wrote w3"},
        {"id": "w1", "ok": True, "text": "wrote w1"},
    ]
    assert out["summary"] == {"summary": "all three"}
    assert handed(engine) == ["w2", "w3", "w1"]
    first, second, third = handbacks(engine)
    assert "Status: applied" in first and "Still working: w1, w3." in first and "+w2" in first
    assert "Still working: w1." in second and "Still working: none." in third
    assert "Reply with one ```json block with fields: summary (text)." in third
    opening = briefs_to(engine, "architect")[0]
    assert "Split this: three files." in opening and "at most 5 workers" in opening
    for i in ("w1", "w2", "w3"):  # each worker sees its own chunk only
        mine = [b for b in briefs_to(engine, "coder") if f"[{i}]" in b]
        assert mine and all(f"Do write {i}.txt" in b for b in mine)
        assert not any(f"[{o}]" in b for b in mine for o in {"w1", "w2", "w3"} - {i})
    branch = out["branch"]
    assert [git(repo, "show", f"{branch}:{i}.txt") for i in ("w1", "w2", "w3")] == ["w1", "w2", "w3"]
    subjects = git(repo, "log", "--format=%s", f"{base}..{branch}").splitlines()
    assert [s for s in subjects if s.startswith("team:")] == ["team: w1", "team: w3", "team: w2"]


async def test_a_cap_of_two_runs_the_first_two_and_names_the_rest_as_not_started(tmp_path: Path) -> None:
    """Five assignments on a cap of two: the first two run, and the supervisor's next turn names the three
    left as not started; nobody works on them."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {"architect": [assign("a", "b", "c", "d", "e"), FakeReply(text="a in"), fenced({"summary": "two"})]},
        worker="product",
        workers={i: [FakeReply(text=f"answer {i}")] for i in ("a", "b")},
        order=["a", "b"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "five questions"}), "ask")
    out = await team_runner(team("product", workers=2), ctx, asker=pipe.asker, st=st)
    assert [r["id"] for r in out["results"]] == ["a", "b"]
    assert out["summary"] == {"summary": "two"}
    first, second = handbacks(engine)
    assert "Not started, over the team's cap: c, d, e." in first
    assert "Not started, over the team's cap: none." in second
    assert "at most 2 workers" in briefs_to(engine, "architect")[0]
    asked = briefs_to(engine, "product")
    assert len(asked) == 2 and not any(f"[{i}]" in b for b in asked for i in "cde")


async def test_a_diff_that_does_not_apply_is_a_failed_hand_back_and_the_node_still_finishes(
    tmp_path: Path,
) -> None:
    """Two workers write the same new file: the first's diff is applied, the second's does not apply to the
    supervisor's checkout and comes back as a failed hand-back with git's reason; the node finishes, its
    branch holding the first's change only."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {
            "architect": [assign("w1", "w2"), FakeReply(text="w1 in"), fenced({"summary": "one of two"})],
            "reviewer": [APPROVE] * 8,
        },
        worker="coder",
        workers={"w1": writes("PASS", "one\n", "wrote one"), "w2": writes("PASS", "two\n", "wrote two")},
        order=["w1", "w2"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "one file twice"}), "split")
    out = await team_runner(team("coder", tools=["write_file"]), ctx, asker=pipe.asker, st=st)
    [one, two] = out["results"]
    assert one == {"id": "w1", "ok": True, "text": "wrote one"}
    assert two["id"] == "w2" and two["ok"] is False and "PASS" in two["text"]
    _, failed = handbacks(engine)
    assert "Status: failed" in failed and "PASS" in failed
    assert out["summary"] == {"summary": "one of two"}
    assert git(repo, "show", f"{out['branch']}:PASS") == "one"


async def test_a_reader_team_hands_back_answers_and_writes_no_branch(tmp_path: Path) -> None:
    """Workers whose tools only read are asks: each hands back its answer, the supervisor reads them, and the
    node has no branch (none is made in the repository)."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {
            "architect": [
                assign("r1", "r2", task="read {id}"),
                FakeReply(text="r2 in"),
                fenced({"summary": "both read"}),
            ]
        },
        worker="product",
        workers={i: [FakeReply(text=f"answer {i}")] for i in ("r1", "r2")},
        order=["r2", "r1"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    branches = git(repo, "branch", "--list")
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "two reads"}), "survey")
    out = await team_runner(team("product"), ctx, asker=pipe.asker, st=st)
    assert out == {
        "results": [
            {"id": "r2", "ok": True, "text": "answer r2"},
            {"id": "r1", "ok": True, "text": "answer r1"},
        ],
        "summary": {"summary": "both read"},
    }
    first, second = handbacks(engine)
    assert "Status: answered" in first and "answer r2" in first and "answer r1" in second
    assert git(repo, "branch", "--list") == branches


def test_a_team_worker_may_name_its_assignment_and_the_story_runs_teams(tmp_path: Path) -> None:
    """Rule 4 lets a team's `worker` text name `{assignment}` (and only the worker text); the story binds the
    team type to `team_runner`."""
    node = {
        "type": "team",
        "role": {"supervisor": "architect", "worker": "coder"},
        "prompt": "Split {card}.",
        "worker": WORKER,
        "inputs": ["card"],
        "outputs": {"results": "list"},
    }
    ok = WorkflowSpec.model_validate({"inputs": ["card"], "nodes": {"t": node}})
    assert check_workflows({"w": ok}) == []
    bad = WorkflowSpec.model_validate(
        {"inputs": ["card"], "nodes": {"t": {**node, "prompt": "{assignment}"}}}
    )
    assert check_workflows({"w": bad}) == [
        "workflow w: node t: prompt names 'assignment', which is not one of its inputs"
    ]
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, FakeEngine({}))
    runner = story_runners(pipe, bare_state(tmp_path, repo), text_card("x"))["team"]
    assert getattr(runner, "func", None) is team_runner
