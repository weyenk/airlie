# The supervisor, layer 1: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At each point where the story pipeline already sends a role back (and where a layer blocks), the reviewer model (Muse) reads the evidence and adds a diagnosis and one next action. Every intervention and its outcome is recorded in an append-only SQLite ledger.

**Architecture:** A new package `airlie.supervisor` holds four pieces:
- the ledger, a SQLite store
- the answer parser
- the evidence builder
- the Muse call (`Triage`), plus a per-layer object (`LayerSupervisor`) that ties them together

The pipeline creates one `LayerSupervisor` per layer and calls it at the pre-code, static and mend retries, and at a block. The studio calls it at the gate retry, through an `Advisor` protocol it declares itself. The native team calls it at the repeat guard, through a `StuckAdvisor` protocol on `TeamContext`. Muse's text is always *added* to today's message (`with_advice`), and any failure falls back to today's text.

**Tech Stack:** Python 3.12, uv, stdlib `sqlite3`, pydantic config, typer CLI, pytest with `FakeEngine`.

**Spec:** `docs/superpowers/specs/2026-10-22-supervisor-triage-design.md`

## Global Constraints

- The test gate passes after every task: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Every action has a Taskfile task. uv only.
- No extra attempts, no rerouting, no skipped retries, and no change to which role a retry goes to.
- Muse never edits code or tests, and never clears or weakens a finding. Its tools are `read_file`, `list_dir`, `grep` and `run_test`, and nothing else.
- Today's message is sent whole. Muse's section is added after the findings, and for `feedback()` before its rule paragraph. Nothing is cut.
- Any Muse error, timeout, unparsable answer or capped-out layer means a fallback: today's text, plus a `supervisor_fallback` event and a ledger row with `fallback` set. The run never stops because of the supervisor.
- The supervisor fires only in story runs. Outside one, `orch.advisor` and `TeamContext.on_stuck` are `None`.
- The ledger is `observability.state_dir / "supervisor.sqlite"`, separate from `board.sqlite`, and append-only (enforced by SQL triggers).
- Fixed values (exact): `per_layer: 6`, triggers `precode_retry`, `static_retry`, `gate_retry`, `amend`, `repeat_guard`, `block`. Owners `tester`, `coder`, `existing_code`, `environment`, `unknown`. Outcomes `helped`, `not_helped`, `blocked`, `unknown`. Verdicts `right`, `wrong`.
- Seed categories: `missing_import`, `sandbox_escape`, `design_relitigation`, `empty_reply`, `repeat_loop`, `wrong_acceptance_test`, `not_this_roles_test`, `environment`.
- No real trace excerpts are committed (the repo is public). Test fixtures are synthetic.
- `ACT_FIRST`, `STILL_RED`, `LAST_TURN_RETRY`, `ONE_STEP_LEFT` and `REPEAT_WRITE_ONLY` are unchanged.

## Review Focus

1. **A layer whose findings are all `engine_error`.** Muse is on the same engine, so there is no triage at the block: the block message stays the "start it (`task up`)" one, and no Muse request is made. *Test in Task 8.*
2. **Muse's answer cut off by `max_tokens`, or prose with no JSON.** This is a fallback with reason `unparsable`. The role gets exactly today's text. *Test in Task 1 and Task 5.*
3. **The per-layer cap reached mid-layer.** The 7th trouble point on one layer makes no Muse request and records a fallback `cap`. *Test in Task 6.*
4. **A Muse `message` that tries to loosen a check** (for example "delete the failing test"). The rules paragraph is still in the message, unchanged, after Muse's section is added. *Test in Task 8: the full `feedback()` rule text is still present.*
5. **A story that stops (killed, cancelled, or blocked elsewhere) with an intervention still open.** It gets outcome `unknown`, written by `LayerSupervisor.close()` in a `finally`, so no intervention is left without an outcome. *Test in Task 6.*

---

### Task 1: Muse's answer, and how it's added to a message

**Files:**
- Create: `src/airlie/supervisor/__init__.py` (docstring only)
- Create: `src/airlie/supervisor/answer.py`
- Create: `src/airlie/supervisor/advice.py` (no airlie imports, so `layer_checks`, `studio` and `native_team` can import it)
- Test: `tests/unit/test_supervisor_answer.py`

**Interfaces:**
- Produces from `answer.py`:
  - `CATEGORIES: tuple[str, ...]`: the eight seed categories, in the order given in Global Constraints.
  - `OWNERS: tuple[str, ...]`
  - `@dataclass(frozen=True) class Answer`, with fields:
    - `category: str`, `owner: str`, `diagnosis: str`
    - `evidence: tuple[str, ...]`
    - `message: str | None`, `would_route_to: str | None`, `fix_if_attempt: str | None`
  - `class AnswerError(ValueError)`
  - `parse_answer(text: str, *, block: bool) -> Answer`
- Produces from `advice.py`:
  - `SUPERVISOR = "Supervisor: a second model read your work and the check's output. Its diagnosis and your next step:"`
  - `with_advice(text: str, advice: str | None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
GOOD = {"category": "missing_import", "owner": "tester", "diagnosis": "Path is not imported.",
        "evidence": ["NameError: name 'Path' is not defined"], "message": "Import Path, then edit.",
        "would_route_to": "tester", "fix_if_attempt": None}

def test_a_fenced_answer_parses(): 
    a = parse_answer("thinking...\n```json\n" + json.dumps(GOOD) + "\n```", block=False)
    assert (a.category, a.owner, a.message) == ("missing_import", "tester", "Import Path, then edit.")

def test_a_new_snake_case_category_is_kept():
    assert parse_answer(json.dumps({**GOOD, "category": "fixture_leak"}), block=False).category == "fixture_leak"

@pytest.mark.parametrize("bad", [{"category": "Missing Import"}, {"category": "other_thing"},
    {"owner": "chief"}, {"message": None}, {"message": "  "}, {"diagnosis": ""}])
def test_an_answer_missing_or_breaking_a_rule_is_refused(bad):
    with pytest.raises(AnswerError):
        parse_answer(json.dumps({**GOOD, **bad}), block=False)

def test_at_a_block_no_message_is_needed():
    assert parse_answer(json.dumps({**GOOD, "message": None, "fix_if_attempt": "add it"}), block=True).message is None

def test_prose_with_no_json_is_refused():
    with pytest.raises(AnswerError):
        parse_answer("I think the tester should import Path.", block=False)

def test_with_advice_keeps_the_text_whole_and_adds_the_section_after_it():
    assert with_advice("RULES", "Import Path.") == f"RULES\n\n{SUPERVISOR}\nImport Path."
    assert with_advice("RULES", None) == "RULES" and with_advice("RULES", "") == "RULES"
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_supervisor_answer.py -q`. Expected: FAIL (import error).

