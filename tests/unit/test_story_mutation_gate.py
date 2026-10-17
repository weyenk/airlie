"""The story's mutation gate: the Mutation gate section of AGENTS.md, run once per story in the top layer's
worktree after its test gate passes and before the stack is published. A failure goes back to the top layer's
coder once; a second one blocks the story. On FakeEngine, a NullBoard and a recorded gh, with a shell script
standing in for mutmut."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from airlie.config.schema import StorySpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import (
    B2,
    NOTE,
    FailingGh,
    RecordedGh,
    agents_md,
    commit_file,
    happy_engine,
    kinds,
    pipeline,
)
from tests.unit.test_studio_orchestrator import DONE, RUN, WRITE_PASS

SURVIVOR = "- airlie.calc.mul__mutmut_3: survived"
STRONGER = FakeReply(
    tool_calls=[ToolCall("s", "write_file", json.dumps({"path": "stronger.txt", "content": "kills it\n"}))]
)
BREAKS_GATE = FakeReply(
    tool_calls=[
        ToolCall("b", "write_file", json.dumps({"path": "check.sh", "content": "#!/bin/sh\nexit 1\n"}))
    ]
)


def mutation_repo(tmp_path: Path, *exits: str, section: bool = True) -> Path:
    """repo_with_marker_tests with an AGENTS.md whose Test gate is ./check.sh and whose Mutation gate runs a
    script outside the repository: each call logs its argument, its directory and whether GH_TOKEN reached
    it, prints a summary with a surviving mutant, and exits with the next of `exits` ("sleep" sleeps)."""
    tools = tmp_path / "mut"
    tools.mkdir()
    (tools / "plan").write_text("".join(f"{e}\n" for e in exits))
    script = tools / "mutate.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1 $(pwd) ${{GH_TOKEN:-no-token}}" >> {tools}/log\n'
        f"n=$(wc -l < {tools}/log)\n"
        f'what=$(sed -n "${{n}}p" {tools}/plan)\n'
        'if [ "$what" = sleep ]; then sleep 30; fi\n'
        f'echo "### Mutation testing (changed)"; echo "{SURVIVOR}"\n'
        'exit "$what"\n'
    )
    script.chmod(0o755)
    repo = repo_with_marker_tests(tmp_path)
    text = agents_md("./check.sh")
    if section:
        text += f"\n## Mutation gate\n\n```sh\n{script} {{base}}\n```\n"
    commit_file(repo, "AGENTS.md", text)
    return repo


def calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "mut" / "log"
    return [line.split() for line in log.read_text().splitlines()] if log.exists() else []


def order(st: StoryState) -> list[str]:
    """The story's events that place the mutation gate, in order: layers built, the gate, the publish."""
    wanted = {"layer_built", "published"}
    out = []
    for line in (st.path / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["kind"] in wanted:
            out.append(f"{e['kind']} {e.get('layer', '')}".strip())
        elif e["kind"].startswith("mutation_gate_"):
            out.append(f"{e['kind']} {e['layer']}")
    return out


async def test_the_mutation_gate_runs_once_in_the_top_layer_after_its_test_gate_and_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_the_persons_own")
    repo = mutation_repo(tmp_path, "0")
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert order(st) == [
        "layer_built 01",
        "mutation_gate_start 02",
        "mutation_gate_passed 02",
        "layer_built 02",
        "published",
    ]
    # {base} is the story's base commit, it ran in the top layer's worktree, with no credential
    [(base, cwd, token)] = calls(tmp_path)
    assert st.base is not None and base == st.base["sha"]
    assert cwd.endswith("/studio") and Path(cwd).resolve() != repo.resolve()
    assert token == "no-token"
    assert st.mutation is not None and st.mutation["status"] == "passed" and st.mutation["attempts"] == 1
    assert st.mutation["head"] == st.built["02"]["sha"]
    for n in ("01", "02"):  # every PR of the stack says how the story's mutation gate went
        assert "Mutation gate: passes on the top layer" in (st.path / f"pr-{n}.md").read_text()


async def test_without_a_mutation_gate_section_the_stage_is_skipped_and_says_so(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, section=False)
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review" and calls(tmp_path) == []
    [skipped] = kinds(st.path / "events.jsonl", "mutation_gate_skipped")
    assert skipped["layer"] == "02" and "no Mutation gate section" in skipped["reason"]
    assert kinds(st.path / "events.jsonl", "mutation_gate_start") == []
    assert st.mutation is not None and st.mutation["status"] == "skipped"
    assert "Mutation gate: skipped" in (st.path / "pr-02.md").read_text()


async def test_a_persons_test_command_keeps_the_repositorys_mutation_gate(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "0")
    st = await pipeline(tmp_path, repo, happy_engine()).run(text_card("avatars"))  # --test-cmd ./check.sh
    assert st.blocked is None and len(calls(tmp_path)) == 1


async def test_a_failed_mutation_gate_goes_back_to_the_top_coder_once_and_a_retry_that_kills_them_passes(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1", "0")
    engine = happy_engine(coder=[WRITE_PASS, RUN, DONE, NOTE, DONE, STRONGER, DONE])
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert order(st) == [
        "layer_built 01",
        "mutation_gate_start 02",
        "mutation_gate_failed 02",
        "mutation_gate_retry 02",
        "mutation_gate_start 02",
        "mutation_gate_passed 02",
        "layer_built 02",
        "published",
    ]
    # the coder was told which mutants survived
    retry = [
        r.messages[-1].content
        for r in engine.requests
        if r.role == "coder" and SURVIVOR in r.messages[-1].content
    ]
    assert len(retry) == 1 and "mutation" in retry[0].lower()
    [failed] = kinds(st.path / "events.jsonl", "mutation_gate_failed")
    assert SURVIVOR in failed["summary"] and failed["attempt"] == 1
    # the layer's checks reran after the retry (this gate runs no pytest, so they say they are skipped)
    assert [e["layer"] for e in kinds(st.path / "events.jsonl", "test_checks_skipped")] == ["01", "02", "02"]
    # what the retry wrote is in the top layer's commit, and the PRs say it passed after one retry
    assert git(repo, "show", f"{B2}:stronger.txt") == "kills it"
    assert st.mutation is not None and st.mutation["attempts"] == 2
    assert "passes on the top layer after one retry" in (st.path / "pr-01.md").read_text()


async def test_a_second_mutation_failure_blocks_the_story_with_the_summary_in_the_note(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1", "1")
    engine = happy_engine(coder=[WRITE_PASS, RUN, DONE, NOTE, DONE, STRONGER, DONE])
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is not None and st.stage == "layers" and "02" not in st.built and st.prs == []
    assert (
        "layer 02 (Profile)" in st.blocked
        and "mutation gate" in st.blocked
        and "after one retry" in st.blocked
    )
    assert SURVIVOR in st.blocked
    notes = (st.path / "notes.md").read_text()
    assert SURVIVOR in notes and "moved to blocked" in notes
    assert len(kinds(st.path / "events.jsonl", "mutation_gate_failed")) == 2
    assert kinds(st.path / "events.jsonl", "mutation_gate_passed") == []
    assert len(calls(tmp_path)) == 2


async def test_a_mutation_retry_that_breaks_the_test_gate_blocks_the_story_without_a_second_mutation_run(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1", "0")
    engine = happy_engine(coder=[WRITE_PASS, RUN, DONE, NOTE, DONE, BREAKS_GATE, DONE])
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is not None and "02" not in st.built
    assert "`./check.sh` fails" in st.blocked and "mutation gate" in st.blocked
    assert len(calls(tmp_path)) == 1


async def test_a_mutation_gate_that_times_out_fails_and_the_timeout_is_config(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "sleep", "0")
    engine = happy_engine(coder=[WRITE_PASS, RUN, DONE, NOTE, DONE, STRONGER, DONE])
    pipe = pipeline(tmp_path, repo, engine, test_cmd=None, story={"mutation_timeout_s": 1})
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is None
    [failed] = kinds(st.path / "events.jsonl", "mutation_gate_failed")
    assert "timed out after 1s" in failed["summary"]


def test_the_mutation_timeout_defaults_to_an_hour() -> None:
    assert StorySpec().mutation_timeout_s == 3600


async def test_a_resume_does_not_rerun_a_mutation_gate_that_passed_for_the_top_layer(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "0", "1")
    card = text_card("avatars")
    st = await pipeline(tmp_path, repo, happy_engine(), FailingGh("push rejected"), test_cmd=None).run(card)
    assert st.stage == "build" and st.blocked is not None and len(calls(tmp_path)) == 1
    idle = FakeEngine()
    st = await pipeline(tmp_path, repo, idle, RecordedGh(), test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review" and idle.requests == []
    assert len(calls(tmp_path)) == 1  # the plan's next exit (1) was never reached
    assert len(kinds(st.path / "events.jsonl", "mutation_gate_start")) == 1
    assert "Mutation gate: passes on the top layer" in (st.path / "pr-02.md").read_text()


async def test_a_mutation_gate_section_that_cannot_run_stops_the_story_before_any_model(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    commit_file(repo, "AGENTS.md", agents_md("./check.sh") + "\n## Mutation gate\n\n```sh\n# none yet\n```\n")
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is not None and engine.requests == []
    assert "Mutation gate section of its AGENTS.md lists no command" in st.blocked
