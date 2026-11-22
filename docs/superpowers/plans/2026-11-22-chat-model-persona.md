# Chat model + persona + airlie allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real upstream model ids in the catalog, an Admin allowlist that gates which live models airlie may use (Chat and workflows), Chat model+persona dropdowns with thread persistence, and a prefix-cache-friendly system prompt order.

**Architecture:** Committed catalog/role defaults use omlx ids. Serve discovers live ids from the workflow engine’s `GET …/models`; Admin toggles **Allowed in airlie** in sqlite under `state_dir`. Chat threads store `model` + `persona`; turns build system prompts as platform → kind → persona → skill and generate with the selected allowed model. Composer and Admin UI expose the choices.

**Tech Stack:** Python 3.12, FastAPI/httpx, sqlite, Vue 3 + Vitest, existing chat turn route and Admin screen patterns.

## Global Constraints

- Spec: [`docs/superpowers/specs/2026-11-22-chat-model-persona-design.md`](../specs/2026-11-22-chat-model-persona-design.md)
- All new tests adhere to [`skills/qa/`](../../../skills/qa/) by layer; mocks/fakes at boundaries; **no real agents/models**; **no real clocks/timers** in the default gate
- Test gate (from repo root, in order): `uv sync --frozen` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run mypy` → `uv run pytest -q -n auto --maxprocesses 8` → `task web:gate`
- Starting catalog ids (role defaults): `Qwen3.8-Flash-Next-oQ4e-mtp`, `mlx-community--Qwen3.8-27B-4bit`, `Qwen3.5-0.8B-oQ8e-mtp`
- Allowlist default for an unseen id: **allowed**
- Prompt order: platform → kind → persona → per-turn skill
- Tools stay on `chat.kinds.*`, not on the persona role’s `tools:`

---

## File map

| File | Responsibility |
| --- | --- |
| `config/models.yaml`, `config/roles.yaml`, `airlie.yaml` | Real catalog ids; role defaults; engine `models` list; empty `passthru` |
| `src/airlie/models/allowlist.py` | Sqlite allowlist + merge with live ids |
| `src/airlie/models/live.py` | Fetch upstream `/v1/models` ids (injectable httpx) |
| `src/airlie/models/api.py` | Admin `GET/PATCH` models routes |
| `src/airlie/chat/threads.py` | `model` / `persona` columns on threads |
| `src/airlie/chat/hooks.py` | `build_system_prompt` order + persona |
| `src/airlie/chat/api.py` | options, PATCH thread, turn uses thread settings |
| `src/airlie/serve/app.py` | Mount admin models; filter `/v1/models` catalog; wire allowlist |
| `web/src/api/models.ts` | Admin + chat options clients |
| `web/src/screens/AdminScreen.vue` | Models table + toggles |
| `web/src/screens/chat/Composer.vue` | Model + persona dropdowns |

---

### Task 1: Catalog ids = upstream names

**Files:**
- Modify: `config/models.yaml`
- Modify: `config/roles.yaml` (every `model: fast|smart|small`)
- Modify: `airlie.yaml` (`engines.workflow.models`)
- Modify: any unit fixture that asserts those three names (grep `fast`/`smart`/`small` under `config/` and committed catalog tests)
- Test: `tests/unit/test_config.py` (load real includes or a minimal yaml with the new ids)

**Interfaces:**
- Produces: catalog keys `Qwen3.8-Flash-Next-oQ4e-mtp`, `mlx-community--Qwen3.8-27B-4bit`, `Qwen3.5-0.8B-oQ8e-mtp`; roles’ `model` fields reference them; `passthru: {}`

- [ ] **Step 1: Write the failing test**

```python
def test_committed_catalog_uses_upstream_ids() -> None:
    from airlie.config import load_config
    cfg = load_config()  # repo root airlie.yaml
    assert set(cfg.models) >= {
        "Qwen3.8-Flash-Next-oQ4e-mtp",
        "mlx-community--Qwen3.8-27B-4bit",
        "Qwen3.5-0.8B-oQ8e-mtp",
    }
    assert "fast" not in cfg.models
    assert cfg.roles["chat"].model == "Qwen3.8-Flash-Next-oQ4e-mtp"
    assert cfg.roles["architect"].model == "mlx-community--Qwen3.8-27B-4bit"
    assert cfg.engines["workflow"].passthru == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py::test_committed_catalog_uses_upstream_ids -v`  
Expected: FAIL (`fast` still present or assertion on chat model)

- [ ] **Step 3: Update committed config**

`config/models.yaml`:

```yaml
models:
  Qwen3.5-0.8B-oQ8e-mtp:
    label: Qwen3.5 0.8B
  mlx-community--Qwen3.8-27B-4bit:
    label: Qwen3.8 27B
  Qwen3.8-Flash-Next-oQ4e-mtp:
    label: Qwen3.8 Flash
