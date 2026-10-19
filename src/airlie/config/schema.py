"""Pydantic schema for airlie.yaml. Every seam of the system is selected here.

Rules: unknown keys are rejected (typos surface immediately); cross-references (role → model → engine) are
checked in `AirlieConfig.validate_references`; defaults mirror the wiki build spec (docs/wiki/build-spec)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


KNOWN_TOOLS = frozenset(
    {
        "read_file", "write_file", "edit_file", "list_dir", "grep", "shell", "run_tests", "git",
        "retrieve_value", "decide", "search_knowledge", "web_search", "web_fetch",
    }
)  # fmt: skip

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "shell", "git", "run_tests"})  # fmt: skip


class Sampling(Strict):
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 0.0
    repetition_penalty: float = Field(
        default=1.0, ge=1.0, description="mlx-lm style (>1.0 penalizes); guards against repetition collapse"
    )


class ModelSpec(Strict):
    """A model in the catalog. `repo` + `revision` pin exactly which weights are used."""

    repo: str
    revision: str = Field(min_length=1, description="HF commit sha; `task models:pull` fetches exactly this")
    path: Path | None = Field(default=None, description="local directory instead of the HF cache")
    file: str | None = Field(
        default=None, description="one file inside the repo (a .gguf) for llama_server engines"
    )
    preload: bool = True
    continuous_batching: bool | None = Field(
        default=None, description="per-model override of the engine default"
    )
    enable_mtp: bool = False
    estimated_memory_gb: float | None = Field(
        default=None, description="weights only; feeds the engine memory budget"
    )
    chat_template_kwargs: dict[str, object] = Field(default_factory=dict)
    tool_call_parser: str | None = None
    reasoning_parser: str | None = None
    kv_mb_per_1k_tokens: float = Field(
        default=256.0, description="KV cache cost; feeds the doctor memory projection"
    )
    bench: bool = Field(default=False, description="include in `task bench:models`")
    tags: list[str] = Field(default_factory=list)


class EngineSpec(Strict):
    """An inference endpoint. `vllm_mlx` is a process airlie manages; `openai_http` is any external server."""

    kind: Literal["vllm_mlx", "openai_http", "llama_server"]
    models: list[str] = Field(min_length=1, description="catalog names this engine serves")
    host: str = "127.0.0.1"
    port: int = 8080
    base_url: str | None = Field(default=None, description="required for openai_http; derived for vllm_mlx")
    api_key: str | None = None
    served_names: dict[str, str] = Field(
        default_factory=dict, description="catalog name → name the server expects"
    )
    # vllm-mlx serve flags (names mirror the CLI; see docs/wiki/build-spec/02 and vllm-mlx cli-inventory)
    memory_budget_gb: float = 90.0
    continuous_batching: bool = True
    max_num_seqs: int = 16
    max_kv_size: int = 65536
    prefill_step_size: int = 2048
    kv_cache_quantization: bool = False
    use_paged_cache: bool = False
    gpu_memory_utilization: float = 0.9
    max_request_tokens: int = 32768
    timeout_s: float = 600.0
    per_request_draft: bool = Field(
        default=False,
        description="engine accepts num_draft_tokens per request (HAL budgets apply); vllm-mlx 0.5.0: no",
    )
    extra_args: list[str] = Field(default_factory=list, description="appended verbatim to `vllm-mlx serve`")
    server_parsers: bool = Field(
        default=True,
        description="vllm_mlx: pass the team model's tool/reasoning parsers as server flags; "
        "False = airlie parses every model's output itself (design 2026-10-13 §4b)",
    )
    ctx_size: int = Field(default=16384, description="llama_server: -c")
    binary: str = Field(default="llama-server", description="llama_server: executable name or path")

    @model_validator(mode="after")
    def _derive_or_require_base_url(self) -> EngineSpec:
        if self.kind == "openai_http" and not self.base_url:
            raise ValueError("engines of kind 'openai_http' need a base_url (e.g. http://127.0.0.1:8080/v1)")
        if self.kind in ("vllm_mlx", "llama_server") and not self.base_url:
            self.base_url = f"http://{self.host}:{self.port}/v1"
        return self

    def served_name(self, model: str) -> str:
        return self.served_names.get(model, model)


class WriteScheduleSpec(Strict):
    """When a role that writes tests and code must have started each, as fractions of its steps: a test
    write (a path under tests/ or named like a test) by `tests`, a code write by `code`. At a deadline the
    role is told once to write; WRITE_ONLY_AFTER steps later with still nothing it keeps only its write
    tools until that write lands (AIR-28, 2026-10-18: the coder read 21 of 40 steps, then 34 of 40)."""

    tests: float = Field(
        default=0.25, gt=0, le=1, description="fraction of the steps by which a test is written"
    )
    code: float = Field(
        default=0.75, gt=0, le=1, description="fraction of the steps by which code is written"
    )

    @model_validator(mode="after")
    def _tests_first(self) -> WriteScheduleSpec:
        if self.tests > self.code:
            raise ValueError(f"write_schedule: tests ({self.tests}) must not come after code ({self.code})")
        return self


class RoleSpec(Strict):
    """A persona: prompt file + model + sampling. The engine is derived from the model."""

    prompt: Path
    model: str
    max_context: int = 65536
    max_tokens: int = Field(
        default=16384,
        description="cap on the whole reply, thinking included: keep it above thinking_budget when thinking",
    )
    sampling: Sampling = Field(default_factory=Sampling)
    thinking: bool = True
    thinking_budget: int | None = Field(
        default=8192, ge=1, description="cap on reasoning tokens (vllm-mlx refuses < 1); None = uncapped"
    )
    last_turn_thinking: bool = Field(
        default=True,
        description="think on the turn after the last step, the one that must answer without tools: False "
        "writes the answer from what the role has read (the architect's took 14-20 minutes thinking); a "
        "tool call on that turn gets one retry, which thinks either way",
    )
    nudge_stuck: bool = Field(
        default=False,
        description="the first time the role repeats the same action it is told to do something different "
        "instead of being stopped (a second run of repeats stops it); off where a repeat is the fork "
        "topology's signal that a team is stuck",
    )
    green_before_done: bool = Field(
        default=False,
        description="a role with run_tests that answers while its last run_tests run failed is told once "
        "that it is not done (the coder: AIR-5 layers ended on 'done' with their own tests red)",
    )
    on_context_full: str = Field(
        default="stop",
        description="what the team loop does when the role reaches its max_context: 'stop' ends the run "
        "with a reason a person can act on; 'trim' drops the oldest tool observations to make room",
    )
    write_schedule: WriteScheduleSpec | None = Field(
        default=None,
        description="deadlines for a test write and a code write, as fractions of the steps; None keeps the "
        "one write-now nudge two-fifths in",
    )
    stop: list[str] = Field(default_factory=list)
    chat_template_kwargs: dict[str, object] = Field(
        default_factory=dict, description="merged over the engine's enable_thinking kwarg per request"
    )
    summary: str | None = Field(default=None, description="one line the router sees; prompt frontmatter wins")
    tools: list[str] | None = Field(
        default=None, description="tool names this role gets (None = tools.builtin); [] = no tools"
    )
    skills: Path | None = Field(
        default=None, description="directory of skills/<name>.md with summary: frontmatter"
    )
    knowledge: Path | None = Field(default=None, description="directory of markdown for search_knowledge")
    vision: bool = Field(default=False, description="the role receives the run's images as content parts")
    max_steps: int | None = Field(
        default=None, ge=1, description="steps per subtask (None = teams.max_steps)"
    )


class SearchBackendSpec(Strict):
    kind: Literal["searxng"]
    url: str


class WebSpec(Strict):
    search: SearchBackendSpec = Field(
        default_factory=lambda: SearchBackendSpec(kind="searxng", url="http://127.0.0.1:8888")
    )
    fetch_timeout_s: float = 30.0


class ToolsSpec(Strict):
    builtin: list[str] = Field(
        default_factory=lambda: [
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
            "grep",
            "shell",
            "run_tests",
            "git",
        ]
    )
    shell_timeout_s: float = 120.0
    output_cap_chars: int = 20000
    write_file_max_overwrite_lines: int = Field(
        default=200, ge=1, description="write_file refuses to overwrite an existing file longer than this"
    )


class MCPServerSpec(Strict):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class ControllerSpec(Strict):
    kind: Literal["always", "never", "heuristic", "judge", "learned"] = "heuristic"
    judge_role: str = "judge"
    adapter_path: Path | None = Field(default=None, description="learned controller weights (M5)")
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    duplicate_jaccard: float = Field(
        default=0.8, ge=0.0, le=1.0, description="heuristic: near-duplicate key cutoff"
    )


class SummarizerSpec(Strict):
    kind: Literal["role", "truncate"] = Field(
        default="role", description="truncate = spec 04 §3 blueprint output[:n]"
    )
    role: str = "summarizer"
    max_words: int = 20
    truncate_chars: int = 100


class LTSSpec(Strict):
    enabled: bool = True
    controller: ControllerSpec = Field(default_factory=ControllerSpec)
    summarizer: SummarizerSpec = Field(default_factory=SummarizerSpec)
    max_keys: int = Field(default=12, description="keys injected into a team's prompt per step")
    max_value_chars: int = 20000
    background_admission: bool = Field(
        default=True, description="summarize + decide off the team's critical path (drained at run end)"
    )
    admission_concurrency: int = Field(default=2, ge=1, description="in-flight summarize+decide calls")
    ranking: Literal["relevance", "recency"] = Field(
        default="relevance", description="relevance = overlap with the team's last step; recency = blueprint"
    )
    inject_own: bool = Field(default=False, description="also show a team its own keys")
    inline_top: int = Field(
        default=2, ge=0, description="top-ranked keys whose value is inlined, once per team"
    )
    inline_chars: int = Field(default=1500, ge=0, description="chars of an inlined value")


class SchedulerSpec(Strict):
    kind: Literal["slots", "hal"] = "slots"
    max_active: int = 4
    starvation_ms: int = 30000


class TeamsSpec(Strict):
    k: int = Field(default=3, ge=1)
    role: str = "coder"
    orchestrator: Literal["autogen", "native"] = Field(
        default="native",
        description="native = built-in ReAct loop (default since 2026-10-13); autogen = spec 01 §2",
    )
    max_steps: int = 40
    token_budget: int = 200000
    stop_on_first_pass: bool = True
    fresh_continuation_tokens: int = Field(
        default=32768,
        ge=1,
        description="a fix round or follow-up on a conversation larger than this (estimated tokens) starts a "
        "new one: the brief, a note that the earlier work is in the worktree, and the findings",
    )


class AggregatorSpec(Strict):
    kind: Literal["llm", "test_score"] = Field(default="llm", description="spec 01 §4 step 9: LLM aggregator")
    role: str = "aggregator"
    judge_role: str = "judge"


class ObservabilitySpec(Strict):
    state_dir: Path = Path("~/.local/state/airlie")
    capture: bool = True
    retain_days: int = 14


class AuthSpec(Strict):
    """Who may use `task serve` as a person (docs/superpowers/plans/2026-10-17-sign-in.md)."""

    tailscale: bool = Field(
        default=False,
        description="trust the Tailscale-User-Login header `tailscale serve` adds (loopback requests only)",
    )
    rp_id: str | None = Field(default=None, description="WebAuthn relying party (default: the request host)")
    origins: list[str] = Field(
        default_factory=list, description="WebAuthn origins (default: https://<rp_id> and http localhost)"
    )
    session_days: int = Field(default=30, ge=1, description="how long a sign-in cookie lasts")
    invite_hours: int = Field(default=24, ge=1, description="how long an invite link may be used")


class MemorySpec(Strict):
    """Host memory expectations checked by `task doctor` (wiki build-spec 01 §1)."""

    wired_limit_mb: int = 106496
    os_reserve_gb: float = Field(default=12.0, description="OS + orchestrator reservation (spec 01 §1)")


class RouterSpec(Strict):
    kind: Literal["jev", "chief"] = Field(
        default="chief", description="jev = /v1/systemone on a llama_server engine"
    )
    model: str | None = Field(default=None, description="catalog name of the decision model (jev)")
    ambiguous_gap: float = Field(default=0.15, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ReviewSpec(Strict):
    enabled: bool = True
    role: str = "reviewer"
    roles: list[str] = Field(default_factory=lambda: ["coder", "security"])
    rounds: int = Field(
        default=1, ge=0, description="fix rounds after request_changes with a blocking finding"
    )
    diff_chars: int = Field(default=40000, ge=0)
    files_chars: int = Field(default=20000, ge=0)


class StudioSpec(Strict):
    chief_role: str = "chief"
    max_subtasks: int = Field(default=12, ge=1)
    max_rounds: int = Field(default=2, ge=1, description="chief plan rounds (delegate after synthesis)")
    parallel: int = Field(default=3, ge=1, description="independent subtasks in flight")
    result_chars: int = Field(default=6000, ge=0, description="chars of a dependency's result in a brief")
    review: ReviewSpec = Field(default_factory=ReviewSpec)
    done_check: bool = Field(
        default=True,
        description="a coder that stops 'done' on a brief with acceptance criteria is handed its diff and "
        "asked to tick each criterion, and fix a miss, before the reviewer sees it",
    )
    done_check_steps: int = Field(default=12, ge=1, description="steps the done-check may spend on a miss")


class JiraStatusesSpec(Strict):
    intake: str = Field(default="To Do", description="issues waiting to be built (story:tick takes the top)")
    building: str = "In Progress"
    review: str = "In Review"
    blocked: str | None = Field(default="Blocked", description="null: a stopped issue stays where it is")
    done: str = Field(default="Done", description="finished issues (board import-jira reads them as done)")


class JiraSpec(Strict):
    site: str | None = Field(default=None, description="https://NAME.atlassian.net; ATLASSIAN_BASE_URL wins")
    project: str | None = Field(default=None, description="project key, e.g. AIR; ATLASSIAN_PROJECT_KEY wins")
    email: SecretStr | None = Field(
        default=None, description="your Atlassian account email (a credential); ATLASSIAN_EMAIL wins"
    )
    token: SecretStr | None = Field(
        default=None, description="API token; ATLASSIAN_API_KEY wins (keep it in .env, not here)"
    )
    statuses: JiraStatusesSpec = Field(default_factory=JiraStatusesSpec)
    jql: str | None = Field(
        default=None,
        description='the issues story:tick takes, in order (default: project = "<project>" AND '
        'status = "<statuses.intake>" ORDER BY Rank ASC)',
    )
    api_url: str | None = Field(
        default=None,
        description="REST base (default <site>/rest/api/3); a scoped token needs "
        "https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3",
    )


class GitHubAppSpec(Strict):
    """story.github_app: publish as a GitHub App's bot (docs/integrations/story.md, Publishing as a bot)."""

    app_id: int | None = Field(
        default=None, ge=1, description="set: publish as the app's bot; null: with your gh login and SSH key"
    )
    key_file: Path = Field(
        default=Path("~/.config/airlie/github-app.pem"),
        description="the app's private key (PEM, keep it 0600); expanded at use, never logged",
    )
    api_url: str = Field(
        default="https://api.github.com",
        description="GitHub's REST API: https://, or http:// on this machine (tests); no user or password",
    )


