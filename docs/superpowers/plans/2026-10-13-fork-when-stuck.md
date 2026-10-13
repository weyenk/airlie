# Fork-when-stuck Topology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second topology, `fork`, where one trunk agent runs until stuck, then forks two children that share its
conversation and working tree; plus a deterministic repo map for every topology; plus `task bench:topology` that
compares `single`, `teams` and `fork` against the right baseline.

**Architecture:** The team seam gains a state snapshot, a stop predicate, a preamble and a nudge; both team
implementations (AutoGen, native) honour them. A new `topology` package holds the `Topology` protocol, the
`ForkOrchestrator`, diversification and the factory; the existing `ParallelAgentTeamOrchestrator` is the `teams`
topology unchanged except for the shared `RunReport` and the preamble. CLI, serve and bench select by
`topology.kind`.

**Tech Stack:** Python 3.12, asyncio, pydantic v2, typer, AutoGen 0.7.5 (`AssistantAgent.save_state/load_state`),
git worktrees, pytest (`FakeEngine`).

**Spec:** `docs/superpowers/specs/2026-10-13-fork-when-stuck-design.md` (decisions: width 2, `hypotheses`
diversifier, default flips on evidence). Deviation from `docs/wiki/build-spec` 01 §2 / 04 §3 is deliberate and
recorded there.

## Global Constraints

- Python 3.12, `uv` only; every user-facing entry is a Taskfile task.
- `ruff` line length 110, `mypy --strict` on `src`; `task check` must stay green after every task.
- Config schema is `extra="forbid"`; every new key gets a default and a one-line description.
- Engine access only through the `Engine` seam; the fork orchestrator makes exactly one extra model call per fork
  point (`diversify: hypotheses`) and none in `temperature` mode.
- No code merging across branches: one branch wins.
- Event names are stable strings: `run_start`, `repo_map`, `branch_start`, `branch_done`, `fork_point`,
  `fork_resolved`, `run_done`.
- Commit after every task with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A child that passes visible tests then breaks them again** must not be declared the winner on the strength of
   the earlier pass: the winner is the child whose *last* `run_tests` passed. Test in Task 6 (`test_resolve_uses_last_run_tests_outcome`).
2. **Untracked files at a fork point** (new test files, new modules) must reach the children: `commit_wip` uses
   `git add -A`. Test in Task 3 (`test_commit_wip_includes_untracked_files`).
3. **A fork point with nothing to commit** (the agent only ran tests) must still fork: `--allow-empty`. Test in Task 3.
4. **Hypotheses output that is empty, too short or unnumbered** must fall back to temperature mode, never raise.
   Test in Task 5 (`test_hypotheses_fallback_on_unparsable_output`).
5. **Repo map on a repo with a non-Python tree, a syntax-error Python file, or a huge tree** must produce a map
   (tree only, file skipped, capped) rather than fail the run. Tests in Task 2.

---

### Task 1: Team seam — state snapshot, stop predicate, preamble, nudge

**Files:**
- Modify: `src/airlie/teams/protocol.py`
- Modify: `src/airlie/teams/native_team.py`
- Modify: `src/airlie/teams/autogen_team.py`
- Test: `tests/unit/test_native_team.py`, `tests/unit/test_autogen_team.py`

**Interfaces:**
- Produces: `StopReason` includes `"fork_point"`; `TeamContext.preamble: str | None`,
  `TeamContext.initial_state: dict[str, Any] | None`, `TeamContext.nudge: str | None`,
  `TeamContext.stop_when: Callable[[StepRecord], bool] | None`; `TeamResult.state: dict[str, Any] | None`,
  `TeamResult.last_test_output: str`; `_goal_message(goal, worktree_hint, team_id="", preamble=None)`;
  `native_team.dump_messages(list[Message]) -> list[dict]`, `native_team.load_messages(list[dict]) -> list[Message]`.
- Contract: when `ctx.stop_when` is not `None`, the team attaches a state snapshot to every result whose
  `stop_reason` is not `"done"`. With `initial_state`, the conversation is restored verbatim and `nudge` (or the
  goal message if `nudge` is `None`) is the next user message.

- [ ] **Step 1: Write the failing native-team tests**

Append to `tests/unit/test_native_team.py`:

```python
async def test_stop_when_returns_fork_point_with_state_and_last_test_output(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "run_tests", "{}")]),
                FakeReply(tool_calls=[ToolCall("c2", "run_tests", "{}")]),
                FakeReply(text="never reached"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    ctx.tools = ToolRegistry.builtin(["run_tests"], ToolContext(worktree=ctx.worktree, test_cmd="echo FAILED x; false"))
    seen: list[str] = []

    def stuck(step: StepRecord) -> bool:
        seen.append(step.u_t.name)
        return len(seen) >= 2

    ctx.stop_when = stuck
    res = await team.run(ctx, Recorder())
    assert res.stop_reason == "fork_point" and res.steps == 2
    assert res.state is not None and len(res.state["messages"]) == 6  # system, goal, 2×(assistant, tool)
    assert "FAILED x" in res.last_test_output
    assert len(engine.requests) == 2  # the third reply was never requested


async def test_initial_state_resumes_verbatim_and_nudge_is_next_user_message(tmp_path: Path) -> None:
    from airlie.teams.native_team import dump_messages
    from airlie.engine.types import Message

    history = [
        Message(role="system", content="You are the coder."),
        Message(role="user", content="Goal:\nmake it pass"),
        Message(role="assistant", content="", tool_calls=(ToolCall("c1", "run_tests", "{}"),)),
        Message(role="tool", content="FAILED", tool_call_id="c1", name="run_tests"),
    ]
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.initial_state = {"messages": dump_messages(history)}
    ctx.nudge = "Branch r1-1 of 2. Try a different approach."
    res = await team.run(ctx, Recorder())
    assert res.ok and res.stop_reason == "done"
    sent = engine.requests[0].messages
    assert [m.role for m in sent] == ["system", "user", "assistant", "tool", "user"]
    assert sent[2].tool_calls == (ToolCall("c1", "run_tests", "{}"),)
    assert sent[-1].content == ctx.nudge


async def test_preamble_precedes_goal(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.preamble = "[Repository map]\ntests/\n  test_a.py"
    await team.run(ctx, Recorder())
    goal_msg = engine.requests[0].messages[1].content
    assert goal_msg.startswith("[Repository map]") and "Goal:\nmake it pass" in goal_msg
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_native_team.py -q -k "stop_when or initial_state or preamble"`
Expected: FAIL (`TeamContext` has no field `stop_when` / `initial_state` / `preamble`).

- [ ] **Step 3: Extend the protocol**

In `src/airlie/teams/protocol.py`:

```python
from typing import Any, Literal, Protocol

StopReason = Literal[
    "done", "max_steps", "token_budget", "repeated_action", "engine_error", "cancelled", "fork_point"
]
```

Add to `TeamContext` (after `draft_budget`):

```python
    preamble: str | None = None
    """Text placed before the goal in the first user message (the repo map)."""
    initial_state: dict[str, Any] | None = None
    """A snapshot from an earlier TeamResult.state; the conversation resumes from it verbatim."""
    nudge: str | None = None
    """With initial_state: the next user message instead of the goal (branch label, hypothesis)."""
    stop_when: Callable[[StepRecord], bool] | None = None
    """Evaluated after every step; True stops the run with stop_reason="fork_point" and a state snapshot."""
```

Add to `TeamResult`:

```python
    state: dict[str, Any] | None = None
    last_test_output: str = ""
```

- [ ] **Step 4: Native team**

In `src/airlie/teams/native_team.py` replace `_goal_message` and add the two helpers:

```python
def _goal_message(goal: str, worktree_hint: str, team_id: str = "", preamble: str | None = None) -> str:
    who = f"You are {team_id}. " if team_id else ""
    head = f"{preamble.rstrip()}\n\n" if preamble else ""
    return (
        f"{head}Goal:\n{goal}\n\n{who}"
        f"You are working inside a git checkout of the repository ({worktree_hint}). "
        "Use the tools to inspect and change it, run the tests, and stop with a short summary when they pass."
    )


def dump_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content, "name": m.name, "tool_call_id": m.tool_call_id}
        d["tool_calls"] = [asdict(c) for c in m.tool_calls] if m.tool_calls else None
        out.append(d)
    return out


def load_messages(dumped: list[dict[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for d in dumped:
        calls = tuple(ToolCall(**c) for c in d["tool_calls"]) if d.get("tool_calls") else None
        out.append(
            Message(
                role=d["role"], content=d["content"], name=d.get("name"), tool_calls=calls,
                tool_call_id=d.get("tool_call_id"),
            )
        )
    return out
```

(imports: `from dataclasses import asdict`, `from typing import Any`.)

In `NativeTeam.run`, replace the initial `messages` construction with:

```python
        if ctx.initial_state is not None:
            messages = load_messages(ctx.initial_state["messages"])
            messages.append(
                Message(
                    role="user",
                    content=ctx.nudge or _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble),
                )
            )
        else:
            messages = [
                Message(role="system", content=system),
                Message(role="user", content=_goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble)),
            ]
        last_test_output = ""
        forked = False
```