```

`airlie.yaml` engine models list those three keys. Replace every role `model: small|smart|fast` with the matching id (`small`→`Qwen3.5-0.8B-oQ8e-mtp`, `smart`→`mlx-community--Qwen3.8-27B-4bit`, `fast`→`Qwen3.8-Flash-Next-oQ4e-mtp`).

On Mac Studio only (not this commit): drop `passthru` aliases from `airlie.local.yaml` once names match (operator note in commit body).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py::test_committed_catalog_uses_upstream_ids -v`  
Expected: PASS. Fix any other tests that hard-coded `fast`/`smart`/`small` as catalog ids.

- [ ] **Step 5: Commit**

```bash
git add config/models.yaml config/roles.yaml airlie.yaml tests/unit/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): catalog models use upstream omlx ids

EOF
)"
```

---

### Task 2: Model allowlist store

**Files:**
- Create: `src/airlie/models/__init__.py`
- Create: `src/airlie/models/allowlist.py`
- Test: `tests/unit/test_model_allowlist.py`

**Interfaces:**
- Produces:
  - `ModelRow(id: str, allowed: bool, label: str | None)`
  - `class ModelAllowlist`:
    - `__init__(self, path: Path) -> None`
    - `is_allowed(self, model_id: str) -> bool`  # True if no row
    - `set_allowed(self, model_id: str, allowed: bool) -> None`
    - `merge(self, live_ids: list[str], labels: Mapping[str, str] | None = None) -> list[ModelRow]`

- [ ] **Step 1: Write the failing tests**

```python
def test_unseen_id_is_allowed(tmp_path: Path) -> None:
    store = ModelAllowlist(tmp_path / "models.sqlite")
    assert store.is_allowed("Qwen3.8-Flash-Next-oQ4e-mtp") is True

def test_set_allowed_persists(tmp_path: Path) -> None:
    path = tmp_path / "models.sqlite"
    ModelAllowlist(path).set_allowed("tts", False)
    assert ModelAllowlist(path).is_allowed("tts") is False

def test_merge_lists_live_ids_with_flags(tmp_path: Path) -> None:
    store = ModelAllowlist(tmp_path / "models.sqlite")
    store.set_allowed("video", False)
    rows = store.merge(["chat-model", "video"], labels={"chat-model": "Chat"})
    assert [(r.id, r.allowed, r.label) for r in rows] == [
        ("chat-model", True, "Chat"),
        ("video", False, None),
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_model_allowlist.py -v`  
Expected: FAIL (import / class missing)

- [ ] **Step 3: Implement allowlist**

Sqlite schema: `allowlist(id text primary key, allowed integer not null)`. `is_allowed` returns True when no row. `merge` returns one `ModelRow` per live id in live order.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_model_allowlist.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/models/ tests/unit/test_model_allowlist.py
git commit -m "$(cat <<'EOF'
feat(models): sqlite allowlist for airlie-wide model gates

EOF
)"
```

---

### Task 3: Live upstream model ids

**Files:**
- Create: `src/airlie/models/live.py`
- Test: `tests/unit/test_model_live.py`

**Interfaces:**
- Consumes: `EngineSpec.base_url`, `EngineSpec.api_key`
- Produces: `async def fetch_live_model_ids(base_url: str, api_key: str | None, *, http: httpx.AsyncClient) -> list[str]`  
  Parses OpenAI-style `{data:[{id:...}]}`; on transport/HTTP error raises `LiveModelsError` with a clear message (tests use `httpx.MockTransport`).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_fetch_live_model_ids_reads_openai_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as http:
        ids = await fetch_live_model_ids("http://engine/v1", "key", http=http)
    assert ids == ["a", "b"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_model_live.py::test_fetch_live_model_ids_reads_openai_list -v`  
