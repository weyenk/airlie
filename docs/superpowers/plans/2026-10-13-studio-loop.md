# Studio Loop Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `studio` topology: a chief that plans in JSON, a router that assigns each subtask to a specialist role (with a skill chosen for roles that have a library), specialists that run as single agents on one shared worktree, a cross-family reviewer with one fix round, and a chief synthesis; plus the prompts, the QA skill library, a knowledge search tool, the `studio` config profile, the bench column and the live checks.

**Architecture:** Nothing in the agent loop changes except two small additions (`TeamContext.images`, prompt frontmatter stripping). The studio is a new `Topology` (`topology/studio.py`) composed of four pure pieces, each tested alone: chief protocol parsing and dependency scheduling (`topology/studio_plan.py`), the review call and its JSON (`topology/review.py`), the skills/knowledge library (`skills/`), and the router from Plan 1. Specialists are `TeamContext`s run by `NativeTeam`, exactly as `fork` runs branches.

**Tech Stack:** Python 3.12, uv, pydantic v2, httpx, pytest (asyncio auto), `rank-bm25` (new, pure Python), git worktrees, vllm-mlx 0.5.0 (unchanged), llama-server (Kev, from Plan 1).

**Spec:** `docs/superpowers/specs/2026-10-13-studio-topology-design.md` (§4 loop, §4a review, §5 chief protocol, §6 router stage B, §7 skills and knowledge, §8 profiles, §9 config, §10 code shape, §11 tests, §12 success criterion). Plan 1 (`docs/superpowers/plans/2026-10-13-studio-foundations.md`) is merged in `main` and is the floor this plan stands on.

## Global Constraints

- Python 3.12 via `uv` only; every user action is a `task`. `task check` (ruff line length 110, mypy strict, pytest) green after every task.
- `vllm-mlx==0.5.0`, `mlx-lm==0.32.0` pinned. New dependency allowed: `rank-bm25` (pure Python, MIT).
- Models: `mlx-community` only, Kev the one exception. Studio profile: generalist `mlx-community/Qwen3.8-27B-4bit@10c35caa…` (chief, architect, product, ux, design, docs, research), coder `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit@6e302ea6…` (coder, qa, security), reviewer `mlx-community/Muse-Glimmer-30B-4bit@3e7677d7…` (19.4 GB, **not on disk: the executor asks Noah before `task models:pull -- reviewer`**), helpers `Qwen3-4B`, router Kev.
- The reviewer is never the coder's family (spec §4a); the reviewer never edits.
- `airlie.yaml` rejects unknown keys; new keys have defaults; `validate_references` names the key path.
- Success criterion (spec §12): pass rate not below `single`, wall-clock on solved runs within 25 %, routing accuracy ≥ 0.9, review never lowers the pass rate. **If a criterion is missed, report the numbers and the candidate fixes to Noah and wait; never drop or disable studio on a bench result** (memory: feedback-no-torching-on-failed-criteria).
- Every long-running thing is cancellable; every tool call is timeboxed; no client-specific hooks.

## Review Focus

1. Chief emits prose, or JSON with an unknown `type`, or a plan whose `depends_on` names a missing id or forms a cycle: the run must end with a readable report (the text as the answer, or a `plan_error` event and chief asked once more), never a traceback. (Task 3, `test_parse_chief_falls_back_to_text`, `test_schedule_rejects_unknown_dependency_and_cycle`; Task 5, `test_unparsable_plan_twice_ends_the_run_with_the_text`.)
2. Two coder subtasks in the same wave must not edit the worktree at the same time, and a read-only role may run beside them. (Task 5, `test_writers_are_serialised_readers_run_in_parallel`.)
3. The reviewer answers something that is not the JSON contract (prose, a truncated object): the subtask is reported as reviewed-unparsable, no fix round, the run continues. (Task 4, `test_parse_review_unparsable`; Task 5, `test_unparsable_review_skips_the_fix_round`.)
4. A role whose prompt has no `summary:` (helpers, chief, reviewer) must never appear in the router's closed set, and a plan hint naming such a role must fall back to a routable one. (Task 2, `test_routable_roles_need_a_summary`; Task 5, `test_hint_to_an_unroutable_role_is_ignored`.)
5. A knowledge directory with no markdown, or a query with no overlap: `search_knowledge` returns an empty, readable observation, never an exception from the BM25 library. (Task 2, `test_knowledge_empty_dir_and_no_overlap`.)

## Decisions carried from the spec's §13 (Noah: say so if any should change)

- Coder seat default stays `Qwen3-Coder-30B-A3B`; `studio-qwen38` is a profile that puts `Qwen3.8-27B` in the coder seat for the bench.
- Router default `jev` (Plan 1 config), `chief` is the fallback kind.
- Prompts: chief and the role summaries ported from ai-mode; coder/qa/architect rewritten for tools; the reviewer prompt is new.

---

### Task 1: Config — studio kind, `StudioSpec`, role tools/skills/knowledge/vision

**Files:**
- Modify: `src/airlie/config/schema.py`, `src/airlie/config/load.py`, `src/airlie/config/__init__.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `TOPOLOGY_KINDS`, `TopologySpec`, `RoleSpec`, `AirlieConfig.validate_references` (Plan 1).
- Produces: `TOPOLOGY_KINDS == ("single", "teams", "fork", "studio")`; `RoleSpec.tools: list[str] | None` (None = `tools.builtin`), `RoleSpec.skills: Path | None`, `RoleSpec.knowledge: Path | None`, `RoleSpec.vision: bool`, `RoleSpec.max_steps: int | None` (None = `teams.max_steps`); `ReviewSpec(enabled: bool = True, role: str = "reviewer", roles: list[str] = ["coder", "qa", "security"], rounds: int = 1, diff_chars: int = 40000, files_chars: int = 20000)`; `StudioSpec(chief_role: str = "chief", max_subtasks: int = 12, max_rounds: int = 2, parallel: int = 3, result_chars: int = 6000, review: ReviewSpec)`; `AirlieConfig.studio`. Validation when `topology.kind == "studio"`: `studio.chief_role`, `studio.review.role` (if enabled) and every `studio.review.roles` entry exist in `roles`; a role's `tools` entries are known builtins or `decide`/`search_knowledge`; `skills`/`knowledge` paths resolved relative to the config file and must be directories when set.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_config.py`)

```python
STUDIO_ROLES = {
    "chief": {"prompt": "prompts/roles/chief.md", "model": "team", "tools": []},
    "reviewer": {"prompt": "prompts/roles/reviewer.md", "model": "team", "tools": []},
    "qa": {"prompt": "prompts/roles/qa.md", "model": "team", "skills": "skills/qa", "tools": ["read_file", "run_tests"]},
}


def test_studio_kind_loads_with_defaults_and_role_fields(tmp_path: Path) -> None:
    data = {**MINIMAL, "roles": {**MINIMAL["roles"], **STUDIO_ROLES}, "topology": {"kind": "studio"}}
    (tmp_path / "skills" / "qa").mkdir(parents=True)
    cfg = load_config(write(tmp_path, data))
    assert cfg.topology.kind == "studio"
    assert cfg.studio.chief_role == "chief" and cfg.studio.review.roles == ["coder", "qa", "security"]
    assert cfg.studio.parallel == 3 and cfg.studio.review.rounds == 1
    assert cfg.roles["qa"].skills == (tmp_path / "skills" / "qa").resolve()
    assert cfg.roles["qa"].tools == ["read_file", "run_tests"] and cfg.roles["coder"].tools is None
    assert cfg.roles["chief"].vision is False and cfg.roles["chief"].max_steps is None


def test_studio_kind_requires_chief_and_reviewer_roles(tmp_path: Path) -> None:
    data = {**MINIMAL, "topology": {"kind": "studio"}}
    with pytest.raises(ConfigError, match=r"studio\.chief_role.*'chief'"):
        load_config(write(tmp_path, data))


def test_role_tools_must_be_known(tmp_path: Path) -> None:
    data = {**MINIMAL, "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "tools": ["teleport"]}}}
    with pytest.raises(ConfigError, match=r"roles\.coder\.tools.*'teleport'"):
        load_config(write(tmp_path, data))


def test_role_skills_dir_must_exist(tmp_path: Path) -> None:
    data = {**MINIMAL, "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "skills": "skills/nope"}}}
    with pytest.raises(ConfigError, match=r"roles\.coder\.skills.*not a directory"):
        load_config(write(tmp_path, data))
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/unit/test_config.py -q 2>&1 | tail -5`
Expected: 4 failures (`studio` rejected as unknown topology; `tools`/`skills` rejected as extra fields).

- [ ] **Step 3: Implement**

`src/airlie/config/schema.py`:

```python
KNOWN_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "list_dir", "grep", "shell", "run_tests", "git",
     "retrieve_value", "decide", "search_knowledge"}
)  # fmt: skip

# RoleSpec: add
    tools: list[str] | None = Field(
        default=None, description="tool names this role gets (None = tools.builtin); [] = no tools"
    )
    skills: Path | None = Field(default=None, description="directory of skills/<name>.md with summary: frontmatter")
    knowledge: Path | None = Field(default=None, description="directory of markdown for search_knowledge")
    vision: bool = Field(default=False, description="the role receives the run's images as content parts")
    max_steps: int | None = Field(default=None, ge=1, description="steps per subtask (None = teams.max_steps)")


class ReviewSpec(Strict):
    enabled: bool = True
    role: str = "reviewer"
    roles: list[str] = Field(default_factory=lambda: ["coder", "qa", "security"])
    rounds: int = Field(default=1, ge=0, description="fix rounds after request_changes with a blocking finding")
    diff_chars: int = Field(default=40000, ge=0)
    files_chars: int = Field(default=20000, ge=0)


class StudioSpec(Strict):
    chief_role: str = "chief"
    max_subtasks: int = Field(default=12, ge=1)
    max_rounds: int = Field(default=2, ge=1, description="chief plan rounds (delegate after synthesis)")
    parallel: int = Field(default=3, ge=1, description="independent subtasks in flight")
    result_chars: int = Field(default=6000, ge=0, description="chars of a dependency's result in a brief")
    review: ReviewSpec = Field(default_factory=ReviewSpec)


TOPOLOGY_KINDS: tuple[str, ...] = ("single", "teams", "fork", "studio")
# TopologySpec.kind: Literal["single", "teams", "fork", "studio"]
# AirlieConfig: studio: StudioSpec = Field(default_factory=StudioSpec)
```

In `validate_references`, after the role loop body (inside `for rname, r in self.roles.items()`):

```python
            for t in r.tools or []:
                if t not in KNOWN_TOOLS:
                    problems.append(f"roles.{rname}.tools names '{t}', which is not a tool ({sorted(KNOWN_TOOLS)})")
            for label, d in (("skills", r.skills), ("knowledge", r.knowledge)):
                if d is not None and not d.is_dir():
                    problems.append(f"roles.{rname}.{label}: {d} is not a directory")
```

and after the `(label, role)` loop:

```python
        if self.topology.kind == "studio":
            need = [("studio.chief_role", self.studio.chief_role)]
            if self.studio.review.enabled:
                need.append(("studio.review.role", self.studio.review.role))
                need += [("studio.review.roles", x) for x in self.studio.review.roles]
            for label, role in need:
                if role not in self.roles:
                    problems.append(f"{label} names '{role}', which is not in roles ({sorted(self.roles)})")
```

`load.py`, in the path-resolution block: `for role in cfg.roles.values(): ... if role.skills is not None: role.skills = (base / role.skills.expanduser()).resolve()` and the same for `knowledge`. Export `StudioSpec`, `ReviewSpec`, `KNOWN_TOOLS`.

- [ ] **Step 4: Run, check, commit**

Run: `uv run pytest tests/unit/test_config.py -q 2>&1 | tail -2 && task check 2>&1 | tail -1`
Expected: pass; check green.

```bash
git add src/airlie/config tests/unit/test_config.py
git commit -m "feat(config): studio topology kind, StudioSpec/ReviewSpec, role tools/skills/knowledge/vision/max_steps"
```

---

### Task 2: Skills library, prompt frontmatter, knowledge search tool