Inside the `for call in res.tool_calls:` loop, after `await observer.after_step(...)`:

```python
                if call.name == "run_tests":
                    last_test_output = result.output
                recent.append((call.name, call.arguments, result.output))
                recent = recent[-ctx.repeat_limit :]
                if ctx.stop_when is not None and ctx.stop_when(step_record):
                    forked = True
                    break
```

(assign the `StepRecord(...)` to a local `step_record` before passing it to `after_step`.) Right after the
`for` loop:

```python
            if forked:
                final_text = "fork point: stuck, handing over to branches"
                stop = "fork_point"
                break
```

And the return:

```python
        return TeamResult(
            team_id=ctx.team_id, ok=ok, stop_reason=stop, final_text=final_text, steps=steps,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            wall_ms=(time.monotonic() - t0) * 1000.0,
            state={"messages": dump_messages(messages)} if ctx.stop_when is not None and stop != "done" else None,
            last_test_output=last_test_output,
        )
```

- [ ] **Step 5: Run the native tests**

Run: `uv run pytest tests/unit/test_native_team.py -q`
Expected: PASS (all, including the three new ones).

- [ ] **Step 6: Write the failing AutoGen test**

Append to `tests/unit/test_autogen_team.py`:

```python
async def test_autogen_state_round_trip_and_nudge(tmp_path: Path) -> None:
    """save_state after a fork point reloads into a fresh agent; the nudge is the next task."""
    from autogen_agentchat.agents import AssistantAgent
    from autogen_ext.models.replay import ReplayChatCompletionClient

    from airlie.teams.autogen_team import agent_state, load_agent_state

    a = AssistantAgent("trunk", model_client=ReplayChatCompletionClient(["first"]), system_message="sys")
    await a.run(task="Goal: x")
    state = await agent_state(a)
    assert state["type"] == "AssistantAgentState" and len(state["llm_context"]["messages"]) == 2
    b = AssistantAgent("child", model_client=ReplayChatCompletionClient(["second"]), system_message="sys")
    await load_agent_state(b, state)
    msgs = await b.model_context.get_messages()
    assert [m.content for m in msgs] == ["Goal: x", "first"]
```

- [ ] **Step 7: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_autogen_team.py -q -k round_trip`
Expected: FAIL (`agent_state` not found).

- [ ] **Step 8: AutoGen team**

In `src/airlie/teams/autogen_team.py` add:

```python
async def agent_state(agent: AssistantAgent) -> dict[str, Any]:
    return dict(await agent.save_state())


async def load_agent_state(agent: AssistantAgent, state: dict[str, Any]) -> None:
    await agent.load_state(state)
```

In `AutoGenTeam.run`, after constructing `agent`:

```python
        if ctx.initial_state is not None:
            await load_agent_state(agent, ctx.initial_state)
        task: str | None = (
            ctx.nudge or _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble)
            if ctx.initial_state is not None
            else _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble)
        )
        last_test_output = ""
        forked = False
```

(replace the existing `task: str | None = _goal_message(...)` line.) In the `for s in new_steps:` loop:

```python
                for s in new_steps:
                    await observer.after_step(s)
                    if s.u_t.name == "run_tests":
                        last_test_output = s.o_t
                    recent.append((s.u_t.name, s.u_t.arguments, s.o_t))
                    if ctx.stop_when is not None and ctx.stop_when(s):
                        forked = True
                        break
                recent = recent[-ctx.repeat_limit :]
                steps += len(new_steps)
                if forked:
                    final_text = "fork point: stuck, handing over to branches"
                    stop = "fork_point"
                    break
```

Before `finally: await client.close()` add a `state` capture inside the `try` (after the `while` loop) — simplest:
declare `state: dict[str, Any] | None = None` before `try`, and in the `finally`:

```python
        finally:
            if ctx.stop_when is not None and stop != "done":
                state = await agent_state(agent)
            await client.close()
```

Return `state=state, last_test_output=last_test_output` in the `TeamResult`.

- [ ] **Step 9: Run everything**

Run: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add src/airlie/teams tests/unit/test_native_team.py tests/unit/test_autogen_team.py
git commit -m "feat(teams): state snapshot, stop predicate, preamble and nudge on the team seam (both teams)"
```

---

### Task 2: Repo map

**Files:**
- Create: `src/airlie/repo_map.py`
- Modify: `src/airlie/config/schema.py` (add `RepoMapSpec`, `AirlieConfig.repo_map`)
- Modify: `airlie.yaml`
- Test: `tests/unit/test_repo_map.py`

**Interfaces:**
- Produces: `build_repo_map(repo: Path, *, test_cmd: str | None, max_chars: int, depth: int, cache_dir: Path | None) -> tuple[str, bool]` (text, cached); `RepoMapSpec(enabled: bool = True, max_chars: int = 6000, depth: int = 3)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_repo_map.py`:

```python
"""Deterministic repository map injected before the goal (design §4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from airlie.repo_map import build_repo_map


def git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


FILES = {
    "pkg/__init__.py": "",
    "pkg/report.py": "class Report:\n    pass\n\n\ndef total_cost(qty: int, unit_cost_cents: int) -> int:\n    return 0\n",
    "pkg/broken.py": "def oops(:\n",
    "tests/test_report.py": "def test_total():\n    pass\n",
    "docs/a/b/c/deep.md": "x",
    "Makefile": "all:\n",
}


def test_map_lists_tree_signatures_and_test_command(tmp_path: Path) -> None:
    text, cached = build_repo_map(git_repo(tmp_path, FILES), test_cmd="pytest -q", max_chars=6000, depth=3, cache_dir=None)
    assert cached is False
    assert text.startswith("[Repository map]\ntest command: pytest -q\n")
    assert "pkg/report.py" in text and "def total_cost(qty: int, unit_cost_cents: int) -> int" in text
    assert "class Report" in text
    assert "pkg/broken.py" in text  # listed, signatures skipped
    assert "Makefile" in text
    assert "docs/a/b/c/deep.md" not in text and "docs/a/b/" in text  # depth 3 cut


def test_map_is_capped_and_deterministic(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, FILES)
    a, _ = build_repo_map(repo, test_cmd=None, max_chars=120, depth=3, cache_dir=None)
    b, _ = build_repo_map(repo, test_cmd=None, max_chars=120, depth=3, cache_dir=None)
    assert a == b and len(a) <= 120 + len("\n[map truncated]") and a.endswith("[map truncated]")


def test_map_cache_hits_on_second_call_and_misses_after_change(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, FILES)
    cache = tmp_path / "cache"
    _, c1 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    _, c2 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    assert (c1, c2) == (False, True)
    (repo / "pkg" / "new.py").write_text("def added(): ...\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    text, c3 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    assert c3 is False and "def added()" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_repo_map.py -q`
Expected: FAIL (`ModuleNotFoundError: airlie.repo_map`).

- [ ] **Step 3: Implement**

`src/airlie/repo_map.py`:

```python
"""Deterministic repository map, injected before the goal so no branch re-discovers the layout (design §4).

Tracked files (git ls-files) to a depth, sizes, the test command, and for Python files the top-level def/class
signatures via ast. No model call. Cached per (path, mtime, size) of the tracked files."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

HEADER = "[Repository map]"
TRUNCATED = "\n[map truncated]"


def _tracked(repo: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, text=True, check=True).stdout
    return sorted(Path(p) for p in out.split("\0") if p)


def _signatures(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, ValueError):
        return []
    sigs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            sigs.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ast.unparse(node.args)
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            sigs.append(f"def {node.name}({args}){ret}")
    return sigs


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _render(repo: Path, files: list[Path], *, test_cmd: str | None, depth: int) -> str:
    lines = [HEADER]
    if test_cmd:
        lines.append(f"test command: {test_cmd}")
    shown_dirs: set[Path] = set()
    for rel in files:
        parts = rel.parts
        if len(parts) > depth:
            d = Path(*parts[:depth])
            if d not in shown_dirs:
                shown_dirs.add(d)
                lines.append(f"{d}/ …")
            continue
        full = repo / rel
        entry = f"{rel} ({_size(full.stat().st_size)})"
        if rel.suffix == ".py":
            sigs = _signatures(full)
            if sigs:
                entry += ": " + "; ".join(sigs)
        lines.append(entry)
    return "\n".join(lines)


def build_repo_map(
    repo: Path, *, test_cmd: str | None, max_chars: int, depth: int, cache_dir: Path | None
) -> tuple[str, bool]:
    """Returns (map text, served_from_cache)."""
    files = _tracked(repo)
    fingerprint = hashlib.sha256()
    for rel in files:
        st = (repo / rel).stat()
        fingerprint.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    fingerprint.update(f"{test_cmd}\0{max_chars}\0{depth}".encode())
    key = fingerprint.hexdigest()
    cache_file = cache_dir / f"{key}.md" if cache_dir else None
    if cache_file is not None and cache_file.is_file():
        return cache_file.read_text(), True
    text = _render(repo, files, test_cmd=test_cmd, depth=depth)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + TRUNCATED
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text)
    return text, False
```

In `src/airlie/config/schema.py` add before `AirlieConfig`:

```python
class RepoMapSpec(Strict):
    enabled: bool = Field(default=True, description="inject a deterministic repository map before the goal")
    max_chars: int = Field(default=6000, ge=0)
    depth: int = Field(default=3, ge=1, description="directory depth shown; deeper paths collapse to 'dir/ …'")
```

and `repo_map: RepoMapSpec = Field(default_factory=RepoMapSpec)` on `AirlieConfig`. In `airlie.yaml`, after the
`lts:` block:

```yaml
# ---- Repository map (every topology) ---------------------------------------------------------------------------
repo_map: { enabled: true, max_chars: 6000, depth: 3 }   # tree + Python signatures, injected before the goal
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repo_map.py tests/unit/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/repo_map.py src/airlie/config/schema.py airlie.yaml tests/unit/test_repo_map.py
git commit -m "feat: deterministic repo map (tree, sizes, Python signatures, cached) with repo_map config"
```

---

### Task 3: Worktree helpers and shared RunReport

**Files:**
- Modify: `src/airlie/teams/worktree.py`
- Create: `src/airlie/report.py`
- Modify: `src/airlie/orchestrator.py` (import `RunReport`, `write_report` from `airlie.report`; keep `RunReport` re-exported)
- Test: `tests/unit/test_runs.py`

**Interfaces:**
- Produces: `add_worktree(repo, path, *, branch, start_point: str | None = None) -> Path`;
  `commit_wip(worktree: Path, message: str) -> str` (sha); `diff_last_commit(worktree: Path, max_chars: int) -> str`;
  `airlie.report.RunReport` (moved verbatim), `airlie.report.write_report(report, reason, path)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_runs.py`:

```python
def test_commit_wip_includes_untracked_files_and_children_start_from_it(tmp_path: Path) -> None:
    from airlie.teams.worktree import commit_wip, diff_last_commit

    repo = git_repo(tmp_path)
    trunk = add_worktree(repo, tmp_path / "runs" / "r1" / "trunk", branch="airlie/r1/trunk")
    (trunk / "new_module.py").write_text("x = 1\n")
    (trunk / "README.md").write_text("changed\n")
    sha = commit_wip(trunk, "airlie fork point 1")
    assert len(sha) == 40
    child = add_worktree(repo, tmp_path / "runs" / "r1" / "r1-1", branch="airlie/r1/r1-1", start_point=sha)
    assert (child / "new_module.py").read_text() == "x = 1\n" and (child / "README.md").read_text() == "changed\n"
    diff = diff_last_commit(trunk, max_chars=4000)
    assert "+x = 1" in diff and "+changed" in diff


def test_commit_wip_with_nothing_to_commit_still_returns_a_sha(tmp_path: Path) -> None:
    from airlie.teams.worktree import commit_wip

    repo = git_repo(tmp_path)
    trunk = add_worktree(repo, tmp_path / "runs" / "r2" / "trunk", branch="airlie/r2/trunk")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=trunk, capture_output=True, text=True).stdout.strip()
    sha = commit_wip(trunk, "empty fork point")
    assert sha != before and len(sha) == 40
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_runs.py -q -k commit_wip`
Expected: FAIL (`ImportError: commit_wip`).

- [ ] **Step 3: Implement worktree helpers**

In `src/airlie/teams/worktree.py`:

```python
_ENV = ["-c", "user.name=airlie", "-c", "user.email=airlie@local"]


def add_worktree(repo: Path, path: Path, *, branch: str, start_point: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = start_point or _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    return path


def commit_wip(worktree: Path, message: str) -> str:
    """Commit everything in the worktree (untracked included; empty allowed) and return the new sha."""
    _git(worktree, *_ENV, "add", "-A")
    _git(worktree, *_ENV, "commit", "-q", "--allow-empty", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_last_commit(worktree: Path, max_chars: int) -> str:
    """The last commit's diff (what the branch changed since the previous fork base), capped."""
    out = subprocess.run(
        ["git", "show", "--format=", "--no-color", "HEAD"], cwd=worktree, capture_output=True, text=True
    ).stdout
    return out[:max_chars]
```

- [ ] **Step 4: Move RunReport**

Create `src/airlie/report.py` with the `RunReport` dataclass copied verbatim from `orchestrator.py` and the body
of `ParallelAgentTeamOrchestrator._write_report` as a module function:

```python
def write_report(report: RunReport, reason: str, path: Path) -> None:
    lines = [ ...same lines as today... ]
    path.write_text("\n".join(lines) + "\n")
```

In `orchestrator.py`: `from airlie.report import RunReport, write_report`; delete the local `RunReport`; make
`_write_report` call `write_report(report, reason, self.run_dir.path / "report.md")`. Add `RunReport` to the
module's names (it is imported from `airlie.orchestrator` by tests and serve) — the `from airlie.report import
RunReport` line keeps that working; add `# noqa: F401`-free usage by referencing it in annotations as today.

- [ ] **Step 5: Run everything**

Run: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run mypy src`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/teams/worktree.py src/airlie/report.py src/airlie/orchestrator.py tests/unit/test_runs.py
git commit -m "feat: worktree start_point, commit_wip, diff_last_commit; RunReport shared in airlie.report"
```

---

### Task 4: Config — `topology` and `fork` sections; `teams` topology gets the preamble

**Files:**
- Modify: `src/airlie/config/schema.py`, `airlie.yaml`, `src/airlie/orchestrator.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_runs.py`

**Interfaces:**
- Produces: `TopologySpec(kind: Literal["teams", "fork"] = "teams")`; `ForkSpec(width=2, after_failures=2,
  child_steps=15, max_rounds=3, max_total_steps=120, diversify: Literal["hypotheses","temperature"]="hypotheses",
  hypothesis_role="judge", temperatures=[0.3, 0.7])`; `AirlieConfig.topology`, `AirlieConfig.fork`;
  `validate_references` checks `fork.hypothesis_role`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_topology_and_fork_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.topology.kind == "teams"
    assert cfg.fork.width == 2 and cfg.fork.after_failures == 2 and cfg.fork.child_steps == 15
    assert cfg.fork.max_rounds == 3 and cfg.fork.max_total_steps == 120
    assert cfg.fork.diversify == "hypotheses" and cfg.fork.hypothesis_role == "judge"
    assert cfg.fork.temperatures == [0.3, 0.7]
    assert cfg.repo_map.enabled is True


def test_fork_hypothesis_role_must_exist(tmp_path: Path) -> None:
    data = {**MINIMAL, "fork": {"hypothesis_role": "nobody"}}
    with pytest.raises(ConfigError, match=r"fork\.hypothesis_role"):
        load_config(write(tmp_path, data))
```

(`write` and `MINIMAL` are the file's existing helpers.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_config.py -q -k "topology or hypothesis_role"`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `schema.py` before `AirlieConfig`:

```python
class TopologySpec(Strict):
    kind: Literal["teams", "fork"] = Field(
        default="teams", description="teams = spec K parallel teams + LTS; fork = fork-when-stuck (design 2026-10-13)"
    )


class ForkSpec(Strict):
    width: int = Field(default=2, ge=1, description="children per fork point")
    after_failures: int = Field(default=2, ge=1, description="consecutive failing run_tests that mark a branch stuck")
    child_steps: int = Field(default=15, ge=1, description="steps a child gets before resolve")
    max_rounds: int = Field(default=3, ge=0)
    max_total_steps: int = Field(default=120, ge=1, description="steps across every branch of a run")
    diversify: Literal["hypotheses", "temperature"] = "hypotheses"
    hypothesis_role: str = Field(default="judge", description="role that lists root-cause hypotheses")
    temperatures: list[float] = Field(default=[0.3, 0.7], min_length=1)
```

On `AirlieConfig`: `topology: TopologySpec = Field(default_factory=TopologySpec)` and
`fork: ForkSpec = Field(default_factory=ForkSpec)`. In `validate_references`, add
`("fork.hypothesis_role", self.fork.hypothesis_role)` to the role-check tuple.

`airlie.yaml` (after `aggregator:`):

```yaml
# ---- Topology -------------------------------------------------------------------------------------------------
topology: { kind: teams }          # teams = spec K parallel teams + LTS | fork = fork-when-stuck
fork:
  width: 2                         # children per fork point
  after_failures: 2                # consecutive failing run_tests that mark a branch stuck
  child_steps: 15                  # steps a child gets before resolve
  max_rounds: 3
  max_total_steps: 120
  diversify: hypotheses            # hypotheses (one small-model call per fork) | temperature
  hypothesis_role: judge
  temperatures: [0.3, 0.7]
```

In `orchestrator.py`, `ParallelAgentTeamOrchestrator.execute_task`: after `run_start`, compute the preamble once:

```python
        self.preamble: str | None = None
        if self.cfg.repo_map.enabled:
            text, cached = build_repo_map(
                repo, test_cmd=test_cmd, max_chars=self.cfg.repo_map.max_chars, depth=self.cfg.repo_map.depth,
                cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps",
            )
            self.preamble = text
            self.sink.emit("repo_map", chars=len(text), cached=cached)
```

and pass `preamble=self.preamble` in `_run_team`'s `TeamContext(...)`. Initialise `self.preamble = None` in
`__init__`.