Expected: FAIL

- [ ] **Step 3: Implement `fetch_live_model_ids`**

GET `{base_url.rstrip('/')}/models` with optional Bearer; return ids in response order.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_model_live.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/models/live.py tests/unit/test_model_live.py
git commit -m "$(cat <<'EOF'
feat(models): fetch live upstream model ids over HTTP

EOF
)"
```

---

### Task 4: Admin models API

**Files:**
- Create: `src/airlie/models/api.py`
- Modify: `src/airlie/serve/app.py` (mount router; construct `ModelAllowlist` at `state_dir/models.sqlite`)
- Test: `tests/functional/test_models_admin.py` (TestClient + faked live fetch)

**Interfaces:**
- Produces:
  - `GET /v1/airlie/models` → `[{id, label, allowed, live}]` (admins only; `live` true when in latest fetch)
  - `PATCH /v1/airlie/models/{id}` body `{allowed: bool}` → 204 (admins only; upserts allowlist row)
  - Non-admin → 403
- Consumes: `ModelAllowlist`, `fetch_live_model_ids` (inject a `live_ids: Callable` in tests)

- [ ] **Step 1: Write the failing functional tests**

```python
def test_member_cannot_list_models(client_as_member):
    assert client_as_member.get("/v1/airlie/models").status_code == 403

def test_admin_lists_live_and_toggles(client_as_admin, monkeypatch):
    monkeypatch.setattr(..., lambda *a, **k: ["keep", "drop"])
    rows = client_as_admin.get("/v1/airlie/models").json()
    assert {r["id"] for r in rows} >= {"keep", "drop"}
    assert client_as_admin.patch("/v1/airlie/models/drop", json={"allowed": False}).status_code == 204
    again = {r["id"]: r["allowed"] for r in client_as_admin.get("/v1/airlie/models").json()}
    assert again["drop"] is False
    assert again["keep"] is True
```

(Follow existing functional auth fixtures in `tests/functional/`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/functional/test_models_admin.py -v`  
Expected: FAIL (404)

- [ ] **Step 3: Implement router + mount**

Mirror `_admin(request)` from `src/airlie/auth/api.py`. On list failure of live fetch, still return allowlist rows with `live: false` and an optional top-level error only if the project already uses that pattern; otherwise 503 with a clear body — prefer 200 + `live: false` so Admin can still toggle offline.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/functional/test_models_admin.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/models/api.py src/airlie/serve/app.py tests/functional/test_models_admin.py
git commit -m "$(cat <<'EOF'
feat(admin): Allowed in airlie model toggles API

EOF
)"
```

---

### Task 5: Gate serve catalog + role resolution on allowlist

**Files:**
- Modify: `src/airlie/serve/app.py` (`GET /v1/models`, `upstream()` for raw catalog ids)
- Modify: helper used by chat options (Task 6) — add `src/airlie/models/allowed.py`: `def allowed_ids(allowlist, live_ids) -> list[str]`
- Test: `tests/unit/test_serve.py` or functional serve tests

**Interfaces:**
- Produces: raw catalog entries on `GET /v1/models` only for allowed∩(cfg.models ∪ live); disallowed raw `model` on completions → 404 with unknown-for-airlie wording
- Role names remain listed; a role whose default model is disallowed still appears as a role, but completions that need that default fail with a clear 503/400 (assert one behaviour and stick to it: **400** `"model '<id>' is not allowed in airlie"`)

- [ ] **Step 1: Write the failing test**

```python
def test_disallowed_raw_catalog_model_is_not_served(tmp_path, allowlist_with_drop_disabled):
    # client posts /v1/chat/completions model=<disallowed id>
    # expect 400 and message containing "not allowed in airlie"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_serve.py -k disallowed_raw -v`  
Expected: FAIL