**Files:**
- Create: `src/airlie/skills/__init__.py`, `src/airlie/skills/library.py`, `src/airlie/skills/knowledge.py`, `src/airlie/tools/knowledge.py`
- Modify: `src/airlie/teams/native_team.py` (system prompt via `prompt_text`), `src/airlie/serve/app.py` (same), `src/airlie/teams/autogen_team.py` (same, where it reads the prompt), `pyproject.toml` (`rank-bm25>=0.2.2`), `src/airlie/config/schema.py` (`KNOWN_TOOLS` already lists `search_knowledge`)
- Test: `tests/unit/test_skills.py`, `tests/unit/test_knowledge_tool.py`

**Interfaces:**
- Produces: `split_frontmatter(text: str) -> tuple[dict[str, str], str]` (YAML-ish `key: value` lines between `---` fences; body without them); `prompt_text(path: Path) -> str` (body only); `role_summary(spec: RoleSpec) -> str | None` (frontmatter `summary:` wins, else `spec.summary`); `routable_roles(cfg: AirlieConfig, *, exclude: set[str]) -> dict[str, str]` (role → summary for every role with one, minus `exclude`); `Skill(name: str, summary: str, text: str, path: Path)`; `load_skills(dir: Path) -> list[Skill]` (sorted by name; files without `summary:` are skipped with a warning list returned via `load_skills_report`); `skill_criteria(skills) -> dict[str, str]`; `KnowledgeIndex.build(dir: Path, *, chunk_chars: int = 1200) -> KnowledgeIndex`, `.search(query: str, k: int = 3) -> list[Hit(path: str, score: float, text: str)]`, `.size: int`; `SearchKnowledge(index)` tool (`name = "search_knowledge"`, params `query: str`, `k: int = 3`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_skills.py`:

```python
"""Skills: frontmatter summaries drive routing (stage A over roles, stage B over a role's skill library)."""

from pathlib import Path

import yaml

from airlie.config import RoleSpec, load_config
from airlie.skills import load_skills, prompt_text, role_summary, routable_roles, skill_criteria, split_frontmatter


def test_split_frontmatter_returns_fields_and_body() -> None:
    meta, body = split_frontmatter("---\nsummary: Test strategy\nrole: qa\n---\n\n# qa\n\nText.\n")
    assert meta == {"summary": "Test strategy", "role": "qa"} and body == "# qa\n\nText.\n"
    assert split_frontmatter("# plain\n") == ({}, "# plain\n")


def test_prompt_text_strips_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "qa.md"
    p.write_text("---\nsummary: s\n---\n# qa\n")
    assert prompt_text(p) == "# qa\n"


def test_role_summary_prefers_frontmatter_over_config(tmp_path: Path) -> None:
    p = tmp_path / "qa.md"
    p.write_text("---\nsummary: from file\n---\n# qa\n")
    assert role_summary(RoleSpec(prompt=p, model="m", summary="from config")) == "from file"
    q = tmp_path / "docs.md"
    q.write_text("# docs\n")
    assert role_summary(RoleSpec(prompt=q, model="m", summary="from config")) == "from config"
    assert role_summary(RoleSpec(prompt=q, model="m")) is None


def test_routable_roles_need_a_summary(tmp_path: Path) -> None:
    for name, text in {
        "coder": "---\nsummary: Implement code\n---\n# coder\n",
        "qa": "---\nsummary: Tests\n---\n# qa\n",
        "chief": "---\nsummary: The chief\n---\n# chief\n",
        "summarizer": "# summarizer\n",
        "judge": "# judge\n",
        "aggregator": "# aggregator\n",
    }.items():
        (tmp_path / f"{name}.md").write_text(text)
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["m"]}},
        "models": {"m": {"repo": "o/m", "revision": "r"}},
        "roles": {
            n: {"prompt": f"{n}.md", "model": "m"} for n in ("coder", "qa", "chief", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_path / "airlie.yaml")
    assert routable_roles(cfg, exclude={"chief"}) == {"coder": "Implement code", "qa": "Tests"}


def test_load_skills_reads_summaries_and_skips_files_without(tmp_path: Path) -> None:
    (tmp_path / "unit.md").write_text("---\nsummary: Pure functions in isolation\n---\n# Unit\nBody\n")
    (tmp_path / "e2e.md").write_text("---\nsummary: Full journeys\n---\n# E2E\n")
    (tmp_path / "README.md").write_text("# index, no summary\n")
    skills = load_skills(tmp_path)
    assert [s.name for s in skills] == ["e2e", "unit"]
    assert skills[1].summary == "Pure functions in isolation" and skills[1].text.startswith("# Unit")
    assert skill_criteria(skills) == {"e2e": "Full journeys", "unit": "Pure functions in isolation"}
```

`tests/unit/test_knowledge_tool.py`:

```python
"""search_knowledge: BM25 over markdown chunks; plain-text hits with their source path."""

import json
from pathlib import Path

from airlie.engine import ToolCall
from airlie.skills.knowledge import KnowledgeIndex
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.knowledge import SearchKnowledge


def corpus(tmp_path: Path) -> Path:
    k = tmp_path / "knowledge"
    (k / "sub").mkdir(parents=True)
    (k / "auth.md").write_text("# Auth\n\nRate limiting on login uses a token bucket per IP.\n")
    (k / "sub" / "billing.md").write_text("# Billing\n\nInvoices are generated nightly by the cron job.\n")
    (k / "notes.txt").write_text("plain text files count too: the cache is redis\n")
    return k


def test_index_ranks_the_matching_chunk_first(tmp_path: Path) -> None:
    idx = KnowledgeIndex.build(corpus(tmp_path))
    assert idx.size == 3
    hits = idx.search("login rate limiting", k=2)
    assert hits[0].path == "auth.md" and "token bucket" in hits[0].text and hits[0].score > 0


async def test_search_knowledge_tool_returns_hits_as_text(tmp_path: Path) -> None:
    reg = ToolRegistry([SearchKnowledge(KnowledgeIndex.build(corpus(tmp_path)))], ToolContext(worktree=tmp_path))
    res = await reg.call(ToolCall("c", "search_knowledge", json.dumps({"query": "invoices cron", "k": 1})))
    assert res.ok and res.output.startswith("[sub/billing.md]") and "nightly" in res.output


def test_knowledge_empty_dir_and_no_overlap(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    idx = KnowledgeIndex.build(empty)
    assert idx.size == 0 and idx.search("anything") == []
    assert KnowledgeIndex.build(corpus(tmp_path)).search("zzqx") == []
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/unit/test_skills.py tests/unit/test_knowledge_tool.py -q 2>&1 | tail -3`
Expected: ImportError (`airlie.skills`).

- [ ] **Step 3: Add the dependency and implement**

```bash
uv add "rank-bm25>=0.2.2"
```

`src/airlie/skills/library.py`:

```python
"""Skills: markdown files with `summary:` frontmatter. The summary is what the router sees (ai-mode's
convention); the body is injected as a specialist's preamble."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from airlie.config import AirlieConfig, RoleSpec


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[end + 5 :].lstrip("\n")


def prompt_text(path: Path) -> str:
    return split_frontmatter(path.read_text())[1]


def role_summary(spec: RoleSpec) -> str | None:
    try:
        meta, _ = split_frontmatter(spec.prompt.read_text())
    except OSError:
        meta = {}
    return meta.get("summary") or spec.summary


def routable_roles(cfg: AirlieConfig, *, exclude: set[str] = frozenset()) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in cfg.roles.items():
        if name in exclude:
            continue
        s = role_summary(spec)
        if s:
            out[name] = s
    return out


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str
    text: str
    path: Path


def load_skills(directory: Path) -> list[Skill]:
    out: list[Skill] = []
    for p in sorted(directory.glob("*.md")):
        meta, body = split_frontmatter(p.read_text())
        if not meta.get("summary"):
            continue
        out.append(Skill(p.stem, meta["summary"], body, p))
    return out


def skill_criteria(skills: list[Skill]) -> dict[str, str]:
    return {s.name: s.summary for s in skills}
```

`src/airlie/skills/knowledge.py`:

```python
"""KnowledgeIndex: BM25 over chunks of markdown/text files; the RAG seam (an embedding backend is a later swap)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def chunk(text: str, chunk_chars: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > chunk_chars:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


@dataclass(frozen=True)
class Hit:
    path: str
    score: float
    text: str


class KnowledgeIndex:
    def __init__(self, chunks: list[tuple[str, str]]) -> None:
        self._chunks = chunks
        self._bm25 = BM25Okapi([tokenize(t) for _, t in chunks]) if chunks else None

    @classmethod
    def build(cls, directory: Path, *, chunk_chars: int = 1200) -> KnowledgeIndex:
        chunks: list[tuple[str, str]] = []
        for p in sorted(directory.rglob("*")):
            if p.suffix not in (".md", ".txt") or not p.is_file():
                continue
            rel = str(p.relative_to(directory))
            chunks += [(rel, c) for c in chunk(p.read_text(errors="replace"), chunk_chars)]
        return cls(chunks)

    @property
    def size(self) -> int:
        return len(self._chunks)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        if self._bm25 is None:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self._bm25.get_scores(q)
        ranked = sorted(range(len(self._chunks)), key=lambda i: (-float(scores[i]), i))
        return [
            Hit(self._chunks[i][0], float(scores[i]), self._chunks[i][1]) for i in ranked[:k] if float(scores[i]) > 0
        ]
```

`src/airlie/tools/knowledge.py`:

```python
"""search_knowledge: a role's knowledge directory, searched with BM25; hits are plain text with their path."""

from __future__ import annotations

from typing import Any

from airlie.skills.knowledge import KnowledgeIndex
from airlie.tools.protocol import ToolContext, ToolResult


class SearchKnowledge:
    name = "search_knowledge"
    description = "Search this role's knowledge base (guides, past decisions, notes). Returns the best matching passages with their source file."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what you want to know, in a few words"},
            "k": {"type": "integer", "description": "passages to return (default 3)"},
        },
        "required": ["query"],
    }

    def __init__(self, index: KnowledgeIndex) -> None:
        self.index = index

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        hits = self.index.search(str(args["query"]), k=int(args.get("k") or 3))
        if not hits:
            return ToolResult(True, "no passages match; try other words or proceed without")
        return ToolResult(True, "\n\n".join(f"[{h.path}] (score {h.score:.1f})\n{h.text}" for h in hits))
```

`src/airlie/skills/__init__.py` exports `Skill, load_skills, prompt_text, role_summary, routable_roles, skill_criteria, split_frontmatter, KnowledgeIndex, Hit`. In `native_team.py` replace `system = ctx.role.prompt.read_text()` with `system = prompt_text(ctx.role.prompt)`; in `serve/app.py` the chat endpoint's `spec.prompt.read_text()` likewise; in `autogen_team.py` wherever the prompt file is read. Add a native-team test: a prompt with frontmatter is sent without it (`messages[0].content == "# coder\n"`).

- [ ] **Step 4: Run, check, commit**

Run: `uv run pytest tests/unit/test_skills.py tests/unit/test_knowledge_tool.py tests/unit/test_native_team.py -q 2>&1 | tail -2 && task check 2>&1 | tail -1`
Expected: pass; green.

```bash
git add -A src tests pyproject.toml uv.lock
git commit -m "feat(skills): frontmatter summaries, skill library, BM25 knowledge index and search_knowledge tool"
```

---

### Task 3: Chief protocol and dependency scheduling

**Files:**
- Create: `src/airlie/topology/studio_plan.py`
- Test: `tests/unit/test_studio_plan.py`

**Interfaces:**
- Produces: `Subtask(id: str, brief: str, role_hint: str | None = None, depends_on: tuple[str, ...] = (), inputs: tuple[str, ...] = ())`; `Plan(assumptions: tuple[str, ...], subtasks: tuple[Subtask, ...])`; `Synthesis(answer: str, open_questions: tuple[str, ...])`; `Delegate(subtask: Subtask)`; `Answer(text: str)`; `parse_chief(text: str) -> Plan | Synthesis | Delegate | Answer | None` (`None` = no JSON object with a known `type`); `schedule(plan: Plan) -> list[list[Subtask]]` (waves; raises `PlanError` on an unknown dependency, a duplicate id, or a cycle); `PlanError(ValueError)`; `extract_json(text: str) -> dict | None` (first `{…}` object, via `repair_json_args`).

- [ ] **Step 1: Write the failing tests**

```python
"""Chief protocol (spec §5): one JSON object per turn; waves in dependency order."""

import pytest

from airlie.topology.studio_plan import (
    Answer, Delegate, Plan, PlanError, Subtask, Synthesis, parse_chief, schedule,
)  # fmt: skip

PLAN = '''Here is my plan.
{"type": "plan", "assumptions": ["the API is Flask"], "subtasks": [
 {"id": "map", "role": "architect", "brief": "Map app/ and tests/."},
 {"id": "fix", "role": "coder", "brief": "Fix the 500.", "depends_on": ["map"]},
 {"id": "ux", "role": "ux", "brief": "Critique the screenshot.", "inputs": ["img:1"]}]}'''


def test_parse_plan_with_prose_around_it() -> None:
    p = parse_chief(PLAN)
    assert isinstance(p, Plan) and p.assumptions == ("the API is Flask",)
    assert [s.id for s in p.subtasks] == ["map", "fix", "ux"]
    assert p.subtasks[1].depends_on == ("map",) and p.subtasks[1].role_hint == "coder"
    assert p.subtasks[2].inputs == ("img:1",) and p.subtasks[0].depends_on == ()


def test_parse_synthesis_delegate_and_answer() -> None:
    s = parse_chief('{"type": "synthesis", "answer": "done", "open_questions": ["q"]}')
    assert isinstance(s, Synthesis) and s.answer == "done" and s.open_questions == ("q",)
    d = parse_chief('{"type": "delegate", "subtask": {"id": "more", "role": "qa", "brief": "add tests"}}')
    assert isinstance(d, Delegate) and d.subtask.id == "more"
    a = parse_chief('{"type": "answer", "answer": "42"}')
    assert isinstance(a, Answer) and a.text == "42"


def test_parse_chief_falls_back_to_text() -> None:
    assert parse_chief("I would start by reading the code.") is None
    assert parse_chief('{"type": "poem", "lines": []}') is None
    assert parse_chief('{"type": "plan", "subtasks": [{"brief": "no id"}]}') is None


def test_schedule_waves_in_dependency_order() -> None:
    plan = Plan((), (
        Subtask("a", "x"), Subtask("b", "y", depends_on=("a",)), Subtask("c", "z"),
        Subtask("d", "w", depends_on=("b", "c")),
    ))  # fmt: skip
    assert [[s.id for s in wave] for wave in schedule(plan)] == [["a", "c"], ["b"], ["d"]]


def test_schedule_rejects_unknown_dependency_and_cycle() -> None:
    with pytest.raises(PlanError, match="'b'.*'zz'"):
        schedule(Plan((), (Subtask("b", "y", depends_on=("zz",)),)))
    with pytest.raises(PlanError, match="cycle"):
        schedule(Plan((), (Subtask("a", "x", depends_on=("b",)), Subtask("b", "y", depends_on=("a",)))))
    with pytest.raises(PlanError, match="duplicate"):
        schedule(Plan((), (Subtask("a", "x"), Subtask("a", "y"))))
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/unit/test_studio_plan.py -q 2>&1 | tail -3`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
"""Chief protocol (design §5): the chief emits one JSON object per turn — plan, synthesis, delegate, answer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from airlie.tools.jsonargs import repair_json_args


class PlanError(ValueError):
    """A plan the scheduler cannot run; the message names the subtask."""


@dataclass(frozen=True)
class Subtask:
    id: str
    brief: str
    role_hint: str | None = None
    depends_on: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    assumptions: tuple[str, ...]
    subtasks: tuple[Subtask, ...]


@dataclass(frozen=True)
class Synthesis:
    answer: str
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Delegate:
    subtask: Subtask


@dataclass(frozen=True)
class Answer:
    text: str


def extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    fixed = repair_json_args(text[start:])
    try:
        obj = json.loads(fixed)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _subtask(d: Any) -> Subtask | None:
    if not isinstance(d, dict) or not d.get("id") or not d.get("brief"):
        return None
    return Subtask(
        id=str(d["id"]),
        brief=str(d["brief"]),
        role_hint=str(d["role"]) if d.get("role") else None,
        depends_on=tuple(str(x) for x in d.get("depends_on") or []),
        inputs=tuple(str(x) for x in d.get("inputs") or []),
    )


def parse_chief(text: str) -> Plan | Synthesis | Delegate | Answer | None:
    obj = extract_json(text)
    if obj is None:
        return None
    kind = obj.get("type")
    if kind == "plan":
        subs = [_subtask(d) for d in obj.get("subtasks") or []]
        if not subs or any(s is None for s in subs):
            return None
        return Plan(tuple(str(a) for a in obj.get("assumptions") or []), tuple(s for s in subs if s))
    if kind == "synthesis":
        return Synthesis(str(obj.get("answer", "")), tuple(str(q) for q in obj.get("open_questions") or []))
    if kind == "delegate":
        s = _subtask(obj.get("subtask"))
        return Delegate(s) if s else None
    if kind == "answer":
        return Answer(str(obj.get("answer", "")))
    return None


def schedule(plan: Plan) -> list[list[Subtask]]:
    ids = [s.id for s in plan.subtasks]
    if len(set(ids)) != len(ids):
        raise PlanError(f"duplicate subtask id in {ids}")
    by = {s.id: s for s in plan.subtasks}
    for s in plan.subtasks:
        for d in s.depends_on:
            if d not in by:
                raise PlanError(f"subtask '{s.id}' depends on '{d}', which is not in the plan")
    done: set[str] = set()
    waves: list[list[Subtask]] = []
    remaining = list(plan.subtasks)
    while remaining:
        wave = [s for s in remaining if all(d in done for d in s.depends_on)]
        if not wave:
            raise PlanError(f"dependency cycle among {[s.id for s in remaining]}")
        waves.append(wave)
        done |= {s.id for s in wave}
        remaining = [s for s in remaining if s.id not in done]
    return waves
```

- [ ] **Step 4: Run, check, commit**

Run: `uv run pytest tests/unit/test_studio_plan.py -q 2>&1 | tail -2 && task check 2>&1 | tail -1`

```bash
git add src/airlie/topology/studio_plan.py tests/unit/test_studio_plan.py
git commit -m "feat(studio): chief protocol parsing (plan/synthesis/delegate/answer) and wave scheduling"
```

---

### Task 4: The review call

**Files:**
- Create: `src/airlie/topology/review.py`, `prompts/roles/reviewer.md`
- Test: `tests/unit/test_review.py`

**Interfaces:**
- Consumes: `Engine.generate`, `GenRequest`, `Message`, `RoleSpec`, `prompt_text` (Task 2), `extract_json` (Task 3).
- Produces: `Finding(severity: str, file: str, line: int | None, claim: str, evidence: str)`; `Review(verdict: str, findings: tuple[Finding, ...], raw: str)` with `verdict` ∈ `approve | request_changes | unparsable`; `Review.blocking -> list[Finding]`; `parse_review(text) -> Review`; `review_message(brief, diff, test_tail, files: dict[str, str], *, diff_chars, files_chars) -> str`; `Reviewer(engine, role_name, role)`, `async review(brief, diff, test_tail, files) -> Review`; `nudge_from_review(r: Review) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
"""Review (spec §4a): one one-shot call on the cross-family reviewer; JSON verdict + findings; one fix round."""

from pathlib import Path

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply
from airlie.topology.review import Reviewer, nudge_from_review, parse_review, review_message

RC = '{"verdict": "request_changes", "findings": [{"severity": "blocking", "file": "app/orders.py", "line": 41, "claim": "qty == 0 reaches the division", "evidence": "guard is qty < 0"}, {"severity": "minor", "file": "app/orders.py", "claim": "name", "evidence": "x"}]}'


def test_parse_review_reads_verdict_and_findings() -> None:
    r = parse_review("to=user says:\n" + RC)
    assert r.verdict == "request_changes" and len(r.findings) == 2
    assert r.blocking[0].line == 41 and r.findings[1].line is None


def test_parse_review_unparsable() -> None:
    r = parse_review("Looks fine to me.")
    assert r.verdict == "unparsable" and r.findings == () and r.raw == "Looks fine to me."
    assert parse_review('{"verdict": "maybe"}').verdict == "unparsable"


def test_review_message_caps_diff_and_files() -> None:
    m = review_message("fix it", "d" * 100, "1 failed", {"a.py": "x" * 100}, diff_chars=10, files_chars=10)
    assert "dddddddddd\n[diff truncated" in m and "xxxxxxxxxx\n[file truncated" in m and "1 failed" in m


async def test_reviewer_calls_the_role_once_without_tools(tmp_path: Path) -> None:
    (tmp_path / "reviewer.md").write_text("---\nsummary: no\n---\nYou review.")
    eng = FakeEngine({"reviewer": [FakeReply(text=RC)]})
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    r = await Reviewer(eng, "reviewer", role).review("fix it", "diff", "tail", {"a.py": "code"})
    assert r.verdict == "request_changes"
    req = eng.requests[0]
    assert req.tools is None and req.messages[0].content == "You review." and "diff" in req.messages[1].content


def test_nudge_lists_blocking_findings_with_lines() -> None:
    text = nudge_from_review(parse_review(RC))
    assert text.startswith("A reviewer found blocking problems") and "app/orders.py:41" in text and "minor" not in text
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/unit/test_review.py -q 2>&1 | tail -3`
Expected: ImportError.

- [ ] **Step 3: Implement**

`src/airlie/topology/review.py`:

```python
"""The adversarial review (design §4a): one call on a different-family model, JSON verdict and findings."""

from __future__ import annotations

from dataclasses import dataclass

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message
from airlie.skills import prompt_text
from airlie.topology.studio_plan import extract_json

VERDICTS = ("approve", "request_changes")


@dataclass(frozen=True)
class Finding:
    severity: str
    file: str
    line: int | None
    claim: str
    evidence: str


@dataclass(frozen=True)
class Review:
    verdict: str
    findings: tuple[Finding, ...]
    raw: str

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


def parse_review(text: str) -> Review:
    obj = extract_json(text)
    if obj is None or obj.get("verdict") not in VERDICTS:
        return Review("unparsable", (), text)
    findings: list[Finding] = []
    for f in obj.get("findings") or []:
        if not isinstance(f, dict):
            continue
        line = f.get("line")
        findings.append(
            Finding(
                str(f.get("severity", "minor")), str(f.get("file", "")),
                int(line) if isinstance(line, int) else None,
                str(f.get("claim", "")), str(f.get("evidence", "")),
            )
        )  # fmt: skip
    return Review(str(obj["verdict"]), tuple(findings), text)


def _cap(text: str, n: int, what: str) -> str:
    return text if len(text) <= n else f"{text[:n]}\n[{what} truncated at {n} chars]"


def review_message(
    brief: str, diff: str, test_tail: str, files: dict[str, str], *, diff_chars: int, files_chars: int
) -> str:
    parts = [f"## Task\n{brief}", f"## Diff\n```\n{_cap(diff, diff_chars, 'diff')}\n```", f"## Test output (tail)\n```\n{test_tail}\n```"]
    budget = files_chars
    for path, text in files.items():
        if budget <= 0:
            break
        shown = _cap(text, budget, "file")
        budget -= len(shown)
        parts.append(f"## {path}\n```\n{shown}\n```")
    parts.append('Reply with one JSON object: {"verdict": "approve" | "request_changes", "findings": [{"severity": "blocking" | "minor", "file": "...", "line": 12, "claim": "...", "evidence": "..."}]}')
    return "\n\n".join(parts)


class Reviewer:
    def __init__(self, engine: Engine, role_name: str, role: RoleSpec) -> None:
        self.engine = engine
        self.role_name = role_name
        self.role = role

    async def review(self, brief: str, diff: str, test_tail: str, files: dict[str, str], *, diff_chars: int = 40000, files_chars: int = 20000) -> Review:
        req = GenRequest(
            role=self.role_name,
            messages=[
                Message(role="system", content=prompt_text(self.role.prompt)),
                Message(role="user", content=review_message(brief, diff, test_tail, files, diff_chars=diff_chars, files_chars=files_chars)),
            ],
            sampling=self.role.sampling,
            max_tokens=self.role.max_tokens,
            thinking=self.role.thinking,
        )
        res = await self.engine.generate(req)
        return parse_review(res.text)


def nudge_from_review(r: Review) -> str:
    lines = ["A reviewer found blocking problems with your change. Fix each one, run the tests, then stop:"]
    for f in r.blocking:
        where = f"{f.file}:{f.line}" if f.line is not None else f.file
        lines.append(f"- {where}: {f.claim} (evidence: {f.evidence})")
    return "\n".join(lines)
```

`prompts/roles/reviewer.md`:

```markdown
---
summary: Adversarial review of a code change against its task, from a different model family (not routable)
routable: false
---
# reviewer

You review one code change, made by another engineer, against the task it was meant to accomplish. You are
looking for what is wrong. Passing tests are not evidence that the tests are adequate. You never edit code.

Read the task, the diff, the test output and the files shown. Then reply with exactly one JSON object and
nothing else:

{"verdict": "approve" | "request_changes",
 "findings": [{"severity": "blocking" | "minor", "file": "path", "line": 12, "claim": "what is wrong",
               "evidence": "the line or test output that shows it"}]}

Rules: every claim cites a file and, when possible, a line; `blocking` means the task is not accomplished or
the change breaks something; `minor` is everything else; an empty findings list with "approve" is a valid
answer when you found nothing; never invent files or lines that are not shown.
```

`routable: false` in the frontmatter is honoured by `routable_roles` (Task 2): add `if meta.get("routable", "").lower() == "false": continue` there (and a one-line test).

- [ ] **Step 4: Run, check, commit**

Run: `uv run pytest tests/unit/test_review.py tests/unit/test_skills.py -q 2>&1 | tail -2 && task check 2>&1 | tail -1`

```bash
git add src/airlie/topology/review.py prompts/roles/reviewer.md src/airlie/skills tests
git commit -m "feat(studio): cross-family review call — JSON verdict/findings, capped message, fix-round nudge; reviewer prompt"
```

---

### Task 5: The studio orchestrator

**Files:**
- Create: `src/airlie/topology/studio.py`
- Modify: `src/airlie/topology/protocol.py` (`studio` kind), `src/airlie/teams/protocol.py` (`TeamContext.images: list[Path]`), `src/airlie/teams/native_team.py` (images → `Message.parts` on the goal message), `src/airlie/teams/worktree.py` (`diff_between(worktree, base_sha, max_chars)`, `changed_files(worktree, base_sha) -> list[str]`)
- Test: `tests/unit/test_studio_orchestrator.py`, `tests/unit/test_native_team.py` (images), `tests/unit/test_worktree.py` (diff helpers; create if absent)

**Interfaces:**
- Consumes: `StudioSpec`/`ReviewSpec`/`RoleSpec.tools/skills/knowledge/vision/max_steps` (T1); `prompt_text`, `routable_roles`, `load_skills`, `skill_criteria`, `KnowledgeIndex`, `SearchKnowledge` (T2); `parse_chief`, `schedule`, `PlanError`, `Plan`, `Subtask`, `Synthesis`, `Delegate`, `Answer` (T3); `Reviewer`, `Review`, `nudge_from_review` (T4); Plan 1: `build_router`, `RoutingLog`, `RouteDecision`, `attach_decide`, `image_part`, `ContentPart`; existing: `NativeTeam`, `TeamContext`, `TeamResult`, `LoggingObserver`, `add_worktree`, `commit_wip`, `ToolRegistry`, `ToolContext`, `build_repo_map`, `RunDir`, `EventSink`, `RunReport`, `SlotScheduler`.
- Produces: `StudioOrchestrator(cfg, engine, *, run_id=None, team_factory=None, router=None)` with `run_dir` and `async execute_task(query_text, *, repo, test_cmd=None, images: list[Path] = ()) -> RunReport`; `build_topology` returns it for `studio`; `execute_task` on every topology accepts `images` (ignored by the others). Events: `plan`, `plan_error`, `route`, `skill`, `subtask_start`, `subtask_done`, `review`, `fix_round`, `synthesis`, `run_done`. Report: `RunReport(teams=[one TeamResult per subtask id], winner="studio", winning_branch="airlie/<run>/studio", final_answer=<synthesis or answer>, tests={"studio": passed|None})` and `report.md` with the subtask table; `runs/<id>/routing.jsonl`. The worktree is `run_dir/studio` so the bench grades it by `report.winner`.

- [ ] **Step 1: Worktree helpers and images (tests first)**

`tests/unit/test_worktree.py` (append or create):

```python
def test_diff_between_and_changed_files(tmp_path: Path) -> None:
    from airlie.teams.worktree import changed_files, commit_wip, diff_between
    repo = repo_with_marker_tests(tmp_path)  # reuse the helper from test_fork_orchestrator
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "new.py").write_text("x = 1\n")
    commit_wip(repo, "wip")
    assert changed_files(repo, base) == ["new.py"]
    assert "+x = 1" in diff_between(repo, base, max_chars=1000)
    assert diff_between(repo, base, max_chars=5) == "diff "
```

`tests/unit/test_native_team.py`:

```python
async def test_images_become_parts_on_the_goal_message(tmp_path: Path) -> None:
    from airlie.engine import FakeEngine, FakeReply
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.images = [png]
    await team.run(ctx, Recorder())
    goal = engine.requests[0].messages[1]
    assert goal.parts is not None and goal.parts[0].type == "text" and goal.parts[1].type == "image_url"
```

Implement in `worktree.py`:

```python
def diff_between(worktree: Path, base_sha: str, max_chars: int) -> str:
    out = subprocess.run(["git", "diff", "--no-color", base_sha, "HEAD"], cwd=worktree, capture_output=True, text=True).stdout
    return out[:max_chars]


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", base_sha, "HEAD"], cwd=worktree, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]
```

`TeamContext.images: list[Path] = field(default_factory=list)`; in `NativeTeam.run`, when `ctx.images` and starting fresh: `Message(role="user", content=goal_msg, parts=(ContentPart("text", text=goal_msg), *[image_part(p) for p in ctx.images]))`.

Run: `uv run pytest tests/unit/test_worktree.py tests/unit/test_native_team.py -q 2>&1 | tail -2` → RED then GREEN after the edits. Commit: `git commit -m "feat(teams): images as content parts on the goal; diff_between/changed_files worktree helpers"`.

- [ ] **Step 2: Write the failing orchestrator tests**

`tests/unit/test_studio_orchestrator.py` — a scripted chief, two specialists on `FakeEngine`, a `ChiefRouter` (no Kev), the marker-test repo:

```python
"""Studio topology end to end on FakeEngine: plan → route → specialists (writers serialised) → review →
synthesis; report and events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.router import ChiefRouter
from airlie.topology.protocol import build_topology
from airlie.topology.studio import StudioOrchestrator
from tests.unit.test_fork_orchestrator import repo_with_marker_tests

ROLES = {
    "chief": ("---\nsummary: no\nroutable: false\n---\n# chief\nJSON only.", []),
    "coder": ("---\nsummary: Implement or change code\n---\n# coder", None),
    "qa": ("---\nsummary: Tests and edge cases\n---\n# qa", ["read_file", "write_file", "run_tests"]),
    "docs": ("---\nsummary: README and guides\n---\n# docs", ["read_file", "list_dir"]),
    "reviewer": ("---\nsummary: no\nroutable: false\n---\n# reviewer", []),
    "summarizer": ("# summarizer", None),
    "judge": ("# judge", None),
    "aggregator": ("# aggregator", None),
}


def cfg_for(tmp_path: Path, **studio: object):  # type: ignore[no-untyped-def]
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    roles = {}
    for name, (text, tools) in ROLES.items():
        (d / f"{name}.md").write_text(text)
        roles[name] = {"prompt": f"{name}.md", "model": "base", "thinking": False}
        if tools is not None:
            roles[name]["tools"] = tools
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": roles,
        "teams": {"orchestrator": "native", "max_steps": 6},
        "tools": {"builtin": ["read_file", "write_file", "list_dir", "run_tests"]},
        "topology": {"kind": "studio"},
        "router": {"kind": "chief"},
        "studio": {"parallel": 2, "review": {"enabled": True, "roles": ["coder"], "rounds": 1}, **studio},
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (d / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(d / "airlie.yaml")


PLAN = FakeReply(text=json.dumps({"type": "plan", "assumptions": ["a1"], "subtasks": [
    {"id": "fix", "role": "coder", "brief": "make the check pass"},
    {"id": "note", "role": "docs", "brief": "describe the repo", "depends_on": ["fix"]},
]}))  # fmt: skip
SYNTH = FakeReply(text=json.dumps({"type": "synthesis", "answer": "all done", "open_questions": []}))
WRITE_PASS = FakeReply(tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "PASS", "content": "ok\n"}))])
RUN = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
DONE = FakeReply(text="done")
APPROVE = FakeReply(text='{"verdict": "approve", "findings": []}')
CHANGES = FakeReply(text='{"verdict": "request_changes", "findings": [{"severity": "blocking", "file": "PASS", "line": 1, "claim": "wrong content", "evidence": "x"}]}')


def events(report):  # type: ignore[no-untyped-def]
    return [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]


async def test_plan_route_run_review_synthesis(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({
        "chief": [PLAN, SYNTH],
        "coder": [WRITE_PASS, RUN, DONE],
        "docs": [DONE],
        "reviewer": [APPROVE],
    })  # fmt: skip
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    assert report.final_answer == "all done" and report.winner == "studio"
    assert [t.team_id for t in report.teams] == ["fix", "note"] and all(t.ok for t in report.teams)
    assert report.tests == {"studio": True}
    kinds = [e["kind"] for e in events(report)]
    assert kinds[:2] == ["run_start", "plan"] and "review" in kinds and kinds[-1] == "run_done"
    route = [e for e in events(report) if e["kind"] == "route"]
    assert [r["chosen"] for r in route] == ["coder", "docs"] and route[0]["decision"] == "hint"
    assert (report.run_dir.path / "routing.jsonl").read_text().count("\n") == 2
    assert (report.run_dir.path / "report.md").read_text().count("| fix |") == 1
    assert (report.run_dir.path / "studio" / "PASS").exists()


async def test_request_changes_gives_one_fix_round_then_continues(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({
        "chief": [PLAN, SYNTH],
        "coder": [WRITE_PASS, RUN, DONE, WRITE_PASS, DONE],  # second DONE ends the fix round
        "docs": [DONE],
        "reviewer": [CHANGES, APPROVE],
    })  # fmt: skip
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    ev = events(report)
    assert [e["verdict"] for e in ev if e["kind"] == "review"] == ["request_changes", "approve"]
    fix = [e for e in ev if e["kind"] == "fix_round"][0]
    assert fix["subtask"] == "fix" and fix["blocking"] == 1
    nudge = [r for r in engine.requests if r.role == "coder"][3].messages[-1].content
    assert nudge.startswith("A reviewer found blocking problems") and "PASS:1" in nudge
    assert report.final_answer == "all done"


async def test_unparsable_review_skips_the_fix_round(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [FakeReply(text="fine")]})
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task("g", repo=repo, test_cmd="./check.sh")
    ev = events(report)
    assert [e["verdict"] for e in ev if e["kind"] == "review"] == ["unparsable"]
    assert not [e for e in ev if e["kind"] == "fix_round"] and report.final_answer == "all done"


async def test_unparsable_plan_twice_ends_the_run_with_the_text(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [FakeReply(text="I would read the code."), FakeReply(text="Still prose.")]})
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task("g", repo=repo)
    assert report.final_answer == "Still prose." and report.teams == []
    assert [e["kind"] for e in events(report)].count("plan_error") == 2


async def test_hint_to_an_unroutable_role_is_ignored(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(text=json.dumps({"type": "plan", "subtasks": [{"id": "x", "role": "chief", "brief": "write docs for the repo"}]}))
    engine = FakeEngine({"chief": [plan, SYNTH], "docs": [DONE], "coder": [DONE]})

    class TopRouter:  # a jev-like router: ignores the hint when it is not in the closed set
        async def route(self, brief, criteria, hint=None, *, question="role", instructions=""):  # type: ignore[no-untyped-def]
            from airlie.router import RouteDecision
            chosen = hint if hint in criteria else "docs"
            return RouteDecision(question, chosen, chosen, "clear", {chosen: 1.0}, 0.9, hint)

    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=TopRouter()).execute_task("g", repo=repo)
    route = [e for e in events(report) if e["kind"] == "route"][0]
    assert route["chosen"] == "docs" and "chief" not in route["criteria"]


async def test_writers_are_serialised_readers_run_in_parallel(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(text=json.dumps({"type": "plan", "subtasks": [
        {"id": "a", "role": "coder", "brief": "write a"},
        {"id": "b", "role": "coder", "brief": "write b"},
        {"id": "r", "role": "docs", "brief": "read"},
    ]}))  # fmt: skip
    order: list[str] = []

    class SlowEngine(FakeEngine):
        async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
            order.append(f"start:{req.role}")
            await asyncio.sleep(0.01)
            order.append(f"end:{req.role}")
            return await super().generate(req, on_token)

    engine = SlowEngine({"chief": [plan, SYNTH], "coder": [DONE, DONE], "docs": [DONE], "reviewer": [APPROVE, APPROVE]})
    cfg = cfg_for(tmp_path, review={"enabled": False})
    await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    coder_starts = [i for i, x in enumerate(order) if x == "start:coder"]
    coder_ends = [i for i, x in enumerate(order) if x == "end:coder"]
    assert coder_starts[1] > coder_ends[0]  # the second writer waited for the first
    assert order.index("start:docs") < coder_ends[0] or order.index("start:docs") < coder_starts[1]  # the reader did not wait


def test_build_topology_returns_studio(tmp_path: Path) -> None:
    orch = build_topology(cfg_for(tmp_path), FakeEngine({}))
    assert isinstance(orch, StudioOrchestrator)
```

Run: `uv run pytest tests/unit/test_studio_orchestrator.py -q 2>&1 | tail -3` → ImportError.

- [ ] **Step 3: Implement `src/airlie/topology/studio.py`**

```python
"""Studio topology (design 2026-10-13): chief plans → router assigns roles (and a skill) → specialists run as
single agents on one worktree (writers serialised) → cross-family review with one fix round → chief synthesis."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from airlie.config import AirlieConfig, RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, GenRequest, Message, image_part
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.repo_map import build_repo_map
from airlie.report import RunReport
from airlie.router import Router, RouteDecision, RoutingLog, build_router
from airlie.scheduler import SlotScheduler
from airlie.skills import KnowledgeIndex, load_skills, prompt_text, routable_roles, skill_criteria
from airlie.teams import NativeTeam, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.worktree import add_worktree, changed_files, commit_wip, diff_between
from airlie.tools import ToolContext, ToolRegistry, attach_decide
from airlie.tools.knowledge import SearchKnowledge
from airlie.topology.review import Review, Reviewer, nudge_from_review
from airlie.topology.studio_plan import Answer, Delegate, Plan, PlanError, Subtask, Synthesis, parse_chief, schedule

WRITE_TOOLS = {"write_file", "edit_file", "shell", "git"}
SKILL_QUESTION = "Which guideline should this specialist follow as the primary lens?"


@dataclass
class SubtaskRun:
    subtask: Subtask
    role: str
    decision: RouteDecision
    skill: str | None = None
    result: TeamResult | None = None
    review: Review | None = None
    fix_rounds: int = 0
    tests_passed: bool | None = None


class StudioOrchestrator:
    def __init__(
        self, cfg: AirlieConfig, engine: Engine, *, run_id: str | None = None,
        team_factory: TeamOrchestrator | None = None, router: Router | None = None,
    ) -> None:  # fmt: skip
        self.cfg = cfg
        self.engine = engine
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id)
        self.sink = EventSink(self.run_dir.events_path, self.run_dir.run_id)
        self.team = team_factory or NativeTeam(engine)
        self.router = router or build_router(cfg)
        self.routing_log = RoutingLog(self.run_dir.path / "routing.jsonl")
        self.observer = LoggingObserver(self.sink, self.run_dir)
        self.scheduler = SlotScheduler(cfg.scheduler.max_active)
        self.write_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(cfg.studio.parallel)
        self.preamble: str | None = None
        self.runs: dict[str, SubtaskRun] = {}
        self.images: list[Path] = []
        self.base_sha = ""
        self.worktree: Path | None = None
        s = cfg.studio
        exclude = {s.chief_role, s.review.role, cfg.lts.summarizer.role, cfg.lts.controller.judge_role,
                   cfg.aggregator.role, cfg.aggregator.judge_role, cfg.fork.hypothesis_role}
        self.criteria = routable_roles(cfg, exclude=exclude)

    # ---- chief ----
    async def _chief(self, user: str, *, with_images: bool) -> str:
        role_name = self.cfg.studio.chief_role
        role = self.cfg.roles[role_name]
        parts = None
        if with_images and self.images:
            parts = (ContentPart("text", text=user), *[image_part(p) for p in self.images])
        req = GenRequest(
            role=role_name,
            messages=[Message(role="system", content=prompt_text(role.prompt)), Message(role="user", content=user, parts=parts)],
            sampling=role.sampling, max_tokens=role.max_tokens, thinking=role.thinking,
        )  # fmt: skip
        return (await self.engine.generate(req)).text

    def _roles_table(self) -> str:
        return "\n".join(f"- {name}: {summary}" for name, summary in self.criteria.items())

    def _plan_message(self, goal: str) -> str:
        images = f"\nAttached images: {len(self.images)} (refer to them as img:1 …)." if self.images else ""
        head = f"{self.preamble}\n\n" if self.preamble else ""
        return (
            f"{head}## Request\n{goal}{images}\n\n## Specialists\n{self._roles_table()}\n\n"
            f"Reply with one JSON object: a plan of at most {self.cfg.studio.max_subtasks} subtasks "
            '({"type": "plan", "assumptions": [...], "subtasks": [{"id", "role", "brief", "depends_on", "inputs"}]}) '
            'or, if no specialist is needed, {"type": "answer", "answer": "..."}.'
        )

    def _synthesis_message(self, goal: str) -> str:
        lines = [f"## Request\n{goal}", "## Subtask results"]
        for r in self.runs.values():
            res = r.result
            outcome = f"{res.stop_reason}, {res.steps} steps" if res else "not run"
            review = f"; review: {r.review.verdict} ({len(r.review.blocking)} blocking)" if r.review else ""
            text = (res.final_text if res else "")[: self.cfg.studio.result_chars]
            lines.append(f"### {r.subtask.id} ({r.role}; {outcome}{review})\n{text}")
        lines.append(
            'Reply with one JSON object: {"type": "synthesis", "answer": "...", "open_questions": [...]} or, for one '
            'more subtask, {"type": "delegate", "subtask": {"id", "role", "brief", "depends_on"}}.'
        )
        return "\n\n".join(lines)

    # ---- specialists ----
    def _tools(self, role_name: str, role: RoleSpec, test_cmd: str | None) -> ToolRegistry:
        assert self.worktree is not None
        names = self.cfg.tools.builtin if role.tools is None else [t for t in role.tools if t not in ("decide", "search_knowledge")]
        reg = ToolRegistry.builtin(names, ToolContext(self.worktree, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars, test_cmd))
        if role.tools and "decide" in role.tools:
            attach_decide(reg, self.cfg)
        if role.knowledge is not None and (role.tools is None or "search_knowledge" in role.tools):
            reg.add(SearchKnowledge(KnowledgeIndex.build(role.knowledge)))
        return reg

    def _writes(self, role: RoleSpec) -> bool:
        names = self.cfg.tools.builtin if role.tools is None else role.tools
        return bool(WRITE_TOOLS & set(names))

    def _brief_with_deps(self, s: Subtask) -> str:
        parts = [s.brief]
        for d in s.depends_on:
            r = self.runs.get(d)
            if r and r.result:
                parts.append(f"## Result of '{d}' ({r.role})\n{r.result.final_text[: self.cfg.studio.result_chars]}")
        return "\n\n".join(parts)

    async def _route(self, s: Subtask) -> tuple[str, RouteDecision]:
        hint = s.role_hint if s.role_hint in self.criteria else None
        d = await self.router.route(s.brief, self.criteria, hint)
        self.routing_log.record(s.id, d)
        self.sink.emit("route", subtask=s.id, chosen=d.chosen, decision=d.decision, confidence=d.confidence,
                       probabilities=d.probabilities, hint=s.role_hint, criteria=sorted(self.criteria))  # fmt: skip
        return d.chosen, d

    async def _skill(self, s: Subtask, role: RoleSpec) -> tuple[str | None, str]:
        if role.skills is None:
            return None, ""
        skills = load_skills(role.skills)
        if not skills:
            return None, ""
        d = await self.router.route(s.brief, skill_criteria(skills), None, question="skill", instructions=SKILL_QUESTION)
        self.routing_log.record(f"{s.id}:skill", d)
        self.sink.emit("skill", subtask=s.id, chosen=d.chosen, decision=d.decision, confidence=d.confidence)
        chosen = [k for k in skills if k.name == d.chosen]
        if d.decision == "ambiguous":  # ai-mode: load the top two when unclear
            second = sorted(d.probabilities.items(), key=lambda kv: -kv[1])[1:2]
            chosen += [k for k in skills if second and k.name == second[0][0]]
        return d.chosen, "\n\n".join(f"## Guideline: {k.name}\n{k.text}" for k in chosen)

    def _context(self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, *, preamble: str, parent: TeamResult | None, nudge: str | None) -> TeamContext:
        assert self.worktree is not None
        return TeamContext(
            team_id=run.subtask.id, goal=self._brief_with_deps(run.subtask), role=role, tools=self._tools(run.role, role, test_cmd),
            worktree=self.worktree, role_name=run.role, max_steps=role.max_steps or self.cfg.teams.max_steps,
            token_budget=self.cfg.teams.token_budget, scheduler=self.scheduler, preamble=preamble or None,
            initial_state=parent.state if parent else None, nudge=nudge, stop_when=lambda _s: False,
            images=list(self.images) if role.vision else [],
        )  # fmt: skip

    async def _run_subtask(self, run: SubtaskRun, test_cmd: str | None) -> None:
        role = self.cfg.roles[run.role]
        run.skill, skill_text = await self._skill(run.subtask, role)
        preamble = "\n\n".join(x for x in (self.preamble or "", skill_text) if x)
        self.sink.emit("subtask_start", subtask=run.subtask.id, role=run.role, skill=run.skill, writes=self._writes(role))
        ctx = self._context(run, role, test_cmd, preamble=preamble, parent=None, nudge=None)
        async with self.sem:
            if self._writes(role):
                async with self.write_lock:
                    run.result = await self.team.run(ctx, self.observer)
                    await self._review_and_fix(run, role, test_cmd, preamble)
            else:
                run.result = await self.team.run(ctx, self.observer)
        res = run.result
        assert res is not None
        self.sink.emit("subtask_done", subtask=run.subtask.id, role=run.role, ok=res.ok, stop_reason=res.stop_reason, steps=res.steps, fix_rounds=run.fix_rounds)

    async def _review_and_fix(self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, preamble: str) -> None:
        rv = self.cfg.studio.review
        assert self.worktree is not None and run.result is not None
        if not rv.enabled or run.role not in rv.roles:
            return
        reviewer = Reviewer(self.engine, rv.role, self.cfg.roles[rv.role])
        before = self.base_sha
        for round_no in range(rv.rounds + 1):
            commit_wip(self.worktree, f"studio: {run.subtask.id} (round {round_no})")
            files = {p: (self.worktree / p).read_text(errors="replace") for p in changed_files(self.worktree, before) if (self.worktree / p).is_file()}
            review = await reviewer.review(run.subtask.brief, diff_between(self.worktree, before, rv.diff_chars), run.result.last_test_output[-3000:], files, diff_chars=rv.diff_chars, files_chars=rv.files_chars)
            run.review = review
            self.sink.emit("review", subtask=run.subtask.id, verdict=review.verdict, findings=len(review.findings), blocking=len(review.blocking), round=round_no)
            if review.verdict != "request_changes" or not review.blocking or round_no >= rv.rounds:
                return
            run.fix_rounds += 1
            self.sink.emit("fix_round", subtask=run.subtask.id, round=run.fix_rounds, blocking=len(review.blocking))
            ctx = self._context(run, role, test_cmd, preamble=preamble, parent=run.result, nudge=nudge_from_review(review))
            run.result = await self.team.run(ctx, self.observer)

    async def _run_plan(self, plan: Plan, test_cmd: str | None) -> None:
        for wave in schedule(plan):
            runs: list[SubtaskRun] = []
            for s in wave:
                role, d = await self._route(s)
                runs.append(SubtaskRun(s, role, d))
                self.runs[s.id] = runs[-1]
            await asyncio.gather(*(self._run_subtask(r, test_cmd) for r in runs))

    # ---- the run ----
    async def execute_task(self, query_text: str, *, repo: Path, test_cmd: str | None = None, images: list[Path] = ()) -> RunReport:  # type: ignore[assignment]
        t0 = time.monotonic()
        self.images = list(images)
        self.sink.emit("run_start", goal=query_text, k=1, repo=str(repo), orchestrator="studio")
        if self.cfg.repo_map.enabled:
            text, cached = build_repo_map(repo, test_cmd=test_cmd, max_chars=self.cfg.repo_map.max_chars, depth=self.cfg.repo_map.depth, cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps")
            self.preamble = text
            self.sink.emit("repo_map", chars=len(text), cached=cached)
        branch = f"airlie/{self.run_dir.run_id}/studio"
        self.worktree = add_worktree(repo, self.run_dir.path / "studio", branch=branch)
        self.base_sha = commit_wip(self.worktree, "studio: base")
        final = ""
        plan: Plan | None = None
        for attempt in range(2):
            text = await self._chief(self._plan_message(query_text), with_images=True)
            parsed = parse_chief(text)
            if isinstance(parsed, Answer):
                final = parsed.text
                break
            if isinstance(parsed, Plan):
                try:
                    schedule(parsed)
                except PlanError as e:
                    self.sink.emit("plan_error", attempt=attempt, error=str(e))
                    final = text
                    continue
                plan = parsed
                self.sink.emit("plan", assumptions=list(plan.assumptions), subtasks=[s.id for s in plan.subtasks], roles=[s.role_hint for s in plan.subtasks])
                break
            self.sink.emit("plan_error", attempt=attempt, error="no plan or answer in the reply")
            final = text
        if plan is not None:
            await self._run_plan(plan, test_cmd)
            for _round in range(self.cfg.studio.max_rounds):
                text = await self._chief(self._synthesis_message(query_text), with_images=False)
                parsed = parse_chief(text)
                if isinstance(parsed, Delegate):
                    extra = Plan((), (parsed.subtask,))
                    try:
                        schedule(extra)
                    except PlanError as e:
                        self.sink.emit("plan_error", attempt=_round, error=str(e))
                        continue
                    await self._run_plan(extra, test_cmd)
                    continue
                final = parsed.answer if isinstance(parsed, Synthesis) else text
                self.sink.emit("synthesis", chars=len(final), open_questions=list(parsed.open_questions) if isinstance(parsed, Synthesis) else [])
                break
        passed = await self._tests_pass(test_cmd)
        report = self._report(query_text, final, t0, branch, passed)
        self.sink.emit("run_done", ok=report.any_ok, wall_ms=round(report.wall_ms, 1), subtasks=len(self.runs), tests=passed)
        return report

    async def _tests_pass(self, test_cmd: str | None) -> bool | None:
        if not test_cmd or self.worktree is None or not self.runs:
            return None
        proc = await asyncio.create_subprocess_shell(test_cmd, cwd=self.worktree, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=self.cfg.tools.shell_timeout_s)
        except TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0

    def _report(self, goal: str, final: str, t0: float, branch: str, passed: bool | None) -> RunReport:
        teams = [r.result for r in self.runs.values() if r.result is not None]
        report = RunReport(self.run_dir.run_id, self.run_dir, goal, teams, (time.monotonic() - t0) * 1000.0)
        report.winner = "studio"
        report.final_answer = final
        report.winning_branch = branch
        report.tests = {"studio": passed}
        report.scores = {"studio": 1.0 if passed else 0.0}
        lines = [f"# airlie studio run {self.run_dir.run_id}", "", f"**Goal:** {goal}", "", "## Final answer", "", final, "",
                 "## Subtasks", "", "| subtask | role | decision | skill | stop | steps | review | fix rounds |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
        for r in self.runs.values():
            res = r.result
            lines.append(f"| {r.subtask.id} | {r.role} | {r.decision.decision} | {r.skill or '-'} | {res.stop_reason if res else '-'} | {res.steps if res else 0} | {r.review.verdict if r.review else '-'} | {r.fix_rounds} |")
        lines += ["", f"Tests on the worktree: {'pass' if passed else ('fail' if passed is False else '-')}; branch `{branch}`."]
        (self.run_dir.path / "report.md").write_text("\n".join(lines) + "\n")
        return report
```

`RunReport.any_ok` is `any(t.ok for t in teams)`; for studio also count a plain `answer` with no subtasks as ok: in `_report`, if `not teams and final`: `report.teams = []` and the CLI's `if not report.any_ok: raise Exit(1)` would fail a successful answer-only run → add `RunReport.ok_override: bool | None = None` consulted by `any_ok` (`return self.ok_override if self.ok_override is not None else any(...)`) and set it to `bool(final)` when there were no subtasks. `build_topology`: `if cfg.topology.kind == "studio": from airlie.topology.studio import StudioOrchestrator; return StudioOrchestrator(cfg, engine, run_id=run_id)`. The `Topology` protocol's `execute_task` gains `images: list[Path] = ()`; `ParallelAgentTeamOrchestrator` and `ForkOrchestrator` accept and ignore it.

- [ ] **Step 4: Run, check, commit**

Run: `uv run pytest tests/unit/test_studio_orchestrator.py tests/unit -q 2>&1 | tail -2 && task check 2>&1 | tail -1`
Expected: all pass (the writer-serialisation test may need `asyncio.sleep(0.01)` → `0.02` on a slow machine; the assertions are on order, not time).

```bash
git add -A src tests
git commit -m "feat(studio): the studio topology — plan, route, specialists on one worktree, review + fix round, synthesis, report"
```

---

### Task 6: CLI, serve, bench and Taskfile

**Files:**
- Modify: `src/airlie/cli.py` (`studio` command, `--image` on `run`, bench review columns), `src/airlie/serve/app.py` (`RunRequest.images`), `src/airlie/bench/topology_bench.py` (`TOPOLOGIES` += studio, `apply_topology("studio")`, `TopoRow.reviews/blocking/fix_rounds`, `events_counts`), `Taskfile.yml` (`studio`)
- Test: `tests/unit/test_cli_run.py`, `tests/unit/test_topology_bench.py`, `tests/unit/test_serve.py`

- [ ] **Step 1: Failing tests**

```python
# test_cli_run.py
def test_studio_command_passes_images(tmp_path: Path, monkeypatch) -> None:
    seen: list = []

    class Stub(StubOrchestrator):
        async def execute_task(self, goal, *, repo, test_cmd=None, images=()):  # type: ignore[no-untyped-def]
            seen.append(list(images))
            return await super().execute_task(goal, repo=repo, test_cmd=test_cmd)

    monkeypatch.setattr(cli, "_orchestrator", lambda cfg, run_id=None: Stub(cfg, None, run_id))
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG")
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    r = runner.invoke(cli.app, ["studio", "critique", "--repo", str(repo), "--image", str(png), "-c", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert seen == [[png.resolve()]]

# test_topology_bench.py
def test_apply_topology_studio_and_review_counts(tmp_path: Path) -> None:
    from airlie.bench.topology_bench import events_counts
    s = apply_topology(cfg(tmp_path), "studio")
    assert s.topology.kind == "studio"
    ev = tmp_path / "events.jsonl"
    ev.write_text("\n".join(json.dumps(e) for e in [
        {"kind": "review", "verdict": "request_changes", "blocking": 2}, {"kind": "fix_round"},
        {"kind": "review", "verdict": "approve", "blocking": 0}, {"kind": "step"},
    ]))  # fmt: skip
    c = events_counts(ev)
    assert c["reviews"] == 2 and c["blocking"] == 2 and c["fix_rounds"] == 1 and c["steps"] == 1

# test_serve.py
def test_run_endpoint_accepts_images(tmp_path: Path) -> None:
    ...  # like test_run_endpoint_accepts_single: POST {"goal","repo","topology":"studio","images":[str(png)]};
         # the factory's Stub.execute_task records `images` and the assertion is seen == [[png]]
```

- [ ] **Step 2: Implement**

`cli.py`: `run` gains `image: Annotated[list[Path] | None, typer.Option("--image", help="image file(s) for vision roles")] = None` passed as `images=[p.resolve() for p in image or []]`; new command:

```python
@app.command()
def studio(
    goal: Annotated[str, typer.Argument(help="what the studio should accomplish")],
    repo: Annotated[Path, typer.Option(help="git repository to work in")] = Path("."),
    image: Annotated[list[Path] | None, typer.Option("--image", help="image file(s) for vision roles")] = None,
    test_cmd: Annotated[str | None, typer.Option(help="command run_tests runs in the worktree")] = None,
    config: ConfigOpt = None,
) -> None:
    """Run the studio topology (chief → router → specialists → review → synthesis) on a goal."""
    run(goal, repo=repo, k=None, test_cmd=test_cmd, topology="studio", config=config, image=image)
```

`select_topology`'s `TOPOLOGY_KINDS` already includes `studio` (T1). `serve/app.py`: `RunRequest.images: list[str] = []`, passed as `images=[Path(p).expanduser().resolve() for p in req.images]`. `topology_bench.py`: `TOPOLOGIES = ("single", "teams", "fork", "studio")`, `apply_topology` branch `elif name == "studio": c.topology.kind = "studio"`, `TopoRow` += `reviews: int = 0`, `blocking: int = 0`, `fix_rounds: int = 0`, `events_counts` counts `review` (and sums `blocking`), `fix_round`; `summarize` adds `mean_reviews`, `mean_fix_rounds`; `render` prints them; `bench_topology` fills the new fields. Taskfile:

```yaml
  studio:
    desc: "Run the studio topology (task studio -- \"goal\" --repo PATH [--image shot.png] [--test-cmd \"pytest -q\"])"
    cmds:
      - '{{.RUN}} airlie studio {{.CLI_ARGS}}'
```

and the `bench:topology` desc mentions `studio`. Note the bench grades `orch.run_dir.path / t.team_id` per team: for studio, `report.teams` are subtasks sharing one worktree, so `bench_topology` must grade `report.winner` ("studio") once: change the grading to `ids = {t.team_id for t in report.teams} | ({report.winner} if report.winner else set())` and grade each id whose directory exists; `winner_passed` stays `graded.get(report.winner)`.

- [ ] **Step 3: Run, check, commit**

Run: `uv run pytest tests/unit -q 2>&1 | tail -2 && task check 2>&1 | tail -1`

```bash
git add -A src tests Taskfile.yml
git commit -m "feat(cli): task studio, --image on run/serve, studio in bench:topology with review columns"
```

---

### Task 7: Prompts, QA skill library, config profile

**Files:**
- Create: `prompts/roles/{chief,architect,product,ux,design,docs,research,qa,security}.md`, `skills/qa/*.md` (vendored from `~/git/ai-mode/presets/agents/qa/guidelines/` with a `summary:` frontmatter line each), `skills/qa/README.md`
- Modify: `prompts/roles/coder.md` (frontmatter summary), `airlie.yaml` (roles, `studio` and `studio-qwen38` profiles), `evals/routing-roles.yaml` (now derived: `task route:eval` reads criteria from the config's routable roles when `--roles` is omitted), `src/airlie/cli.py` (`route eval --roles` default from config)
- Test: `tests/unit/test_prompts.py` (every studio role prompt has a `summary:`; chief and reviewer are `routable: false`; every `skills/qa/*.md` has a summary; config loads for the `studio` and `studio-qwen38` profiles and `routable_roles` equals the nine ai-mode roles)

- [ ] **Step 1: Failing test**

```python
"""The studio prompts and the QA skill library carry the frontmatter the router needs."""

from pathlib import Path

from airlie.config import load_config
from airlie.skills import load_skills, routable_roles, split_frontmatter

ROOT = Path(__file__).resolve().parents[2]
NINE = {"architect", "coder", "product", "research", "docs", "qa", "security", "design", "ux"}


def test_studio_prompts_have_summaries_and_the_right_routability() -> None:
    for name in NINE:
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("summary"), name
    for name in ("chief", "reviewer"):
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("routable") == "false", name


def test_qa_skill_library_has_nine_guides_with_summaries() -> None:
    skills = load_skills(ROOT / "skills/qa")
    assert {s.name for s in skills} == {"unit", "functional", "integration", "component", "contract", "e2e", "visual-regression", "mutation", "ai-skill"}


def test_studio_profiles_load_and_route_the_nine_roles() -> None:
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    assert cfg.topology.kind == "studio" and cfg.engines["main"].server_parsers is False
    assert set(routable_roles(cfg, exclude={cfg.studio.chief_role, cfg.studio.review.role})) == NINE
    q = load_config(ROOT / "airlie.yaml", topology="studio-qwen38")
    assert q.topology.kind == "studio" and q.roles["coder"].model == "generalist" and q.roles["coder"].thinking is True
```

- [ ] **Step 2: Write the prompts**

Each file starts with frontmatter. `summary:` lines are ai-mode's (they scored 54/54 in Plan 1's routing eval). Bodies, ported and cut for an agent with tools:

`prompts/roles/chief.md`:
```markdown
---
summary: Chief of staff: plans, delegates, synthesises (not routable)
routable: false
---
# chief

You are the chief of staff of a small software studio. You are the only voice that talks to the person; the
specialists work for you. You do not do specialist work yourself: you scope it, delegate it, and synthesise.

You answer with exactly one JSON object per turn and nothing else.

When given a request and the list of specialists: decide whether a specialist is needed at all. If not, reply
{"type": "answer", "answer": "..."}. Otherwise reply with a plan: {"type": "plan", "assumptions": ["..."],
"subtasks": [{"id": "short-id", "role": "specialist", "brief": "one clear task", "depends_on": ["other-id"],
"inputs": ["img:1"]}]}. Briefs are self-contained: a specialist sees only its brief and the results it depends
on. Put a repository map or architecture task first when the work needs one. State assumptions instead of
asking questions. Keep plans short: the fewest subtasks that get the request done.

When given the results of the subtasks: reply {"type": "synthesis", "answer": "...", "open_questions": ["..."]}
with the answer the person asked for, written for them, naming what each specialist found and what remains
open. If one more subtask would change the answer, reply {"type": "delegate", "subtask": {...}} instead, once.

Never claim a specialist ran if its result is missing or failed; say so.
```

`prompts/roles/architect.md`:
```markdown
---
summary: Plan multi-step changes: migrations, system design, ADRs, mapping large codebases
---
# architect

You are a software architect working inside a checkout of the repository with read-only tools.

Mission: turn a request and the actual code into a concrete, ordered plan a coder can execute without further
design decisions: files to touch, functions and interfaces, sequence, risks, test strategy, rollback. For a
non-trivial choice, state forces, options, decision and consequences (an ADR, briefly).

How you work: `list_dir` once, then `read_file` the files that matter and `grep` for the symbols you name. Read
before planning; never invent a file path or signature you have not seen. Stop with the plan as your reply:
1. Context and constraints  2. Options and decision  3. Ordered per-file steps  4. Test plan  5. Risks.
Prefer boring technology; do not over-engineer for hypothetical scale.
```

`prompts/roles/coder.md`: prepend `---\nsummary: Implement, fix or refactor code: features, bug fixes, CLI flags, per an approved plan\n---\n` to the existing file (body unchanged).

`prompts/roles/qa.md`:
```markdown
---
summary: Test strategy: write tests and test cases, edge cases, regression plans, find missing coverage
---
# qa

You are a QA engineer paired with the developers, working inside a checkout of the repository.

Mission: write tests that would catch real bugs: edge cases, regression cases, missing coverage, tied to the
code as it is. A guideline for the chosen test layer may precede your brief; follow its checklist and cite it
in your reply.

How you work: `read_file` the code under test and the existing tests. Write tests under `tests/` with
`write_file`. Run `run_tests`; a test that fails for the right reason is a finding, say so; a test that fails
because of a mistake in the test is yours to fix. Stop with: the layer you used, the cases you added and why,
and any bug you found (file, line, what you expected).
```

`prompts/roles/security.md`:
```markdown
---
summary: Threat model, secure review
---
# security

You are a security reviewer (assistive; humans own ship decisions), working inside a checkout of the
repository with read-only tools.

Mission: threat-model the change or feature and review the code for common vulnerabilities: assets, trust
boundaries, STRIDE or OWASP categories; secrets handling, authorization gaps, injection, SSRF, XSS, insecure
defaults. Rate each finding Critical / High / Medium / Low / Info with an exploit scenario and an actionable
fix. Never provide weaponized exploit steps for third-party systems.

How you work: `grep` for the entry points and trust boundaries, `read_file` the code that handles untrusted
input. Stop with the findings, most severe first, each with file and line.
```

`prompts/roles/product.md`:
```markdown
---
summary: Write user stories, acceptance criteria and PRDs; prioritise features, scope and MVP
---
# product

You are a product manager embedded with an engineering team.

Mission: turn a fuzzy ask into a shippable spec: problem and success metrics, users and jobs to be done, user
stories, testable acceptance criteria (Given/When/Then or bullet checks), out of scope, risks and open
questions. Separate must / should / could. Prefer one thin vertical slice over a vague epic. Do not invent
market numbers. If a repository is available, `read_file` the README and any spec before writing.
```

`prompts/roles/ux.md`:
```markdown
---
summary: User flows, usability, journey friction, microcopy, interaction patterns and layout choices
---
# ux

You are a UX partner focused on flows, information architecture and usability. You may be given screenshots.

Mission: improve task success: navigation, copy, empty states, errors, cognitive load. Start from the user's
goal and primary path; identify friction, dead ends and unclear affordances; suggest microcopy alternatives;
separate UX recommendations from visual polish. For a screenshot, critique what is shown, specifically, in
the order a user meets it.
```

`prompts/roles/design.md`:
```markdown
---
summary: UI and visual critique
---
# design

You are a frontend / UI design specialist who can see screenshots.

Mission: critique interfaces and propose concrete visual improvements: spacing, type, contrast, hierarchy,
accessibility, mobile. Be specific (px, hex, WCAG contrast where possible). Prefer one coherent direction
over a list of trends. If asked for HTML/CSS, keep it self-contained.
```

`prompts/roles/docs.md`:
```markdown
---
summary: Documentation prose: README, changelog, release notes, guides, API reference, config docs
---
# docs

You are a technical writer for engineers, working inside a checkout of the repository with read-only tools.

Mission: README sections, changelogs, migration notes, API docs. Lead with what and why, then how; use
runnable examples and mark placeholders obviously; match the project's voice; call out breaking changes
loudly. `read_file` the code you document; never describe behaviour you have not seen.
```

`prompts/roles/research.md`:
```markdown
---
summary: Compare libraries, tools or approaches; investigate APIs and feasibility; competitive analysis; summarise tradeoffs; technical spikes
---
# research

You are a technical research scout.

Mission: investigate APIs, libraries and approaches and produce evidence-backed notes. Separate facts,
inferences and unknowns; prefer primary sources available in the repository or your knowledge base
(`search_knowledge` when you have it); end with a recommendation and what to validate next; keep a short
sources section.
```

- [ ] **Step 3: Vendor the QA guidelines with summaries**

```bash
mkdir -p skills/qa && cp ~/git/ai-mode/presets/agents/qa/guidelines/*.md skills/qa/ && rm skills/qa/README.md && python3 - <<'PY'
from pathlib import Path
summaries = {
    "unit": "Pure functions or single units in isolation, fast, no I/O",
    "functional": "Feature or use-case through its public boundary/API",
    "integration": "Modules with real collaborators (db, http, queues)",
    "component": "UI components rendered and exercised like a user",
    "contract": "Consumer/provider API compatibility (e.g. Pact)",
    "e2e": "Full user journeys across the deployed system",
    "visual-regression": "Rendered UI pixel or snapshot diffs",
    "mutation": "Mutation testing: would the tests catch real bugs? Assertion strength, surviving mutants",
    "ai-skill": "Evals for AI skills, prompts, agent behavior",
}
for name, s in summaries.items():
    p = Path(f"skills/qa/{name}.md")
    p.write_text(f"---\nsummary: {s}\n---\n" + p.read_text())
Path("skills/qa/README.md").write_text("# QA guidelines (vendored from ai-mode, 2026-10-13)\n\nOne file per test layer; the `summary:` line is what the router sees (stage B); the body is the guideline the qa role follows.\n")
PY
```

- [ ] **Step 4: Config**

`airlie.yaml` roles (add; `coder`, `summarizer`, `judge`, `hypothesizer`, `aggregator` unchanged):

```yaml
  chief:        { prompt: prompts/roles/chief.md, model: generalist, tools: [], max_context: 131072, max_tokens: 4096, thinking: true, thinking_budget: 8192, sampling: { temperature: 0.3, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  architect:    { prompt: prompts/roles/architect.md, model: generalist, tools: [read_file, list_dir, grep], max_context: 131072, max_tokens: 4096, thinking: true, thinking_budget: 8192, max_steps: 20, sampling: { temperature: 0.3, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  product:      { prompt: prompts/roles/product.md, model: generalist, tools: [read_file, list_dir], max_tokens: 4096, thinking: false, max_steps: 8, sampling: { temperature: 0.5, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  ux:           { prompt: prompts/roles/ux.md, model: generalist, tools: [], vision: true, max_tokens: 2048, thinking: false, sampling: { temperature: 0.5, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  design:       { prompt: prompts/roles/design.md, model: generalist, tools: [], vision: true, max_tokens: 2048, thinking: false, sampling: { temperature: 0.5, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  docs:         { prompt: prompts/roles/docs.md, model: generalist, tools: [read_file, list_dir, grep], max_tokens: 4096, thinking: false, max_steps: 12, sampling: { temperature: 0.4, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  research:     { prompt: prompts/roles/research.md, model: generalist, tools: [read_file, list_dir, grep, search_knowledge], knowledge: knowledge/research, max_tokens: 4096, thinking: true, thinking_budget: 8192, max_steps: 12, sampling: { temperature: 0.4, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
  qa:           { prompt: prompts/roles/qa.md, model: base, tools: [read_file, list_dir, grep, write_file, edit_file, run_tests], skills: skills/qa, max_tokens: 4096, thinking: false, sampling: { temperature: 0.3, top_p: 0.8, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.05 } }
  security:     { prompt: prompts/roles/security.md, model: base, tools: [read_file, list_dir, grep], max_tokens: 4096, thinking: false, max_steps: 15, sampling: { temperature: 0.3, top_p: 0.8, top_k: 20, min_p: 0.0, presence_penalty: 0.0, repetition_penalty: 1.05 } }
  reviewer:     { prompt: prompts/roles/reviewer.md, model: reviewer, tools: [], max_context: 131072, max_tokens: 2048, thinking: false, chat_template_kwargs: { reasoning_strength: medium }, sampling: { temperature: 0.2, top_p: 0.9, top_k: 20, min_p: 0.0, presence_penalty: 0.0 } }
```

(`knowledge/research/` is created with one `README.md` placeholder note: "Drop markdown here; `search_knowledge` indexes it."). Profiles:

```yaml
    studio:
      engines: { main: { server_parsers: false, max_kv_size: 131072 } }
      models: { generalist: { preload: true }, reviewer: { preload: true } }
      teams: { k: 1, max_steps: 40 }
      lts: { enabled: false }
      studio: { parallel: 3 }
    studio-qwen38:
      extends: studio
      roles: { coder: { model: generalist, thinking: true, thinking_budget: 8192 } }
```

Memory check for `studio` (doctor): base 17 + small 3 + generalist 16.1 + reviewer 19.4 + Kev 3 = 58.5 GB weights, KV 1 × 131072 × 192 MB/1k = 24.6 GB, reserve 12 → 95 GB ≤ 104. Run `uv run airlie doctor` and read the `memory_projection[studio]` row.

`route eval`: `--roles` default `None` → criteria from `routable_roles(cfg, exclude={chief, reviewer})`; the YAML remains an override. Update the task description.

- [ ] **Step 5: Run, check, commit**

Run: `uv run pytest tests/unit/test_prompts.py tests/unit -q 2>&1 | tail -2 && task check 2>&1 | tail -1 && uv run airlie doctor --topology studio 2>&1 | grep -E "projection|reviewer|generalist"`
(Add `--topology` to `doctor` via `_load(config, topology)` if absent: one line.)

```bash
git add -A prompts skills knowledge airlie.yaml src tests evals
git commit -m "feat(studio): role prompts with routing summaries, vendored QA skill library, studio and studio-qwen38 profiles"
```

---

### Task 8: Evals for the studio and the bench run

**Files:**
- Create: `evals/tasks/studio_feature.yaml` (a feature that needs a plan, an implementation and tests: hidden tests grade it like the others)
- Modify: `docs/bench/README.md`, `docs/bench/2026-10-13-topology-bench.md` (studio rows)
- Test: `tests/unit/test_evals.py` (the new task loads and its hidden tests reference files its requirement names)

- [ ] **Step 1: The task**

```yaml
id: studio_feature
language: python
requirement: |
  Add a tiny expense ledger to this repository as the package `ledger/` with `__init__.py` exposing:
  `Ledger()` with `add(amount: float, category: str, note: str = "") -> int` (returns a 1-based entry id,
  rejects amount <= 0 with ValueError), `total(category: str | None = None) -> float` (sum over all entries or
  one category, 0.0 when none), `by_category() -> dict[str, float]` (categories sorted by name, no empty
  categories), and `remove(entry_id: int) -> None` (KeyError when unknown; totals update).
  Write a short README section describing the API, and tests under `tests/`; `python -m pytest` must pass.
setup:
  files:
    pyproject.toml: |
      [project]
      name = "ledger-task"
      version = "0.0.1"
      requires-python = ">=3.12"
      [tool.pytest.ini_options]
      testpaths = ["tests", "hidden_tests"]
    README.md: |
      # ledger task
run_cmd: python -m pytest -q
hidden_tests:
  hidden_tests/test_hidden.py: |
    import pytest
    from ledger import Ledger

    def test_add_total_and_ids():
        l = Ledger()
        assert l.add(10, "food") == 1 and l.add(2.5, "food", "coffee") == 2 and l.add(7, "travel") == 3
        assert l.total() == 19.5 and l.total("food") == 12.5 and l.total("none") == 0.0

    def test_rejects_non_positive():
        with pytest.raises(ValueError):
            Ledger().add(0, "x")

    def test_by_category_sorted_and_no_empties():
        l = Ledger()
        l.add(1, "b"); l.add(2, "a")
        assert list(l.by_category().items()) == [("a", 2.0), ("b", 1.0)]

    def test_remove_updates_totals_and_rejects_unknown():
        l = Ledger()
        i = l.add(5, "food")
        l.remove(i)
        assert l.total() == 0.0 and l.by_category() == {}
        with pytest.raises(KeyError):
            l.remove(99)
```

Unit test: `load_task(ROOT / "evals/tasks/studio_feature.yaml").id == "studio_feature"` and its hidden test imports `ledger`.

- [ ] **Step 2: Commit the task**

```bash
git add evals/tasks/studio_feature.yaml tests/unit/test_evals.py
git commit -m "feat(evals): studio_feature task (plan + implement + tests + docs)"
```

The bench run itself is Task 9 (it needs the live engines).

---

### Task 9: Live — Muse, the studio profile, live tests, the bench, docs

**Files:**
- Create: `tests/live/test_studio_live.py`
- Modify: `README.md` (Studio section), `docs/bench/README.md`, `docs/bench/2026-10-13-topology-bench.md`, `docs/superpowers/specs/2026-10-13-studio-topology-design.md` (§12 result)

- [ ] **Step 1: Pull Muse (ask Noah first: `mlx-community/Muse-Glimmer-30B-4bit`, 19.4 GB, Apache 2.0, revision `3e7677d7`), restart on the studio profile**

```bash
task models:pull -- reviewer
task restart -- --topology studio     # add `topology` to `engine up/restart` via _load(config, topology) if absent
uv run airlie doctor --topology studio | grep -E "projection|reviewer|generalist|decision"
```
Expected: `memory_projection[studio] ok` (≈95 GB), `model:reviewer ok`, decision engine ok; `task status` shows generalist and reviewer `ready`.

- [ ] **Step 2: Live tests**

```python
"""Live studio checks: Muse reviews a real diff as JSON; the chief plans as JSON; a vision role sees an image."""

import json
from pathlib import Path

import pytest

from airlie.cli import _http_engine
from airlie.config import load_config
from airlie.engine import ContentPart, GenRequest, Message, image_part
from airlie.skills import prompt_text
from airlie.topology.review import Reviewer

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]


async def test_reviewer_returns_the_json_contract_on_a_real_diff() -> None:
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("reviewer"))
    diff = "--- a/app/orders.py\n+++ b/app/orders.py\n@@ -1,3 +1,4 @@\n def unit_price(total, qty):\n-    return total / qty\n+    if qty < 0:\n+        raise ValueError\n+    return total / qty\n"
    r = await Reviewer(eng, "reviewer", cfg.roles["reviewer"]).review(
        "Fix the ZeroDivisionError when qty is 0 in unit_price", diff, "1 passed", {"app/orders.py": "def unit_price(total, qty):\n    if qty < 0:\n        raise ValueError\n    return total / qty\n"}
    )
    assert r.verdict in ("approve", "request_changes"), r.raw
    assert r.verdict == "request_changes" and any("0" in f.claim or "zero" in f.claim.lower() for f in r.findings), r.raw
    await eng.stop()


async def test_chief_answers_with_a_plan_object() -> None:
    from airlie.topology.studio_plan import Plan, parse_chief
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("chief"))
    role = cfg.roles["chief"]
    user = "## Request\nAdd input validation to the signup form and tests for it.\n\n## Specialists\n- coder: Implement or change code\n- qa: Test strategy\n- ux: User flows and microcopy\n\nReply with one JSON object: a plan ({\"type\": \"plan\", ...}) or {\"type\": \"answer\", ...}."
    res = await eng.generate(GenRequest(role="chief", messages=[Message(role="system", content=prompt_text(role.prompt)), Message(role="user", content=user)], sampling=role.sampling, max_tokens=role.max_tokens, thinking=role.thinking))
    p = parse_chief(res.text)
    assert isinstance(p, Plan) and p.subtasks, res.text
    await eng.stop()


async def test_vision_role_sees_the_image(tmp_path: Path) -> None:
    import struct, zlib
    def png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:  # a solid-colour PNG, no Pillow needed
        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
        def chunk(t: bytes, d: bytes) -> bytes:
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    p = tmp_path / "red.png"
    p.write_bytes(png(64, 64, (220, 20, 20)))
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("design"))
    role = cfg.roles["design"]
    msg = Message(role="user", content="What single colour fills this image? One word.", parts=(ContentPart("text", text="What single colour fills this image? One word."), image_part(p)))
    res = await eng.generate(GenRequest(role="design", messages=[Message(role="system", content=prompt_text(role.prompt)), msg], sampling=role.sampling, max_tokens=20, thinking=False))
    assert "red" in res.text.lower(), res.text
    await eng.stop()
```

Run: `task test:live -- tests/live/test_studio_live.py -q` → 3 passed. If the reviewer's first live output shows markers the fixtures did not anticipate, record it as `tests/fixtures/parsers/muse_live.txt` and add a `parse_output` test on it (ledger the finding).

- [ ] **Step 3: Routing eval from the config's roles, then the bench**

```bash
task route:eval -- --topology studio --min-accuracy 0.9 --out docs/bench/routing.json
task restart -- --topology studio   # engine hygiene before timing
task bench:topology -- --topologies single,studio --tasks lang_detect,inventory_cli,studio_feature --repeats 2 --out docs/bench/topology-studio.json
```

Record in `docs/bench/2026-10-13-topology-bench.md` (new section "Studio") and `docs/bench/README.md`: per-task pass rate, mean wall-clock on solved runs, reviews / blocking / fix rounds, whether the fix round changed any outcome, routing accuracy. Spec §12: studio is kept if pass rate ≥ single's, solved wall-clock within 25 %, routing ≥ 0.9, review never lowers the pass rate. **Whatever the numbers, report them to Noah with the candidate fixes (`studio-qwen38`, `review.rounds`, prompts, parallelism) and wait for his call before changing defaults; do not disable studio.**

- [ ] **Step 4: Docs**

README: a "Studio" subsection under Topologies (what the chief does, roles and their models, `task studio -- "goal" --repo . --image shot.png`, where routing.jsonl and report.md land, the profile and `task restart -- --topology studio`), the task table rows for `task studio`, and the config section's new role keys (`tools`, `skills`, `knowledge`, `vision`, `max_steps`). Design doc §12: a "Result (2026-10-13)" paragraph with the numbers.

- [ ] **Step 5: Final check and commit**

```bash
task check 2>&1 | tail -1
git add -A
git commit -m "feat(studio): live checks (Muse review JSON, chief plan, vision), routing eval from config roles, studio bench and docs"
```

---

## Self-review

- **Spec coverage.** §4 loop (T5: plan → route → specialists in waves, writers serialised, dependencies appended, budgets via `max_subtasks`/`max_rounds`/`parallel`/`result_chars`); §4a review and fix round (T4, T5); §5 chief protocol incl. repair and the text fallback (T3, T5); §6 stage B skill routing with top-two on ambiguous (T5 `_skill`), `route` events and `routing.jsonl` (T5), `route:eval` criteria from config (T7); §7 skills and knowledge (T2, T7); §8 studio profiles (T7); §9 keys (T1, T7); §10 rows `topology/studio.py`, `review.py`, `skills/*`, `tools/knowledge.py`, prompts, bench, CLI (T2–T8); §11 tests (each task); §12 the bench run and the no-torch rule (T9); §14 `decide` wired through `RoleSpec.tools` (T5 `_tools`). Not in this plan, by design: `needs_input`, cross-team memory, merging parallel edits (spec non-goals).
- **Placeholders.** None; every step carries its code, prompt text or exact command. Task 6's serve test and Task 8's unit test are described by reference to a test two lines above them in the same file with complete assertions stated.
- **Type consistency.** `TeamContext.images` (T5 step 1) consumed by `_context` (T5 step 3) and the live test (T9); `Reviewer.review(brief, diff, test_tail, files, *, diff_chars, files_chars)` (T4) called with those keywords in T5; `parse_chief` returns the four dataclasses T5 branches on; `routable_roles(cfg, exclude=)` (T2) used in T5 and T7; `events_counts` keys `reviews/blocking/fix_rounds` (T6) match the events T5 emits (`review` with `blocking`, `fix_round`); `RunReport.ok_override` added in T5 and read by `any_ok`.
- **Review Focus.** 1 → T3 `test_parse_chief_falls_back_to_text`, `test_schedule_rejects_unknown_dependency_and_cycle`, T5 `test_unparsable_plan_twice_ends_the_run_with_the_text`; 2 → T5 `test_writers_are_serialised_readers_run_in_parallel`; 3 → T4 `test_parse_review_unparsable`, T5 `test_unparsable_review_skips_the_fix_round`; 4 → T2 `test_routable_roles_need_a_summary`, T5 `test_hint_to_an_unroutable_role_is_ignored`; 5 → T2 `test_knowledge_empty_dir_and_no_overlap`.