Append to `tests/unit/test_runs.py` a check on the existing orchestrator test that drives a run (whichever test
there asserts on `events.jsonl`): `assert "repo_map" in kinds` and that the first coder request's goal message
starts with `[Repository map]`.

- [ ] **Step 4: Run everything**

Run: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run mypy src`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py airlie.yaml src/airlie/orchestrator.py tests/unit/test_config.py tests/unit/test_runs.py
git commit -m "feat(config): topology and fork sections; teams topology injects the repo map"
```

---

### Task 5: Diversify — hypotheses with temperature fallback

**Files:**
- Create: `src/airlie/topology/__init__.py` (empty docstring module), `src/airlie/topology/diversify.py`
- Modify: `prompts/roles/judge.md`, `prompts/roles/coder.md`
- Test: `tests/unit/test_fork_diversify.py`

**Interfaces:**
- Produces: `parse_hypotheses(text: str, width: int) -> list[str] | None`;
  `async hypotheses(engine, role_name, role: RoleSpec, *, goal, diff, test_tail, width) -> list[str] | None`;
  `nudge_text(label: str, width: int, hypothesis: str | None, test_tail: str) -> str`;
  `role_at_temperature(role: RoleSpec, t: float) -> RoleSpec`;
  `parse_failing(test_output: str) -> int | None`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_fork_diversify.py`:

```python
"""Fork-point diversification: hypotheses from the judge role, temperature fallback, nudges (design §3.3)."""

from __future__ import annotations

from pathlib import Path

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply
from airlie.topology.diversify import (
    hypotheses, nudge_text, parse_failing, parse_hypotheses, role_at_temperature,
)


def test_parse_hypotheses_numbered_lines() -> None:
    text = "Some preamble\n1. total_cost adds instead of multiplying\n2) the CLI parses qty as str\n3. unrelated"
    assert parse_hypotheses(text, 2) == ["total_cost adds instead of multiplying", "the CLI parses qty as str"]


def test_parse_hypotheses_rejects_short_or_duplicate_output() -> None:
    assert parse_hypotheses("1. only one", 2) is None
    assert parse_hypotheses("1. same\n2. same", 2) is None
    assert parse_hypotheses("no numbers here", 2) is None


async def test_hypotheses_calls_the_role_once_and_falls_back_on_unparsable_output(tmp_path: Path) -> None:
    (tmp_path / "judge.md").write_text("judge")
    role = RoleSpec(prompt=tmp_path / "judge.md", model="small", thinking=False, max_tokens=200)
    engine = FakeEngine({"judge": [FakeReply(text="1. A is wrong\n2. B is wrong"), FakeReply(text="dunno")]})
    got = await hypotheses(engine, "judge", role, goal="g", diff="+x", test_tail="FAILED", width=2)
    assert got == ["A is wrong", "B is wrong"]
    assert engine.requests[-1].role == "judge" and "FAILED" in engine.requests[-1].messages[-1].content
    assert await hypotheses(engine, "judge", role, goal="g", diff="", test_tail="", width=2) is None


def test_nudge_text_names_branch_hypothesis_and_failure() -> None:
    n = nudge_text("r1-2", 2, "the CLI parses qty as str", "FAILED test_x")
    assert n.startswith("You are now branch r1-2 of 2")
    assert "the CLI parses qty as str" in n and "FAILED test_x" in n and "do not repeat" in n.lower()
    m = nudge_text("r1-1", 2, None, "")
    assert "different approach" in m.lower()


def test_role_at_temperature_copies(tmp_path: Path) -> None:
    (tmp_path / "c.md").write_text("c")
    role = RoleSpec(prompt=tmp_path / "c.md", model="base")
    hot = role_at_temperature(role, 0.9)
    assert hot.sampling.temperature == 0.9 and role.sampling.temperature != 0.9 and hot.model == "base"


def test_parse_failing() -> None:
    assert parse_failing("3 failed, 2 passed in 0.1s") == 3
    assert parse_failing("5 passed in 0.1s") == 0
    assert parse_failing("ok\n") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_fork_diversify.py -q`
Expected: FAIL (`ModuleNotFoundError: airlie.topology`).

- [ ] **Step 3: Implement**

`src/airlie/topology/__init__.py`:

```python
"""Topologies: how branches/teams are arranged over the engine. `teams` (spec) and `fork` (design 2026-10-13)."""
```

`src/airlie/topology/diversify.py`:

```python
"""Fork-point diversification (design §3.3): distinct hypotheses from a small-model role, or a temperature spread."""

from __future__ import annotations

import re

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message

_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
_FAILED = re.compile(r"(\d+) failed")
_PASSED = re.compile(r"(\d+) passed")


def parse_hypotheses(text: str, width: int) -> list[str] | None:
    found: list[str] = []
    for line in text.splitlines():
        m = _NUMBERED.match(line)
        if m and m.group(2) not in found:
            found.append(m.group(2))
    return found[:width] if len(found) >= width else None


async def hypotheses(
    engine: Engine, role_name: str, role: RoleSpec, *, goal: str, diff: str, test_tail: str, width: int
) -> list[str] | None:
    """One call; None when the output cannot be parsed (caller falls back to temperature)."""
    prompt = (
        f"Goal:\n{goal}\n\nThe branch's last change:\n```\n{diff or '(no change)'}\n```\n\n"
        f"The tests still fail:\n```\n{test_tail or '(no output)'}\n```\n\n"
        f"List {width} distinct root-cause hypotheses, each with a different fix approach, one per line, "
        f"numbered 1. to {width}. No other text."
    )
    req = GenRequest(
        role=role_name,
        messages=[Message(role="system", content=role.prompt.read_text()), Message(role="user", content=prompt)],
        sampling=role.sampling,
        max_tokens=role.max_tokens,
        stop=list(role.stop),
        tools=[],
        thinking=role.thinking,
    )
    try:
        res = await engine.generate(req)
    except EngineError:
        return None
    return parse_hypotheses(res.text, width)


def nudge_text(label: str, width: int, hypothesis: str | None, test_tail: str) -> str:
    lines = [
        f"You are now branch {label} of {width}, continuing from the conversation above in your own copy of the "
        "repository. The previous approach did not make the tests pass; do not repeat it.",
    ]
    if test_tail:
        lines.append(f"Last test output:\n```\n{test_tail[-1500:]}\n```")
    if hypothesis:
        lines.append(f"Pursue this hypothesis: {hypothesis}")
    else:
        lines.append("Take a different approach from the one you tried.")
    lines.append("Make the change, run the tests, and stop with a short summary when they pass.")
    return "\n\n".join(lines)


def role_at_temperature(role: RoleSpec, t: float) -> RoleSpec:
    return role.model_copy(update={"sampling": role.sampling.model_copy(update={"temperature": t})})


def parse_failing(test_output: str) -> int | None:
    m = _FAILED.search(test_output)
    if m:
        return int(m.group(1))
    return 0 if _PASSED.search(test_output) else None
```

`prompts/roles/judge.md`, append:

```markdown
When asked to list root-cause hypotheses: output exactly the requested number of lines, numbered `1.` to `N`,
each a different, concrete hypothesis with a different fix approach, nothing else.
```

`prompts/roles/coder.md`, append a section:

```markdown
## Branches
You may be told you are now a branch continuing an earlier conversation. The previous approach failed; the
message names a hypothesis to pursue. Do not repeat the earlier change. Work from the files as they are now.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_fork_diversify.py -q && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/topology prompts/roles tests/unit/test_fork_diversify.py
git commit -m "feat(fork): hypotheses/temperature diversification, nudges, failing-test parser; prompts"
```

---

### Task 6: ForkOrchestrator and the Topology factory

**Files:**
- Create: `src/airlie/topology/protocol.py`, `src/airlie/topology/fork.py`
- Test: `tests/unit/test_fork_orchestrator.py`

**Interfaces:**
- Consumes: Task 1 seam fields; Task 3 `add_worktree(start_point)`, `commit_wip`, `diff_last_commit`,
  `RunReport`, `write_report`; Task 4 `cfg.fork`, `cfg.topology`, `cfg.repo_map`; Task 5 diversify; existing
  `LoggingObserver`, `SlotScheduler`, `ToolRegistry.builtin`, `ToolContext`, `EventSink`, `RunDir`.
- Produces: `Topology` protocol (`run_dir: RunDir`; `async execute_task(query_text, *, repo, test_cmd=None) ->
  RunReport`); `build_topology(cfg, engine, run_id=None) -> Topology`; `ForkOrchestrator(cfg, engine, *,
  run_id=None, team_factory=None)`; `ForkObserver`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_fork_orchestrator.py`:

```python
"""Fork-when-stuck: trunk forks after two failing run_tests; children share state and tree; first pass wins."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.topology.fork import ForkOrchestrator
from airlie.topology.protocol import build_topology


def repo_with_marker_tests(tmp_path: Path) -> Path:
    """Tests pass only once a file named PASS exists in the worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    (repo / "check.sh").write_text("#!/bin/sh\nif [ -f PASS ]; then echo '1 passed'; exit 0; fi\necho '1 failed'; exit 1\n")
    os.chmod(repo / "check.sh", 0o755)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def cfg_for(tmp_path: Path, **fork: object):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text(f"you are {r}")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {r: {"prompt": f"{r}.md", "model": "base", "thinking": False} for r in ("coder", "summarizer", "judge", "aggregator")},
        "teams": {"orchestrator": "native", "max_steps": 10},
        "tools": {"builtin": ["write_file", "run_tests"]},
        "topology": {"kind": "fork"},
        "fork": {"width": 2, "after_failures": 2, "child_steps": 4, "max_rounds": 2, "diversify": "temperature", **fork},
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(cfg_dir / "airlie.yaml")


RUN = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
WRITE_PASS = FakeReply(tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "PASS", "content": "ok\n"}))])
WRITE_NOTE = FakeReply(tool_calls=[ToolCall("n", "write_file", json.dumps({"path": "note.txt", "content": "trunk\n"}))])


def events(report):  # type: ignore[no-untyped-def]
    return [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]


async def test_trunk_forks_after_two_failures_and_passing_child_wins(tmp_path: Path) -> None:
    # Replies are consumed in request order: trunk (note, run, run) → fork → children interleave.
    # Child A (r1-1) is asked first: it writes PASS, runs tests (pass), then finishes.
    # Child B (r1-2) runs tests (fail) and would keep going; it is cancelled after A's pass.
    engine = FakeEngine(
        {
            "coder": [
                WRITE_NOTE, RUN, RUN,                 # trunk: note written, two failures → fork point
                WRITE_PASS,                           # r1-1 step 1
                RUN,                                  # r1-2 step 1 (fails)
                RUN,                                  # r1-1 step 2 (passes) → siblings cancelled
                FakeReply(text="fixed it"),           # r1-1 final
                RUN, RUN, RUN,                        # spare replies if r1-2 is still running
            ]
        }
    )
    orch = ForkOrchestrator(cfg_for(tmp_path), engine, run_id="f1")
    report = await orch.execute_task("make check.sh pass", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    ids = [t.team_id for t in report.teams]
    assert ids[0] == "trunk" and set(ids[1:]) == {"r1-1", "r1-2"}
    trunk = report.teams[0]
    assert trunk.stop_reason == "fork_point" and trunk.steps == 3
    assert report.winner == "r1-1" and report.winning_branch == "airlie/f1/r1-1"
    assert report.final_answer == "fixed it" and report.any_ok
    assert (orch.run_dir.path / "r1-1" / "note.txt").read_text() == "trunk\n"  # child started from the WIP commit
    assert (orch.run_dir.path / "r1-2" / "note.txt").read_text() == "trunk\n"
    kinds = [e["kind"] for e in events(report)]
    assert kinds.count("fork_point") == 1 and kinds.count("fork_resolved") == 1
    fp = next(e for e in events(report) if e["kind"] == "fork_point")
    assert fp["round"] == 1 and fp["parent"] == "trunk" and fp["children"] == ["r1-1", "r1-2"] and "1 failed" in fp["test_tail"]
    fr = next(e for e in events(report) if e["kind"] == "fork_resolved")
    assert fr["winner"] == "r1-1" and fr["how"] == "visible_pass"
    done = next(e for e in events(report) if e["kind"] == "run_done")
    assert done["forks"] == 1 and done["rounds"] == 1 and done["ok"] is True
    # the child resumed the trunk's conversation: its first request carries the trunk's history plus a nudge
    child_first = next(r for r in engine.requests[3:] if "branch r1-1" in r.messages[-1].content)
    assert [m.role for m in child_first.messages[:2]] == ["system", "user"] and len(child_first.messages) > 5
    assert (orch.run_dir.path / "report.md").is_file()


async def test_no_child_passes_best_effort_then_max_rounds(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [RUN] * 40})  # everyone only ever runs failing tests
    orch = ForkOrchestrator(cfg_for(tmp_path, child_steps=2, max_rounds=2), engine, run_id="f2")
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    ev = events(report)
    resolved = [e for e in ev if e["kind"] == "fork_resolved"]
    assert [e["how"] for e in resolved] == ["best_effort", "best_effort"]
    assert sum(1 for e in ev if e["kind"] == "fork_point") == 2
    assert report.winner in {"r2-1", "r2-2"} and not report.any_ok
    assert {t.team_id for t in report.teams} == {"trunk", "r1-1", "r1-2", "r2-1", "r2-2"}


async def test_resolve_uses_last_run_tests_outcome(tmp_path: Path) -> None:
    """A child that passed once and then broke the tests is not the winner."""
    from airlie.teams.protocol import StepRecord
    from airlie.topology.fork import ForkObserver
    from airlie.teams import NullObserver
    from airlie.obs import EventSink

    obs = ForkObserver(NullObserver(), EventSink(tmp_path / "e.jsonl", "r"), after_failures=2)
    ok = StepRecord("r1-1", 1, ToolCall("a", "run_tests", "{}"), "1 passed", True)
    bad = StepRecord("r1-1", 2, ToolCall("b", "run_tests", "{}"), "1 failed", False)
    await obs.after_step(ok)
    assert obs.passed["r1-1"] is True
    await obs.after_step(bad)
    assert obs.passed["r1-1"] is False and obs.failing["r1-1"] == 1 and obs.stuck(bad) is False
    await obs.after_step(bad)
    assert obs.stuck(bad) is True


async def test_total_step_cap_ends_the_run(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [RUN] * 40})
    orch = ForkOrchestrator(cfg_for(tmp_path, max_total_steps=5, max_rounds=5), engine, run_id="f3")
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    assert sum(t.steps for t in report.teams) <= 5 + 2 * 4  # trunk hit the cap check before a second fork
    assert sum(1 for e in events(report) if e["kind"] == "fork_point") <= 1


def test_build_topology_selects_by_kind(tmp_path: Path) -> None:
    from airlie.orchestrator import ParallelAgentTeamOrchestrator

    engine = FakeEngine({})
    cfg = cfg_for(tmp_path)
    assert isinstance(build_topology(cfg, engine, run_id="b1"), ForkOrchestrator)
    cfg.topology.kind = "teams"
    assert isinstance(build_topology(cfg, engine, run_id="b2"), ParallelAgentTeamOrchestrator)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_fork_orchestrator.py -q`
Expected: FAIL (`ModuleNotFoundError: airlie.topology.fork`).

- [ ] **Step 3: Implement the protocol and factory**

`src/airlie/topology/protocol.py`:

```python
"""Topology seam: one object per run that arranges branches/teams and returns a RunReport."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.obs import RunDir
from airlie.report import RunReport


class Topology(Protocol):
    run_dir: RunDir

    async def execute_task(self, query_text: str, *, repo: Path, test_cmd: str | None = None) -> RunReport: ...


def build_topology(cfg: AirlieConfig, engine: Engine, run_id: str | None = None) -> Topology:
    if cfg.topology.kind == "fork":
        from airlie.topology.fork import ForkOrchestrator

        return ForkOrchestrator(cfg, engine, run_id=run_id)
    from airlie.orchestrator import ParallelAgentTeamOrchestrator

    return ParallelAgentTeamOrchestrator(cfg, engine, run_id=run_id)
```

- [ ] **Step 4: Implement the ForkOrchestrator**

`src/airlie/topology/fork.py`:

```python
"""Fork-when-stuck (docs/superpowers/specs/2026-10-13-fork-when-stuck-design.md §3).

One trunk branch runs until it is stuck (after_failures consecutive failing run_tests, or a repeated action);
its working tree is committed, `width` children get worktrees at that commit and the trunk's conversation
verbatim plus one nudge each; the first child whose run_tests passes cancels its siblings and becomes the trunk."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.repo_map import build_repo_map
from airlie.report import RunReport, write_report
from airlie.scheduler import SlotScheduler
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.worktree import add_worktree, commit_wip, diff_last_commit
from airlie.tools import ToolContext, ToolRegistry
from airlie.topology.diversify import hypotheses, nudge_text, parse_failing, role_at_temperature

FORK_REASONS: frozenset[str] = frozenset({"fork_point", "repeated_action"})
ResolveHow = Literal["visible_pass", "best_effort", "none"]


class ForkObserver:
    """Tracks every branch's run_tests outcomes; fires on_pass when a branch's latest run passes."""

    def __init__(self, inner: StepObserver, sink: EventSink, *, after_failures: int) -> None:
        self.inner = inner
        self.sink = sink
        self.after_failures = after_failures
        self.failing: dict[str, int] = {}
        self.passed: dict[str, bool] = {}
        self.last_test: dict[str, str] = {}
        self.on_pass: Callable[[str], None] | None = None

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        return await self.inner.before_step(team_id, step_num)

    async def after_step(self, step: StepRecord) -> None:
        await self.inner.after_step(step)
        if step.u_t.name != "run_tests":
            return
        self.last_test[step.team_id] = step.o_t
        if step.ok:
            self.failing[step.team_id] = 0
            self.passed[step.team_id] = True
            if self.on_pass is not None:
                self.on_pass(step.team_id)
        else:
            self.failing[step.team_id] = self.failing.get(step.team_id, 0) + 1
            self.passed[step.team_id] = False

    def stuck(self, step: StepRecord) -> bool:
        return (
            step.u_t.name == "run_tests"
            and not step.ok
            and self.failing.get(step.team_id, 0) >= self.after_failures
        )


@dataclass
class Branch:
    id: str
    worktree: Path
    git_branch: str
    result: TeamResult | None = None
    hypothesis: str | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ForkOrchestrator:
    def __init__(
        self,
        cfg: AirlieConfig,
        engine: Engine,
        *,
        run_id: str | None = None,
        team_factory: TeamOrchestrator | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id)
        self.sink = EventSink(self.run_dir.events_path, self.run_dir.run_id)
        self.scheduler = SlotScheduler(cfg.scheduler.max_active, cfg.scheduler.starvation_ms)
        self.team: TeamOrchestrator = team_factory or self._default_team()
        self.observer = ForkObserver(
            LoggingObserver(self.sink, self.run_dir), self.sink, after_failures=cfg.fork.after_failures
        )
        self.preamble: str | None = None
        self.total_steps = 0
        self.branches: list[Branch] = []

    def _default_team(self) -> TeamOrchestrator:
        if self.cfg.teams.orchestrator == "autogen":
            from airlie.teams.autogen_team import AutoGenTeam

            role = self.cfg.roles[self.cfg.teams.role]
            e = self.cfg.engines[self.cfg.engine_for_model(role.model)]
            return AutoGenTeam(e.base_url or "", e.served_name(role.model), e.api_key)
        return NativeTeam(self.engine)

    # ---- run ----
    async def execute_task(self, query_text: str, *, repo: Path, test_cmd: str | None = None) -> RunReport:
        t0 = time.monotonic()
        f = self.cfg.fork
        self.sink.emit("run_start", goal=query_text, topology="fork", repo=str(repo), width=f.width)
        if self.cfg.repo_map.enabled:
            text, cached = build_repo_map(
                repo, test_cmd=test_cmd, max_chars=self.cfg.repo_map.max_chars, depth=self.cfg.repo_map.depth,
                cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps",
            )
            self.preamble = text
            self.sink.emit("repo_map", chars=len(text), cached=cached)
        trunk = self._branch("trunk", repo, start_point=None)
        res = await self._run_branch(trunk, query_text, test_cmd, parent=None, max_steps=self.cfg.teams.max_steps)
        rounds = forks = 0
        while (
            res.stop_reason in FORK_REASONS
            and res.state is not None
            and rounds < f.max_rounds
            and self.total_steps < f.max_total_steps
        ):
            rounds += 1
            forks += 1
            base = commit_wip(trunk.worktree, f"airlie fork point {rounds}")
            kids = [self._branch(f"r{rounds}-{i + 1}", repo, start_point=base) for i in range(f.width)]
            await self._diversify(kids, query_text, trunk, res.last_test_output)
            self.sink.emit(
                "fork_point", round=rounds, parent=trunk.id, reason=res.stop_reason, base=base,
                children=[k.id for k in kids], test_tail=res.last_test_output[-1500:],
                hypotheses=[k.hypothesis for k in kids], temperatures=[k.temperature for k in kids],
            )
            await self._run_children(kids, query_text, test_cmd, parent=res)
            winner, how = self._resolve(kids)
            self.sink.emit(
                "fork_resolved", round=rounds, winner=winner.id if winner else None, how=how,
                outcomes={k.id: (k.result.stop_reason if k.result else "none") for k in kids},
            )
            if winner is None or winner.result is None:
                break
            trunk, res = winner, winner.result
        report = self._report(query_text, trunk, res, t0, forks=forks, rounds=rounds)
        self.sink.emit(
            "run_done", ok=report.any_ok, wall_ms=round(report.wall_ms, 1), forks=forks, rounds=rounds,
            winner=report.winner, total_steps=self.total_steps,
        )
        return report

    # ---- branches ----
    def _branch(self, bid: str, repo: Path, *, start_point: str | None) -> Branch:
        git_branch = f"airlie/{self.run_dir.run_id}/{bid}"
        wt = add_worktree(repo, self.run_dir.path / bid, branch=git_branch, start_point=start_point)
        b = Branch(bid, wt, git_branch)
        self.branches.append(b)
        return b

    def _context(self, b: Branch, goal: str, test_cmd: str | None, *, max_steps: int, parent: TeamResult | None) -> TeamContext:
        role_name = self.cfg.teams.role
        role = self.cfg.roles[role_name]
        if b.temperature is not None:
            role = role_at_temperature(role, b.temperature)
        tools = ToolRegistry.builtin(
            self.cfg.tools.builtin,
            ToolContext(b.worktree, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars, test_cmd),
        )
        return TeamContext(
            team_id=b.id, goal=goal, role=role, tools=tools, worktree=b.worktree, role_name=role_name,
            max_steps=max_steps, token_budget=self.cfg.teams.token_budget, scheduler=self.scheduler,
            preamble=self.preamble,
            initial_state=parent.state if parent is not None else None,
            nudge=nudge_text(b.id, self.cfg.fork.width, b.hypothesis, parent.last_test_output) if parent else None,
            stop_when=self.observer.stuck,
        )

    async def _run_branch(
        self, b: Branch, goal: str, test_cmd: str | None, *, parent: TeamResult | None, max_steps: int
    ) -> TeamResult:
        self.sink.emit("branch_start", branch=b.id, worktree=str(b.worktree), parent=bool(parent))
        ctx = self._context(b, goal, test_cmd, max_steps=max_steps, parent=parent)
        try:
            result = await self.team.run(ctx, self.observer)
        except asyncio.CancelledError:
            result = TeamResult(b.id, False, "cancelled", "cancelled: a sibling passed first", 0, 0, 0, 0.0)
        b.result = result
        self.total_steps += result.steps
        self.sink.emit(
            "branch_done", branch=b.id, ok=result.ok, stop_reason=result.stop_reason, steps=result.steps,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            wall_ms=round(result.wall_ms, 1), visible_pass=self.observer.passed.get(b.id, False),
        )
        return result

    async def _run_children(self, kids: list[Branch], goal: str, test_cmd: str | None, *, parent: TeamResult) -> None:
        tasks = {
            k.id: asyncio.create_task(
                self._run_branch(k, goal, test_cmd, parent=parent, max_steps=self.cfg.fork.child_steps)
            )
            for k in kids
        }

        def on_pass(team_id: str) -> None:
            for kid, t in tasks.items():
                if kid != team_id and not t.done():
                    t.cancel()

        self.observer.on_pass = on_pass
        try:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            self.observer.on_pass = None
        for k in kids:
            if k.result is None:  # cancelled before _run_branch could record it
                k.result = TeamResult(k.id, False, "cancelled", "cancelled: a sibling passed first", 0, 0, 0, 0.0)

    def _resolve(self, kids: list[Branch]) -> tuple[Branch | None, ResolveHow]:
        live = [k for k in kids if k.result is not None and k.result.stop_reason not in ("engine_error", "cancelled")]
        passed = [k for k in live if self.observer.passed.get(k.id, False)]
        if passed:
            passed.sort(key=lambda k: (k.result.steps if k.result else 0, k.id))
            return passed[0], "visible_pass"
        if not live:
            return None, "none"
        def key(k: Branch) -> tuple[int, int, str]:
            failing = parse_failing(self.observer.last_test.get(k.id, ""))
            return (failing if failing is not None else 10**6, k.result.steps if k.result else 0, k.id)
        live.sort(key=key)
        return live[0], "best_effort"

    async def _diversify(self, kids: list[Branch], goal: str, trunk: Branch, test_tail: str) -> None:
        f = self.cfg.fork
        hyps: list[str] | None = None
        if f.diversify == "hypotheses":
            role = self.cfg.roles[f.hypothesis_role]
            diff = diff_last_commit(trunk.worktree, max_chars=6000)
            hyps = await hypotheses(
                self.engine, f.hypothesis_role, role, goal=goal, diff=diff, test_tail=test_tail, width=len(kids)
            )
        for i, k in enumerate(kids):
            if hyps is not None:
                k.hypothesis = hyps[i]
            else:
                k.temperature = f.temperatures[i % len(f.temperatures)]

    # ---- report ----
    def _report(self, goal: str, trunk: Branch, res: TeamResult, t0: float, *, forks: int, rounds: int) -> RunReport:
        teams = [b.result for b in self.branches if b.result is not None]
        report = RunReport(self.run_dir.run_id, self.run_dir, goal, teams, (time.monotonic() - t0) * 1000.0)
        report.winner = trunk.id
        report.final_answer = res.final_text
        report.winning_branch = trunk.git_branch
        report.tests = {b.id: self.observer.passed.get(b.id) for b in self.branches}
        report.scores = {b.id: (1.0 if self.observer.passed.get(b.id) else 0.0) for b in self.branches}
        write_report(report, f"fork-when-stuck ({forks} forks, {rounds} rounds)", self.run_dir.path / "report.md")
        return report
```

Note on `RunReport.any_ok`: it is `any(t.ok for t in teams)`; for the fork topology the trunk's final result is
what counts, so override in `_report` is not needed — a `done` winner has `ok=True`, all others are `False` or
cancelled. If `any_ok` ever disagrees with `res.ok` in the tests, set `report.teams` so the winner's result is
the one with `ok` (it already is).