- [ ] **Step 3: Implement `parse_answer` and `with_advice`**

Parse with `airlie.topology.studio_plan.extract_json_block` (it handles fenced and bare JSON).

Validation rules:
- The category matches `^[a-z][a-z0-9_]{2,39}$` and does not start with `other`.
- `owner` is in `OWNERS`.
- `diagnosis` is non-blank.
- `message` is non-blank unless `block`.
- `would_route_to` is `None`, `"tester"` or `"coder"`.
- `evidence` is a list of strings (it may be missing; that means `()`).

Strip strings.

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_supervisor_answer.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/supervisor/__init__.py src/airlie/supervisor/answer.py src/airlie/supervisor/advice.py tests/unit/test_supervisor_answer.py
git commit -m "Supervisor: Muse's answer, parsed and checked, and how its advice is added to a message"
```

---

### Task 2: The ledger

**Files:**
- Create: `src/airlie/supervisor/ledger.py`
- Test: `tests/unit/test_supervisor_ledger.py`

**Interfaces:**
- Consumes: `Answer` from Task 1 (`airlie.supervisor.answer`).
- Produces:
  - `Trigger = Literal["precode_retry","static_retry","gate_retry","amend","repeat_guard","block"]`
  - `Outcome = Literal["helped","not_helped","blocked","unknown"]`
  - `Verdict = Literal["right","wrong"]`
  - `@dataclass(frozen=True) class Intervention`, with fields:
    - `story: str`, `layer: str`, `studio_run: str`, `subtask: str`, `role: str`, `trigger: Trigger`
    - `kinds: tuple[str, ...]`
    - `answer: Answer | None`, `fallback: str | None`
    - `model: str`, `seconds: float`, `tokens: int`
  - `@dataclass(frozen=True) class Row`: every column of `interventions`, plus `outcome: Outcome | None` (the latest) and `verdict: Verdict | None` (the latest).
  - `@dataclass(frozen=True) class Stats`, with fields:
    - `by_trigger: dict[str, tuple[int, int]]` as (interventions, helped)
    - `by_category: dict[str, tuple[int, int]]`
    - `fallbacks: int`, `total: int`
    - `blocks_with_fix: list[int]` (intervention ids)
  - `class LedgerError(ValueError)`
  - `class Ledger`, with methods:
    - `__init__(self, path: Path)`
    - `record(self, i: Intervention) -> int`
    - `outcome(self, intervention_id: int, outcome: Outcome, detail: str = "") -> None`
    - `verdict(self, intervention_id: int, verdict: Verdict, note: str = "") -> None`
    - `recent(self, limit: int = 20) -> list[Row]`
    - `for_layer(self, story: str, layer: str) -> list[Row]`
    - `recall(self, trigger: Trigger, kinds: Sequence[str], limit: int = 3) -> list[Row]`
    - `stats(self) -> Stats`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_recorded_intervention_reads_back_with_its_latest_outcome(tmp_path):
    led = Ledger(tmp_path / "supervisor.sqlite")
    i = led.record(intervention(trigger="precode_retry", category="missing_import"))
    led.outcome(i, "not_helped"); led.outcome(i, "helped", "check passed")
    [row] = led.recent()
    assert (row.id, row.category, row.outcome) == (i, "missing_import", "helped")

def test_the_ledger_refuses_updates_and_deletes(tmp_path):
    led = Ledger(tmp_path / "s.sqlite"); led.record(intervention())
    con = sqlite3.connect(tmp_path / "s.sqlite")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE interventions SET category = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM outcomes")

def test_a_verdict_on_an_unknown_id_is_refused(tmp_path):
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "s.sqlite").verdict(99, "right")

def test_a_fallback_row_has_no_answer_and_counts_as_a_fallback(tmp_path):
    led = Ledger(tmp_path / "s.sqlite")
    led.record(intervention(answer=None, fallback="timeout"))
    assert led.stats().fallbacks == 1 and led.recent()[0].category is None

def test_recall_returns_only_helped_rows_with_the_same_trigger_and_a_shared_kind(tmp_path):
    led = Ledger(tmp_path / "s.sqlite")
    a = led.record(intervention(trigger="precode_retry", kinds=("passes_before_code",))); led.outcome(a, "helped")
    b = led.record(intervention(trigger="precode_retry", kinds=("passes_before_code",))); led.outcome(b, "not_helped")
    c = led.record(intervention(trigger="gate_retry", kinds=("passes_before_code",))); led.outcome(c, "helped")
    assert [r.id for r in led.recall("precode_retry", ["passes_before_code"])] == [a]

def test_stats_count_help_by_trigger_and_category_and_list_blocks_with_a_fix(tmp_path):
    led = Ledger(tmp_path / "s.sqlite")
    a = led.record(intervention(trigger="precode_retry", category="missing_import")); led.outcome(a, "helped")
    b = led.record(intervention(trigger="block", fix_if_attempt="add the import")); led.outcome(b, "blocked")
    s = led.stats()
    assert s.by_trigger["precode_retry"] == (1, 1) and s.by_category["missing_import"] == (1, 1)
    assert s.blocks_with_fix == [b] and s.total == 2
```

`intervention(**kw)` is a test helper that builds an `Intervention` with an `Answer`. Its keywords: `category`, `fix_if_attempt`, `answer`, `fallback`, `trigger`, `kinds`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_supervisor_ledger.py -q`
Expected: FAIL (`ModuleNotFoundError: airlie.supervisor`).

- [ ] **Step 3: Implement `Ledger` in `src/airlie/supervisor/ledger.py`**

Follow `src/airlie/board/store.py`: stdlib sqlite3, WAL, one connection with `check_same_thread=False`, a `threading.Lock` around writes, and the schema created on open with `CREATE ... IF NOT EXISTS`.

Tables:
- `interventions`: `id INTEGER PRIMARY KEY`, `at REAL`, `story`, `layer`, `studio_run`, `subtask`, `role`, `trigger TEXT NOT NULL`, `kinds_json TEXT NOT NULL`. Then nullable: `category`, `owner`, `diagnosis`, `evidence_json`, `message`, `would_route_to`, `fix_if_attempt`, `fallback`. Then `model TEXT`, `seconds REAL`, `tokens INTEGER`.
- `outcomes(id INTEGER PRIMARY KEY, intervention_id INTEGER NOT NULL, at REAL, outcome TEXT NOT NULL, detail TEXT NOT NULL)`
- `verdicts(id INTEGER PRIMARY KEY, intervention_id INTEGER NOT NULL, at REAL, verdict TEXT NOT NULL, note TEXT NOT NULL)`

For each of the three tables, add a `BEFORE UPDATE` and a `BEFORE DELETE` trigger that does `SELECT RAISE(ABORT, 'append-only')`.

The latest outcome or verdict is the row with the highest `id` for that intervention. The path is `expanduser`'d and its parent created.

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/unit/test_supervisor_ledger.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/supervisor tests/unit/test_supervisor_ledger.py
git commit -m "Supervisor: an append-only ledger of interventions, outcomes and verdicts"
```

