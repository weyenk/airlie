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

import pytest

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.engine.protocol import OnToken
from airlie.engine.types import GenRequest, GenResult
from airlie.flow.composer import Registry
from airlie.flow.prompt_files import parse_draft_prompts
from airlie.flow.spec import WorkflowSpec, check_workflows
from airlie.flow.store import MemoryStore
from airlie.flow.team import Handback, _result, team_runner
from airlie.story.board import text_card
from airlie.story.flow import story_runners
from airlie.story.model import StoryError
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
    first_ask = next(r for r in engine.requests if r.role == "coder" and "[w1]" in r.messages[1].content)
    assert first_ask.messages[1].content.startswith("Goal:\nDo write w1.txt [w1].\n\n")  # the worker text
    assert re.search(r"w1\.txt \|\s+1 \+", third)  # an applied change comes with its --stat
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


async def test_with_no_cap_set_the_default_five_run_and_the_sixth_is_not_started(tmp_path: Path) -> None:
    """Review Focus 5 on the default cap: six assignments and no `workers` on the node; the first five run,
    the supervisor is told the sixth was not started, and the node finishes."""
    ids = ("a", "b", "c", "d", "e", "f")
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {
            "architect": [
                assign(*ids),
                *[FakeReply(text=f"{i} in") for i in ids[:4]],
                fenced({"summary": "five"}),
            ]
        },
        worker="product",
        workers={i: [FakeReply(text=f"answer {i}")] for i in ids[:5]},
        order=list(ids[:5]),
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "six questions"}), "ask")
    out = await team_runner(team("product"), ctx, asker=pipe.asker, st=st)
    assert [r["id"] for r in out["results"]] == list(ids[:5])
    assert out["summary"] == {"summary": "five"}
    assert "Not started, over the team's cap: f." in handbacks(engine)[0]
    assert "at most 5 workers" in briefs_to(engine, "architect")[0]
    asked = briefs_to(engine, "product")
    assert len(asked) == 5 and not any("[f]" in b for b in asked)


def test_the_spec_refuses_a_team_cap_below_one() -> None:
    from airlie.flow.spec import WorkflowSpec, check_workflows

    for workers in (0, -2):
        w = WorkflowSpec(inputs=["card"], nodes={"t": team("product", workers=workers)})
        assert "workflow w: node t: team workers must be at least 1" in check_workflows({"w": w})
    assert (
        check_workflows({"w": WorkflowSpec(inputs=["card"], nodes={"t": team("product", workers=1)})}) == []
    )


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


async def test_a_worker_that_raises_is_a_failed_hand_back_and_the_node_finishes(tmp_path: Path) -> None:
    """A worker whose turn fails (here its engine has nothing to say) hands back failed with the reason; the
    supervisor is told, and its synthesis is still the node's."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {"architect": [assign("a", "b"), FakeReply(text="a in"), fenced({"summary": "half"})]},
        worker="product",
        workers={"a": [FakeReply(text="answer a")], "b": []},
        order=["a", "b"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "two questions"}), "ask")
    out = await team_runner(team("product"), ctx, asker=pipe.asker, st=st)
    [a, b] = out["results"]
    assert a == {"id": "a", "ok": True, "text": "answer a"}
    assert b["id"] == "b" and b["ok"] is False and b["text"]
    _, failed = handbacks(engine)
    assert "Worker b has handed back" in failed and "Status: failed" in failed and b["text"] in failed
    assert out["summary"] == {"summary": "half"}


def follow_ups(engine: FakeEngine) -> list[str]:
    """What the supervisor was sent after its opening (its last request holds it all)."""
    last = [r for r in engine.requests if r.role == "architect"][-1]
    return [m.content for m in last.messages if m.role == "user"][1:]


async def test_assignments_that_never_parse_are_asked_again_then_block_with_the_reason(
    tmp_path: Path,
) -> None:
    """A supervisor reply with no usable assignments goes back with the reason and the assignment rule; after
    the third, the node stops (on_fail: block) with that reason, and no worker was started."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {"architect": [FakeReply(text="no plan"), fenced({"assignments": []}), FakeReply(text="still none")]},
        worker="product",
        workers={"a": []},
        order=["a"],
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "q"}), "ask")
    with pytest.raises(StoryError, match="did not produce usable assignments: the reply has no JSON object"):
        await team_runner(team("product"), ctx, asker=pipe.asker, st=st)
    sent = follow_ups(engine)
    assert len(sent) == 2 and sent[0].startswith("the reply has no JSON object")
    assert sent[1].startswith("field 'assignments' must be a list of at least one assignment")
    assert all("You lead a team of at most 5 workers." in s for s in sent)
    assert not [r for r in engine.requests if r.role == "product"]


async def test_a_writer_team_whose_supervisor_checkout_never_opened_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer team needs the supervisor's checkout to apply hand-backs on: if it never opened, the node
    fails naming itself, and no worker is started."""
    repo = repo_with_marker_tests(tmp_path)
    engine = TeamEngine(
        {"architect": [assign("w1")]}, worker="coder", workers={"w1": writes("A", "a\n", "a")}, order=["w1"]
    )
    pipe = pipeline(tmp_path, repo, engine)
    conversation = pipe.asker.conversation

    def unprepared(*args: Any, writes: Any = None, **kwargs: Any) -> Any:
        return conversation(*args, **kwargs)  # its checkout is never reported

    monkeypatch.setattr(pipe.asker, "conversation", unprepared)
    st = bare_state(tmp_path, repo)
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "q"}), "split")
    with pytest.raises(StoryError, match="team node 'split': the supervisor's checkout never opened"):
        await team_runner(team("coder", tools=["write_file"]), ctx, asker=pipe.asker, st=st)
    assert not [r for r in engine.requests if r.role == "coder"]


def test_a_hand_back_shows_the_stat_and_the_diff_cut_to_the_cap() -> None:
    """The supervisor is shown an applied change's --stat and its diff up to the tools' output cap, with how
    much was cut (the whole change is in its checkout)."""
    back = Handback("w1", True, "wrote it", diff="x" * 50, stat=" A | 1 +")
    shown = _result(back, True, 20)
    assert "wrote it" in shown and " A | 1 +" in shown
    assert "x" * 20 + "\n… [truncated 30 chars]" in shown and "x" * 21 not in shown
    assert _result(back, True, 100).count("x") == 50


def test_a_team_worker_prompt_file_may_read_its_assignment() -> None:
    """A team's `worker: prompt:<id>` file may name `assignment` beside the node's inputs; its `prompt` file
    may not."""
    raw = "---\nkind: rule\nroles: [coder]\nfields: [card, assignment]\nwhy: w\n---\n{card}: {assignment}\n"
    prompts = parse_draft_prompts({"work": raw})
    node = {
        "type": "team",
        "role": {"supervisor": "architect", "worker": "coder"},
        "prompt": "Split {card}.",
        "worker": "prompt:work",
        "inputs": ["card"],
        "outputs": {"results": "list"},
    }
    ok = WorkflowSpec.model_validate({"inputs": ["card"], "nodes": {"t": node}})
    assert check_workflows({"w": ok}, prompts=prompts) == []
    bad = WorkflowSpec.model_validate({"inputs": ["card"], "nodes": {"t": {**node, "prompt": "prompt:work"}}})
    assert check_workflows({"w": bad}, prompts=prompts) == [
        "workflow w: node t: prompt file 'work' reads 'assignment', which is not an input of the node"
    ]
