# Studio Foundations Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put in place everything the studio topology stands on, each piece usable on its own: client-side output parsing with vllm-mlx's parser classes (incl. Muse Glimmer), topology profiles, a managed llama-server engine serving Kev for System One decisions, the router + its policy + routing eval, the `decide` tool, and image content parts.

**Architecture:** Nothing here changes the agent loop. Config gains a `llama_server` engine kind, `router`, `topology.profiles` and a `single` topology kind; the engine seam gains a post-parse step (`parse_output`) and content parts; a new `router` package wraps `/v1/systemone`; doctor, `task up`, `models:pull` and the CLI learn about the second engine and profiles. Plan 2 (the studio loop, review, skills/knowledge, prompts, bench) is written after this plan lands, against the real interfaces.

**Tech Stack:** Python 3.12, uv, pydantic v2, httpx (+ `httpx.MockTransport` in tests), typer, pytest (asyncio auto), vllm-mlx 0.5.0 (`vllm_mlx.reasoning`, `vllm_mlx.tool_parsers`), llama-server 0.6.0 (`/v1/systemone`), Taskfile.

**Spec:** `docs/superpowers/specs/2026-10-13-studio-topology-design.md` (§3 models, §4a reviewer format, §4b parsers, §6 router, §8 profiles, §9 config, §10 code shape, §11 tests, §14 `decide` tool). Jev work: "add jev" design referenced there.

## Global Constraints

- Python 3.12 via `uv` only; every user-facing action is a `task` (Taskfile). No `pip`, no ad-hoc scripts for users.
- `vllm-mlx==0.5.0` and `mlx-lm==0.32.0` stay pinned (its parser API is internal; the pin is the contract).
- Models are `mlx-community` only; the one exception is `ggml-org/Kev-4B-GGUF` (Apache 2.0) for the router. Pins: `mlx-community/Qwen3.8-27B-4bit@10c35caafbb80f7dc6a7a432cdd11af10a6d4818`, `mlx-community/Muse-Glimmer-30B-4bit@3e7677d7a40d348a3daba263a2b1c0aa41910710`, `ggml-org/Kev-4B-GGUF@d924f2e2c3872da8b8aaf3eb4453b4126deceb79` file `Kev-4B-Q4_K_M.gguf`.
- `airlie.yaml` rejects unknown keys; every new key has a default; cross-references are checked in `validate_references` with path-qualified messages.
- `task check` (ruff, mypy strict, pytest) green after every task; ruff line length 110.
- Every long-running thing is cancellable; every tool call is timeboxed; no client-specific hooks.
- Downloads (Kev 3 GB, Muse 19.4 GB) are a user action: the executor asks Noah before `task models:pull` of either.

## Review Focus

1. Muse output with a tool channel and no `to=user` block: `parse_output` must return `text == ""` and the tool call, never raise. (Task 5, `test_muse_tool_channel_without_user_block`.)
2. A response the server already parsed (non-empty `tool_calls`) must not be parsed again on the client, or the same call would be duplicated or the arguments mangled. (Task 5, `test_server_parsed_calls_win`.)
3. Kev answering a choice outside the closed set: the router must fall back to the hint (or the top in-set probability), mark `decision: "invalid"`, log it, and never raise into the loop. (Task 7, `test_choice_outside_closed_set_falls_back`.)
4. `llama-server` on PATH but its `libllama-server-impl.dylib` lacks `/v1/systemone`: doctor fails that row with the brew hint instead of letting `task up` succeed and the first routing call 404. (Task 3, `test_llama_server_without_systemone_fails`.)
5. A profile whose `extends` chain loops or names a missing profile: `ConfigError` naming `topology.profiles.<name>.extends`, not a recursion error. (Task 1, `test_profile_extends_cycle_is_a_config_error`.)

## Rulings made while planning (the spec is the authority; these resolve its silences)

- **Per-profile engine settings win.** Spec §8 says the engine runs at the largest `max_kv_size` any profile needs. A profile may instead set `engines.main.max_kv_size` (and `server_parsers`) itself; when the active profile's engine settings differ from the running process, `task status` says "restart required". Reason: a 262k KV cap next to 55 GB of studio weights does not fit the doctor projection, and the projection is what keeps the wired limit honest.
- **`single` is a topology kind.** The bench already names it; `topology.kind: single` = the teams orchestrator with `k` forced to 1 and LTS off, so profiles can key on it.
- **Muse parsers are written here, not copied.** PR #812 is unmerged and may change; §4b's regexes are reimplemented against vllm-mlx's base classes (Apache 2.0 either way) with PR #812 credited in the module docstring, and deleted when a release ships them.

---

### Task 1: Config schema — `llama_server` engines, `router`, profiles, `single` kind

**Files:**
- Modify: `src/airlie/config/schema.py`
- Modify: `src/airlie/config/load.py`
- Modify: `src/airlie/config/__init__.py` (export `TOPOLOGY_KINDS`, `RouterSpec`, `profile_configs`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ModelSpec.file: str | None`; `EngineSpec.kind: Literal["vllm_mlx", "openai_http", "llama_server"]`, `EngineSpec.server_parsers: bool`, `EngineSpec.ctx_size: int`, `EngineSpec.binary: str`; `RoleSpec.chat_template_kwargs: dict[str, object]`, `RoleSpec.summary: str | None`; `RouterSpec(kind, model, ambiguous_gap, min_confidence)`; `TopologySpec.kind: Literal["single", "teams", "fork"]`, `TopologySpec.profiles: dict[str, dict[str, Any]]`; `TOPOLOGY_KINDS: tuple[str, ...]`; `AirlieConfig.router`, `AirlieConfig.source_path: Path | None`; `load_config(path=None, local_path=None, *, topology: str | None = None)`; `apply_profile(data: dict, kind: str) -> dict`; `profile_configs(cfg) -> dict[str, AirlieConfig]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_profile_for_the_selected_kind_is_deep_merged(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "teams",
            "profiles": {
                "single": {"teams": {"k": 1}, "lts": {"enabled": False}},
                "teams": {"teams": {"k": 3}},
            },
        },
    }
    cfg = load_config(write(tmp_path, data), topology="single")
    assert cfg.topology.kind == "single"
    assert cfg.teams.k == 1 and cfg.lts.enabled is False
    assert cfg.source_path == (tmp_path / "airlie.yaml").resolve()
    base = load_config(tmp_path / "airlie.yaml")  # no override → the file's own kind
    assert base.topology.kind == "teams" and base.teams.k == 3


def test_profile_extends_chain_merges_parent_first(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "fork",
            "profiles": {
                "teams": {"teams": {"k": 3, "max_steps": 20}},
                "fork": {"extends": "teams", "teams": {"k": 1}},
            },
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.teams.k == 1 and cfg.teams.max_steps == 20


def test_profile_extends_cycle_is_a_config_error(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "fork",
            "profiles": {"teams": {"extends": "fork"}, "fork": {"extends": "teams"}},
        },
    }
    with pytest.raises(ConfigError, match=r"topology\.profiles\.fork\.extends.*cycle"):
        load_config(write(tmp_path, data))


def test_profile_extends_unknown_parent_is_a_config_error(tmp_path: Path) -> None:
    data = {**MINIMAL, "topology": {"kind": "fork", "profiles": {"fork": {"extends": "nope"}}}}
    with pytest.raises(ConfigError, match=r"topology\.profiles\.fork\.extends.*'nope'"):
        load_config(write(tmp_path, data))


def test_unknown_topology_override_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"topology.*'studio'.*single, teams, fork"):
        load_config(write(tmp_path, MINIMAL), topology="studio")


def test_jev_router_needs_a_llama_server_engine_serving_its_model(tmp_path: Path) -> None:
    data = {**MINIMAL, "router": {"kind": "jev", "model": "team"}}
    with pytest.raises(ConfigError, match=r"router\.model.*'team'.*llama_server"):
        load_config(write(tmp_path, data))


def test_llama_server_engine_serves_one_gguf_model(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "engines": {
            **MINIMAL["engines"],
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {**MINIMAL["models"], "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9"}},
        "router": {"kind": "jev", "model": "kev"},
    }
    with pytest.raises(ConfigError, match=r"models\.kev\.file.*llama_server"):
        load_config(write(tmp_path, data))
    data["models"]["kev"]["file"] = "Kev-4B-Q4_K_M.gguf"
    cfg = load_config(write(tmp_path, data))
    assert cfg.engines["decision"].base_url == "http://127.0.0.1:8081/v1"
    assert cfg.engines["decision"].server_parsers is True and cfg.engines["decision"].ctx_size == 16384
    assert cfg.router.kind == "jev" and cfg.router.ambiguous_gap == 0.15


def test_profile_configs_loads_every_profile_from_the_source_file(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {"kind": "teams", "profiles": {"single": {"teams": {"k": 1}}, "teams": {}}},
    }
    cfg = load_config(write(tmp_path, data))
    profiles = profile_configs(cfg)
    assert set(profiles) == {"single", "teams"}
    assert profiles["single"].teams.k == 1 and profiles["teams"].teams.k == 3
```

Add `profile_configs` to the import line at the top: `from airlie.config import AirlieConfig, ConfigError, load_config, profile_configs`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q 2>&1 | tail -5`
Expected: ImportError on `profile_configs` (collection fails).

- [ ] **Step 3: Extend the schema**

In `src/airlie/config/schema.py`:

```python
# ModelSpec: add after `path`
    file: str | None = Field(
        default=None, description="one file inside the repo (a .gguf) for llama_server engines"
    )

# EngineSpec: replace the kind line and add fields after `extra_args`
    kind: Literal["vllm_mlx", "openai_http", "llama_server"]
    ...
    server_parsers: bool = Field(
        default=True,
        description="vllm_mlx: pass the team model's tool/reasoning parsers as server flags; "
        "False = airlie parses every model's output itself (design 2026-10-13 §4b)",
    )
    ctx_size: int = Field(default=16384, description="llama_server: -c")
    binary: str = Field(default="llama-server", description="llama_server: executable name or path")

# EngineSpec._derive_or_require_base_url: treat llama_server like vllm_mlx
        if self.kind in ("vllm_mlx", "llama_server") and not self.base_url:
            self.base_url = f"http://{self.host}:{self.port}/v1"

# RoleSpec: add
    chat_template_kwargs: dict[str, object] = Field(
        default_factory=dict, description="merged over the engine's enable_thinking kwarg per request"
    )
    summary: str | None = Field(default=None, description="one line the router sees; prompt frontmatter wins")

# new, before TopologySpec
class RouterSpec(Strict):
    kind: Literal["jev", "chief"] = Field(default="chief", description="jev = /v1/systemone on a llama_server engine")
    model: str | None = Field(default=None, description="catalog name of the decision model (jev)")
    ambiguous_gap: float = Field(default=0.15, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


TOPOLOGY_KINDS: tuple[str, ...] = ("single", "teams", "fork")


class TopologySpec(Strict):
    kind: Literal["single", "teams", "fork"] = Field(
        default="teams",
        description="single = one agent (teams k=1, no LTS); teams = spec K teams + LTS; fork = fork-when-stuck",
    )
    profiles: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="per-kind partial configs deep-merged over this file when that kind is selected; "
        "`extends: <profile>` chains",
    )

# AirlieConfig: add fields
    router: RouterSpec = Field(default_factory=RouterSpec)
    source_path: Path | None = Field(default=None, exclude=True, description="set by load_config")
```

Add `from typing import Any, Literal`. In `validate_references`, after the ports loop:

```python
        for ename, e in self.engines.items():
            if e.kind != "llama_server":
                continue
            if len(e.models) != 1:
                problems.append(f"engines.{ename}.models: a llama_server engine serves exactly one model")
            for m in e.models:
                if m in self.models and not self.models[m].file:
                    problems.append(f"models.{m}.file is required (served by llama_server engine '{ename}')")
        if self.router.kind == "jev":
            m = self.router.model
            if m is None or m not in self.models:
                problems.append(f"router.model must name a catalog model when router.kind is jev (got {m!r})")
            else:
                hosts = [n for n, e in self.engines.items() if m in e.models and e.kind == "llama_server"]
                if not hosts:
                    problems.append(f"router.model '{m}' must be served by a llama_server engine")
```