---

### Task 3: Configuration, the role, and its prompt

**Files:**
- Modify: `src/airlie/config/schema.py` (add `SupervisorSpec`; add `StorySpec.supervisor`; add a validator on the root config next to the existing role checks, around `schema.py:625`)
- Modify: `airlie.yaml` (the `supervisor` role after `reviewer`, and `supervisor:` under `story:`)
- Create: `prompts/roles/supervisor.md`
- Test: `tests/unit/test_config_supervisor.py`

**Interfaces:**
- Produces `class SupervisorSpec(Strict)`, with fields:
  - `enabled: bool = False`. False in the schema, so other configs and tests are unchanged; `airlie.yaml` turns it on.
  - `role: str = "supervisor"`
  - `per_layer: int = Field(default=6, ge=1)`
  - `timeout_s: float = Field(default=300.0, gt=0)`
  - `trace_steps: int = Field(default=6, ge=1)`
  - `recall: int = Field(default=3, ge=0)`
- `StorySpec.supervisor: SupervisorSpec = Field(default_factory=SupervisorSpec)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_shipped_config_turns_the_supervisor_on_with_muse_and_read_only_tools():
    cfg = load_config(Path("airlie.yaml"))
    assert cfg.story.supervisor.enabled and cfg.story.supervisor.per_layer == 6
    role = cfg.roles["supervisor"]
    assert role.model == "reviewer" and role.tools == ["read_file", "list_dir", "grep"]
    assert role.prompt.read_text().strip()

def test_an_enabled_supervisor_needs_its_role(tmp_path):
    with pytest.raises(ValueError, match="story.supervisor.role"):
        config_with(tmp_path, story={"supervisor": {"enabled": True, "role": "nobody"}})

def test_off_by_default_in_the_schema(tmp_path):
    assert not config_with(tmp_path).story.supervisor.enabled
```

`config_with` is whatever the existing config tests use to build a config from a dict (see `tests/unit/test_config*.py`). Reuse it.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_config_supervisor.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement the schema, the yaml and the prompt**

The validator raises `"story.supervisor.role '<name>' is not a role"` when `enabled` and the role is missing.

The `airlie.yaml` role copies the reviewer's settings:

```yaml
  supervisor:   { prompt: prompts/roles/supervisor.md, model: reviewer, tools: [read_file, list_dir, grep], max_steps: 6, max_context: 131072, max_tokens: 8192, thinking: false, chat_template_kwargs: { reasoning_strength: low }, sampling: { temperature: 0.2, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }   # layer 1 of the supervisor (spec 2026-10-22): advisory triage on the reviewer model; run_test is added by the triage, not listed
```

Under `story:`:

```yaml
  supervisor: { enabled: true, per_layer: 6, timeout_s: 300 }   # the trial (spec 2026-10-22); false = today's behaviour exactly
```

`prompts/roles/supervisor.md` uses the same front-matter shape as `chief.md` (`summary:`, `routable: false`). Its body says:
- Who it is: the studio's supervisor, a second reader from another model family.
- What it gets: the trigger, the findings, what the role is being told, the role's recent steps, the diff, the layer's events, and past fixes.
- Its tools are read-only, and `run_test` runs one pytest node id.
- Its job: find what actually trips the role, and give one concrete next action that the role can take inside its own files.
- It never tells a role to delete, skip, weaken or loosen a test or a check. It never asks the tester to write the feature, or the coder to change a locked test.
- Name a seed category when one fits, or a new snake_case one.
- Answer with exactly one JSON object with the seven fields of `Answer`. `message` is null only at `block`, and `fix_if_attempt` is set only at `block`.

- [ ] **Step 4: Run the tests and the config doctor**

Run: `uv run pytest tests/unit/test_config_supervisor.py -q && uv run airlie config check`. If the config command is named differently, use `task config`.
Expected: PASS, and the config validates.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py airlie.yaml prompts/roles/supervisor.md tests/unit/test_config_supervisor.py
git commit -m "Supervisor: the role on the reviewer model, its prompt, and story.supervisor (on for the trial)"
```

---

### Task 4: What Muse sees (the evidence builder)

**Files:**
- Create: `src/airlie/supervisor/evidence.py`
- Test: `tests/unit/test_supervisor_evidence.py`

**Interfaces:**
- Consumes: `Row` (Task 2).
- Produces:
  - `trace_tail(trace_dir: Path, subtask: str, steps: int, chars: int = 12_000) -> str`. It reads `trace_dir/<subtask>/` or its highest `round-N/`, whichever holds the newest `step-NNN.json`, and returns the last `steps` steps. Each step gives: its number, the tail of `reasoning` (last 1,500 chars), `assistant_text`, and each tool call with the first 300 chars of its observation (from `u_t` / `o_t`). The whole is cut to `chars`, keeping the end. A missing folder gives `""`.
  - `layer_events(events: Path, layer: str, chars: int = 6_000) -> str`: the `events.jsonl` lines whose `layer` equals `layer`, one compact JSON per line, cut keeping the end.
  - `@dataclass(frozen=True) class TriageInput`, with fields:
    - `trigger: str`, `role: str`, `subtask: str`
    - `told: str` (today's message)
    - `evidence: str` (findings or the gate's tail)
    - `kinds: tuple[str, ...]`
    - `brief: str`, `trace: str`, `diff: str`, `events: str`
    - `earlier: tuple[Row, ...]`, `recall: tuple[Row, ...]`
  - `triage_message(inp: TriageInput) -> str`: one user message with `##` sections in this order: Trigger, What the role is about to be told, Findings, The layer's brief, The role's recent steps, The diff, This layer's events, Earlier on this layer, Fixes that helped before. Empty sections are left out. The brief is capped at 8,000 chars, the diff at 20,000, and each recalled row at 600.

- [ ] **Step 1: Write the failing tests**