- [ ] **Step 3: Implement gate in `upstream()` and `/v1/models`**

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/serve/app.py src/airlie/models/allowed.py tests/unit/test_serve.py
git commit -m "$(cat <<'EOF'
feat(serve): omit and refuse models not allowed in airlie

EOF
)"
```

---

### Task 6: Thread `model` + `persona` + chat options/PATCH

**Files:**
- Modify: `src/airlie/chat/threads.py` (columns `model`, `persona`; create/load/patch)
- Modify: `src/airlie/chat/api.py` (`GET /v1/airlie/chat/options`, `PATCH .../threads/{id}`, create seeds, status fields)
- Test: `tests/functional/test_chat_sessions.py` (extend) or `tests/functional/test_chat_options.py`

**Interfaces:**
- Produces:
  - Thread fields `model: str`, `persona: str`
  - Create seeds: `persona="chat"`, `model=cfg.roles["chat"].model` if allowed else first `allowed_ids(...)`
  - `GET /v1/airlie/chat/options` → `{models:[{id,label}], personas:[{id,label}]}`
  - `PATCH /v1/airlie/chat/threads/{id}` body `{model?, persona?}` → thread JSON; validate against allowed models + known roles

- [ ] **Step 1: Write the failing tests**

```python
def test_new_thread_seeds_chat_persona_and_default_model(c):
    t = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()
    assert t["persona"] == "chat"
    assert t["model"]  # equals chat role default when allowed

def test_options_lists_allowed_models_and_roles(c):
    body = c.get("/v1/airlie/chat/options").json()
    assert "models" in body and "personas" in body
    assert any(p["id"] == "chat" for p in body["personas"])

def test_patch_refuses_disallowed_model(c):
    t = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()
    r = c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "not-allowed"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Migrate schema + implement routes**

`alter table threads add column model text` / `persona text` (same pattern as `repo`). Backfill nulls on read: persona `chat`, model from role default.

- [ ] **Step 4: Run tests to verify they pass**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/threads.py src/airlie/chat/api.py tests/functional/test_chat_*.py
git commit -m "$(cat <<'EOF'
feat(chat): thread model and persona with options and PATCH

EOF
)"
```

---

### Task 7: System prompt order + turn uses thread settings

**Files:**
- Modify: `src/airlie/chat/hooks.py` (`build_system_prompt`)
- Modify: `src/airlie/chat/api.py` / turn wiring (persona prompt, selected model for HTTPEngine / sampling)
- Test: `tests/unit/test_chat_hooks.py` (create if missing), functional turn test with mocked engine

**Interfaces:**
- Produces: `build_system_prompt(..., *, persona: str, skill: str | None = None) -> str`  
  Order of sections: (1) superpowers bootstrap+listing if enabled (2) kind note (3) `prompt_text(cfg.roles[persona].prompt)` (4) slash skill body  
  Note: platform before kind before persona — even if platform is empty when superpowers off, kind still precedes persona.
- Turn: read thread.model / thread.persona; optional body fields apply via same validation as PATCH before run; tools from kind; sampling from persona role; upstream model = thread.model

- [ ] **Step 1: Write the failing unit test for order**

```python
def test_system_prompt_order_platform_kind_persona_skill(tmp_path, cfg_with_superpowers):
    text = build_system_prompt(cfg, "plain", library, chat_skills, persona="chat", skill="brainstorming")
    i_boot = text.index("## Skills")  # or bootstrap marker
    i_kind = text.index("plain chat")  # PLAIN_NOTE fragment
    i_persona = text.index("You are airlie's assistant")  # chat.md
    i_skill = text.index("## Skill: /brainstorming")
    assert i_boot < i_kind < i_persona < i_skill
```

Adjust markers to match real strings in `PLAIN_NOTE` / bootstrap.

- [ ] **Step 2: Run test to verify it fails** (current order is persona-first)

- [ ] **Step 3: Reorder `build_system_prompt` and wire turn**

- [ ] **Step 4: Run unit + a functional turn test with httpx mock proving selected model id is forwarded**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/hooks.py src/airlie/chat/api.py tests/unit/test_chat_hooks.py tests/functional/test_chat_turn*.py
git commit -m "$(cat <<'EOF'
feat(chat): prefix-cache prompt order and per-thread model/persona turns

EOF
)"
```