class StorySpec(Strict):
    repo: Path | None = Field(
        default=None,
        description="the repository a written request (--text) is built in (--repo wins); a Jira issue's "
        "comes from its repo:<owner>/<name> label",
    )
    repos_root: Path = Field(
        default=Path("~/git"),
        description="where a Jira issue's repo:<owner>/<name> label finds its checkout: the directory named "
        "<name>, in any case",
    )
    test_cmd: str | None = Field(
        default=None,
        description="a test command airlie_run accepts (its default); a story's gate comes from its "
        "repository's AGENTS.md",
    )
    trunk: str = Field(default="main", description="the stack's base branch")
    remote: str = "origin"
    topology: str = Field(default="studio", description="the profile story runs load")
    max_layers: int = Field(default=5, ge=1)
    stage_attempts: int = Field(
        default=2, ge=1, description="tries for the story and the layer plan before the issue is blocked"
    )
    plan_round_steps: int = Field(
        default=5,
        ge=0,
        description="the architect plans in rounds of this many steps (a plan after each, the next round "
        "refines it) and stops when a round leaves the plan unchanged; 0 plans in one go, as before",
    )
    plan_max_rounds: int = Field(
        default=3,
        ge=1,
        description="the most rounds the architect plans in, whatever its role's step allowance would give",
    )
    survey: bool = Field(
        default=True,
        description="while product refines the request, the architect reads the code it touches and takes "
        "notes the layer planning starts from",
    )
    product_role: str = "product"
    architect_role: str = "architect"
    build_role: str = "coder"
    tester_role: str | None = Field(
        default=None,
        description="the role that writes every layer's tests before its coder builds it, as the coder's "
        "contract: checked to fail before any code is written, then read-only to the coder (airlie.yaml "
        "seats 'tester'); null: the coder writes its own tests, one subtask per layer",
    )
    jira: JiraSpec = Field(default_factory=JiraSpec)
    github_app: GitHubAppSpec = Field(default_factory=GitHubAppSpec)
    board: Literal["jira", "local"] = Field(
        default="local",
        description="where stories come from: airlie's own board (board:, the default) or the Jira project "
        "(story.jira)",
    )