```python
def step(n, reasoning="", text="", tool=None, obs=""):  # writes a synthetic step-NNN.json shaped like LoggingObserver's
    ...

def test_trace_tail_reads_the_latest_round_and_its_last_steps(tmp_path):
    for n in range(1, 4): write_step(tmp_path / "tests", n, reasoning=f"first-run {n}")
    for n in range(1, 9): write_step(tmp_path / "tests" / "round-1", n, reasoning=f"retry {n}", tool="read_file")
    tail = trace_tail(tmp_path, "tests", steps=3)
    assert "retry 8" in tail and "retry 6" in tail and "retry 5" not in tail and "first-run" not in tail

def test_trace_tail_keeps_the_end_of_long_reasoning(tmp_path):
    write_step(tmp_path / "tests", 1, reasoning="x" * 5000 + "THE END")
    assert "THE END" in trace_tail(tmp_path, "tests", steps=1)

def test_a_missing_trace_is_empty(tmp_path):
    assert trace_tail(tmp_path, "build", steps=3) == ""

def test_layer_events_keep_only_this_layers_lines(tmp_path):
    ev = tmp_path / "events.jsonl"
    ev.write_text('{"kind":"a","layer":"01"}\n{"kind":"b","layer":"02"}\n{"kind":"c"}\n')
    assert '"b"' in layer_events(ev, "02") and '"a"' not in layer_events(ev, "02")

def test_the_message_orders_its_sections_and_drops_empty_ones():
    msg = triage_message(TriageInput("precode_retry", "tester", "tests", "TOLD", "FINDINGS", ("passes_before_code",),
                                     "BRIEF", "", "DIFF", "", (), ()))
    assert msg.index("TOLD") < msg.index("FINDINGS") < msg.index("BRIEF") < msg.index("DIFF")
    assert "recent steps" not in msg and "Earlier on this layer" not in msg
```

Read `LoggingObserver.after_step` (`src/airlie/orchestrator.py:98`) for the step file's exact keys, and make `write_step` produce that shape.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_supervisor_evidence.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement the three functions and `TriageInput`**

- [ ] **Step 4: Run the tests to see them pass.** Run: `uv run pytest tests/unit/test_supervisor_evidence.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/supervisor/evidence.py tests/unit/test_supervisor_evidence.py
git commit -m "Supervisor: what Muse reads (the role's trace tail, the layer's events, the triage message)"
```

---

### Task 5: The Muse call, and `run_test`

**Files:**
- Create: `src/airlie/supervisor/triage.py`
- Modify: `src/airlie/story/layer_checks.py` (add `PytestChecks.run_one`)
- Test: `tests/unit/test_supervisor_triage.py`, plus one test in `tests/unit/test_story_layer_checks.py`