- [ ] **Step 4: Profiles in `load.py`**

Replace `load_config` and add helpers in `src/airlie/config/load.py`:

```python
from airlie.config.schema import TOPOLOGY_KINDS, AirlieConfig


def resolve_profile(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    """The profile `name` with its `extends` chain merged parent-first; `extends` itself is dropped."""
    chain: list[str] = []
    cur: str | None = name
    while cur is not None:
        if cur in chain:
            raise ConfigError(f"topology.profiles.{chain[-1]}.extends: cycle {' -> '.join([*chain, cur])}")
        if cur not in profiles:
            where = f"topology.profiles.{chain[-1]}.extends" if chain else "topology.profiles"
            raise ConfigError(f"{where} names '{cur}', which is not a profile ({sorted(profiles)})")
        chain.append(cur)
        parent = profiles[cur].get("extends")
        cur = str(parent) if parent is not None else None
    merged: dict[str, Any] = {}
    for p in reversed(chain):
        body = {k: v for k, v in profiles[p].items() if k != "extends"}
        merged = deep_merge(merged, body)
    return merged


def apply_profile(data: dict[str, Any], kind: str | None) -> dict[str, Any]:
    """Select `kind` (or the file's own topology.kind) and merge its profile, if one is declared."""
    topo = data.get("topology") if isinstance(data.get("topology"), dict) else {}
    selected = kind or str(topo.get("kind", "teams"))
    if selected not in TOPOLOGY_KINDS:
        raise ConfigError(f"topology kind '{selected}' is unknown; use one of {', '.join(TOPOLOGY_KINDS)}")
    profiles = topo.get("profiles") if isinstance(topo.get("profiles"), dict) else {}
    out = dict(data)
    if selected in profiles:
        out = deep_merge(out, resolve_profile(profiles, selected))
    out["topology"] = {**(out.get("topology") or {}), "kind": selected}
    return out


def load_config(
    path: Path | None = None, local_path: Path | None = None, *, topology: str | None = None
) -> AirlieConfig:
    """Parse + validate. `local_path` defaults to `airlie.local.yaml` beside `path`; `topology` picks the
    profile (default: the file's own topology.kind)."""
    path = (path or find_config()).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    data = _read_yaml(path)
    local = local_path if local_path is not None else path.with_name(DEFAULT_LOCAL)
    if local.is_file():
        data = deep_merge(data, _read_yaml(local))
    data = apply_profile(data, topology)
    try:
        cfg = AirlieConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e)) from e
    cfg.source_path = path
    base = path.parent
    ...  # unchanged path resolution and validate_references
    return cfg


def profile_configs(cfg: AirlieConfig) -> dict[str, AirlieConfig]:
    """Every declared profile, loaded from the same source file (empty when the config has no file)."""
    if cfg.source_path is None:
        return {}
    return {name: load_config(cfg.source_path, topology=name) for name in cfg.topology.profiles}
```

Export `profile_configs`, `apply_profile`, `RouterSpec`, `TOPOLOGY_KINDS` from `src/airlie/config/__init__.py`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_config.py -q 2>&1 | tail -3`
Expected: all pass (the old tests unchanged; `source_path` exclusion keeps `config show` output clean).

- [ ] **Step 6: Full check and commit**

Run: `task check 2>&1 | tail -5`
Expected: green.

```bash
git add src/airlie/config tests/unit/test_config.py
git commit -m "feat(config): llama_server engine kind, router spec, topology profiles with extends, single kind"
```

---

### Task 2: `single` topology kind and profile selection in the CLI, serve and bench

**Files:**
- Modify: `src/airlie/topology/protocol.py`
- Modify: `src/airlie/cli.py` (`_load`, `_set_topology`, `run`, `eval_run`, `TopologyOpt`, `bench_topology`)
- Modify: `src/airlie/serve/app.py` (`_default_factory`)
- Modify: `src/airlie/bench/topology_bench.py` (`apply_topology`)
- Test: `tests/unit/test_cli_run.py`, `tests/unit/test_serve.py`, `tests/unit/test_topology_bench.py`

**Interfaces:**
- Consumes: `load_config(..., topology=)`, `TOPOLOGY_KINDS`, `cfg.source_path` (Task 1).
- Produces: `build_topology` handles `single`; `cli._load(config, topology=None)`; `cli.select_topology(cfg, topology) -> AirlieConfig` (reloads with the profile when `source_path` is set, else sets the kind in place).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_cli_run.py` add:

```python
def test_run_with_topology_single_selects_the_profile(tmp_path: Path, monkeypatch) -> None:
    seen: list = []

    def fake_orch(cfg, run_id=None):
        seen.append(cfg)
        return StubOrchestrator(cfg, None, run_id)

    monkeypatch.setattr(cli, "_orchestrator", fake_orch)
    p = write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["topology"] = {"kind": "teams", "profiles": {"single": {"teams": {"max_steps": 7}}}}
    p.write_text(yaml.safe_dump(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(cli.app, ["run", "go", "--repo", str(repo), "--topology", "single", "-c", str(p)])
    assert r.exit_code == 0, r.output
    assert seen[0].topology.kind == "single" and seen[0].teams.k == 1 and seen[0].teams.max_steps == 7


def test_run_with_unknown_topology_exits_2(tmp_path: Path) -> None:
    p = write_cfg(tmp_path)
    r = runner.invoke(cli.app, ["run", "go", "--repo", str(tmp_path), "--topology", "studio", "-c", str(p)])
    assert r.exit_code == 2 and "single, teams, fork" in r.output
```

In `tests/unit/test_serve.py` add (next to the existing unknown-topology test):

```python
def test_run_endpoint_accepts_single(tmp_path: Path) -> None:
    seen: list = []

    def factory(cfg, k, topology=None):
        seen.append((cfg.topology.kind, cfg.teams.k, topology))
        return StubOrch(cfg)

    app = create_app(cfg(tmp_path), orchestrator_factory=factory)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with TestClient(app) as c:
        r = c.post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "topology": "single"})
    assert r.status_code == 200, r.text
```

