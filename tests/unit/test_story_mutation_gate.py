"""The story's mutation gate: the Mutation gate section of AGENTS.md, run once per story over the built stack
and before it is published. Survivors go to the architect, never the coder: it drops the ones that do not
matter and may ask for test cases, which one last layer adds. On FakeEngine, a NullBoard and a recorded gh,
with a shell script standing in for mutmut."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from airlie.config.schema import StorySpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_layer_checks_flow import FEATURE, RED, calc_repo
from tests.unit.test_story_layer_checks_flow import LAYERS as ONE_LAYER
from tests.unit.test_story_pipeline import (
    B1,
    B2,
    LAYERS,
    NOTE,
    STORY,
    FailingGh,
    RecordedGh,
    agents_md,
    commit_file,
    fenced,
    happy_engine,
    kinds,
    pipeline,
)
from tests.unit.test_studio_orchestrator import APPROVE, DONE, RUN, WRITE_PASS

SURVIVOR = "airlie.calc.mul__mutmut_3"
REPORT = f"### Mutation testing (changed)\n- {SURVIVOR}: survived\n- airlie.calc.log__mutmut_1: survived"
NOISE = "mutmut progress noise"
HARDEN = fenced(
    {
        "harden": [
            {
                "test_file": "tests/test_profile.py",
                "cases": [
                    {
                        "name": "test_profile_shows_the_avatar_twice",
                        "given": "a stored avatar",
                        "expect": "both views show it",
                        "kills": [SURVIVOR],
                    }
                ],
            }
        ],
        "ignored": [{"mutant": "airlie.calc.log__mutmut_1", "why": "changes a log message only"}],
    }
)
ALL_IGNORED = fenced(
    {
        "harden": [],
        "ignored": [
            {"mutant": SURVIVOR, "why": "equivalent: x * 1 and x // 1 agree on ints"},
            {"mutant": "airlie.calc.log__mutmut_1", "why": "changes a log message only"},
        ],
    }
)
UNUSABLE = FakeReply(text="These survivors look fine to me.")
HARDENED = FakeReply(
    tool_calls=[ToolCall("h", "write_file", json.dumps({"path": "hardened.txt", "content": "kills it\n"}))]
)
TWO_LAYERS_CODER = [WRITE_PASS, RUN, DONE, NOTE, DONE]


def tools_dir(tmp_path: Path, *exits: str) -> Path:
    """A script outside the repository standing in for the mutation tool: each call logs its argument, its
    directory and whether GH_TOKEN reached it, writes REPORT to the file it is given, prints NOISE, and exits
    with the next of `exits` ("sleep" sleeps)."""
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
        f"echo '{NOISE}'\n"
        f"printf '%s\\n' '{REPORT}' > \"$2\"\n"
        'exit "$what"\n'
    )
    script.chmod(0o755)
    return tools


def mutation_section(tmp_path: Path) -> str:
    return f"\n## Mutation gate\n\n```sh\n{tmp_path / 'mut' / 'mutate.sh'} {{base}} {{report}}\n```\n"


def mutation_repo(tmp_path: Path, *exits: str, section: bool = True) -> Path:
    """repo_with_marker_tests with an AGENTS.md whose Test gate is ./check.sh and whose Mutation gate runs
    tools_dir's script."""
    tools_dir(tmp_path, *exits)
    repo = repo_with_marker_tests(tmp_path)
    text = agents_md("./check.sh") + (mutation_section(tmp_path) if section else "")
    commit_file(repo, "AGENTS.md", text)
    return repo


def calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "mut" / "log"
    return [line.split() for line in log.read_text().splitlines()] if log.exists() else []