**Interfaces:**
- Consumes: `TriageInput`, `triage_message` (Task 4), `parse_answer`, `Answer`, `AnswerError` (Task 1), `generate_with_tools` (`airlie.tools.loop`), `ToolRegistry`.
- Produces:
  - `PytestChecks.run_one(self, worktree: Path, test: str) -> str`: the gate's pytest step on that node id alone, confined as `_suite` is, returning the end of the output (`_TAIL_CHARS`).
  - `class RunOneTest`, a Tool:
    - `name = "run_test"`
    - parameters `{"test": {"type": "string", "description": "a pytest node id, e.g. tests/test_x.py::test_y"}}`, required `["test"]`
    - `__init__(self, checks: PytestChecks)`
    - `run(args, ctx)` returns `ToolResult(True, await checks.run_one(ctx.worktree, args["test"]))`. A blank `test` is `ToolResult(False, "error: give one pytest node id")`.
  - `class TriageFailed(Exception)`, with a `reason: str` attribute: one of `engine_error`, `timeout`, `unparsable`, `cut_off`.
  - `@dataclass(frozen=True) class Triaged`, with fields `answer: Answer`, `seconds: float`, `tokens: int`.
  - `class Triage`, with:
    - `__init__(self, engine: Engine, role_name: str, role: RoleSpec, tools: Callable[[Path], ToolRegistry], timeout_s: float)`
    - `async def __call__(self, inp: TriageInput, worktree: Path) -> Triaged` (raises `TriageFailed`)
    - `model: str` attribute (the role's model name, for the ledger)

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_scripted_answer_comes_back_parsed(tmp_path):
    engine = FakeEngine({"supervisor": [FakeReply(text=json.dumps(GOOD))]})
    out = await triage(engine)(an_input(), tmp_path)
    assert out.answer.category == "missing_import" and engine.requests[0].role == "supervisor"

async def test_muse_may_run_one_test_before_it_answers(tmp_path):
    engine = FakeEngine({"supervisor": [
        FakeReply(tool_calls=[ToolCall("c", "run_test", json.dumps({"test": "tests/test_a.py::test_a"}))]),
        FakeReply(text=json.dumps(GOOD))]})
    ran = []
    await triage(engine, run_one=lambda wt, t: ran.append(t) or "NameError")(an_input(), tmp_path)
    assert ran == ["tests/test_a.py::test_a"]
    assert "NameError" in engine.requests[1].messages[-1].content

@pytest.mark.parametrize("reply,reason", [(FakeReply(text="no json here"), "unparsable"),
                                           (FakeReply(text="", finish_reason="length"), "cut_off")])
async def test_an_unusable_answer_is_a_triage_failure(tmp_path, reply, reason):
    with pytest.raises(TriageFailed) as e:
        await triage(FakeEngine({"supervisor": [reply]}))(an_input(), tmp_path)
    assert e.value.reason == reason

async def test_an_engine_error_is_a_triage_failure(tmp_path):
    with pytest.raises(TriageFailed, match="engine_error"):
        await triage(FakeEngine({}))(an_input(), tmp_path)   # no script: FakeEngine raises EngineError

async def test_a_slow_answer_times_out(tmp_path):
    with pytest.raises(TriageFailed) as e:
        await triage(SlowEngine(seconds=5), timeout_s=0.05)(an_input(), tmp_path)
    assert e.value.reason == "timeout"
```

The `triage(...)` helper builds a `Triage` whose tools factory gives a registry with `read_file` and a `RunOneTest` over a stub with `run_one`. If `FakeReply` has no `finish_reason`, use whatever `FakeEngine` offers for a cut-off reply (see `src/airlie/engine/fake.py`). `SlowEngine` is a small test double whose `generate` sleeps.

In `test_story_layer_checks.py`, add a test that runs `run_one` on a real tiny repo, in the style of that file's existing suite tests:

```python
async def test_run_one_runs_only_the_named_test(tmp_path):
    ... two tests, one failing with NameError ...
    out = await checks.run_one(repo, "tests/test_a.py::test_bad")
    assert "NameError" in out and "test_good" not in out
```

- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_supervisor_triage.py tests/unit/test_story_layer_checks.py -q -k "triage or run_one"`. Expected: FAIL.

- [ ] **Step 3: Implement `run_one`, `RunOneTest` and `Triage`**

`Triage.__call__`:
- Build the `GenRequest`: role `role_name`, system prompt `prompt_text(role.prompt)`, user `triage_message(inp)`, and the role's `sampling`, `max_tokens`, `thinking` and `chat_template_kwargs`, the same way `Reviewer.review` builds its request (`src/airlie/topology/review.py:122`).
- Run `generate_with_tools(engine, req, tools(worktree), role.max_steps or DEFAULT_STEPS)` inside `asyncio.wait_for(..., timeout_s)`.
- Map the errors to reasons:
  - `EngineError` or `OSError`: `engine_error`
  - `asyncio.TimeoutError`: `timeout`
  - `finish_reason == "length"` with blank text: `cut_off`
  - `AnswerError`: `unparsable`
- `block = inp.trigger == "block"`.
- `tokens` is the final result's `prompt_tokens + completion_tokens`.

- [ ] **Step 4: Run the tests to see them pass.** Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/supervisor/triage.py src/airlie/story/layer_checks.py tests/unit/test_supervisor_triage.py tests/unit/test_story_layer_checks.py
git commit -m "Supervisor: the Muse call (read-only tools and run_test, a timeout, every failure a reason)"
```

---

### Task 6: `LayerSupervisor`, one per layer

**Files:**
- Create: `src/airlie/supervisor/layer.py`
- Test: `tests/unit/test_supervisor_layer.py`

**Interfaces:**
- Consumes: `Ledger`, `Intervention`, `Trigger`, `Outcome` (Task 2), `Triage`, `Triaged`, `TriageFailed` (Task 5), `TriageInput`, `trace_tail`, `layer_events` (Task 4), `SupervisorSpec` (Task 3), `diff_between` (`airlie.teams.worktree`).
- Produces `class LayerSupervisor`:
  - Constructor: `__init__(self, *, ledger: Ledger, triage: Triage, spec: SupervisorSpec, story: str, layer: str, studio_run: str, trace_dir: Path, worktree: Path, base_sha: str, brief: str, events: Path, emit: Callable[..., None])`.
  - `async def advise(self, trigger: str, subtask: str, role: str, told: str, *, kinds: Sequence[str] = (), evidence: str = "") -> str | None`
    - Returns Muse's `message`, or `None` on any fallback.
    - Records one ledger row either way, and emits `supervisor_triage` (`trigger`, `category`, `owner`, `ledger_id`, `layer`) or `supervisor_fallback` (`trigger`, `reason`, `ledger_id`, `layer`).
    - Keeps the row open for `settle`.
    - Past `spec.per_layer` calls on this layer, it makes no Muse request, and the fallback reason is `cap`.
  - `async def at_block(self, subtask: str, role: str, reason: str, *, kinds: Sequence[str] = (), evidence: str = "") -> str | None`
    - Trigger `block`.
    - Returns the text for the card: `diagnosis`, followed by `"If it had one more attempt: <fix_if_attempt>"` when given. Returns `None` on fallback.
    - Writes outcome `blocked` at once.
  - `advise`, `at_block` and `settle` take `trigger` and `outcome` typed `str`, not the Literals, so that `LayerSupervisor` satisfies Task 7's `Advisor` protocol under mypy. They check the value against `Trigger` and `Outcome` at run time and raise `ValueError` on anything else.
  - `def settle(self, trigger: str, outcome: str, detail: str = "") -> None`: writes the outcome of the open row for `trigger` (the latest one), then closes it. With none open, nothing happens.
  - The `StuckAdvisor` protocol (Task 7):
    - `async def stuck(self, subtask: str, told: str, messages: Sequence[Message]) -> str | None`: `advise("repeat_guard", ...)`, with `evidence` set to the last 8 messages rendered `role: content[:800]`.
    - `def stuck_settled(self, subtask: str, helped: bool) -> None`: `settle("repeat_guard", "helped" if helped else "not_helped")`.
  - `def close(self) -> None`: every row still open gets `unknown`.
- Behaviour:
  - `earlier` is `ledger.for_layer(story, layer)` and `recall` is `ledger.recall(trigger, kinds, spec.recall)`, both taken before the call.
  - `trace` is `trace_tail(trace_dir, subtask, spec.trace_steps)`, `diff` is `diff_between(worktree, base_sha, 20_000)`, and `events` is `layer_events(events, layer)`.
  - Only the triage call is guarded. A failure in building the evidence (for example a missing worktree) also falls back, with reason `evidence: <ExceptionName>`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_advice_is_returned_recorded_and_announced(tmp_path):
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD])
    msg = await sup.advise("precode_retry", "tests", "tester", "TOLD", kinds=("passes_before_code",), evidence="F")
    assert msg == GOOD["message"]
    [row] = led.recent(); assert row.trigger == "precode_retry" and row.outcome is None
    assert [e[0] for e in events] == ["supervisor_triage"]

async def test_settle_writes_the_outcome_once(tmp_path):
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    sup.settle("precode_retry", "helped"); sup.settle("precode_retry", "not_helped")
    assert led.recent()[0].outcome == "helped"

async def test_a_failed_triage_is_a_recorded_fallback_and_no_advice(tmp_path):
    sup, led, events = layer_supervisor(tmp_path, answers=["not json"])
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    assert led.recent()[0].fallback == "unparsable" and events[0][0] == "supervisor_fallback"

async def test_past_the_per_layer_cap_muse_is_not_asked(tmp_path):
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD] * 6, per_layer=6)
    for _ in range(7): await sup.advise("repeat_guard", "build", "coder", "TOLD")
    assert led.recent(1)[0].fallback == "cap" and sup.triage_calls == 6

async def test_a_block_gets_the_diagnosis_and_the_fix_it_would_have_tried(tmp_path):
    ans = {**GOOD, "message": None, "fix_if_attempt": "import Path"}
    sup, led, _ = layer_supervisor(tmp_path, answers=[ans])
    text = await sup.at_block("tests", "tester", "layer 02: ...")
    assert GOOD["diagnosis"] in text and "import Path" in text and led.recent()[0].outcome == "blocked"

async def test_close_leaves_no_intervention_without_an_outcome(tmp_path):
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    await sup.advise("amend", "tests", "tester", "TOLD"); sup.close()
    assert led.recent()[0].outcome == "unknown"

async def test_earlier_interventions_on_the_layer_reach_the_next_triage(tmp_path):
    sup, _, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    assert "Earlier on this layer" in sup.engine.requests[-1].messages[1].content
```

`layer_supervisor(tmp_path, answers, per_layer=6)` builds a `LayerSupervisor` from:
- a `FakeEngine` scripted with `answers` for role `supervisor` (it exposes `.engine`)
- a `Ledger` in `tmp_path`
- a real git worktree with one commit
- an emit that appends `(kind, fields)` to `events`

`triage_calls` is a counter on `LayerSupervisor`, kept for this test and for `stats` debugging.

- [ ] **Step 2: Run them to see them fail.** `uv run pytest tests/unit/test_supervisor_layer.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement `LayerSupervisor`**

- [ ] **Step 4: Run the tests to see them pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/supervisor/layer.py tests/unit/test_supervisor_layer.py
git commit -m "Supervisor: one per layer (advise, settle, at_block, a cap, nothing left open)"
```

---

### Task 7: The gate retry (studio) and the repeat guard (team)

**Files:**
- Modify: `src/airlie/teams/protocol.py` (the `StuckAdvisor` protocol, and `TeamContext.on_stuck: StuckAdvisor | None = None`)
- Modify: `src/airlie/teams/native_team.py` (the repeat-guard branch at `native_team.py:680-705`, and the end of `run`)
- Modify: `src/airlie/topology/studio.py` (the `Advisor` protocol, `self.advisor: Advisor | None = None` in `__init__`, `_context` passing `on_stuck=self.advisor`, and `_gate`)
- Test: `tests/unit/test_native_team.py`, `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Produces in `teams/protocol.py`:
  ```python
  class StuckAdvisor(Protocol):
      async def stuck(self, subtask: str, told: str, messages: Sequence[Message]) -> str | None: ...
      def stuck_settled(self, subtask: str, helped: bool) -> None: ...
  ```
- Produces in `topology/studio.py`:
  ```python
  class Advisor(StuckAdvisor, Protocol):
      async def advise(self, trigger: str, subtask: str, role: str, told: str, *,
                       kinds: Sequence[str] = (), evidence: str = "") -> str | None: ...
      def settle(self, trigger: str, outcome: str, detail: str = "") -> None: ...
  ```
  `LayerSupervisor` satisfies both structurally. `studio.py` does not import `airlie.supervisor` except `with_advice` from `airlie.supervisor.advice`.
- Behaviour, `native_team`:
  - Where `REPEATED`/`CYCLED` is sent, `told = with_advice(told, await ctx.on_stuck.stuck(ctx.team_id, told, messages))` when `on_stuck` is set.
  - When `REPEAT_WRITE_ONLY` is sent, or the run stops `repeated_action`, call `ctx.on_stuck.stuck_settled(ctx.team_id, False)`.
  - When the run ends, call `ctx.on_stuck.stuck_settled(ctx.team_id, ok)`. A repeat-guard row that is already settled ignores it.
  - An exception from `on_stuck.stuck` is caught, and the fixed text is used.
- Behaviour, `studio._gate`:
  - Build `text = f"{GATE_FAILED}\n\n```\n{tail}\n```"`.
  - If `self.advisor`, set `text = with_advice(text, await self.advisor.advise("gate_retry", follow_up, run.role, text, kinds=("gate_fails",), evidence=tail))`.
  - After the re-run, call `self.advisor.settle("gate_retry", "helped" if passed else "not_helped")`.

- [ ] **Step 1: Write the failing tests**

```python
# test_native_team.py
async def test_the_repeat_nudge_carries_the_advisors_section_once(tmp_path):
    adv = RecordingAdvisor(reply="Stop reading calc.py; write the test.")
    res = await run_repeating_writer(tmp_path, on_stuck=adv)        # reuse the nudge_stuck repeat fixture at :790
    nudge = user_messages(res)[REPEAT_INDEX]
    assert nudge.startswith(REPEATED.format(n=3, tool="read_file")[:40]) and "write the test" in nudge
    assert adv.stuck_calls == 1 and adv.settled == [("build", False)]   # it went on to REPEAT_WRITE_ONLY

async def test_an_advisor_that_raises_leaves_the_fixed_nudge(tmp_path):
    res = await run_repeating_writer(tmp_path, on_stuck=RaisingAdvisor())
    assert SUPERVISOR not in user_messages(res)[REPEAT_INDEX]

# test_studio_orchestrator.py
async def test_the_gate_follow_up_carries_the_advisors_section_and_its_outcome(tmp_path):
    ...  # reuse the existing gate-fails-then-follow-up test; set orch.advisor = RecordingAdvisor("Fix rounding in mul.")
    assert "Fix rounding in mul." in coder_follow_up and GATE_FAILED in coder_follow_up
    assert adv.settled_triggers == [("gate_retry", "helped")]
```

`RecordingAdvisor` and `RaisingAdvisor` are small test doubles implementing `Advisor`. `run_repeating_writer` wraps the existing fixture of `test_a_writer_with_nudge_stuck_that_repeats_again_loses_the_tool_then_its_run`.

- [ ] **Step 2: Run them to see them fail.** `uv run pytest tests/unit/test_native_team.py tests/unit/test_studio_orchestrator.py -q -k "advisor"`. Expected: FAIL.

- [ ] **Step 3: Implement the protocol, the hook and the gate change**

- [ ] **Step 4: Run both full files.** `uv run pytest tests/unit/test_native_team.py tests/unit/test_studio_orchestrator.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/teams src/airlie/topology/studio.py tests/unit/test_native_team.py tests/unit/test_studio_orchestrator.py
git commit -m "Studio and team: an advisor's section at the gate retry and the repeat guard"
```

---

### Task 8: The pipeline's trouble points (pre-code, static, mend, block)

**Files:**
- Modify: `src/airlie/story/layer_checks.py` (`feedback()` gets `advice`, and a `findings_text` helper is added)
- Modify: `src/airlie/story/pipeline.py`: `StoryPipeline.__init__` (the ledger), `_build`, `_check_tests`, `_check_static`, `_amend_tests`, `StoryBlocked`, `_block`
- Test: `tests/unit/test_story_supervisor_flow.py` (new; it reuses the helpers in `test_story_layer_checks_flow.py`)

**Interfaces:**
- Consumes: `LayerSupervisor` (Task 6), `Triage`, `RunOneTest` (Task 5), `Ledger` (Task 2), `with_advice` (Task 1).
- Produces:
  - `feedback(findings, *, before_code=True, advice: str | None = None) -> str`: the findings lines, then `SUPERVISOR` and the advice, then the rule paragraph (`what`), unchanged. With no advice, the output is exactly today's.
  - `findings_text(found: Sequence[Finding]) -> str`: one block per finding with `kind`, `test`, `why` and `source[:1500]`.
  - `class StoryBlocked(Exception)`: `__init__(self, reason: str, diagnosis: str | None = None)`, keeping `.diagnosis`. `str(e)` is still the reason.
  - `StoryPipeline._block(st, card, reason, diagnosis: str | None = None)`. When a diagnosis is given, the note is `"stopped after …: <reason>\nSupervisor: <diagnosis capped at REASON_CHARS>\nResume: …"`, and the `blocked` event gets `diagnosis=`.
  - `StoryPipeline._supervisor(self, st, card, layer, nn, orch, wt, base_sha) -> LayerSupervisor | None`. It returns `None` when `story.supervisor.enabled` is false.
- Wiring, in this order:
  1. `__init__`: `self.ledger = Ledger(cfg.observability.state_dir / "supervisor.sqlite") if self.s.supervisor.enabled else None`.
  2. `_build`: create `sup` after `orch` exists, then set `orch.advisor = sup` (the attribute from Task 7). Wrap the whole layer body after that point in `try: ... finally: sup.close()`.
     - The brief is `layer_plan`'s brief for the subtask, or `layer.brief` with its acceptance criteria.
     - The trace dir is `orch.run_dir.path / "trace"`.
     - `Triage`'s tools factory builds `ToolRegistry.builtin(["read_file","list_dir","grep"], ToolContext(worktree, cfg.tools.shell_timeout_s, cfg.tools.output_cap_chars, **cfg.tools.confinement()))`, plus `RunOneTest(checks)` when `checks_for(self.commands, ...)` is not `None`.
  3. `_check_tests`: before the retry, `advice = await sup.advise("precode_retry", "tests", orch.runs["tests"].role, feedback(found), kinds=[f.kind for f in found], evidence=findings_text(found))`, then `follow_up("tests", feedback(found, advice=advice))`. On the next attempt, after the findings are known, `sup.settle("precode_retry", "helped" if not found else "not_helped")`. An `engine_error` early return settles `unknown`.
  4. `_check_static`: the same, with `"static_retry"`, `"build"` and `before_code=False`.
  5. `_amend_tests`: the advice comes from `advise("amend", "tests", ..., amend_feedback(...), kinds=("locked_tests_fail",), evidence="\n".join(failing))`, and the follow-up sends `with_advice(amend_feedback(...), advice)`. Settle `helped` where `tests_mended` is emitted, and `not_helped` at each `return False` after the follow-up.
  6. The three `raise StoryBlocked(...)` in `_build` after `execute_task` (findings, changed nothing, gate fails) first call `diagnosis = await sup.at_block(...)`, with:
     - the subtask and role of `"tests"` for tester findings, else `"build"`
     - `kinds` set to the finding kinds, or `("changed_nothing",)` / `("gate_fails",)`
     - `evidence` set to `findings_text` or the reason

     They then raise `StoryBlocked(reason, diagnosis)`. **No** `at_block` is made when every finding is `engine_error`. `run()` passes `e.diagnosis` to `_block`.

- [ ] **Step 1: Write the failing tests** (on the calc repository from `test_story_layer_checks_flow.py`, with `story={"tester_role": "tester", "supervisor": {"enabled": True}}` and a `supervisor` role added to `TESTER_ROLES`)

```python
async def test_the_testers_retry_carries_muses_advice_and_every_rule(tmp_path):
    # tester writes VACUOUS (passes before code) → precode finding → Muse answers → tester gets the retry
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], supervisor=[answer("Assert mul, not add.")], coder=[FEATURE, DONE])
    st = await run_layer(tmp_path, engine)
    retry = last_user_message(engine, "tester")
    assert "Assert mul, not add." in retry and SUPERVISOR in retry
    assert feedback([]).split("\n")[-1] in retry          # the rule paragraph, whole
    assert retry.index(SUPERVISOR) < retry.index("Every test you wrote must fail now")
    assert ledger(tmp_path).recent()[0].outcome == "helped"