class BoardSpec(Strict):
    """airlie's own board (initiative → epic → story → subtask) in observability.state_dir/board.sqlite."""

    prefix: str = Field(default="AIR", description="item keys are <prefix>-<n>, like Jira issue keys")

    @model_validator(mode="after")
    def _prefix_is_a_key_prefix(self) -> BoardSpec:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", self.prefix, re.ASCII):
            raise ValueError("board.prefix must be letters, digits or _ starting with a letter")
        return self


TOPOLOGY_KINDS: tuple[str, ...] = ("single", "teams", "fork", "studio")


class TopologySpec(Strict):
    kind: Literal["single", "teams", "fork", "studio"] = Field(
        default="teams",
        description="single = one agent (teams k=1, no LTS); teams = spec K teams + LTS; "
        "fork = fork-when-stuck; studio = chief + router + specialists (2026-10-13)",
    )
    profiles: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="per-kind partial configs deep-merged over this file when that kind is selected; "
        "`extends: <profile>` chains",
    )


class ForkSpec(Strict):
    width: int = Field(default=2, ge=1, description="children per fork point")
    after_failures: int = Field(default=2, ge=1, description="consecutive failing run_tests = stuck")
    child_steps: int = Field(default=15, ge=1, description="steps a child gets before resolve")
    max_rounds: int = Field(default=3, ge=0)
    max_total_steps: int = Field(default=120, ge=1, description="steps across every branch of a run")
    diversify: Literal["hypotheses", "temperature"] = "hypotheses"
    hypothesis_role: str = Field(default="judge", description="role that lists root-cause hypotheses")
    temperatures: list[float] = Field(default=[0.3, 0.7], min_length=1)