---

### Task 8: Composer model + persona dropdowns

**Files:**
- Create: `web/src/api/chat_options.ts` (or extend `web/src/api/sessions.ts`)
- Modify: `web/src/screens/chat/Composer.vue`
- Modify: `web/src/stores/sessionInfo.ts` (expose persona + model; load options)
- Test: `web/src/screens/chat/Composer.test.ts`

**Interfaces:**
- Consumes: `GET /v1/airlie/chat/options`, `PATCH /v1/airlie/chat/threads/{id}`
- Produces: two `<select>` (or accessible listboxes) in the composer footer; changing either PATCHes the thread; footer no longer a read-only chip only

- [ ] **Step 1: Write the failing component test**

```ts
test("the person can choose a model and persona", async () => {
  // mock options + patch
  const { w } = await open();
  await w.get("[data-model-select]").setValue("mlx-community--Qwen3.8-27B-4bit");
  await w.get("[data-persona-select]").setValue("architect");
  expect(patchMock).toHaveBeenCalledWith(
    expect.objectContaining({ model: "mlx-community--Qwen3.8-27B-4bit", persona: "architect" }),
  );
});
```

- [ ] **Step 2: Run `npm test` for Composer (via `task web:gate` subset or vitest path) — expect FAIL**

- [ ] **Step 3: Implement dropdowns + API client**

- [ ] **Step 4: Run Composer tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add web/src/api/ web/src/screens/chat/Composer.vue web/src/screens/chat/Composer.test.ts web/src/stores/sessionInfo.ts
git commit -m "$(cat <<'EOF'
feat(web): chat model and persona dropdowns

EOF
)"
```

---

### Task 9: Admin Models UI

**Files:**
- Create: `web/src/api/models.ts`
- Modify: `web/src/screens/AdminScreen.vue`
- Test: `web/src/screens/AdminScreen.test.ts`

**Interfaces:**
- Consumes: `GET /v1/airlie/models`, `PATCH /v1/airlie/models/{id}`
- Produces: Admin section **Models** with id, label, live badge, toggle **Allowed in airlie**

- [ ] **Step 1: Failing test** — Models section renders rows; toggling calls PATCH

- [ ] **Step 2: Run AdminScreen tests — expect FAIL**

- [ ] **Step 3: Implement section** (same card layout language as Engines)

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add web/src/api/models.ts web/src/screens/AdminScreen.vue web/src/screens/AdminScreen.test.ts
git commit -m "$(cat <<'EOF'
feat(web): Admin Allowed in airlie model toggles

EOF
)"
```

---

### Task 10: Docs + gate

**Files:**
- Modify: `README.md` (Chat turn / Admin models blurb)
- Modify: spec link from board-run-dispatcher Task 9 if needed (optional one-liner)
- Run full gate

- [ ] **Step 1: Update README Chat section** — model/persona dropdowns; Admin allowlist; catalog = upstream ids

- [ ] **Step 2: Run full test gate**

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

Expected: all green

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-22-chat-model-persona-design.md
git commit -m "$(cat <<'EOF'
docs: chat model/persona and airlie model allowlist

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| Catalog = upstream ids; empty passthru when match | 1 |
| Live `/v1/models` discovery | 3 |
| Admin **Allowed in airlie** (sqlite, default on) | 2, 4, 9 |
| Allowed ⇒ Chat + workflows + serve catalog | 5, 6, 8 |
| Thread model/persona; options; PATCH; turn | 6, 7 |
| Prompt order platform → kind → persona → skill | 7 |
| Composer dropdowns | 8 |
| Tools from kind not persona | 7 (assert in turn test) |
| skills/qa; no real models/clocks | all test steps |
| Tool/mode dropdowns | out of scope |

## Placeholder / consistency notes

- Use the three catalog ids verbatim everywhere; do not reintroduce `fast`/`smart`/`small`.
- Admin copy is **Allowed in airlie**, not “Available in Chat”.
- Functional tests fake live fetch; never call real omlx.