def order(st: StoryState) -> list[str]:
    """The story's events that place the mutation gate, in order: layers built, the gate, the publish."""
    out = []
    for line in (st.path / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "layer_built":
            out.append(f"layer_built {e['layer']}")
        elif e["kind"] in ("published", "mutation_review") or e["kind"].startswith("mutation_gate_"):
            out.append(e["kind"])
    return out


def prompts(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def engine_with(architect: list[FakeReply], *, hardening: bool = False) -> FakeEngine:
    """happy_engine whose architect replies `architect` after the layer plan; `hardening` scripts the coder
    and the reviewer for a third layer."""
    if not hardening:
        return happy_engine(architect=[LAYERS, *architect])
    return happy_engine(
        architect=[LAYERS, *architect],
        coder=[*TWO_LAYERS_CODER, HARDENED, DONE],
        reviewer=[APPROVE, APPROVE, APPROVE],
    )


async def test_the_mutation_gate_runs_once_over_the_built_stack_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_the_persons_own")
    repo = mutation_repo(tmp_path, "0")
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert order(st) == [
        "layer_built 01",
        "layer_built 02",
        "mutation_gate_start",
        "mutation_gate_passed",
        "published",
    ]
    # {base} is the story's base commit; it ran in a worktree of the top layer, with no credential
    [(base, cwd, token)] = calls(tmp_path)
    assert st.base is not None and base == st.base["sha"]
    assert cwd.endswith("/mutate") and token == "no-token" and not Path(cwd).exists()
    assert st.mutation is not None and st.mutation["status"] == "passed"
    assert st.mutation["head"] == st.built["02"]["sha"] and st.mutation_review is None
    assert len(prompts(engine, "architect")) == 1  # the layer plan only
    for n in ("01", "02"):  # every PR of the stack says how the story's mutation gate went
        assert "Mutation gate: passes (" in (st.path / f"pr-{n}.md").read_text()


async def test_without_a_mutation_gate_section_the_stage_is_skipped_and_says_so(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, section=False)
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review" and calls(tmp_path) == []
    [skipped] = kinds(st.path / "events.jsonl", "mutation_gate_skipped")
    assert "no Mutation gate section" in skipped["reason"]
    assert kinds(st.path / "events.jsonl", "mutation_gate_start") == []
    assert st.mutation is not None and st.mutation["status"] == "skipped"
    assert "Mutation gate: skipped" in (st.path / "pr-02.md").read_text()


async def test_a_persons_test_command_keeps_the_repositorys_mutation_gate(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "0")
    st = await pipeline(tmp_path, repo, happy_engine()).run(text_card("avatars"))  # --test-cmd ./check.sh
    assert st.blocked is None and len(calls(tmp_path)) == 1


async def test_survivors_go_to_the_architect_whose_cases_become_one_last_layer_and_the_gate_runs_once(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1", "1")
    engine = engine_with([HARDEN], hardening=True)
    gh = RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert order(st) == [
        "layer_built 01",
        "layer_built 02",
        "mutation_gate_start",
        "mutation_gate_failed",
        "mutation_review",
        "layer_built 03",
        "published",
    ]
    assert len(calls(tmp_path)) == 1  # not run again after the hardening layer
    # the architect read the report (not the noise), the story's diff and its layers
    review = prompts(engine, "architect")[1]
    assert SURVIVOR in review and NOISE not in review and "diff --git" in review
    assert "profile.txt" in review and "1. Storage" in review
    # the coder sees the architect's cases (each names what it kills), never the gate's report
    assert not any("### Mutation testing" in p or "log__mutmut_1" in p for p in prompts(engine, "coder"))
    hardening = [p for p in prompts(engine, "coder") if "test_profile_shows_the_avatar_twice" in p]
    assert len(hardening) == 1 and "Change test files only" in hardening[0]
    # the third layer is stacked on top, built like the others
    b3 = st.built["03"]["branch"]
    assert b3.endswith("-03-harden-tests") and git(repo, "show", f"{b3}:hardened.txt") == "kills it"
    assert ["gh", "stack", "init", "--base", git(repo, "branch", "--show-current"), B1, B2, b3] in gh.calls
    [verdict] = kinds(st.path / "events.jsonl", "mutation_review")
    assert (verdict["harden"], verdict["ignored"], verdict["layer"]) == (1, 1, "03")
    assert verdict["test_files"] == ["tests/test_profile.py"]
    first, top = (st.path / "pr-01.md").read_text(), (st.path / "pr-03.md").read_text()
    assert "asked for 1 test case(s) in layer 03 (Harden the tests) and ignored 1" in first
    assert "changes a log message only" in top and "changes a log message only" not in first
    assert "mutation gate: mutants survived" in (st.path / "notes.md").read_text()


async def test_when_the_architect_ignores_every_survivor_the_stack_is_published_with_its_reasons(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1")
    st = await pipeline(tmp_path, repo, engine_with([ALL_IGNORED]), test_cmd=None).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review" and set(st.built) == {"01", "02"}
    assert st.layers is not None and len(st.layers) == 2
    [verdict] = kinds(st.path / "events.jsonl", "mutation_review")
    assert (verdict["harden"], verdict["ignored"], verdict["layer"]) == (0, 2, None)
    top = (st.path / "pr-02.md").read_text()
    assert "### Surviving mutants the architect ignored" in top
    assert "equivalent: x * 1 and x // 1 agree on ints" in top and "ignored 2 (listed below)" in top


async def test_an_unusable_review_twice_blocks_the_story_with_the_summary_and_a_resume_asks_again(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "1", "0")
    card = text_card("avatars")
    engine = engine_with([UNUSABLE, UNUSABLE])
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(card)
    assert st.blocked is not None and st.prs == [] and len(prompts(engine, "architect")) == 3
    assert "did not produce a usable mutation review" in st.blocked and SURVIVOR in st.blocked
    assert SURVIVOR in (st.path / "notes.md").read_text()
    # a resume does not run the gate again for the same stack: it asks the architect again
    again = FakeEngine({"architect": [ALL_IGNORED]})
    st = await pipeline(tmp_path, repo, again, test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review" and len(calls(tmp_path)) == 1
    assert len(prompts(again, "architect")) == 1


async def test_a_resume_does_not_rerun_a_mutation_gate_that_passed_for_the_stack(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "0", "1")
    card = text_card("avatars")
    st = await pipeline(tmp_path, repo, happy_engine(), FailingGh("push rejected"), test_cmd=None).run(card)
    assert st.stage == "build" and st.blocked is not None and len(calls(tmp_path)) == 1
    idle = FakeEngine()
    st = await pipeline(tmp_path, repo, idle, RecordedGh(), test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review" and idle.requests == []
    assert len(calls(tmp_path)) == 1  # the plan's next exit (1) was never reached
    assert len(kinds(st.path / "events.jsonl", "mutation_gate_start")) == 1
    assert "Mutation gate: passes (" in (st.path / "pr-02.md").read_text()


async def test_a_resume_after_a_failed_hardening_layer_rebuilds_only_that_layer(tmp_path: Path) -> None:
    repo = mutation_repo(tmp_path, "1", "0")
    card = text_card("avatars")
    # the hardening layer changes nothing: the story stops there, with the review kept
    lazy = happy_engine(architect=[LAYERS, HARDEN], reviewer=[APPROVE, APPROVE, APPROVE])
    st = await pipeline(tmp_path, repo, lazy, test_cmd=None).run(card)
    assert st.blocked is not None and "layer 03 (Harden the tests) changed nothing" in st.blocked
    assert st.mutation_review is not None and set(st.built) == {"01", "02"}
    again = FakeEngine({"coder": [HARDENED, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again, test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review" and "03" in st.built
    assert len(calls(tmp_path)) == 1 and prompts(again, "architect") == []


async def test_a_mutation_gate_that_times_out_stops_the_story_and_the_timeout_is_config(
    tmp_path: Path,
) -> None:
    repo = mutation_repo(tmp_path, "sleep")
    pipe = pipeline(tmp_path, repo, happy_engine(), test_cmd=None, story={"mutation_timeout_s": 1})
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None and "did not finish" in st.blocked and "timed out after 1s" in st.blocked
    assert kinds(st.path / "events.jsonl", "mutation_review") == []


def test_the_mutation_timeout_defaults_to_an_hour() -> None:
    assert StorySpec().mutation_timeout_s == 3600


async def test_a_mutation_gate_section_that_cannot_run_stops_the_story_before_any_model(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    commit_file(repo, "AGENTS.md", agents_md("./check.sh") + "\n## Mutation gate\n\n```sh\n# none yet\n```\n")
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.blocked is not None and engine.requests == []
    assert "Mutation gate section of its AGENTS.md lists no command" in st.blocked


# ---- the hardening layer's red check: against the story's base ----

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q"
HARDEN_MUL = fenced(
    {
        "harden": [
            {
                "test_file": "tests/test_calc_more.py",
                "cases": [{"name": "test_mul_by_zero", "given": "5 and 0", "expect": "0", "kills": SURVIVOR}],
            }
        ],
        "ignored": [],
    }
)


def write(path: str, content: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(path, "write_file", json.dumps({"path": path, "content": content}))]
    )


async def harden_calc(tmp_path: Path, hardening: list[FakeReply]) -> StoryState:
    """One layer that adds calc.mul with a test, a mutation gate that finds survivors, and the architect's
    case for them, which the coder writes with `hardening`."""
    tools_dir(tmp_path, "1")
    repo = calc_repo(tmp_path)
    commit_file(repo, "AGENTS.md", agents_md(PYTEST) + mutation_section(tmp_path))
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [ONE_LAYER, HARDEN_MUL],
            "coder": [FEATURE, RED, DONE, *hardening],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    return await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("multiply"))


async def test_the_hardening_layers_tests_pass_the_red_check_against_the_storys_base(tmp_path: Path) -> None:
    # the new test passes on the hardening layer's own base (mul is there), so a red check against that base
    # would call it vacuous; with the whole story undone mul is gone, and it fails as it should
    more = write(
        "tests/test_calc_more.py",
        "from calc import mul\n\n\ndef test_mul_by_zero():\n    assert mul(5, 0) == 0\n",
    )
    st = await harden_calc(tmp_path, [more, DONE])
    assert st.blocked is None and st.stage == "review" and "02" in st.built
    events = st.path / "events.jsonl"
    assert kinds(events, "vacuous_test") == []
    [red] = kinds(events, "test_checks_red_base")
    assert st.base is not None and (red["layer"], red["base"]) == ("02", st.base["sha"])
    assert [e["layer"] for e in kinds(events, "test_checks_passed")] == ["01", "02"]


async def test_a_hardening_test_that_passes_without_the_story_is_still_a_finding(tmp_path: Path) -> None:
    weak = write(
        "tests/test_calc_more.py",
        "from calc import add\n\n\ndef test_add_zero():\n    assert add(5, 0) == 5\n",
    )
    st = await harden_calc(tmp_path, [weak, DONE, DONE])
    assert st.blocked is not None and "layer 02 (Harden the tests)" in st.blocked
    found = kinds(st.path / "events.jsonl", "vacuous_test")
    assert [(e["layer"], e["test"]) for e in found] == [("02", "tests/test_calc_more.py::test_add_zero")] * 2