async def test_when_muse_fails_the_tester_gets_exactly_todays_text(tmp_path):
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], supervisor=[FakeReply(text="no json")], coder=[FEATURE, DONE])
    st = await run_layer(tmp_path, engine)
    assert SUPERVISOR not in last_user_message(engine, "tester")
    assert kinds(st.path / "events.jsonl", "supervisor_fallback")

async def test_a_retry_that_still_fails_is_recorded_as_not_helped_and_the_block_carries_the_diagnosis(tmp_path):
    engine = scripted(tester=[VACUOUS, LISTED, VACUOUS, LISTED],
                      supervisor=[answer("Assert mul."), block_answer("Tests assert add, which exists.", fix="assert mul(2,3)")])
    st = await run_layer(tmp_path, engine)
    rows = ledger(tmp_path).recent()
    assert [r.trigger for r in rows] == ["block", "precode_retry"] and rows[1].outcome == "not_helped"
    note = board_notes(st)[-1]
    assert "Supervisor: Tests assert add, which exists." in note and "assert mul(2,3)" in note

async def test_an_engine_down_block_asks_muse_nothing(tmp_path):
    engine = scripted(tester=[EngineError("ConnectError")], supervisor=[])
    st = await run_layer(tmp_path, engine)
    assert not [r for r in engine.requests if r.role == "supervisor"]
    assert "task up" in st.blocked