(Use the file's existing stub orchestrator class name in place of `StubOrch`.) In `tests/unit/test_topology_bench.py` add:

```python
def test_apply_topology_single_sets_the_single_kind() -> None:
    from airlie.bench.topology_bench import apply_topology

    c = apply_topology(load_config(CFG_PATH), "single")  # the file's existing config helper
    assert c.topology.kind == "single" and c.teams.k == 1 and c.lts.enabled is False
```

(Adapt `load_config(CFG_PATH)` to how the file already builds a config.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_cli_run.py tests/unit/test_serve.py tests/unit/test_topology_bench.py -q 2>&1 | tail -5`
Expected: FAIL — `--topology single` exits 2 ("use teams or fork"); serve returns 400; bench sets kind "teams".

- [ ] **Step 3: Implement**

`src/airlie/topology/protocol.py`:

```python
def build_topology(cfg: AirlieConfig, engine: Engine, run_id: str | None = None) -> Topology:
    if cfg.topology.kind == "fork":
        from airlie.topology.fork import ForkOrchestrator

        return ForkOrchestrator(cfg, engine, run_id=run_id)
    if cfg.topology.kind == "single":
        cfg.teams.k = 1
        cfg.lts.enabled = False
    from airlie.orchestrator import ParallelAgentTeamOrchestrator

    return ParallelAgentTeamOrchestrator(cfg, engine, run_id=run_id)
```

`src/airlie/cli.py`:

```python
from airlie.config import TOPOLOGY_KINDS, AirlieConfig, ConfigError, load_config


def _load(config: Path | None, topology: str | None = None) -> AirlieConfig:
    try:
        return load_config(config, topology=topology)
    except ConfigError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2 if topology and "topology kind" in str(e) else 1) from None


TopologyOpt = Annotated[
    str | None, typer.Option(help=f"{' | '.join(TOPOLOGY_KINDS)} (default: topology.kind; selects its profile)")
]


def select_topology(cfg: AirlieConfig, topology: str | None) -> AirlieConfig:
    """The config with `topology`'s profile applied: reloaded from its file when it has one."""
    if topology is None:
        return cfg
    if topology not in TOPOLOGY_KINDS:
        err.print(f"[red]unknown topology '{topology}'; use one of {', '.join(TOPOLOGY_KINDS)}[/red]")
        raise typer.Exit(2)
    if cfg.source_path is not None:
        return load_config(cfg.source_path, topology=topology)
    cfg.topology.kind = topology  # type: ignore[assignment]
    return cfg
```

In `run` and `eval_run`: replace `cfg = _load(config)` + `_set_topology(cfg, topology)` with `cfg = _load(config, topology)`. Delete `_set_topology`; `bench_topology` and `serve/app.py` import `select_topology` instead:

```python
# serve/app.py
def _default_factory(cfg: AirlieConfig, k: int | None, topology: str | None = None) -> Any:
    from airlie.cli import _orchestrator, select_topology
    from airlie.config import TOPOLOGY_KINDS

    if topology is not None and topology not in TOPOLOGY_KINDS:
        raise HTTPException(400, f"unknown topology '{topology}'; use one of {', '.join(TOPOLOGY_KINDS)}")
    cfg = select_topology(cfg, topology)
    if k is not None:
        cfg.teams.k = k
    return _orchestrator(cfg)
```

`bench/topology_bench.py` `apply_topology`: the `single` branch sets `c.topology.kind = "single"` (and keeps `k = 1`, `lts.enabled = False`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit -q 2>&1 | tail -3`
Expected: all pass, including the existing unknown-topology tests (message now lists three kinds; update their `match` strings if they pin "teams or fork").

- [ ] **Step 5: Commit**

```bash
task check 2>&1 | tail -3
git add -A src tests
git commit -m "feat(topology): single is a topology kind; --topology selects the profile in run, eval, serve and bench"
```

---

### Task 3: Doctor — per-profile projection, llama-server and `/v1/systemone`, GGUF files

**Files:**
- Modify: `src/airlie/doctor.py`
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: `profile_configs`, `EngineSpec.kind == "llama_server"`, `EngineSpec.binary`, `ModelSpec.file` (Task 1).
- Produces: `SystemProbe.resolve_binary(name) -> Path | None`, `SystemProbe.file_contains(path, needle) -> bool | None`, `SystemProbe.model_file(repo, revision, file) -> Path | None`; `systemone_impl_path(binary: Path) -> Path` (the `lib/libllama-server-impl.dylib` beside the binary's prefix, as ai-mode's doctor checks); rows `engine:<name>:memory_projection[<profile>]`, `engine:<name>:binary`, `engine:<name>:systemone`, `model:<name>:file`.

- [ ] **Step 1: Write the failing tests**

Extend `FakeProbe` in `tests/unit/test_doctor.py` with three scripted methods and add tests:

```python
    # in FakeProbe.__init__ add: binaries: dict[str, str] | None = None, dylib_has_systemone: bool = True,
    #                            files: set[str] | None = None
        self._bins = binaries or {}
        self._systemone = dylib_has_systemone
        self._files = files or set()

    def resolve_binary(self, name: str) -> Path | None:
        return Path(self._bins[name]) if name in self._bins else None

    def file_contains(self, path: Path, needle: str) -> bool | None:
        return self._systemone if needle == "/v1/systemone" else None

    def model_file(self, repo: str, revision: str, file: str) -> Path | None:
        return Path(f"/fake/{repo}/{revision}/{file}") if f"{repo}@{revision}/{file}" in self._files else None


def decision_cfg(tmp_path: Path) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r1", "estimated_memory_gb": 16},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf", "preload": True},
        },
        "roles": {r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")},
        "router": {"kind": "jev", "model": "kev"},
        "topology": {"kind": "teams", "profiles": {"single": {"teams": {"k": 1}}, "teams": {"teams": {"k": 3}}}},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_llama_server_engine_checks_binary_systemone_and_gguf(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(
        snapshots={"org/t@r1"},
        binaries={"llama-server": "/opt/homebrew/bin/llama-server"},
        files={"ggml-org/Kev-4B-GGUF@d9/Kev-4B-Q4_K_M.gguf"},
    )
    rows = {r.name: r for r in run_checks(cfg, probe)}
    assert rows["engine:decision:binary"].status == "ok"
    assert rows["engine:decision:systemone"].status == "ok"
    assert rows["model:kev:file"].status == "ok"


def test_llama_server_without_systemone_fails(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(
        snapshots={"org/t@r1"},
        binaries={"llama-server": "/opt/homebrew/bin/llama-server"},
        dylib_has_systemone=False,
        files={"ggml-org/Kev-4B-GGUF@d9/Kev-4B-Q4_K_M.gguf"},
    )
    rows = {r.name: r for r in run_checks(cfg, probe)}
    assert rows["engine:decision:systemone"].status == "fail"
    assert "brew" in rows["engine:decision:systemone"].detail


def test_missing_gguf_file_fails_with_pull_hint(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(snapshots={"org/t@r1"}, binaries={"llama-server": "/x/llama-server"})
    rows = {r.name: r for r in run_checks(cfg, probe)}
    assert rows["model:kev:file"].status == "fail" and "task models:pull -- kev" in rows["model:kev:file"].detail


def test_memory_projection_is_reported_per_profile(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    rows = {r.name: r for r in run_checks(cfg, FakeProbe(snapshots={"org/t@r1"}))}
    assert "engine:main:memory_projection[single]" in rows and "engine:main:memory_projection[teams]" in rows
    assert "1 teams" in rows["engine:main:memory_projection[single]"].detail
    assert "3 teams" in rows["engine:main:memory_projection[teams]"].detail
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_doctor.py -q 2>&1 | tail -5`
Expected: FAIL — KeyError on the new row names (and `FakeProbe` does not satisfy the extended protocol until implemented).

- [ ] **Step 3: Implement**

In `src/airlie/doctor.py`:

```python
class SystemProbe(Protocol):
    ...  # existing methods
    def resolve_binary(self, name: str) -> Path | None: ...
    def file_contains(self, path: Path, needle: str) -> bool | None: ...
    def model_file(self, repo: str, revision: str, file: str) -> Path | None: ...


def systemone_impl_path(binary: Path) -> Path:
    """llama-server's HTTP routes live in lib/libllama-server-impl.dylib beside the binary's prefix
    (ai-mode's doctor checked the same file); the binary itself is a thin launcher."""
    real = binary.resolve()
    return real.parent.parent / "lib" / "libllama-server-impl.dylib"


# RealProbe additions
    def resolve_binary(self, name: str) -> Path | None:
        exe = shutil.which(name)
        return Path(exe) if exe else None

    def file_contains(self, path: Path, needle: str) -> bool | None:
        try:
            return needle.encode() in path.read_bytes()
        except OSError:
            return None

    def model_file(self, repo: str, revision: str, file: str) -> Path | None:
        d = self.hf_cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision / file
        return d if d.is_file() and d.resolve().is_file() and d.resolve().stat().st_size > 0 else None
```

In `run_checks`, inside the engines loop, before the `if e.kind != "vllm_mlx": continue` add a `llama_server` branch:

```python
        if e.kind == "llama_server":
            managed_models.update(e.models)
            exe = probe.resolve_binary(e.binary)
            results.append(
                CheckResult(
                    f"engine:{ename}:binary",
                    "ok" if exe else "fail",
                    str(exe) if exe else f"'{e.binary}' not on PATH; run: brew install llama.cpp",
                )
            )
            if exe:
                impl = systemone_impl_path(exe)
                has = probe.file_contains(impl, "/v1/systemone")
                if has:
                    results.append(CheckResult(f"engine:{ename}:systemone", "ok", f"{impl.name} has /v1/systemone"))
                else:
                    results.append(
                        CheckResult(
                            f"engine:{ename}:systemone",
                            "fail",
                            f"{impl} lacks /v1/systemone (Kev needs it); run: brew install --HEAD llama.cpp",
                        )
                    )
            health_url = f"{(e.base_url or '').removesuffix('/v1')}/health"
            results.append(
                CheckResult(f"engine:{ename}:health", "ok", f"{health_url} answers")
                if probe.http_ok(health_url)
                else CheckResult(f"engine:{ename}:health", "warn", f"not running ({health_url}); run: task up")
            )
            continue
```

Replace the single projection row with one per profile (plus the base when no profiles):

```python
        from airlie.config import profile_configs

        variants = profile_configs(cfg) or {cfg.topology.kind: cfg}
        limit_gb = cfg.memory.wired_limit_mb / 1024.0
        for pname, pcfg in variants.items():
            if ename not in pcfg.engines:
                continue
            pe = pcfg.engines[ename]
            projected, unknown = project_memory_gb(pcfg, ename)
            detail = (
                f"projected {projected:.0f} GB (preloaded weights + {pcfg.teams.k} teams × {pe.max_kv_size} KV"
                f" + {pcfg.memory.os_reserve_gb:.0f} GB reserve) vs wired limit {limit_gb:.0f} GB"
            )
            if unknown:
                detail += f"; no estimated_memory_gb for {', '.join(unknown)}"
            status: Status = "fail" if projected > limit_gb else ("warn" if unknown else "ok")
            results.append(CheckResult(f"engine:{ename}:memory_projection[{pname}]", status, detail))
```

In the models loop, a model with `file` set is checked by file:

```python
        if m.file:
            f = probe.model_file(m.repo, m.revision, m.file)
            results.append(
                CheckResult(f"model:{mname}:file", "ok", str(f))
                if f
                else CheckResult(
                    f"model:{mname}:file", "fail", f"{m.repo}@{m.revision[:8]} {m.file} not in HF cache; run: task models:pull -- {mname}"
                )
            )
            continue
```

Update `tests/unit/test_doctor.py`'s existing projection assertions to the new row name `engine:main:memory_projection[teams]` (the base kind when no profiles are declared).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_doctor.py -q 2>&1 | tail -3`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
task check 2>&1 | tail -3
git add src/airlie/doctor.py tests/unit/test_doctor.py
git commit -m "feat(doctor): per-profile memory projection; llama-server binary, /v1/systemone and GGUF file checks"
```

---

### Task 4: Managed llama-server engine (`task up` starts Kev), GGUF pulls

**Files:**
- Modify: `src/airlie/engine/registry.py` (add `build_llama_command`, `gguf_path`)
- Modify: `src/airlie/engine/process.py` (`EngineProcess` by kind; `RealRunner.spawn` prefixes `uv run` only for `vllm-mlx`)
- Modify: `src/airlie/models_cmd.py` (file pulls)
- Modify: `src/airlie/cli.py` (`_managed` includes `llama_server`)
- Test: `tests/unit/test_registry.py`, `tests/unit/test_process.py`, `tests/unit/test_models_cmd.py`

**Interfaces:**
- Consumes: `EngineSpec.kind/binary/ctx_size/served_name`, `ModelSpec.file` (Task 1).
- Produces: `build_llama_command(cfg, engine_name, gguf: Path) -> list[str]`; `gguf_path(m: ModelSpec, snapshot_dir: SnapshotResolver) -> Path`; `EngineProcess.start()` works for both kinds; `plan_pulls` emits `hf download <repo> <file> --revision <rev>` for file models.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_registry.py`:

```python
def test_llama_command_serves_one_gguf_under_its_catalog_name(tmp_path: Path) -> None:
    from airlie.engine.registry import build_llama_command, gguf_path

    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["base"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"], "ctx_size": 4096},
        },
        "models": {
            "base": {"repo": "org/base", "revision": "r1"},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf"},
        },
        "roles": {r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_path / "airlie.yaml")
    gguf = gguf_path(cfg.models["kev"], snap)
    assert gguf == Path("/hf/ggml-org/Kev-4B-GGUF/d9/Kev-4B-Q4_K_M.gguf")
    argv = build_llama_command(cfg, "decision", gguf)
    assert argv == [
        "llama-server", "-m", str(gguf), "--host", "127.0.0.1", "--port", "8081", "-c", "4096",
        "--alias", "kev", "--jinja",
    ]  # fmt: skip


def test_server_parsers_false_emits_no_parser_flags(tmp_path: Path) -> None:
    argv = build_serve_command(cfg(tmp_path, server_parsers=False), "main", Path("/r.yaml"))
    assert "--tool-call-parser" not in argv and "--reasoning-parser" not in argv
    assert "--enable-auto-tool-choice" not in argv
    argv = build_serve_command(cfg(tmp_path), "main", Path("/r.yaml"))
    assert argv[argv.index("--tool-call-parser") + 1] == "qwen"
```

`tests/unit/test_process.py`:

```python
def llama_cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["base"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "base": {"repo": "org/base", "revision": "r1"},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf"},
        },
        "roles": {r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


async def test_llama_server_engine_starts_with_the_gguf_and_waits_for_its_alias(tmp_path: Path) -> None:
    runner = FakeRunner()
    prober = FakeProber(health=[False, True], models=[[], ["kev"]])
    p = EngineProcess(
        llama_cfg(tmp_path), "decision", state_dir=tmp_path / "state", runner=runner, prober=prober,
        snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"), poll_s=0.0,
    )
    h = await p.start(timeout_s=5)
    argv, _ = runner.spawned[0]
    assert argv[:3] == ["llama-server", "-m", "/hf/ggml-org/Kev-4B-GGUF/d9/Kev-4B-Q4_K_M.gguf"]
    assert h.ok and h.models == {"kev": "ready"}
    assert not p.registry_path.exists()  # no vllm-mlx registry for a llama_server engine
```

`tests/unit/test_models_cmd.py`:

```python
def test_file_models_pull_only_that_file(tmp_path: Path) -> None:
    ...  # build a config like the file's existing helper, with a model {"repo": "ggml-org/Kev-4B-GGUF",
         #  "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf"} named "kev"
    plans = {p.name: p for p in plan_pulls(cfg)}
    assert plans["kev"].cmd == ("hf", "download", "ggml-org/Kev-4B-GGUF", "Kev-4B-Q4_K_M.gguf", "--revision", "d9")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_registry.py tests/unit/test_process.py tests/unit/test_models_cmd.py -q 2>&1 | tail -5`
Expected: ImportError `build_llama_command`; the process test fails on `KeyError`/registry write; the pull plan lacks the file.

- [ ] **Step 3: Implement**

`src/airlie/engine/registry.py`:

```python
def gguf_path(m: ModelSpec, snapshot_dir: SnapshotResolver) -> Path:
    if not m.file:
        raise ValueError(f"model {m.repo} has no `file`; llama_server engines serve one .gguf")
    return (m.path if m.path is not None else snapshot_dir(m)) / m.file


def build_llama_command(cfg: AirlieConfig, engine_name: str, gguf: Path) -> list[str]:
    e = cfg.engines[engine_name]
    name = e.models[0]
    argv = [
        e.binary, "-m", str(gguf),
        "--host", e.host, "--port", str(e.port),
        "-c", str(e.ctx_size),
        "--alias", e.served_name(name),
        "--jinja",
    ]  # fmt: skip
    if e.api_key:
        argv += ["--api-key", e.api_key]
    argv += list(e.extra_args)
    return argv
```

and gate the parser flags in `build_serve_command`:

```python
    if e.server_parsers and team_model.tool_call_parser:
        argv += ["--enable-auto-tool-choice", "--tool-call-parser", team_model.tool_call_parser]
    if e.server_parsers and team_model.reasoning_parser:
        argv += ["--reasoning-parser", team_model.reasoning_parser]
```

`src/airlie/engine/process.py`: `RealRunner.spawn` builds `cmd = ["uv", "run", "--quiet", *argv] if argv[0] == "vllm-mlx" else list(argv)`. In `EngineProcess.start`:

```python
        if self.spec.kind == "llama_server":
            argv = build_llama_command(self.cfg, self.name, gguf_path(self.cfg.models[self.spec.models[0]], self.snapshot_dir))
        else:
            registry = build_registry(self.cfg, self.name, self.snapshot_dir)
            write_registry(registry, self.registry_path)
            argv = build_serve_command(self.cfg, self.name, self.registry_path)
```

`_probe` already marks a model `ready` when its id is in `/v1/models` and `preload` is true; llama-server lists the `--alias`, so no change. In `cli.py`: `_managed` returns engines whose kind is `vllm_mlx` or `llama_server`; `engine up` starts them in config order (main first, so the big weights load while Kev starts).

`src/airlie/models_cmd.py`:

```python
        cmd = ("hf", "download", m.repo, *( (m.file,) if m.file else () ), "--revision", m.revision)
        plans.append(PullPlan(name, m.repo, m.revision, cmd))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_registry.py tests/unit/test_process.py tests/unit/test_models_cmd.py tests/unit/test_cli_engine.py -q 2>&1 | tail -3`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
task check 2>&1 | tail -3
git add -A src tests
git commit -m "feat(engine): managed llama-server engine kind (Kev); server_parsers gates vllm-mlx parser flags; GGUF file pulls"
```

---

### Task 5: Client-side output parsing with vllm-mlx's parsers, Muse Glimmer parsers

**Files:**
- Create: `src/airlie/engine/parsers/__init__.py`, `src/airlie/engine/parsers/muse_glimmer.py`
- Create: `src/airlie/engine/output_parse.py`
- Modify: `src/airlie/engine/http_engine.py` (optional `model_spec_for`, apply `parse_output`)
- Modify: `src/airlie/cli.py` (`_http_engine` passes `model_spec_for`)
- Create: `tests/fixtures/parsers/{qwen3_coder_xml.txt, qwen_json.txt, harmony.txt, muse_self_user.txt, muse_bare_user.txt, muse_unterminated.txt, muse_tool.txt}`
- Test: `tests/unit/test_output_parse.py`, `tests/unit/test_muse_parsers.py`, `tests/unit/test_http_engine.py` (one test)

**Interfaces:**
- Consumes: `ModelSpec.tool_call_parser / reasoning_parser`, `GenResult`, `ToolCall`.
- Produces: `parse_output(model: ModelSpec, result: GenResult) -> GenResult`; `airlie.engine.parsers.ensure_registered() -> None` (idempotent; registers `muse_glimmer` in both vllm-mlx registries); `split_channels(text: str) -> list[tuple[str, str]]`; `HTTPEngine(..., model_spec_for: Callable[[str], ModelSpec] | None = None)`.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/parsers/qwen3_coder_xml.txt`:
```
I'll look at the file first.
<tool_call>
<function=read_file>
<parameter=path>
app.py
</parameter>
</function>
</tool_call>
```
`tests/fixtures/parsers/qwen_json.txt`:
```
<think>
The user wants the file.
</think>

<tool_call>
{"name": "read_file", "arguments": {"path": "app.py"}}
</tool_call>
```
`tests/fixtures/parsers/harmony.txt`:
```
<|channel|>analysis<|message|>Need to read app.py<|end|><|start|>assistant<|channel|>final<|message|>Reading it now.<|return|>
```
`tests/fixtures/parsers/muse_self_user.txt` (note the leading space):
```
 to=self<|message|>The guard checks qty < 0 so zero slips through.<|eom|><|start|>assistant to=user<|message|>{"verdict": "request_changes", "findings": [{"severity": "blocking", "file": "app/orders.py", "line": 41, "claim": "qty == 0 reaches the division", "evidence": "guard is qty < 0"}]}<|eot|>
```
`tests/fixtures/parsers/muse_bare_user.txt`:
```
to=user<|message|>{"verdict": "approve", "findings": []}<|eot|>
```
`tests/fixtures/parsers/muse_unterminated.txt`:
```
 to=self<|message|>Still thinking about the edge case
```
`tests/fixtures/parsers/muse_tool.txt`:
```
 to=self<|message|>I need the file.<|eom|><|start|>assistant to=read_file<|message|><atem:function_calls>
<atem:invoke name="read_file">
<atem:parameter name="path">app.py</atem:parameter>
<atem:parameter name="lines">[1, 40]</atem:parameter>
</atem:invoke>
</atem:function_calls><|eom|>
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_muse_parsers.py`:

```python
"""Muse Glimmer channel format (design §4a/§4b): to=self reasoning, to=user answer, to=<tool> atem calls."""

from pathlib import Path

from airlie.engine.parsers import ensure_registered
from airlie.engine.parsers.muse_glimmer import MuseGlimmerReasoningParser, MuseGlimmerToolParser, split_channels

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parsers"


def fx(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def test_split_channels_self_then_user() -> None:
    assert split_channels(fx("muse_self_user")) == [
        ("self", "The guard checks qty < 0 so zero slips through."),
        ("user", fx("muse_self_user").split("to=user<|message|>")[1].removesuffix("<|eot|>\n")),
    ]


def test_reasoning_parser_moves_self_to_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_self_user"))
    assert reasoning == "The guard checks qty < 0 so zero slips through."
    assert content is not None and content.startswith('{"verdict": "request_changes"')


def test_bare_user_turn_has_no_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_bare_user"))
    assert reasoning is None and content == '{"verdict": "approve", "findings": []}'


def test_unterminated_self_block_is_all_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_unterminated"))
    assert reasoning == "Still thinking about the edge case" and content is None


def test_tool_parser_reads_atem_invoke_and_ignores_self_channel() -> None:
    info = MuseGlimmerToolParser().extract_tool_calls(fx("muse_tool"), None)
    assert info.tools_called and len(info.tool_calls) == 1
    call = info.tool_calls[0]
    assert call["name"] == "read_file"
    assert call["arguments"] == '{"path": "app.py", "lines": [1, 40]}'
    assert info.content is None


def test_tool_parser_without_calls_reports_user_content() -> None:
    info = MuseGlimmerToolParser().extract_tool_calls(fx("muse_bare_user"), None)
    assert not info.tools_called and info.content == '{"verdict": "approve", "findings": []}'


def test_registered_under_muse_glimmer_in_both_registries() -> None:
    from vllm_mlx.reasoning import get_parser
    from vllm_mlx.tool_parsers import ToolParserManager

    ensure_registered()
    ensure_registered()  # idempotent
    assert get_parser("muse_glimmer") is MuseGlimmerReasoningParser
    assert ToolParserManager.get_tool_parser("muse_glimmer") is MuseGlimmerToolParser
```

`tests/unit/test_output_parse.py`:

```python
"""parse_output: vllm-mlx's parser classes applied on the client (design §4b). Server-parsed calls win."""

from pathlib import Path

from airlie.config import ModelSpec
from airlie.engine import GenResult, ToolCall
from airlie.engine.output_parse import parse_output

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parsers"


def result(text: str, **kw) -> GenResult:
    base = dict(text=text, reasoning="", tool_calls=[], finish_reason="stop", prompt_tokens=1, completion_tokens=1)
    return GenResult(**{**base, **kw})


def model(**kw) -> ModelSpec:
    return ModelSpec(repo="org/m", revision="r", **kw)


def test_qwen3_coder_xml_becomes_a_tool_call() -> None:
    out = parse_output(model(tool_call_parser="qwen3_coder"), result((FIX / "qwen3_coder_xml.txt").read_text()))
    assert out.finish_reason == "tool_calls"
    assert [(c.name, c.arguments) for c in out.tool_calls] == [("read_file", '{"path": "app.py"}')]
    assert out.text == "I'll look at the file first."
    assert out.tool_calls[0].id.startswith("call_")


def test_qwen_json_call_and_think_block() -> None:
    out = parse_output(
        model(tool_call_parser="qwen", reasoning_parser="qwen3"), result((FIX / "qwen_json.txt").read_text())
    )
    assert out.reasoning == "The user wants the file."
    assert out.tool_calls[0].name == "read_file" and out.tool_calls[0].arguments == '{"path": "app.py"}'


def test_harmony_reasoning_and_final() -> None:
    out = parse_output(model(reasoning_parser="harmony"), result((FIX / "harmony.txt").read_text()))
    assert out.reasoning == "Need to read app.py" and out.text == "Reading it now."


def test_muse_review_one_shot() -> None:
    out = parse_output(
        model(tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer"),
        result((FIX / "muse_self_user.txt").read_text()),
    )
    assert out.reasoning.startswith("The guard") and out.text.startswith('{"verdict"') and not out.tool_calls


def test_muse_tool_channel_without_user_block() -> None:
    out = parse_output(
        model(tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer"),
        result((FIX / "muse_tool.txt").read_text()),
    )
    assert out.text == "" and out.tool_calls[0].name == "read_file" and out.finish_reason == "tool_calls"


def test_server_parsed_calls_win() -> None:
    already = [ToolCall(id="call_1", name="read_file", arguments='{"path": "x"}')]
    raw = (FIX / "qwen3_coder_xml.txt").read_text()
    out = parse_output(model(tool_call_parser="qwen3_coder"), result(raw, tool_calls=already, finish_reason="tool_calls"))
    assert out.tool_calls == already and out.text == raw


def test_model_without_parsers_is_untouched() -> None:
    raw = (FIX / "qwen3_coder_xml.txt").read_text()
    out = parse_output(model(), result(raw))
    assert out.text == raw and not out.tool_calls


def test_server_reasoning_is_not_reparsed() -> None:
    out = parse_output(model(reasoning_parser="qwen3"), result("<think>x</think>y", reasoning="server"))
    assert out.reasoning == "server" and out.text == "<think>x</think>y"
```

In `tests/unit/test_http_engine.py` add:

```python
async def test_engine_applies_the_models_parsers_to_raw_content() -> None:
    from airlie.config import ModelSpec

    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "parsers" / "qwen3_coder_xml.txt").read_text()
    frames = [
        {"choices": [{"delta": {"content": raw}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 9}},
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    rec = Recorder({"stream": body.encode()})
    eng = HTTPEngine(
        base_url="http://engine.test/v1",
        served_name_for_role=lambda role: "base",
        role_spec_for=lambda role: role_spec(),
        model_spec_for=lambda role: ModelSpec(repo="o/m", revision="r", tool_call_parser="qwen3_coder"),
        transport=httpx.MockTransport(rec),
    )
    res = await eng.generate(plain_request())
    assert res.finish_reason == "tool_calls" and res.tool_calls[0].name == "read_file"
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/unit/test_muse_parsers.py tests/unit/test_output_parse.py tests/unit/test_http_engine.py -q 2>&1 | tail -5`
Expected: ImportError (`airlie.engine.parsers`, `airlie.engine.output_parse`); the http test fails on the unknown kwarg.

- [ ] **Step 4: Implement the Muse parsers**

`src/airlie/engine/parsers/muse_glimmer.py`:

```python
"""Muse Glimmer's channel format, parsed with vllm-mlx 0.5.0's parser base classes.

Generation begins ` to=self<|message|>…<|eom|>` (reasoning) and continues `to=user<|message|>…<|eot|>`
(answer) or `to=<tool><|message|><atem:function_calls>…` (a call); later turns may open with a bare
`to=user`. Regexes follow vllm-mlx PR #812 (unmerged on 2026-10-13); delete this module when a vllm-mlx
release registers `muse_glimmer` itself."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from vllm_mlx.reasoning.base import DeltaMessage, ReasoningParser
from vllm_mlx.tool_parsers.abstract_tool_parser import ExtractedToolCallInformation, ToolParser

_HEADER = re.compile(
    r"(?:^|<\|eom\|>|<\|eot\|>)\s*(?:<\|start\|>)?\s*(?:assistant\s+)?to=(?P<r>[\w.\-]+)\s*(?:<\|message\|>)?",
)
_END = re.compile(r"<\|eom\|>|<\|eot\|>|<\|start\|>|<\|message\|>")
_INVOKE = re.compile(r'<atem:invoke\b[^>]*?\bname="(?P<name>[^"]+)">(?P<body>.*?)</atem:invoke>', re.DOTALL)
_PARAM = re.compile(r'<atem:parameter\b[^>]*?\bname="(?P<name>[^"]+)"[^>]*?>(?P<value>.*?)</atem:parameter>', re.DOTALL)


def split_channels(text: str) -> list[tuple[str, str]]:
    """[(recipient, body)] in order; text with no header is one `user` block."""
    heads = list(_HEADER.finditer(text))
    if not heads:
        return [("user", _END.sub("", text).strip())]
    out: list[tuple[str, str]] = []
    lead = text[: heads[0].start()]
    if _END.sub("", lead).strip():
        out.append(("user", _END.sub("", lead).strip()))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((h.group("r"), _END.sub("", text[h.end() : end]).strip()))
    return out


def _coerce(value: str) -> Any:
    v = value.strip()
    try:
        return json.loads(v)
    except ValueError:
        return v


class MuseGlimmerReasoningParser(ReasoningParser):
    def extract_reasoning(self, model_output: str) -> tuple[str | None, str | None]:
        parts = split_channels(model_output)
        reasoning = "\n".join(b for r, b in parts if r == "self" and b)
        rest: list[str] = []
        for r, b in parts:
            if r == "self":
                continue
            rest.append(b if r == "user" else f"to={r}\n{b}")  # keep tool channels for the tool parser
        content = "\n".join(x for x in rest if x)
        return (reasoning or None, content or None)

    def extract_reasoning_streaming(
        self, previous_text: str, current_text: str, delta_text: str
    ) -> DeltaMessage | None:
        return DeltaMessage(content=delta_text)  # airlie parses complete outputs; stream raw


class MuseGlimmerToolParser(ToolParser):
    def extract_tool_calls(
        self, model_output: str, request: dict[str, Any] | None = None
    ) -> ExtractedToolCallInformation:
        calls: list[dict[str, Any]] = []
        user: list[str] = []
        for recipient, body in split_channels(model_output):
            if recipient == "self":
                continue
            if recipient == "user":
                if body:
                    user.append(body)
                continue
            for inv in _INVOKE.finditer(body):
                args = {m.group("name"): _coerce(m.group("value")) for m in _PARAM.finditer(inv.group("body"))}
                calls.append(
                    {"id": f"call_{uuid.uuid4().hex[:8]}", "name": inv.group("name"), "arguments": json.dumps(args)}
                )
        return ExtractedToolCallInformation(
            tools_called=bool(calls), tool_calls=calls, content=("\n".join(user) or None)
        )
```

`src/airlie/engine/parsers/__init__.py`:

```python
"""Parsers airlie adds to vllm-mlx's registries (client-side parsing, design 2026-10-13 §4b)."""

from __future__ import annotations

_done = False


def ensure_registered() -> None:
    global _done
    if _done:
        return
    from vllm_mlx.reasoning import register_parser
    from vllm_mlx.tool_parsers import ToolParserManager

    from airlie.engine.parsers.muse_glimmer import MuseGlimmerReasoningParser, MuseGlimmerToolParser

    register_parser("muse_glimmer", MuseGlimmerReasoningParser)
    ToolParserManager.register_module("muse_glimmer", MuseGlimmerToolParser)
    _done = True
```

- [ ] **Step 5: Implement `parse_output` and wire the engine**

`src/airlie/engine/output_parse.py`:

```python
"""parse_output: apply a model's tool/reasoning parsers (vllm-mlx's own classes) to a GenResult.

Precedence: tool calls the server already parsed are kept; reasoning the server already split is kept;
otherwise the model's `tool_call_parser` runs on `text`, then its `reasoning_parser` on what is left."""

from __future__ import annotations

import json
import uuid
from functools import cache
from typing import Any

from airlie.config import ModelSpec
from airlie.engine.parsers import ensure_registered
from airlie.engine.types import GenResult, ToolCall


@cache
def _tool_parser(name: str) -> Any:
    ensure_registered()
    from vllm_mlx.tool_parsers import ToolParserManager

    return ToolParserManager.get_tool_parser(name)()


@cache
def _reasoning_parser(name: str) -> Any:
    ensure_registered()
    from vllm_mlx.reasoning import get_parser

    return get_parser(name)()


def _arguments(raw: Any) -> str:
    return raw if isinstance(raw, str) else json.dumps(raw)


def parse_output(model: ModelSpec, result: GenResult) -> GenResult:
    text = result.text
    if model.reasoning_parser and not result.reasoning and text:
        reasoning, content = _reasoning_parser(model.reasoning_parser).extract_reasoning(text)
        if reasoning:
            result.reasoning = reasoning
            text = content or ""
    if model.tool_call_parser and not result.tool_calls and text:
        parser = _tool_parser(model.tool_call_parser)
        parser.reset()
        info = parser.extract_tool_calls(text, None)
        if info.tools_called:
            result.tool_calls = [
                ToolCall(
                    id=str(c.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                    name=str(c["name"]),
                    arguments=_arguments(c.get("arguments", "{}")),
                )
                for c in info.tool_calls
            ]
            text = (info.content or "").strip()
            if result.finish_reason != "cancelled":
                result.finish_reason = "tool_calls"
    result.text = text
    return result
```

Note the Muse reasoning parser keeps tool channels in `content` as `to=<tool>\n<atem:…>` so the tool parser still finds them; for Muse the tool parser also drops the `to=user` text only when there are calls (`muse_tool.txt` has none → `text == ""`). Check the order in the tests: reasoning first, then tools.

`HTTPEngine.__init__` gains `model_spec_for: Callable[[str], ModelSpec] | None = None` stored as `self._model`; at the end of `generate`, before returning:

```python
        res = GenResult(...)  # the existing construction, assigned to `res`
        if self._model is not None:
            res = parse_output(self._model(req.role), res)
        return res
```

`cli._http_engine` passes `model_spec_for=lambda role: cfg.models[model_override or cfg.roles[role].model]`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_muse_parsers.py tests/unit/test_output_parse.py tests/unit/test_http_engine.py -q 2>&1 | tail -3`
Expected: pass. If `split_channels` test 1 fails on whitespace, fix the fixture slice in the test, not the parser (the parser strips).

- [ ] **Step 7: Commit**

```bash
task check 2>&1 | tail -3
git add -A src tests
git commit -m "feat(engine): client-side output parsing with vllm-mlx's parser classes; Muse Glimmer channel parsers"
```

---

### Task 6: Content parts (images) on the engine seam

**Files:**
- Modify: `src/airlie/engine/types.py` (`ContentPart`, `Message.parts`, `image_part`)
- Modify: `src/airlie/engine/http_engine.py` (`_wire_message`, per-role `chat_template_kwargs`)
- Modify: `src/airlie/teams/native_team.py` (`dump_messages`/`load_messages` carry `parts`)
- Modify: `src/airlie/engine/__init__.py` (export `ContentPart`, `image_part`)
- Test: `tests/unit/test_http_engine.py`, `tests/unit/test_native_team.py`

**Interfaces:**
- Consumes: `RoleSpec.chat_template_kwargs` (Task 1).
- Produces: `ContentPart(type: Literal["text", "image_url"], text: str = "", url: str = "")`; `Message.parts: tuple[ContentPart, ...] | None = None` (when set, the wire `content` is the list; `content` stays the text summary for logs/token estimates); `image_part(path: Path) -> ContentPart` (data URL, mime from suffix: png/jpg/jpeg/gif/webp).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_http_engine.py`:

```python
async def test_message_parts_are_sent_as_content_list_and_role_kwargs_merge(tmp_path: Path) -> None:
    from airlie.engine import ContentPart, image_part

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    eng, rec = engine({"stream": sse("plain")})
    eng._role = lambda role: RoleSpec(
        prompt=Path("/dev/null"), model="m", thinking=False, chat_template_kwargs={"reasoning_strength": "low"}
    )
    req = plain_request()
    req.messages = [
        Message(role="user", content="critique this", parts=(ContentPart("text", text="critique this"), image_part(png)))
    ]
    await eng.generate(req)
    body = json.loads(rec.requests[0].content)
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "critique this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_strength": "low"}
```

`tests/unit/test_native_team.py`:

```python
def test_dump_and_load_messages_keep_parts() -> None:
    from airlie.engine import ContentPart, Message
    from airlie.teams.native_team import dump_messages, load_messages

    m = Message(role="user", content="see image", parts=(ContentPart("image_url", url="data:image/png;base64,AA=="),))
    assert load_messages(dump_messages([m])) == [m]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_http_engine.py tests/unit/test_native_team.py -q 2>&1 | tail -4`
Expected: ImportError `ContentPart`.

- [ ] **Step 3: Implement**

`src/airlie/engine/types.py`:

```python
import base64
import mimetypes
from pathlib import Path


@dataclass(frozen=True)
class ContentPart:
    type: Literal["text", "image_url"]
    text: str = ""
    url: str = ""


def image_part(path: Path) -> ContentPart:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return ContentPart("image_url", url=f"data:{mime};base64,{data}")


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    parts: tuple[ContentPart, ...] | None = None
    """When set, the wire content is this list (text + image_url parts); `content` stays the text."""
```

`http_engine._wire_message`:

```python
    content: Any = m.content
    if m.parts:
        content = [
            {"type": "text", "text": p.text} if p.type == "text" else {"type": "image_url", "image_url": {"url": p.url}}
            for p in m.parts
        ]
    d: dict[str, Any] = {"role": m.role, "content": content}
```

`HTTPEngine._body`: `"chat_template_kwargs": {"enable_thinking": bool(thinking), **spec.chat_template_kwargs}`.

`native_team.dump_messages`: `d["parts"] = [asdict(p) for p in m.parts] if m.parts else None`; `load_messages`: `parts=tuple(ContentPart(**p) for p in d["parts"]) if d.get("parts") else None`.

- [ ] **Step 4: Run the tests, then the suite**

Run: `uv run pytest tests/unit -q 2>&1 | tail -3`
Expected: pass (FakeEngine's token estimate still uses `content`).

- [ ] **Step 5: Commit**

```bash
task check 2>&1 | tail -3
git add -A src tests
git commit -m "feat(engine): content parts (text + image_url) on Message; per-role chat_template_kwargs"
```

---

### Task 7: Kev client, routing policy, routers, routing log

**Files:**
- Create: `src/airlie/router/__init__.py`, `src/airlie/router/jev.py`, `src/airlie/router/policy.py`, `src/airlie/router/protocol.py`, `src/airlie/router/chief.py`, `src/airlie/router/log.py`
- Test: `tests/unit/test_router.py`

**Interfaces:**
- Consumes: `RouterSpec`, `cfg.engines[...]` (Task 1).
- Produces:
  - `Answer(choice: str, probabilities: dict[str, float], confidence: float, input_tokens: int)`
  - `JevClient(base_url: str, *, model: str = "jev", transport=None, timeout_s: float = 60.0)`, `async ask(state, question, instructions, criteria: dict[str, str]) -> Answer`, `async aclose()`
  - `ranked(probabilities) -> list[tuple[str, float]]` (prob desc, name asc), `policy(ranked, confidence, *, ambiguous_gap, min_confidence) -> Literal["clear", "ambiguous", "low-confidence"]`
  - `RouteDecision(question, choice, chosen, decision, probabilities, confidence, hint, input_tokens)`; `decision` ∈ clear | ambiguous | low-confidence | invalid | hint | error
  - `Router` protocol: `async route(brief: str, criteria: dict[str, str], hint: str | None = None, *, question: str = "role", instructions: str = "Which specialist should handle this task?") -> RouteDecision`
  - `JevRouter(client, *, ambiguous_gap, min_confidence)`, `ChiefRouter()`, `build_router(cfg, *, transport=None) -> Router`
  - `RoutingLog(path: Path)`, `record(subtask: str, d: RouteDecision) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_router.py`:

```python
"""Router: Kev (/v1/systemone) client, ai-mode's decision policy, chief fallback, JSONL log."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from airlie.router import ChiefRouter, JevClient, JevRouter, RoutingLog, policy, ranked

CRITERIA = {"coder": "Implement or change code", "qa": "Tests and edge cases", "docs": "README and guides"}


def systemone(answers: dict[str, float], confidence: float, choice: str | None = None):
    """A MockTransport handler answering /v1/systemone with the given probabilities."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        seen.append(body)
        q = next(iter(body["questions"]))
        top = choice or max(answers, key=answers.get)
        return httpx.Response(
            200,
            json={
                "answers": {q: {"choice": top, "probabilities": answers, "confidence": confidence}},
                "usage": {"input_tokens": 42},
            },
        )

    return handler, seen


def test_ranked_orders_by_probability_then_name() -> None:
    assert ranked({"qa": 0.3, "coder": 0.6, "docs": 0.1}) == [("coder", 0.6), ("qa", 0.3), ("docs", 0.1)]
    assert ranked({"b": 0.5, "a": 0.5}) == [("a", 0.5), ("b", 0.5)]


def test_policy_matches_ai_mode() -> None:
    assert policy([("coder", 0.8), ("qa", 0.2)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "clear"
    assert policy([("coder", 0.5), ("qa", 0.4)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "ambiguous"
    assert policy([("coder", 0.8), ("qa", 0.2)], 0.3, ambiguous_gap=0.15, min_confidence=0.5) == "low-confidence"


async def test_client_posts_the_system_one_request_shape() -> None:
    handler, seen = systemone({"coder": 0.9, "qa": 0.1}, 0.95)
    client = JevClient("http://kev.test/v1", model="kev", transport=httpx.MockTransport(handler))
    ans = await client.ask("fix the 500", "role", "Which specialist should handle this task?", CRITERIA)
    assert seen[0] == {
        "model": "kev",
        "state": "fix the 500",
        "questions": {
            "role": {"type": "choice", "instructions": "Which specialist should handle this task?", "criteria": CRITERIA}
        },
    }
    assert ans.choice == "coder" and ans.confidence == 0.95 and ans.input_tokens == 42


async def test_clear_decision_takes_the_choice_over_the_hint() -> None:
    handler, _ = systemone({"coder": 0.9, "qa": 0.1}, 0.95)
    r = JevRouter(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    d = await r.route("fix the 500", CRITERIA, hint="docs")
    assert d.decision == "clear" and d.chosen == "coder" and d.hint == "docs"


async def test_ambiguous_takes_the_hint_when_it_is_in_the_top_two() -> None:
    handler, _ = systemone({"coder": 0.45, "qa": 0.4, "docs": 0.15}, 0.9)
    r = JevRouter(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    assert (await r.route("add tests for the fix", CRITERIA, hint="qa")).chosen == "qa"
    assert (await r.route("add tests for the fix", CRITERIA, hint="docs")).chosen == "coder"
    assert (await r.route("add tests for the fix", CRITERIA)).chosen == "coder"


async def test_low_confidence_takes_the_hint_else_top() -> None:
    handler, _ = systemone({"coder": 0.8, "qa": 0.2}, 0.2)
    r = JevRouter(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    assert (await r.route("x", CRITERIA, hint="docs")).chosen == "docs"
    assert (await r.route("x", CRITERIA)).chosen == "coder"


async def test_choice_outside_closed_set_falls_back() -> None:
    handler, _ = systemone({"coder": 0.6, "qa": 0.4}, 0.9, choice="chief")
    r = JevRouter(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    d = await r.route("x", CRITERIA, hint="qa")
    assert d.decision == "invalid" and d.chosen == "qa"
    assert (await r.route("x", CRITERIA)).chosen == "coder"  # top in-set probability


async def test_transport_error_is_a_decision_not_an_exception() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    r = JevRouter(JevClient("http://k/v1", transport=httpx.MockTransport(boom)), ambiguous_gap=0.15, min_confidence=0.5)
    d = await r.route("x", CRITERIA, hint="qa")
    assert d.decision == "error" and d.chosen == "qa"
    with pytest.raises(ValueError, match="no hint"):
        await r.route("x", CRITERIA)


async def test_chief_router_uses_the_hint_and_rejects_unknown_roles() -> None:
    d = await ChiefRouter().route("x", CRITERIA, hint="qa")
    assert d.decision == "hint" and d.chosen == "qa" and d.confidence == 1.0
    with pytest.raises(ValueError, match="'ux'"):
        await ChiefRouter().route("x", CRITERIA, hint="ux")


async def test_routing_log_writes_one_json_line_per_decision(tmp_path: Path) -> None:
    log = RoutingLog(tmp_path / "routing.jsonl")
    d = await ChiefRouter().route("x", CRITERIA, hint="qa")
    log.record("fix", d)
    rec = json.loads((tmp_path / "routing.jsonl").read_text().splitlines()[0])
    assert rec["subtask"] == "fix" and rec["chosen"] == "qa" and rec["decision"] == "hint"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_router.py -q 2>&1 | tail -3`
Expected: ImportError `airlie.router`.

- [ ] **Step 3: Implement**

`src/airlie/router/policy.py`:

```python
"""ai-mode's decision policy (presets/agents/jev.md): clear / ambiguous / low-confidence."""

from __future__ import annotations

from typing import Literal

Decision = Literal["clear", "ambiguous", "low-confidence"]


def ranked(probabilities: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(((k, float(v)) for k, v in probabilities.items()), key=lambda kv: (-kv[1], kv[0]))


def policy(r: list[tuple[str, float]], confidence: float, *, ambiguous_gap: float, min_confidence: float) -> Decision:
    if len(r) >= 2 and r[0][1] - r[1][1] < ambiguous_gap:
        return "ambiguous"
    if confidence < min_confidence:
        return "low-confidence"
    return "clear"
```

`src/airlie/router/jev.py`:

```python
"""JevClient: POST /v1/systemone on a llama-server serving Kev (System One decision model)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Answer:
    choice: str
    probabilities: dict[str, float]
    confidence: float
    input_tokens: int


class JevClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "jev",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_s, headers=headers)

    async def ask(self, state: str, question: str, instructions: str, criteria: dict[str, str]) -> Answer:
        body = {
            "model": self.model,
            "state": state,
            "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        r = await self._client.post(f"{self.base_url}/systemone", json=body)
        if r.status_code != 200:
            raise httpx.HTTPStatusError(f"systemone returned {r.status_code}: {r.text[:200]}", request=r.request, response=r)
        data = r.json()
        ans = (data.get("answers") or {}).get(question)
        if not isinstance(ans, dict) or "choice" not in ans:
            raise ValueError(f"systemone answered without '{question}': {str(data)[:200]}")
        probs = {str(k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
        usage = data.get("usage") or {}
        return Answer(str(ans["choice"]), probs, float(ans.get("confidence", 0.0)), int(usage.get("input_tokens", 0)))

    async def aclose(self) -> None:
        await self._client.aclose()
```

`src/airlie/router/protocol.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

ROLE_QUESTION = "Which specialist should handle this task?"


@dataclass(frozen=True)
class RouteDecision:
    question: str
    choice: str | None
    chosen: str
    decision: str
    """clear | ambiguous | low-confidence | invalid | error | hint"""
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    hint: str | None = None
    input_tokens: int = 0
    detail: str = ""


class Router(Protocol):
    async def route(
        self, brief: str, criteria: dict[str, str], hint: str | None = None, *, question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision: ...
```

`src/airlie/router/chief.py`:

```python
from __future__ import annotations

from airlie.router.protocol import ROLE_QUESTION, RouteDecision


class ChiefRouter:
    """The plan's own `role` field is the decision (design §6)."""

    async def route(
        self, brief: str, criteria: dict[str, str], hint: str | None = None, *, question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision:
        if hint is None or hint not in criteria:
            raise ValueError(f"chief router needs a role hint from the closed set; got {hint!r} (set: {sorted(criteria)})")
        return RouteDecision(question, hint, hint, "hint", {hint: 1.0}, 1.0, hint)
```

`JevRouter` in `src/airlie/router/jev.py` (same module, below the client):

```python
from airlie.router.policy import policy, ranked
from airlie.router.protocol import ROLE_QUESTION, RouteDecision


class JevRouter:
    def __init__(self, client: JevClient, *, ambiguous_gap: float, min_confidence: float) -> None:
        self.client = client
        self.gap = ambiguous_gap
        self.min_conf = min_confidence

    async def route(
        self, brief: str, criteria: dict[str, str], hint: str | None = None, *, question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision:
        try:
            ans = await self.client.ask(brief, question, instructions, criteria)
        except (httpx.HTTPError, ValueError) as e:
            if hint is None:
                raise ValueError(f"routing failed and no hint to fall back on: {e}") from e
            return RouteDecision(question, None, hint, "error", {}, 0.0, hint, detail=str(e))
        r = [(k, p) for k, p in ranked(ans.probabilities) if k in criteria]
        top = r[0][0] if r else None
        if ans.choice not in criteria:
            chosen = hint if hint in criteria else top
            if chosen is None:
                raise ValueError(f"systemone chose {ans.choice!r}, outside the closed set, and no fallback")
            return RouteDecision(question, ans.choice, chosen, "invalid", ans.probabilities, ans.confidence, hint, ans.input_tokens)
        decision = policy(r, ans.confidence, ambiguous_gap=self.gap, min_confidence=self.min_conf)
        if decision == "clear":
            chosen = ans.choice
        elif decision == "ambiguous":
            top_two = {k for k, _ in r[:2]}
            chosen = hint if hint in top_two else ans.choice
        else:
            chosen = hint if hint in criteria else ans.choice
        return RouteDecision(question, ans.choice, chosen, decision, ans.probabilities, ans.confidence, hint, ans.input_tokens)
```

`src/airlie/router/log.py`:

```python
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from airlie.router.protocol import RouteDecision


class RoutingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, subtask: str, d: RouteDecision) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "subtask": subtask, **asdict(d)}) + "\n")
```

`src/airlie/router/__init__.py`:

```python
from __future__ import annotations

import httpx

from airlie.config import AirlieConfig
from airlie.router.chief import ChiefRouter
from airlie.router.jev import Answer, JevClient, JevRouter
from airlie.router.log import RoutingLog
from airlie.router.policy import policy, ranked
from airlie.router.protocol import ROLE_QUESTION, RouteDecision, Router


def jev_client(cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> JevClient:
    """The client for the llama_server engine that serves router.model."""
    if cfg.router.model is None:
        raise ValueError("router.model is not set")
    e = cfg.engines[cfg.engine_for_model(cfg.router.model)]
    return JevClient(e.base_url or "", model=e.served_name(cfg.router.model), transport=transport, api_key=e.api_key)


def build_router(cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> Router:
    if cfg.router.kind == "jev":
        return JevRouter(
            jev_client(cfg, transport=transport),
            ambiguous_gap=cfg.router.ambiguous_gap,
            min_confidence=cfg.router.min_confidence,
        )
    return ChiefRouter()


__all__ = [
    "ROLE_QUESTION", "Answer", "ChiefRouter", "JevClient", "JevRouter", "RouteDecision", "Router", "RoutingLog",
    "build_router", "jev_client", "policy", "ranked",
]  # fmt: skip
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_router.py -q 2>&1 | tail -3`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
task check 2>&1 | tail -3
git add -A src/airlie/router tests/unit/test_router.py
git commit -m "feat(router): Kev /v1/systemone client, ai-mode decision policy, jev and chief routers, routing log"
```

---

### Task 8: The `decide` tool for any role

**Files:**
- Create: `src/airlie/tools/decide.py`
- Modify: `src/airlie/tools/registry.py` (not in `BUILTIN`; attached explicitly), `src/airlie/tools/__init__.py`
- Test: `tests/unit/test_decide_tool.py`

**Interfaces:**
- Consumes: `JevClient`, `policy`, `ranked` (Task 7); `Tool`, `ToolContext`, `ToolResult`.
- Produces: `Decide(client: JevClient, *, ambiguous_gap: float, min_confidence: float)` with `name = "decide"`, parameters `{question: str, state: str, options: {name: criterion}}`; `attach_decide(registry: ToolRegistry, cfg: AirlieConfig, *, transport=None) -> bool` (adds the tool when `router.model` is served by a `llama_server` engine; returns whether it did).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_decide_tool.py`:

```python
"""decide: a calibrated closed-set choice from Kev, available to any role as a tool (design §14)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from airlie.engine import ToolCall
from airlie.router import JevClient
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.decide import Decide
from tests.unit.test_router import systemone


async def test_decide_returns_choice_probabilities_and_policy(tmp_path: Path) -> None:
    handler, seen = systemone({"blocking": 0.8, "minor": 0.2}, 0.9)
    tool = Decide(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    reg = ToolRegistry([tool], ToolContext(worktree=tmp_path))
    args = {"question": "severity", "state": "qty == 0 reaches the division", "options": {"blocking": "breaks users", "minor": "cosmetic"}}
    res = await reg.call(ToolCall(id="c1", name="decide", arguments=json.dumps(args)))
    assert res.ok
    out = json.loads(res.output)
    assert out == {"choice": "blocking", "decision": "clear", "confidence": 0.9, "probabilities": {"blocking": 0.8, "minor": 0.2}}
    assert seen[0]["questions"]["severity"]["criteria"] == args["options"]


async def test_decide_needs_at_least_two_options(tmp_path: Path) -> None:
    handler, _ = systemone({"a": 1.0}, 1.0)
    tool = Decide(JevClient("http://k/v1", transport=httpx.MockTransport(handler)), ambiguous_gap=0.15, min_confidence=0.5)
    res = await tool.run({"question": "q", "state": "s", "options": {"a": "only"}}, ToolContext(worktree=tmp_path))
    assert not res.ok and "two options" in res.output


async def test_decide_reports_a_down_decision_engine_as_an_observation(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    tool = Decide(JevClient("http://k/v1", transport=httpx.MockTransport(boom)), ambiguous_gap=0.15, min_confidence=0.5)
    res = await tool.run({"question": "q", "state": "s", "options": {"a": "x", "b": "y"}}, ToolContext(worktree=tmp_path))
    assert not res.ok and "decision engine" in res.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_decide_tool.py -q 2>&1 | tail -3`
Expected: ImportError `airlie.tools.decide`.

- [ ] **Step 3: Implement**

`src/airlie/tools/decide.py`:

```python
"""decide: ask the System One decision model (Kev) for a calibrated choice among named options."""

from __future__ import annotations

import json
from typing import Any

import httpx

from airlie.config import AirlieConfig
from airlie.router import JevClient, jev_client, policy, ranked
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry


class Decide:
    name = "decide"
    description = (
        "Make a calibrated choice among named options with a decision model. Use it for closed-set calls "
        "(which option fits, is this blocking, which category) — not for free-text questions."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "short key for the decision, e.g. severity"},
            "state": {"type": "string", "description": "the situation the decision is about"},
            "options": {
                "type": "object",
                "description": "option name -> one-line criterion; at least two",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["question", "state", "options"],
    }

    def __init__(self, client: JevClient, *, ambiguous_gap: float, min_confidence: float) -> None:
        self.client = client
        self.gap = ambiguous_gap
        self.min_conf = min_confidence

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        options = args.get("options")
        if not isinstance(options, dict) or len(options) < 2:
            return ToolResult(False, "error: decide needs at least two options ({name: criterion})")
        criteria = {str(k): str(v) for k, v in options.items()}
        try:
            ans = await self.client.ask(str(args["state"]), str(args["question"]), "Pick the option that fits best.", criteria)
        except (httpx.HTTPError, ValueError) as e:
            return ToolResult(False, f"error: decision engine unavailable: {e}")
        r = [(k, p) for k, p in ranked(ans.probabilities) if k in criteria]
        choice = ans.choice if ans.choice in criteria else (r[0][0] if r else "")
        d = policy(r, ans.confidence, ambiguous_gap=self.gap, min_confidence=self.min_conf)
        out = {"choice": choice, "decision": d, "confidence": ans.confidence, "probabilities": ans.probabilities}
        return ToolResult(True, json.dumps(out))


def attach_decide(
    registry: ToolRegistry, cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Add `decide` when a llama_server engine serves router.model; False (and no tool) otherwise."""
    m = cfg.router.model
    if m is None or m not in cfg.models:
        return False
    try:
        e = cfg.engines[cfg.engine_for_model(m)]
    except KeyError:
        return False
    if e.kind != "llama_server":
        return False
    registry.add(Decide(jev_client(cfg, transport=transport), ambiguous_gap=cfg.router.ambiguous_gap, min_confidence=cfg.router.min_confidence))
    return True
```

Export `Decide`, `attach_decide` from `airlie.tools`. (Wiring `attach_decide` into the team tool set is Plan 2's job, when roles declare tools; here it is a tested, importable seam.)

- [ ] **Step 4: Run the tests and commit**

Run: `uv run pytest tests/unit/test_decide_tool.py -q 2>&1 | tail -3`
Expected: pass.

```bash
task check 2>&1 | tail -3
git add -A src/airlie/tools tests/unit/test_decide_tool.py
git commit -m "feat(tools): decide — calibrated closed-set choices from Kev for any role"
```

---

### Task 9: Routing eval (`task route:eval`) seeded from ai-mode

**Files:**
- Create: `src/airlie/bench/route_eval.py`
- Create: `evals/routing.jsonl` (generated from `~/git/ai-mode/evals/classify*.jsonl`), `evals/routing-roles.yaml`
- Modify: `src/airlie/cli.py` (`route` typer group with `eval`), `Taskfile.yml` (`route:eval`)
- Test: `tests/unit/test_route_eval.py`

**Interfaces:**
- Consumes: `Router`, `build_router`, `RouteDecision` (Task 7).
- Produces: `Case(task: str, expect: str, question: str)`; `load_cases(path) -> list[Case]`; `load_criteria(path) -> dict[str, str]`; `evaluate(router, cases, criteria) -> RoutingReport(n, correct, accuracy, mean_confidence, wrong_but_clear, confusions: dict[str, int], rows: list[dict])`; `render(report) -> str`; `save(report, path)`.

- [ ] **Step 1: Generate the seed files**

```bash
mkdir -p evals && python3 - <<'PY'
import json, pathlib
src = pathlib.Path.home() / "git/ai-mode/evals"
rows = []
for f in ("classify.jsonl", "classify-holdout.jsonl"):
    for line in (src / f).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        r = json.loads(line)
        if r["question"] == "role":
            rows.append({"question": "role", "task": r["task"], "expect": r["expect"]})
pathlib.Path("evals/routing.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
print(len(rows), "cases")
PY
```
Expected: `54 cases`.

`evals/routing-roles.yaml` (ai-mode's `summary:` lines, the criteria Kev sees):
```yaml
# role -> one-line criterion. Plan 2 moves these into prompts/roles/<role>.md `summary:` frontmatter.
architect: "Plan multi-step changes: migrations, system design, ADRs, mapping large codebases"
coder: "Implement, fix or refactor code: features, bug fixes, CLI flags, per an approved plan"
product: "Write user stories, acceptance criteria and PRDs; prioritise features, scope and MVP"
research: "Compare libraries, tools or approaches; investigate APIs and feasibility; competitive analysis; summarise tradeoffs; technical spikes"
docs: "Documentation prose: README, changelog, release notes, guides, API reference, config docs"
qa: "Test strategy: write tests and test cases, edge cases, regression plans, find missing coverage"
security: "Threat model, secure review"
design: "UI and visual critique"
ux: "User flows, usability, journey friction, microcopy, interaction patterns and layout choices"
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_route_eval.py`:

```python
"""route:eval scores a labelled routing set the way ai-mode's classify --eval did."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.bench.route_eval import evaluate, load_cases, load_criteria, render
from airlie.router.protocol import RouteDecision

ROOT = Path(__file__).resolve().parents[2]


class ScriptedRouter:
    def __init__(self, answers: dict[str, tuple[str, str, float]]) -> None:
        self.answers = answers  # task -> (chosen, decision, confidence)

    async def route(self, brief, criteria, hint=None, *, question="role", instructions=""):
        chosen, decision, conf = self.answers[brief]
        return RouteDecision(question, chosen, chosen, decision, {chosen: conf}, conf, hint)


def test_seed_files_parse_and_cover_every_role() -> None:
    cases = load_cases(ROOT / "evals/routing.jsonl")
    criteria = load_criteria(ROOT / "evals/routing-roles.yaml")
    assert len(cases) == 54 and {c.expect for c in cases} == set(criteria)


async def test_evaluate_reports_accuracy_confidence_wrong_but_clear_and_confusions(tmp_path: Path) -> None:
    p = tmp_path / "cases.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"question": "role", "task": "write tests", "expect": "qa"},
        {"question": "role", "task": "fix the bug", "expect": "coder"},
        {"question": "role", "task": "write the README", "expect": "docs"},
    ]))
    router = ScriptedRouter({"write tests": ("qa", "clear", 0.9), "fix the bug": ("qa", "clear", 0.8), "write the README": ("coder", "ambiguous", 0.4)})
    rep = await evaluate(router, load_cases(p), {"qa": "t", "coder": "c", "docs": "d"})
    assert rep.n == 3 and rep.correct == 1 and abs(rep.accuracy - 1 / 3) < 1e-9
    assert abs(rep.mean_confidence - 0.7) < 1e-9
    assert rep.wrong_but_clear == 1
    assert rep.confusions == {"coder->qa": 1, "docs->coder": 1}
    text = render(rep)
    assert "accuracy 0.33" in text and "wrong-but-clear 1" in text and "docs->coder" in text
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/unit/test_route_eval.py -q 2>&1 | tail -3`
Expected: ImportError `airlie.bench.route_eval`.

- [ ] **Step 4: Implement**

`src/airlie/bench/route_eval.py`:

```python
"""route:eval — accuracy, mean confidence, wrong-but-clear and confusions over a labelled routing set."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from airlie.router.protocol import Router


@dataclass(frozen=True)
class Case:
    task: str
    expect: str
    question: str = "role"


@dataclass
class RoutingReport:
    n: int
    correct: int
    accuracy: float
    mean_confidence: float
    wrong_but_clear: int
    confusions: dict[str, int]
    rows: list[dict[str, Any]] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    out: list[Case] = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        r = json.loads(line)
        out.append(Case(str(r["task"]), str(r["expect"]), str(r.get("question", "role"))))
    return out


def load_criteria(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text()) or {}
    return {str(k): str(v) for k, v in data.items()}


async def evaluate(router: Router, cases: list[Case], criteria: dict[str, str]) -> RoutingReport:
    rows: list[dict[str, Any]] = []
    correct = 0
    conf_sum = 0.0
    wrong_clear = 0
    confusions: Counter[str] = Counter()
    for c in cases:
        d = await router.route(c.task, criteria, None, question=c.question)
        ok = d.chosen == c.expect
        correct += int(ok)
        conf_sum += d.confidence
        if not ok:
            confusions[f"{c.expect}->{d.chosen}"] += 1
            if d.decision == "clear":
                wrong_clear += 1
        rows.append({"task": c.task, "expect": c.expect, "chosen": d.chosen, "decision": d.decision, "confidence": d.confidence, "ok": ok})
    n = len(cases)
    return RoutingReport(n, correct, correct / n if n else 0.0, conf_sum / n if n else 0.0, wrong_clear, dict(confusions), rows)


def render(rep: RoutingReport) -> str:
    lines = [
        f"routing eval: {rep.correct}/{rep.n} accuracy {rep.accuracy:.2f} mean-confidence {rep.mean_confidence:.2f} "
        f"wrong-but-clear {rep.wrong_but_clear}",
    ]
    for pair, k in sorted(rep.confusions.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  confusion {pair} x{k}")
    for r in rep.rows:
        if not r["ok"]:
            lines.append(f"  miss [{r['decision']} {r['confidence']:.2f}] {r['expect']} -> {r['chosen']}: {r['task'][:80]}")
    return "\n".join(lines)


def save(rep: RoutingReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(rep), indent=1))
    return path
```

`cli.py`:

```python
route_app = typer.Typer(no_args_is_help=True, help="Routing (Kev / chief) decisions and their eval.")
app.add_typer(route_app, name="route")


@route_app.command("eval")
def route_eval(
    cases: Annotated[Path, typer.Option(help="labelled JSONL: question, task, expect")] = Path("evals/routing.jsonl"),
    roles: Annotated[Path, typer.Option(help="role -> criterion YAML")] = Path("evals/routing-roles.yaml"),
    min_accuracy: Annotated[float, typer.Option(help="exit 1 below this")] = 0.0,
    out: Annotated[Path | None, typer.Option(help="save the JSON report here")] = None,
    config: ConfigOpt = None,
) -> None:
    """Score the router on a labelled set (accuracy, mean confidence, wrong-but-clear, confusions)."""
    import asyncio

    from airlie.bench.route_eval import evaluate, load_cases, load_criteria, render, save
    from airlie.router import build_router

    cfg = _load(config)
    if cfg.router.kind != "jev":
        err.print("[red]router.kind must be jev to evaluate routing (chief routing has nothing to score)[/red]")
        raise typer.Exit(2)
    rep = asyncio.run(evaluate(build_router(cfg), load_cases(cases), load_criteria(roles)))
    console.print(render(rep), markup=False, highlight=False)
    if out is not None:
        console.print(f"saved {save(rep, out)}")
    if rep.accuracy < min_accuracy:
        raise typer.Exit(1)
```

`Taskfile.yml`:

```yaml
  route:eval:
    desc: "Score the Kev router on evals/routing.jsonl (task route:eval -- --min-accuracy 0.9 --out docs/bench/routing.json)"
    cmds:
      - '{{.RUN}} airlie route eval {{.CLI_ARGS}}'
```

- [ ] **Step 5: Run the tests and commit**

Run: `uv run pytest tests/unit/test_route_eval.py -q 2>&1 | tail -3`
Expected: pass.

```bash
task check 2>&1 | tail -3
git add -A src evals Taskfile.yml tests/unit/test_route_eval.py
git commit -m "feat(bench): task route:eval — routing accuracy/confidence/confusions on the ai-mode seed set"
```

---

### Task 10: Config wiring, live checks, docs

**Files:**
- Modify: `airlie.yaml` (models `generalist`, `reviewer`, `kev4b`; engine `decision`; `router`; `topology.profiles`)
- Modify: `README.md` (engines, profiles, `task route:eval`), `docs/bench/README.md` (parser note)
- Create: `tests/live/test_router_live.py`, `tests/live/test_parsers_live.py`
- Modify: `docs/superpowers/specs/2026-10-13-studio-topology-design.md` (§13: record the three rulings above)

**Interfaces:** consumes everything above; produces the config the studio plan (Plan 2) extends with its `studio` profile and roles.

- [ ] **Step 1: airlie.yaml**

Add to `models:`:

```yaml
  generalist:                      # Qwen3.8-27B: chief + vision in the studio design; hybrid attention (no prefix cache)
    repo: mlx-community/Qwen3.8-27B-4bit
    revision: 10c35caafbb80f7dc6a7a432cdd11af10a6d4818   # 2026-09-14
    preload: false
    estimated_memory_gb: 16.1
    kv_mb_per_1k_tokens: 256
    tool_call_parser: qwen
    reasoning_parser: qwen3
    tags: [modern, vision, hybrid-attention]
  reviewer:                        # Muse Glimmer 30B (Meta, Apache 2.0): the cross-family adversarial reviewer
    repo: mlx-community/Muse-Glimmer-30B-4bit
    revision: 3e7677d7a40d348a3daba263a2b1c0aa41910710   # 2026-08-10
    preload: false
    estimated_memory_gb: 19.4
    kv_mb_per_1k_tokens: 64        # 2 KV heads × 128 dim × 52 layers, 3/4 of them sliding-window
    tool_call_parser: muse_glimmer
    reasoning_parser: muse_glimmer
    tags: [modern, vision, reviewer]
  kev4b:                           # System One decision model behind the Jev API (router, decide tool)
    repo: ggml-org/Kev-4B-GGUF
    revision: d924f2e2c3872da8b8aaf3eb4453b4126deceb79
    file: Kev-4B-Q4_K_M.gguf
    preload: true
    estimated_memory_gb: 3
```

Add `generalist` and `reviewer` to `engines.main.models`; add the engine and router:

```yaml
  decision:                        # llama-server serving Kev; POST /v1/systemone (needs llama.cpp ≥ 0.6.0 with the endpoint)
    kind: llama_server
    host: 127.0.0.1
    port: 8081
    models: [kev4b]
    ctx_size: 16384

router: { kind: jev, model: kev4b, ambiguous_gap: 0.15, min_confidence: 0.5 }   # jev | chief (design §6)
```

Replace `topology: { kind: teams }` with:

```yaml
topology:
  kind: single                     # single | teams | fork; each selects its profile below (task run -- --topology X)
  profiles:
    single: { teams: { k: 1 }, lts: { enabled: false } }
    teams:  { teams: { k: 3 }, lts: { enabled: true } }
    fork:   { teams: { k: 1 }, lts: { enabled: false } }
```

(`teams.k: 1` and `lts.enabled: false` in the base block stay as the defaults for `config show`; the profiles make each kind explicit. Context sizes per profile wait for Plan 2's studio profile, where the memory math is decided together.)

Run: `uv run airlie config show | head -5 && uv run airlie doctor`
Expected: config loads; doctor shows `engine:decision:binary ok`, `engine:decision:systemone ok`, `model:kev4b:file fail … task models:pull -- kev4b`, `model:generalist ok` (on disk), `model:reviewer warn` (lazy, not on disk), three `memory_projection[...]` rows ok.

- [ ] **Step 2: Pull Kev (ask Noah first: 3.03 GB from ggml-org/Kev-4B-GGUF), start the decision engine, run the live checks**

```bash
task models:pull -- kev4b
task up
uv run airlie doctor | grep -E "decision|kev4b"
```
Expected: `task up` starts `main` (already running → "already running (pid …); use restart" is fine for main: run `uv run airlie engine up` only after `task down` if needed, or start just the new engine with `task restart`) and `decision`; doctor rows for the decision engine all `ok`.

`tests/live/test_router_live.py`:

```python
"""Live: Kev answers a closed-set question with probabilities over exactly the offered options."""

import pytest

from airlie.config import load_config
from airlie.router import jev_client

pytestmark = pytest.mark.live


async def test_kev_routes_a_test_task_to_qa() -> None:
    cfg = load_config()
    client = jev_client(cfg)
    criteria = {"coder": "Implement or change code", "qa": "Write tests, edge cases, regression plans", "docs": "README and guides"}
    ans = await client.ask("Add edge-case tests for login rate limiting", "role", "Which specialist should handle this task?", criteria)
    assert set(ans.probabilities) == set(criteria)
    assert ans.choice == "qa" and ans.confidence > 0.5
    await client.aclose()
```

`tests/live/test_parsers_live.py`:

```python
"""Live: the coder model's raw tool-call text parses on the client the same way the server parses it."""

import pytest

from airlie.cli import _http_engine
from airlie.config import load_config
from airlie.engine import GenRequest, Message, ToolSchema

pytestmark = pytest.mark.live


async def test_coder_tool_call_round_trip_through_parse_output() -> None:
    cfg = load_config()
    eng = _http_engine(cfg, cfg.engine_for_role("coder"))
    tool = ToolSchema("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    req = GenRequest(
        role="coder",
        messages=[Message(role="user", content="Read app.py with the tool. Do not answer in prose.")],
        sampling=cfg.roles["coder"].sampling,
        max_tokens=200,
        tools=[tool],
        thinking=False,
    )
    res = await eng.generate(req)
    assert res.finish_reason == "tool_calls" and res.tool_calls[0].name == "read_file"
    await eng.stop()
```

Run: `task test:live -- tests/live/test_router_live.py tests/live/test_parsers_live.py -q 2>&1 | tail -3`
Expected: 2 passed.

- [ ] **Step 3: Routing eval on the seed set**

```bash
task route:eval -- --out docs/bench/routing.json
```
Expected: prints accuracy over 54 cases; the design's bar is ≥ 0.9. Record the number in `docs/bench/README.md` under a "Routing (Kev-4B)" heading with the date and the confusions. **If it is below 0.9, do not change the bar or drop Kev: report the misses to Noah (memory: a missed criterion is his call) and continue; the criteria lines are the tuning knob, as they were in ai-mode.**

- [ ] **Step 4: Docs**

README: an "Engines" paragraph (vllm-mlx `main` + llama-server `decision`), "Topology profiles" paragraph (`--topology` selects a profile; engine settings in a profile need `task restart`), `task route:eval` in the task table. `docs/bench/README.md`: the 2026-10-13 live check that tools render without the server parser flag and that the generic server fallback parses hermes-style JSON calls. Design doc §13: append the three planning rulings verbatim.

- [ ] **Step 5: Full check and commit**

```bash
task check 2>&1 | tail -3
git add -A
git commit -m "feat(config): decision engine (Kev on llama-server), router, generalist/reviewer catalog entries, topology profiles; live checks; docs"
```

---

## Self-review

- **Spec coverage.** §3 catalog entries (Task 10); §4a Muse format (Task 5 parsers; the review loop itself is Plan 2); §4b parsers + `server_parsers` (Tasks 4, 5); §6 router, policy, log, `route:eval` (Tasks 7, 9); §8 profiles + per-profile doctor (Tasks 1, 3); §9 keys (Task 1; `RoleSpec.tools/skills/knowledge/vision` and `StudioSpec` are Plan 2 with the code that reads them); §10 rows for engine/registry/router/parsers (all here; `topology/studio.py`, skills, prompts, bench are Plan 2); §11 unit tests for profiles, `JevRouter` cases, `ChiefRouter`, parser fixtures incl. the precedence rule, registry argv without flags, content parts (all here); §14 `decide` (Task 8). Vision input on the native loop (`Message.parts`) is here; a role flag `vision` arrives with the roles in Plan 2.
- **Placeholders.** None: every step has its code or exact command. Task 2's three test snippets name helpers from their files (`StubOrch`, `CFG_PATH`) that the executor adapts to the existing names in those files; the assertions are complete.
- **Type consistency.** `load_config(..., topology=)` (T1) used by T2/T3; `profile_configs` (T1) by T3; `build_llama_command`/`gguf_path` (T4) by T4's process change; `parse_output` (T5) by `HTTPEngine` (T5) and the live test (T10); `JevClient.ask` signature (T7) by T8/T9/T10; `RouteDecision.chosen/decision` (T7) by T9; `policy`/`ranked` (T7) by T8.
- **Review Focus.** 1 → T5 `test_muse_tool_channel_without_user_block`; 2 → T5 `test_server_parsed_calls_win`; 3 → T7 `test_choice_outside_closed_set_falls_back`; 4 → T3 `test_llama_server_without_systemone_fails`; 5 → T1 `test_profile_extends_cycle_is_a_config_error`.