class RepoMapSpec(Strict):
    enabled: bool = Field(default=True, description="inject a deterministic repository map before the goal")
    max_chars: int = Field(default=6000, ge=0)
    depth: int = Field(
        default=3, ge=1, description="directory depth shown; deeper paths collapse to 'dir/ …'"
    )


class AirlieConfig(Strict):
    version: Literal[1] = 1
    engines: dict[str, EngineSpec]
    models: dict[str, ModelSpec]
    roles: dict[str, RoleSpec]
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    mcp: dict[str, MCPServerSpec] = Field(default_factory=dict)
    lts: LTSSpec = Field(default_factory=LTSSpec)
    scheduler: SchedulerSpec = Field(default_factory=SchedulerSpec)
    teams: TeamsSpec = Field(default_factory=TeamsSpec)
    aggregator: AggregatorSpec = Field(default_factory=AggregatorSpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    repo_map: RepoMapSpec = Field(default_factory=RepoMapSpec)
    topology: TopologySpec = Field(default_factory=TopologySpec)
    fork: ForkSpec = Field(default_factory=ForkSpec)
    router: RouterSpec = Field(default_factory=RouterSpec)
    studio: StudioSpec = Field(default_factory=StudioSpec)
    story: StorySpec = Field(default_factory=StorySpec)
    board: BoardSpec = Field(default_factory=BoardSpec)
    web: WebSpec = Field(default_factory=WebSpec)
    source_path: Path | None = Field(default=None, exclude=True, description="set by load_config")

    # ---- cross-reference checks (run by load_config after path resolution) ----
    def validate_references(self) -> list[str]:
        """Return human-readable problems; empty means consistent."""
        problems: list[str] = []
        served_by: dict[str, list[str]] = {m: [] for m in self.models}
        for ename, e in self.engines.items():
            for m in e.models:
                if m not in self.models:
                    problems.append(
                        f"engines.{ename}.models names '{m}', which is not in models ({sorted(self.models)})"
                    )
                else:
                    served_by[m].append(ename)
        ports: dict[int, list[str]] = {}
        for ename, e in self.engines.items():
            if e.kind in ("vllm_mlx", "llama_server"):
                ports.setdefault(e.port, []).append(ename)
        for port, names in ports.items():
            if len(names) > 1:
                problems.append(f"port {port} is used by more than one managed engine: {', '.join(names)}")
        for ename, e in self.engines.items():
            if e.kind != "llama_server":
                continue
            if len(e.models) != 1:
                problems.append(f"engines.{ename}.models: a llama_server engine serves exactly one model")
            for m in e.models:
                if m in self.models and not self.models[m].file:
                    problems.append(f"models.{m}.file is required (served by llama_server engine '{ename}')")
        if self.router.kind == "jev":
            rm = self.router.model
            if rm is None or rm not in self.models:
                problems.append(
                    f"router.model must name a catalog model when router.kind is jev (got {rm!r})"
                )
            else:
                hosts = [n for n, e in self.engines.items() if rm in e.models and e.kind == "llama_server"]
                if not hosts:
                    problems.append(f"router.model '{rm}' must be served by a llama_server engine")
        for rname, r in self.roles.items():
            if r.on_context_full not in ("stop", "trim"):
                problems.append(f"roles.{rname}.on_context_full must be one of: stop, trim")
            if r.thinking and r.thinking_budget is not None and r.max_tokens <= r.thinking_budget:
                problems.append(
                    f"roles.{rname}.max_tokens ({r.max_tokens}) must be larger than "
                    f"roles.{rname}.thinking_budget ({r.thinking_budget}): max_tokens caps the thinking and "
                    "the answer together, so the model can think its way to the cap and never answer. "
                    "Raise max_tokens, lower thinking_budget, or set thinking_budget: null (no cap)"
                )
            if r.model not in self.models:
                problems.append(
                    f"roles.{rname}.model names '{r.model}', which is not in models ({sorted(self.models)})"
                )
                continue
            engines = served_by[r.model]
            if not engines:
                problems.append(
                    f"models.{r.model} (used by roles.{rname}) is served by no engine; "
                    "add it to an engine's models list"
                )
            elif len(engines) > 1:
                problems.append(
                    f"models.{r.model} is served by more than one engine ({', '.join(engines)}); pick one"
                )
            if not r.prompt.is_file():
                problems.append(f"roles.{rname}.prompt file not found: {r.prompt}")
            for t in r.tools or []:
                if t not in KNOWN_TOOLS:
                    problems.append(
                        f"roles.{rname}.tools names '{t}', which is not a tool ({sorted(KNOWN_TOOLS)})"
                    )
            effective = set(r.tools if r.tools is not None else self.tools.builtin)
            if effective & {"web_search", "web_fetch"} and effective & MUTATING_TOOLS:
                problems.append(
                    f"role '{rname}': web tools cannot be combined with mutating tools "
                    f"({', '.join(sorted(effective & MUTATING_TOOLS))})"
                )
            for label, d in (("skills", r.skills), ("knowledge", r.knowledge)):
                if d is not None and not d.is_dir():
                    problems.append(f"roles.{rname}.{label}: {d} is not a directory")
        for label, role in (
            ("teams.role", self.teams.role),
            ("lts.summarizer.role", self.lts.summarizer.role),
            ("lts.controller.judge_role", self.lts.controller.judge_role),
            ("aggregator.role", self.aggregator.role),
            ("aggregator.judge_role", self.aggregator.judge_role),
            ("fork.hypothesis_role", self.fork.hypothesis_role),
        ):
            if role not in self.roles:
                problems.append(f"{label} names '{role}', which is not in roles ({sorted(self.roles)})")
        if self.topology.kind == "studio":
            need = [("studio.chief_role", self.studio.chief_role)]
            if self.studio.review.enabled:
                need.append(("studio.review.role", self.studio.review.role))
                need += [("studio.review.roles", x) for x in self.studio.review.roles]
            for label, role in need:
                if role not in self.roles:
                    problems.append(f"{label} names '{role}', which is not in roles ({sorted(self.roles)})")
        return problems

    def engine_for_model(self, model: str) -> str:
        for ename, e in self.engines.items():
            if model in e.models:
                return ename
        raise KeyError(model)

    def engine_for_role(self, role: str) -> str:
        return self.engine_for_model(self.roles[role].model)