async def test_the_mend_carries_muses_advice(tmp_path):
    ...  # reuse the existing amend flow test's script; add supervisor=[answer("Unpack both comments.")]
    assert "Unpack both comments." in the tester's mend message and ledger(tmp_path).recent()[0].trigger == "amend"

async def test_with_the_supervisor_off_nothing_changes(tmp_path):
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], coder=[FEATURE, DONE])   # no supervisor script
    st = await run_layer(tmp_path, engine, supervisor=False)
    assert not (tmp_path / "state" / "supervisor.sqlite").exists() or not ledger(tmp_path).recent()
```

The helpers `scripted`, `run_layer`, `last_user_message`, `answer`, `block_answer`, `ledger` and `board_notes` are new and small. Build them on `pipeline()`, `calc_repo()` and `kinds()` from the existing flow tests.

Adjust each script to the exact number of replies the existing flow tests use for that path. `test_story_layer_checks_flow.py` is the reference for each sequence. The mend test copies that file's amend test.

- [ ] **Step 2: Run them to see them fail.** `uv run pytest tests/unit/test_story_supervisor_flow.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement the wiring above**

- [ ] **Step 4: Run the new tests and the existing story suites**

Run: `uv run pytest tests/unit/test_story_supervisor_flow.py tests/unit/test_story_layer_checks_flow.py tests/unit/test_story_pipeline.py tests/unit/test_story_layer_checks.py -q`
Expected: all PASS. The existing ones are unchanged, since the supervisor is off in their config.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/story tests/unit/test_story_supervisor_flow.py
git commit -m "Story: Muse's advice at the pre-code, static and mend retries, and its diagnosis on a block"
```

---

### Task 9: Reading the ledger (CLI, Taskfile, docs)

**Files:**
- Modify: `src/airlie/cli.py` (a `supervisor_app` typer with `log`, `stats` and `verdict`, added as `supervisor`)
- Modify: `Taskfile.yml` (`supervisor:log`, `supervisor:stats` and `supervisor:verdict`, next to `story:restack`, in the same style)
- Modify: `docs/integrations/story.md` (a "The supervisor (trial)" section: what it does, where the ledger is, the three tasks, and how to turn it off) and `README.md` (one line, next to the story tasks)
- Test: `tests/unit/test_cli_supervisor.py`

**Interfaces:**
- `airlie supervisor log [--limit N=20]` prints one line per row: `#id  YYYY-MM-DD HH:MM  STORY/LAYER  trigger  category|fallback:<reason>  outcome|open`.
- `airlie supervisor stats` prints, in this order:
  - `help by trigger` (`trigger  helped/total`)
  - `help by category`
  - `fallbacks  n/total`
  - `blocks Muse could have fixed with one more attempt  #ids`