- [ ] **Step 5: Run the fork tests, then everything**

Run: `uv run pytest tests/unit/test_fork_orchestrator.py -q -x`
Expected: PASS. If the first test's reply ordering does not match the interleaving of the two children under the
slot scheduler, route replies per team instead: give each child its own role script by passing `role_name` =
`coder` for both but asserting only on outcomes (winner `r1-1` is whichever child wrote `PASS`); change the
assertion to `report.winner in {"r1-1", "r1-2"}` and check `(run_dir / report.winner / "PASS").exists()`.

Then: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/topology tests/unit/test_fork_orchestrator.py
git commit -m "feat(fork): ForkOrchestrator (trunk → fork on stuck → first visible pass wins), Topology protocol and factory"
```

---

### Task 7: CLI, serve, Taskfile — topology selection

**Files:**
- Modify: `src/airlie/cli.py` (`_orchestrator`, `run`, `eval_run`), `src/airlie/serve/app.py`, `Taskfile.yml`
- Test: `tests/unit/test_cli_run.py`, `tests/unit/test_serve.py`

**Interfaces:**
- Produces: `airlie run --topology fork|teams`, `airlie eval run --topology …`; `RunRequest.topology: str | None`;
  `cli._orchestrator(cfg, run_id)` returns `build_topology(cfg, engine, run_id)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_cli_run.py`:

```python
def test_run_topology_option_sets_config(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []

    def fake(cfg, run_id=None):  # type: ignore[no-untyped-def]
        seen.append(cfg.topology.kind)
        return StubOrchestrator(cfg, None, run_id)

    monkeypatch.setattr(cli, "_orchestrator", fake)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(cli.app, ["run", "g", "--repo", str(repo), "--topology", "fork", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert seen == ["fork"]
```

Append to `tests/unit/test_serve.py` (mirroring `test_run_endpoint_uses_orchestrator_factory`): post
`{"goal": "g", "repo": <repo>, "topology": "fork"}` and assert the factory saw `cfg.topology.kind == "fork"`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_cli_run.py tests/unit/test_serve.py -q -k topology`
Expected: FAIL (no such option / field).

- [ ] **Step 3: Implement**

`cli.py`:

```python
TopologyOpt = Annotated[str | None, typer.Option(help="teams | fork (default: topology.kind)")]


def _orchestrator(cfg: AirlieConfig, run_id: str | None = None):  # type: ignore[no-untyped-def]
    """Factory, patched in tests."""
    from airlie.topology.protocol import build_topology

    role = cfg.roles[cfg.teams.role]
    engine = _http_engine(cfg, cfg.engine_for_model(role.model))
    return build_topology(cfg, engine, run_id=run_id)
```

Add `topology: TopologyOpt = None` to `run` and `eval_run`; after `_load`:

```python
    if topology is not None:
        if topology not in ("teams", "fork"):
            err.print(f"[red]unknown topology '{topology}'; use teams or fork[/red]")
            raise typer.Exit(2)
        cfg.topology.kind = topology  # type: ignore[assignment]
```

In `_print_report`, print `worktrees: {report.run_dir.path}/<branch>` text generically: `f"worktrees: {report.run_dir.path}/*"`.

`serve/app.py`: `RunRequest` gains `topology: str | None = None`; `_default_factory(cfg, k, topology)` sets
`cfg.topology.kind = topology` when given; the `run` endpoint passes `req.topology`. Keep the factory callable
signature backward compatible for the existing test by giving the new parameter a default:
`Callable[..., Any]` and call `factory(cfg, req.k, topology=req.topology)` only when `req.topology` is set, else
`factory(cfg, req.k)`.

`Taskfile.yml`: update `run` and `eval` descs to mention `--topology fork`.

- [ ] **Step 4: Run everything**

Run: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run mypy src`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/cli.py src/airlie/serve/app.py Taskfile.yml tests/unit/test_cli_run.py tests/unit/test_serve.py
git commit -m "feat(cli,serve): topology selection (--topology, RunRequest.topology) through build_topology"
```

---

### Task 8: `task bench:topology`

**Files:**
- Create: `src/airlie/bench/topology_bench.py`
- Modify: `src/airlie/cli.py` (`bench topology` command), `Taskfile.yml`
- Test: `tests/unit/test_topology_bench.py`

**Interfaces:**
- Produces: `TopoRow(task, topology, repeat, wall_s, steps, prompt_tokens, completion_tokens, forks, rounds,
  winner_passed: bool, any_passed: bool, branches: int, run_id)`; `apply_topology(cfg, name) -> AirlieConfig`
  for `single` (teams, k=1, lts off), `teams` (as configured, kind teams), `fork` (kind fork);
  `summarize(rows) -> dict[str, dict[str, float]]` with `runs, mean_wall_s, mean_wall_solved_s, mean_steps,
  mean_forks, pass_rate, any_pass_rate`; `verdict(summary, *, baseline, candidate, target_pct) -> Verdict`
  (reuse `lts_bench.Verdict`); `render`, `save`, `events_counts(path) -> {"steps","forks","rounds"}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_topology_bench.py`:

```python
"""bench:topology — single vs teams vs fork; verdict against the single-agent baseline (design §9)."""

from __future__ import annotations

from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.bench.topology_bench import TopoRow, apply_topology, render, summarize, verdict


def cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")},
        "teams": {"k": 3},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_apply_topology(tmp_path: Path) -> None:
    base = cfg(tmp_path)
    s = apply_topology(base, "single")
    assert (s.topology.kind, s.teams.k, s.lts.enabled) == ("teams", 1, False)
    t = apply_topology(base, "teams")
    assert (t.topology.kind, t.teams.k, t.lts.enabled) == ("teams", 3, True)
    f = apply_topology(base, "fork")
    assert f.topology.kind == "fork"
    assert base.teams.k == 3 and base.topology.kind == "teams"  # untouched


def row(topo: str, rep: int, wall: float, passed: bool, forks: int = 0) -> TopoRow:
    return TopoRow("t", topo, rep, wall, 10, 100, 50, forks, forks, passed, passed, 1 + 2 * forks, f"r{rep}")


def test_summary_and_verdict() -> None:
    rows = [row("single", 0, 100, True), row("single", 1, 300, False), row("fork", 0, 60, True, 1), row("fork", 1, 200, True, 2)]
    s = summarize(rows)
    assert s["single"]["mean_wall_s"] == 200 and s["single"]["mean_wall_solved_s"] == 100 and s["single"]["pass_rate"] == 0.5
    assert s["fork"]["mean_forks"] == 1.5 and s["fork"]["pass_rate"] == 1.0
    v = verdict(s, baseline="single", candidate="fork", target_pct=25.0)
    assert v.met and "MET" in v.text
    text = render(s, v)
    assert "| topology |" in text and "fork" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_topology_bench.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

`src/airlie/bench/topology_bench.py`:

```python
"""`task bench:topology`: single-agent baseline vs the spec's K teams vs fork-when-stuck on graded tasks.

Verdict (design §9): the candidate must not lower the pass rate and must either raise it or cut mean wall-clock
on solved runs by ≥ target_pct against `single`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from airlie.bench.lts_bench import Verdict
from airlie.config import AirlieConfig

TOPOLOGIES = ("single", "teams", "fork")


@dataclass
class TopoRow:
    task: str
    topology: str
    repeat: int
    wall_s: float
    steps: int
    prompt_tokens: int
    completion_tokens: int
    forks: int
    rounds: int
    winner_passed: bool
    any_passed: bool
    branches: int
    run_id: str


def apply_topology(cfg: AirlieConfig, name: str) -> AirlieConfig:
    c = cfg.model_copy(deep=True)
    if name == "single":
        c.topology.kind = "teams"
        c.teams.k = 1
        c.lts.enabled = False
    elif name == "teams":
        c.topology.kind = "teams"
    elif name == "fork":
        c.topology.kind = "fork"
    else:
        raise ValueError(f"unknown topology {name!r}; known: {', '.join(TOPOLOGIES)}")
    return c


def summarize(rows: list[TopoRow]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    by: dict[str, list[TopoRow]] = {}
    for r in rows:
        by.setdefault(r.topology, []).append(r)
    for topo, rs in by.items():
        n = len(rs)
        solved = [r for r in rs if r.winner_passed]
        out[topo] = {
            "runs": float(n),
            "mean_wall_s": round(sum(r.wall_s for r in rs) / n, 1),
            "mean_wall_solved_s": round(sum(r.wall_s for r in solved) / len(solved), 1) if solved else 0.0,
            "mean_steps": round(sum(r.steps for r in rs) / n, 1),
            "mean_forks": round(sum(r.forks for r in rs) / n, 2),
            "pass_rate": round(len(solved) / n, 3),
            "any_pass_rate": round(sum(1 for r in rs if r.any_passed) / n, 3),
        }
    return out


def verdict(summary: dict[str, dict[str, float]], *, baseline: str, candidate: str, target_pct: float) -> Verdict:
    b, c = summary.get(baseline), summary.get(candidate)
    if not b or not c:
        return Verdict(0.0, False, f"missing rows for {baseline!r} or {candidate!r}")
    bw, cw = b["mean_wall_solved_s"], c["mean_wall_solved_s"]
    red = round((bw - cw) / bw * 100.0, 1) if bw else 0.0
    no_regression = c["pass_rate"] >= b["pass_rate"]
    met = no_regression and (c["pass_rate"] > b["pass_rate"] or red >= target_pct)
    text = (
        f"{candidate} vs {baseline}: pass rate {b['pass_rate']:.2f} → {c['pass_rate']:.2f}; "
        f"wall-clock on solved runs {bw:.1f}s → {cw:.1f}s ({red:+.1f} %; target ≥ {target_pct:.0f} %)"
    )
    if not no_regression:
        text += "; pass rate regressed"
    text += "; criterion " + ("MET" if met else "NOT met")
    return Verdict(red, met, text)


def render(summary: dict[str, dict[str, float]], v: Verdict) -> str:
    cols = ["topology", "runs", "mean_wall_s", "mean_wall_solved_s", "mean_steps", "mean_forks", "pass_rate", "any_pass_rate"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for topo, s in summary.items():
        lines.append("| " + " | ".join([topo] + [f"{s[c]:g}" for c in cols[1:]]) + " |")
    return "\n".join([*lines, "", v.text])


def save(rows: list[TopoRow], summary: dict[str, dict[str, float]], v: Verdict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": [asdict(r) for r in rows], "summary": summary, "verdict": asdict(v)}, indent=1))
    return path


def events_counts(events_path: Path) -> dict[str, Any]:
    steps = forks = rounds = 0
    for line in events_path.read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "step":
            steps += 1
        elif e["kind"] == "fork_point":
            forks += 1
            rounds = max(rounds, int(e.get("round", 0)))
    return {"steps": steps, "forks": forks, "rounds": rounds}
```

`cli.py` — new command under `bench_app`, modelled on `bench_lts`:

```python
@bench_app.command("topology")
def bench_topology(
    tasks: Annotated[str, typer.Option(help="comma-separated task ids (default: all)")] = "",
    topologies: Annotated[str, typer.Option(help="comma-separated: single,teams,fork")] = "single,fork",
    repeats: int = 3,
    baseline: str = "single",
    target_pct: float = 25.0,
    tasks_dir: Path = Path("evals/tasks"),
    out: Path | None = None,
    config: ConfigOpt = None,
) -> None:
    """Single-agent baseline vs K teams vs fork-when-stuck on graded tasks; design 2026-10-13 §9 verdict."""
    import asyncio
    import time

    from airlie.bench.topology_bench import TopoRow, apply_topology, events_counts, render, save, summarize, verdict
    from airlie.evals import find_task, load_task, materialize

    cfg = _load(config)
    ids = [t.strip() for t in tasks.split(",") if t.strip()] or sorted(p.stem for p in tasks_dir.glob("*.yaml"))
    topos = [t.strip() for t in topologies.split(",") if t.strip()]
    rows: list[TopoRow] = []
    for task_id in ids:
        task = find_task(tasks_dir.resolve(), task_id) if task_id else load_task(Path(task_id))
        for rep in range(repeats):
            for topo in topos:
                orch = _orchestrator(apply_topology(cfg, topo))
                repo = materialize(task, orch.run_dir.path / "task-repo")
                console.print(f"[bold]{task.id}[/bold] topology={topo} repeat={rep} run={orch.run_dir.run_id}")
                t0 = time.monotonic()
                report = asyncio.run(orch.execute_task(task.requirement, repo=repo, test_cmd=task.run_cmd))
                wall = time.monotonic() - t0
                graded = {t.team_id: task.grade(orch.run_dir.path / t.team_id).passed for t in report.teams}
                counts = events_counts(report.run_dir.events_path)
                rows.append(
                    TopoRow(
                        task.id, topo, rep, round(wall, 1), counts["steps"],
                        sum(t.prompt_tokens for t in report.teams), sum(t.completion_tokens for t in report.teams),
                        counts["forks"], counts["rounds"], bool(graded.get(report.winner or "", False)),
                        any(graded.values()), len(report.teams), report.run_id,
                    )
                )
                console.print(
                    f"  wall {wall:.0f}s, steps {counts['steps']}, forks {counts['forks']}, "
                    f"winner {report.winner} {'PASS' if rows[-1].winner_passed else 'FAIL'}, any {any(graded.values())}"
                )
    summary = summarize(rows)
    for topo in topos:
        if topo != baseline:
            print(render(summary, verdict(summary, baseline=baseline, candidate=topo, target_pct=target_pct)))
    if out:
        v = verdict(summary, baseline=baseline, candidate=[t for t in topos if t != baseline][-1], target_pct=target_pct)
        console.print(f"saved {save(rows, summary, v, out)}")
```

`Taskfile.yml`, after `bench:lts`:

```yaml
  bench:topology:
    desc: "single agent vs K teams vs fork-when-stuck on graded tasks (task bench:topology -- --topologies single,teams,fork --repeats 3)"
    cmds:
      - '{{.RUN}} airlie bench topology {{.CLI_ARGS}}'
```

- [ ] **Step 4: Run everything**

Run: `uv run pytest tests/unit -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && task --list | grep bench:topology`
Expected: green; task listed.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/bench/topology_bench.py src/airlie/cli.py Taskfile.yml tests/unit/test_topology_bench.py
git commit -m "feat(bench): task bench:topology — single vs teams vs fork with the design §9 verdict"
```

---

### Task 9: Live verification, docs, decision on the default

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-10-12-airlie-design.md` (topology seam row + result
  section), `docs/bench/README.md`; create `docs/bench/2026-10-13-topology-bench.md` + JSON; possibly
  `airlie.yaml` (`topology.kind`).

- [ ] **Step 1: Smoke run**

Run (engine up via `task up`): `task eval -- inventory_cli --topology fork`
Expected: a report with `trunk` and, if it got stuck, `r1-1`/`r1-2` rows; `fork_point`/`fork_resolved` events in
`events.jsonl`; no tracebacks. Fix anything live-only (AutoGen state load with tools, nudge as first task) before
going on; add a unit test for each such fix.

- [ ] **Step 2: Benchmark**

Run: `task bench:topology -- --topologies single,teams,fork --tasks lang_detect,size_parse,inventory_cli,log_pipeline --repeats 3 --out docs/bench/topology.json`
(runs in the background; ~2–3 h). Record the table and the per-row list in
`docs/bench/2026-10-13-topology-bench.md` with the same honesty as the LTS write-ups: per-row stop reasons,
forks, where the wall-clock went.

- [ ] **Step 3: Decide the default**

If the verdict for `fork` vs `single` is MET: set `topology: { kind: fork }` in `airlie.yaml` and note the spec
deviation in the design doc's audit table (new row: "Topology — 👤 fork-when-stuck by evidence, teams selectable").
If `single` wins: set `teams.k: 1` and `lts.enabled: false` in `airlie.yaml` and say so. Either way the README
gains a "Topologies" section: the three options, the config line, `--topology`, `task bench:topology`.

- [ ] **Step 4: Commit**

```bash
git add README.md docs airlie.yaml
git commit -m "docs: topology bench results; default topology set by evidence"
```

---

## Self-review

- **Spec coverage.** §3 loop → Task 6; §3.1 trigger → `ForkObserver.stuck` + `FORK_REASONS` (Task 6); §3.2
  snapshot/resume → Task 1 (both teams) + Task 3 (`start_point`, `commit_wip`); §3.3 diversify → Task 5; §3.4
  resolve → `_resolve` (Task 6); §3.5 budgets → `child_steps`, `max_total_steps`, `teams.max_steps` (Tasks 4, 6);
  §4 repo map → Task 2 + wiring in Tasks 4 and 6; §5 config → Task 4; §6 code shape → Tasks 1–8 (the design's
  `preamble` lands on `_goal_message`); §7 errors → `_run_branch` cancel handling, `_resolve` drops
  `engine_error`, `hypotheses` returns `None` on `EngineError`; §8 tests → each task; §9 criterion → Task 8
  `verdict`; §10 decisions → applied (width 2, hypotheses, flip on evidence in Task 9).
- **Placeholders.** None; every step carries code and uses names verified in the repo (`write`/`MINIMAL` in
  `test_config.py`, `bench_app` in `cli.py`, `RoleSpec.stop/max_tokens/thinking`, `EngineSpec.served_name`).
- **Type consistency.** `TeamContext.stop_when: Callable[[StepRecord], bool] | None` is what `ForkObserver.stuck`
  satisfies; `TeamResult.state: dict[str, Any] | None` flows into `TeamContext.initial_state`; `add_worktree`'s
  `start_point` is the `commit_wip` sha; `build_topology` returns the `Topology` protocol both orchestrators
  satisfy (`run_dir` attribute + `execute_task` signature); `Verdict` is reused from `lts_bench`.
- **Review Focus.** 1 → Task 6 `test_resolve_uses_last_run_tests_outcome`; 2, 3 → Task 3 tests; 4 → Task 5
  `test_hypotheses_calls_the_role_once_and_falls_back_on_unparsable_output`; 5 → Task 2 tests (syntax-error file,
  non-Python file, cap).