- `airlie supervisor verdict ID right|wrong [NOTE]` prints `recorded: #ID right`. An unknown id prints an error and exits 1.
- All three read `cfg.observability.state_dir / "supervisor.sqlite"`. With no file, `log` and `stats` print `no interventions yet` and exit 0.

- [ ] **Step 1: Write the failing tests** (CliRunner, as in `test_cli_story.py`, with a config whose `state_dir` is `tmp_path`)

```python
def test_log_and_stats_read_the_ledger(tmp_path):
    led = Ledger(tmp_path / "supervisor.sqlite")
    a = led.record(intervention(story="AIR-9", layer="02", category="missing_import")); led.outcome(a, "helped")
    out = invoke(tmp_path, "supervisor", "log").stdout
    assert "AIR-9/02" in out and "missing_import" in out and "helped" in out
    assert "missing_import  1/1" in invoke(tmp_path, "supervisor", "stats").stdout

def test_a_verdict_is_recorded_and_an_unknown_id_fails(tmp_path):
    led = Ledger(tmp_path / "supervisor.sqlite"); a = led.record(intervention())
    assert invoke(tmp_path, "supervisor", "verdict", str(a), "right").exit_code == 0
    assert led.recent()[0].verdict == "right"
    assert invoke(tmp_path, "supervisor", "verdict", "99", "wrong").exit_code == 1

def test_no_ledger_yet(tmp_path):
    assert "no interventions yet" in invoke(tmp_path, "supervisor", "log").stdout
```

- [ ] **Step 2: Run them to see them fail.** `uv run pytest tests/unit/test_cli_supervisor.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement the CLI, the tasks and the docs**

- [ ] **Step 4: Run the tests, then the tasks by hand**

Run: `uv run pytest tests/unit/test_cli_supervisor.py -q && task supervisor:log && task supervisor:stats`
Expected: PASS, then `no interventions yet` twice (or real rows, if the ledger already exists).

- [ ] **Step 5: Run the whole test gate** (Global Constraints). Expected: every command clean.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/cli.py Taskfile.yml docs/integrations/story.md README.md tests/unit/test_cli_supervisor.py
git commit -m "Supervisor: task supervisor:log, :stats and :verdict, and the trial in the story docs"
```

---

### Task 10: The live check, the real Muse on AIR-9 case 1 (not in CI)

This is the spec's "once, before the trial is called built" check. It needs the real engine, so it runs on Noah's Mac, and its result goes in the PR description. No trace content is committed.

**Files:**
- Modify: `src/airlie/cli.py` (`airlie supervisor try`)
- Modify: `Taskfile.yml` (`supervisor:try`)
- Test: `tests/unit/test_cli_supervisor.py` (one test on `FakeEngine` through the same code path)

**Interfaces:**
- `airlie supervisor try --worktree PATH --base SHA [--run RUN_ID] [--subtask tests]`:
  1. Runs the pre-code check (`checks_for(...).run(worktree, base)`) on the worktree, using the gate from that worktree's `AGENTS.md`.
  2. Builds a `TriageInput` for `precode_retry`, with the trace tail from `state_dir/runs/RUN_ID/trace` when `--run` is given.
  3. Calls `Triage` against the configured engine, and prints the findings, then Muse's `Answer` as JSON, then seconds and tokens.
  4. Writes **no** ledger row: a try is not an intervention.

- [ ] **Step 1: Write the failing test.** On the calc repo with a test that has a missing import, and `FakeEngine` scripted with `GOOD`, `invoke("supervisor", "try", ...)` prints `"missing_import"` and the ledger has no rows.

- [ ] **Step 2: Run it to see it fail, implement, and run it to see it pass.** Run: `uv run pytest tests/unit/test_cli_supervisor.py -q -k try`.

- [ ] **Step 3: Rebuild AIR-9 layer 02 at the tester's commit**

```bash
grep '"layer_start"' ~/.local/state/airlie/stories/AIR-9/events.jsonl | grep '"layer": "02"'
```

That gives the branch and `base_sha` of each attempt. Use the attempt whose `studio_run` is `20261021-132205-8733`. The tester's commit is the `studio: layer 02 (pre-code check 1)` commit on that branch (`git log --oneline <branch>`). If the branch is gone, ask Noah before rebuilding it from the trace's `write_file` calls.

```bash
git -C <airlie checkout> worktree add --detach "$SCRATCH/air9-02" <tester-commit>
```

- [ ] **Step 4: Run the real triage**

```bash
task up -- --topology studio
task supervisor:try -- --worktree "$SCRATCH/air9-02" --base <base_sha> --run 20261021-132205-8733 --subtask tests
```

Expected: an `Answer` whose category is `missing_import` and whose message names the `Path` and `LocalBoard` imports. Record the printed category, message, seconds and tokens.

- [ ] **Step 5: Clean up, and put the result in the PR**

```bash
git -C <airlie checkout> worktree remove "$SCRATCH/air9-02"
```

Add the recorded result to the PR description under "Live check (AIR-9 layer 02, case 1)". If Muse does not name the imports, say so plainly in the PR, and tell Noah before calling layer 1 built: a missed criterion means talking about a change of direction, not dropping the feature.

- [ ] **Step 6: Run the test gate and commit**

```bash
git add src/airlie/cli.py Taskfile.yml tests/unit/test_cli_supervisor.py
git commit -m "Supervisor: task supervisor:try, the real triage on a worktree with no ledger row"
```
