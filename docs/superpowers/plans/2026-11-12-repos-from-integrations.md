# Repositories from an integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The chat panel's repositories come from each person's own GitHub account (linked by the GitHub App's device flow), plus manual extras for local-only checkouts; fetch, push and PRs stay as the App's bot and are allowed only for repositories in that person's list.

**Architecture:** A `RepoSource` protocol (`src/airlie/sources/`) with one implementation, `GitHubSource`, over the App (device flow, user-token listing, installation-token publisher). A `TokenVault` encrypts the user tokens kept in the user store's new `source_links` table. `RepoAccess` turns a person into their repository list (cached, token refreshed) and answers `access(person, repo)`, which the chat routes, the pre-warm, the mirror and the push tools ask. The Vue panel reads `GET /v1/airlie/chat/repositories` and shows "Connect GitHub" while a person is not linked.

**Tech Stack:** Python 3.12, FastAPI, httpx, `cryptography` (AESGCM), SQLite (user store); Vue 3.5, Pinia, vitest 5; Playwright e2e.

**Spec:** [`docs/superpowers/specs/2026-11-12-repos-from-integrations-design.md`](../specs/2026-11-12-repos-from-integrations-design.md)

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Tests follow `skills/qa/*.md`: GitHub is faked at the HTTP seam only (`httpx.MockTransport` over `tests/support/github_user.py`'s `FakeGitHubUser`; no real request: the autouse guard of `tests/unit/test_github_app.py` is copied to every new GitHub test module); a route goes through `TestClient` in `tests/functional` with its happy path, 401, the 404 and 403 the task names, 422 for a bad body and a dependency failure, asserting the body; web tests follow `component.md` (real component tree, role/text queries, `web/src/test-utils/fetch-stub.ts`, fake timers, no sleeps); one e2e (Task 7). Fakes and clocks are rebuilt per test; every test is seen red first; a unit test over 500 ms or on the real clock goes in `tests/unit/quarantine.toml`.
- Device flow, verbatim: `POST {oauth_url}/login/device/code` `{client_id}`; poll `POST {oauth_url}/login/oauth/access_token` `{client_id, device_code, grant_type: "urn:ietf:params:oauth:grant-type:device_code"}`; refresh the same URL with `{client_id, client_secret, grant_type: "refresh_token", refresh_token}`; both with `Accept: application/json`. Poll errors: `authorization_pending`, `slow_down` (new `interval`), `expired_token`, `access_denied`, `incorrect_device_code`, `device_flow_disabled`; refresh errors: `bad_refresh_token`.
- Listing, verbatim: `GET {api_url}/user`, `GET {api_url}/user/installations?per_page=100`, `GET {api_url}/user/installations/{id}/repositories?per_page=100`, following the `Link: rel="next"` header. Revoke: `DELETE {api_url}/applications/{client_id}/grant` with basic auth `client_id:client_secret` and body `{"access_token": …}`.
- The user token is used for those three reads and nothing else; every write uses an installation token (`GitHubApp.token`).
- No token, refresh token, device code, client secret or vault key in a log line, an event, an error message, a `repr`, a reply body or the browser. Errors from GitHub are masked as `GitHubApp._said` does.
- `oauth_url` and `api_url` are https://, or http:// to this machine only (`github_app.api_base`'s rule).
- Refresh when the access token has less than `REFRESH_MARGIN_S = 300` seconds left; one refresh per `(user_id, source)` at a time.
- Listing cache: `chat.repo_list_cache_s`, default 300. Push and PR always ask with `fresh=True`.
- Every route answers 401 `{"error": "not signed in"}` without a principal. Copy fixed here: `"Connect GitHub"`, `"Refresh repositories"`, `"Disconnect GitHub"`, `"you have no access to {repo}: connect GitHub, or ask for access to it on GitHub"`, `"{repo} comes from GitHub: it shows when your GitHub account can see it"`, `"the code expired: start again"`, `"you declined on GitHub"`, `"GitHub ended airlie's access: connect again"`, `"you cannot push to {repo} on GitHub, so airlie will not push for you"`, `"{repo} is a local checkout: airlie does not push it"`.
- `/v1/airlie/repos/names` keeps its answer (the board uses it).
- Dependencies through uv (`uv add cryptography`, exact as the lock has it); npm pins exact. No Docker.

## Review Focus

1. **Two requests refresh one person's token at once** (the panel's list and a turn's access check). Expected: one refresh call to GitHub, both use its result; the old refresh token is never sent twice. Test in Task 3 (`test_concurrent_views_refresh_the_token_once`).
2. **The person revokes the App on GitHub, or the refresh token has expired.** Expected: the link is dropped, the panel shows "Connect GitHub" with "GitHub ended airlie's access: connect again", their repository chats stay listed and a turn in one answers 403. Test in Task 3 (`test_a_refused_token_drops_the_link_with_the_reason`) and Task 5 (`test_a_turn_after_access_was_lost_is_403_and_the_chat_stays_listed`).
3. **A person with more than 100 repositories, or several installations.** Expected: every page of every installation is listed, no duplicate when two installations hold the same repository. Test in Task 2 (`test_listing_follows_every_page_of_every_installation`).
4. **Serve restarts while a person is approving the device code on their phone.** Expected: the next poll is 404, the panel says the code is gone and offers Start again, without an error banner loop. Test in Task 6 (`test_a_link_lost_to_a_restart_offers_start_again`).
5. **A repository the App reaches that is also a local checkout.** Expected: it is never offered or accepted as an extra, and a person without it in their GitHub list cannot open, pre-warm or push it. Test in Task 4 (`test_a_github_sourced_repository_cannot_be_pinned_as_an_extra`) and Task 5 (`test_a_person_without_access_cannot_make_a_chat_of_it`).

---

## File Structure

Create:

| File | Responsibility |
|---|---|
| `src/airlie/sources/__init__.py` | re-exports |
| `src/airlie/sources/protocol.py` | `RepoSource`, `DeviceCode`, `LinkPending`, `UserTokens`, `SourceRepo`, `SourceError`, `TokenRefused`, `LinkExpired`, `LinkDenied` |
| `src/airlie/sources/vault.py` | `TokenVault`: the AES-GCM key file, `seal` / `open` |
| `src/airlie/sources/github.py` | `GitHubSource`: device flow, refresh, account, listing, revoke, `reaches`, `publisher` |
| `src/airlie/sources/access.py` | `RepoAccess`: a person's view, `access()`, link save/forget, refresh, caches |
| `src/airlie/chat/sources_api.py` | link routes and `GET /v1/airlie/chat/repositories` |
| `tests/support/github_user.py` | `FakeGitHubUser`: GitHub's OAuth and user endpoints, delegating app routes to `tests/unit/github_helpers.FakeGitHub` |
| `web/src/api/sources.ts`, `web/src/stores/repositories.ts`, `web/src/screens/chat/panel/ConnectSource.vue` | the client side |

Modify: `src/airlie/config/schema.py` (`GitHubAppSpec`, `ChatSpec`), `airlie.yaml`, `src/airlie/auth/store.py`, `src/airlie/chat/pins.py`, `src/airlie/chat/api.py`, `src/airlie/chat/repo_session.py`, `src/airlie/chat/repo_tools.py`, `src/airlie/boxes/mirror.py`, `src/airlie/serve/app.py`, `web/src/screens/chat/panel/{ChatPanel,PinPicker,PanelRepository}.vue`, `web/src/stores/pins.ts`, `README.md`, `docs/integrations/story.md`, `pyproject.toml`, `uv.lock`.

---

### Task 1: Config, the token vault and the link table

**Files:**
- Modify: `src/airlie/config/schema.py:818-832` (`GitHubAppSpec`), `:408-413` (`ChatSpec`), `airlie.yaml` (`chat.sources: [github_app]`), `src/airlie/auth/store.py`, `pyproject.toml`, `uv.lock`
- Create: `src/airlie/sources/__init__.py`, `src/airlie/sources/vault.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_token_vault.py`, `tests/unit/test_auth_store_links.py`

**Interfaces:**
- Produces:

```python
# schema.py, GitHubAppSpec gains
client_id: str | None = None            # the App's client id (airlie.local.yaml); None: nobody can link
client_secret_file: Path | None = None  # 0600; needed to refresh a token and to revoke a grant
token_key_file: Path = Path("~/.config/airlie/github-user-tokens.key")
oauth_url: str = "https://github.com"
# ChatSpec gains
sources: list[Literal["github_app"]] = []   # validator: "github_app" needs story.github_app.app_id
repo_list_cache_s: float = Field(default=300.0, gt=0)

# sources/vault.py
class VaultError(RuntimeError): ...
class TokenVault:
    def __init__(self, key_file: Path) -> None: ...   # expanded; the key is read lazily
    def seal(self, plaintext: str, *, aad: str) -> bytes: ...   # nonce(12) || ciphertext
    def open(self, sealed: bytes, *, aad: str) -> str: ...      # VaultError on a wrong key or aad
def aad(user_id: str, source: str, field: Literal["access", "refresh"]) -> str: ...  # "\0"-joined

# auth/store.py
@dataclass(frozen=True)
class SourceLink:
    user_id: str; source: str; login: str; account_id: int
    access_token: bytes; access_expires_at: float | None      # sealed
    refresh_token: bytes | None; refresh_expires_at: float | None
    created_at: float
AuthStore.link(user_id: str, source: str) -> SourceLink | None
AuthStore.save_link(link: SourceLink) -> None          # upsert on (user_id, source)
AuthStore.drop_link(user_id: str, source: str) -> bool
```

`source_links` has no foreign key (the admin key and bootstrap link too, as they pin); `delete_user` deletes the person's rows.

- [ ] **Step 1: Write the failing tests**
  - `test_a_vault_makes_its_key_0600_on_first_use` (file absent → after `seal`, mode `0o600`, 32 bytes).
  - `test_a_vault_refuses_a_key_others_can_read` (mode `0o644` → `VaultError` naming the path and `chmod 600`, never the key bytes).
  - `test_sealed_tokens_round_trip_and_differ_each_time` (`seal("ghu_x", aad=a)` twice → two different bytes, both `open` to `"ghu_x"`; `"ghu_x"` not in either).
  - `test_a_token_sealed_for_one_person_does_not_open_for_another` (`aad("u1","github_app","access")` vs `"u2"` → `VaultError`).
  - `test_another_key_cannot_open_a_token` (second vault, other file → `VaultError`).
  - `test_a_link_is_saved_replaced_and_dropped`, `test_deleting_a_user_deletes_their_links`, `test_the_admin_key_can_hold_a_link` (`user_id="api-key"`).
  - `test_github_app_source_needs_an_app_id` (`chat.sources: [github_app]` with `app_id: null` → config error naming `story.github_app.app_id`); `test_client_id_is_optional_and_oauth_url_defaults_to_github`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_token_vault.py tests/unit/test_auth_store_links.py tests/unit/test_config.py -q` → FAIL (no module / no field).
- [ ] **Step 3: Implement.** `uv add cryptography` (the locked version). `TokenVault` uses `cryptography.hazmat.primitives.ciphers.aead.AESGCM`; the key file is made with `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` as `notify/vapid.py` does; a race on creation reads the file the winner wrote.
- [ ] **Step 4: Run** the same tests → PASS; then the gate.
- [ ] **Step 5: Commit** (`sources: GitHub App client settings, the token vault and the link table`).

### Task 2: `RepoSource` and `GitHubSource`, with a fake GitHub

**Files:**
- Create: `src/airlie/sources/protocol.py`, `src/airlie/sources/github.py`, `tests/support/github_user.py`
- Test: `tests/unit/test_github_source.py`

**Interfaces:**
- Consumes: `GitHubAppSpec` (Task 1), `GitHubApp`, `api_base`, `masked` from `src/airlie/story/github_app.py`, `Publisher` from `src/airlie/story/publish.py`.
- Produces:

```python
# sources/protocol.py
@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False); user_code: str; verification_uri: str
    expires_in: int; interval: int
@dataclass(frozen=True)
class LinkPending:
    interval: int                      # seconds before the next poll (raised by slow_down)
@dataclass(frozen=True)
class UserTokens:
    access_token: str = field(repr=False); access_expires_at: float | None
    refresh_token: str | None = field(repr=False); refresh_expires_at: float | None
@dataclass(frozen=True)
class SourceRepo:
    repo: str          # "owner/name", GitHub's full_name
    private: bool; push: bool; url: str
class SourceError(RuntimeError): ...        # says what failed; never a token
class TokenRefused(SourceError): ...        # 401, bad_refresh_token: the link is over
class LinkExpired(SourceError): ...         # expired_token, incorrect_device_code
class LinkDenied(SourceError): ...          # access_denied
class RepoSource(Protocol):
    name: str                               # "github_app"
    title: str                              # "GitHub"
    async def start_link(self) -> DeviceCode: ...
    async def poll_link(self, device_code: str) -> UserTokens | LinkPending: ...
    async def refresh(self, refresh_token: str) -> UserTokens: ...
    async def account(self, access_token: str) -> tuple[str, int]: ...     # (login, id)
    async def repositories(self, access_token: str) -> list[SourceRepo]: ...
    async def revoke(self, access_token: str) -> None: ...                 # best effort, never raises
    async def reaches(self, repo: str) -> bool: ...                        # the App is installed on it
    def publisher(self) -> Publisher: ...                                  # fetch and push as the App's bot

# sources/github.py
class GitHubSource:   # implements RepoSource
    def __init__(self, spec: GitHubAppSpec, remote: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None: ...   # ValueError when app_id or client_id is None

# tests/support/github_user.py
class FakeGitHubUser:   # an httpx MockTransport handler; app routes go to FakeGitHub
    def __init__(self, app: FakeGitHub, *, client_id: str, client_secret: str | None, clock: Callable[[], float]): ...
    def grant(self, login: str, installations: dict[int, list[tuple[str, bool]]]) -> None: ...  # repo, push
    def approve(self, user_code: str, login: str) -> None: ...
    def deny(self, user_code: str) -> None: ...
    def revoke_all(self, login: str) -> None: ...      # the person removed the App on GitHub
    calls: list[tuple[str, str]]                        # (method, path) in order
    # tokens: access lasts 28800 s, refresh 15897600 s, on the fake's clock; a refresh token works once
```

- [ ] **Step 1: Write the failing tests**
  - `test_start_link_asks_for_a_device_code_with_the_client_id` (body has `client_id`, the `DeviceCode`'s `user_code`/`verification_uri`/`interval` are the fake's; `device_code` not in `repr`).
  - `test_poll_is_pending_until_approved_then_gives_both_tokens` (pending → `LinkPending(5)`; after `approve` → `UserTokens` with `access_expires_at == now + 28800`).
  - `test_slow_down_raises_the_interval`; `test_an_expired_code_is_link_expired`; `test_a_declined_code_is_link_denied`; `test_device_flow_disabled_names_the_app_setting` (message says to enable Device Flow in the App's settings).
  - `test_refresh_sends_the_client_secret_and_gets_a_new_pair` (the old refresh token then refused → `TokenRefused`).
  - `test_refresh_without_a_client_secret_is_token_refused` (no file → `TokenRefused`, no call to GitHub).
  - `test_listing_follows_every_page_of_every_installation` (2 installations, 150 + 3 repositories, one repository in both → 152 unique `SourceRepo`, sorted by `repo.lower()`, `push` carried from `permissions.push`).
  - `test_a_401_on_listing_is_token_refused`; `test_a_500_on_listing_is_source_error_without_the_token` (`"ghu_"` not in `str(e)`).
  - `test_revoke_deletes_the_grant_with_basic_auth_and_never_raises` (and a 500 or no secret → no exception).
  - `test_reaches_is_false_for_a_repository_the_app_is_not_installed_on`.
  - `test_an_oauth_url_with_http_to_another_host_is_refused`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_github_source.py -q` → FAIL.
- [ ] **Step 3: Implement** `GitHubSource` over one `httpx.AsyncClient` per call (as `github_app._request`), redirects off; `reaches` is `GitHubApp._installation` (made public as `installation(repo)`, returning `None` on 404) with its answer cached; `publisher()` is `Publisher(remote=remote, app=self._app, publish="single")`.
- [ ] **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** (`sources: the RepoSource seam and GitHub through the App's user authorization`).

### Task 3: `RepoAccess`: a person's repositories

**Files:**
- Create: `src/airlie/sources/access.py`
- Test: `tests/unit/test_repo_access.py`

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces:

```python
Access = Literal["source", "extra"]
@dataclass(frozen=True)
class SourceView:
    name: str; title: str; linked: bool; login: str | None
    repositories: tuple[SourceRepo, ...]; error: str | None; listed_at: float | None
@dataclass(frozen=True)
class PersonRepos:
    sources: tuple[SourceView, ...]
    extras: tuple[str, ...]            # the person's pins that are extra candidates, pin order
    extra_candidates: tuple[str, ...]  # local names no source reaches
class RepoAccess:
    def __init__(self, store: AuthStore, vault: TokenVault, sources: Mapping[str, RepoSource],
                 local_names: Callable[[], Awaitable[list[str]]], *, cache_s: float,
                 clock: Callable[[], float] = time.time) -> None: ...
    async def view(self, user_id: str, *, refresh: bool = False) -> PersonRepos: ...
    async def access(self, user_id: str, repo: str, *, fresh: bool = False) -> Access | None: ...
    async def may_push(self, user_id: str, repo: str) -> str | None: ...   # the refusal, or None
    async def save(self, user_id: str, source: str, tokens: UserTokens) -> str: ...   # login
    async def forget(self, user_id: str, source: str) -> bool: ...   # revoke (best effort), drop, clear cache
REFRESH_MARGIN_S = 300
```

With `sources` empty, `access()` answers `"source"` for every local name (today's rule) and `extra_candidates` is every local name; `may_push` then defers to the mirror (`None`). Repository names compare case-insensitively.

- [ ] **Step 1: Write the failing tests** (over `FakeGitHubUser`, an in-memory `AuthStore` from `tests/support/auth_store.py`, a vault in `tmp_path`, a fake clock):
  - `test_an_unlinked_person_sees_connect_and_their_extras` (`linked False`, no repositories, `extras` = pinned candidates).
  - `test_two_people_see_their_own_repositories` (noah: `weyenk/airlie`, `o/r`; ashley: `o/r` → `access("ashley","weyenk/airlie") is None`).
  - `test_the_listing_is_cached_until_refresh` (two views → one listing round in `calls`; `refresh=True` → a second; clock past `cache_s` → a third).
  - `test_a_token_near_expiry_is_refreshed_first` (clock at `expires - 299` → one refresh call, the new sealed pair saved).
  - `test_concurrent_views_refresh_the_token_once` (`asyncio.gather` of `view` and `access(fresh=True)` → one `POST /login/oauth/access_token`).
  - `test_a_refused_token_drops_the_link_with_the_reason` (`revoke_all` → view `linked False`, `error == "GitHub ended airlie's access: connect again"`, `store.link(...) is None`).
  - `test_a_vault_that_cannot_open_the_tokens_reads_as_unlinked` (key file replaced → same as above, logged without the token).
  - `test_a_repository_the_app_reaches_is_never_an_extra_candidate`; `test_an_old_pin_of_a_github_repository_is_not_an_extra`.
  - `test_may_push_needs_the_persons_push_permission` (`push False` → `"you cannot push to o/r on GitHub, so airlie will not push for you"`).
  - `test_forget_revokes_drops_and_clears_the_cache`.
  - `test_with_no_source_every_local_name_is_source`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_repo_access.py -q` → FAIL.
- [ ] **Step 3: Implement.** One `asyncio.Lock` per `(user_id, source)` around refresh-and-list; the cache is in memory, keyed the same way; a `SourceError` other than `TokenRefused` keeps the last good list (if any) and sets `error` to its message.
- [ ] **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** (`sources: a person's repositories, cached, with the token refreshed before expiry`).

### Task 4: Routes: link, list, extras, and the serve wiring

**Files:**
- Create: `src/airlie/chat/sources_api.py`
- Modify: `src/airlie/chat/pins.py` (the `pinnable` check becomes `access.view(user).extra_candidates`), `src/airlie/serve/app.py:513-530,887-919` (`create_app(..., repo_sources: Mapping[str, RepoSource] | None = None)`; default: `GitHubSource(cfg.story.github_app, cfg.story.remote)` for each name in `chat.sources` whose spec has a `client_id`; `RepoAccess` built once and stored as `app.state.repo_access`)
- Test: `tests/functional/test_chat_sources.py`, `tests/functional/test_chat_sessions.py` (pins)

**Interfaces:**
- Consumes: Task 3's `RepoAccess`.
- Produces (routes; the web codes against these):

| Route | Answer |
|---|---|
| `GET /v1/airlie/chat/repositories?refresh=true\|false` | 200 `{sources: [{name, title, linked, login, repositories: [{repo, private, push, url}], error, listed_at}], extras: string[], extra_candidates: string[]}` |
| `POST /v1/airlie/chat/sources/{source}/link` | 201 `{link_id, user_code, verification_uri, expires_at, interval}`; 404 a source not in `chat.sources`; 503 `"set story.github_app.client_id in airlie.local.yaml to connect GitHub"`; 502 GitHub's failure, masked |
| `POST /v1/airlie/chat/sources/{source}/link/{link_id}` | 200 `{state: "linked", login}`; 202 `{state: "pending", interval}`; 404 an unknown link or another person's; 410 `"the code expired: start again"`; 403 `"you declined on GitHub"` |
| `DELETE /v1/airlie/chat/sources/{source}/link` | 204; 404 not linked |
| `PUT /v1/airlie/chat/pins/{owner}/{name}` | as today, but 422 `"{repo} comes from GitHub: it shows when your GitHub account can see it"` for a repository a source reaches |

```python
def sources_router(access: RepoAccess, sources: Mapping[str, RepoSource], publish: Publish,
                   *, clock: Callable[[], float] = time.time) -> APIRouter: ...
class LinkAttempts:   # in memory: link_id -> (user_id, source, DeviceCode, expires_at, next_poll_at); one per (user, source)
```

A poll sooner than `next_poll_at` answers 202 without calling GitHub. Link success and unlink publish `("chat", None)`.

- [ ] **Step 1: Write the failing tests** (TestClient over `create_app` with `repo_sources={"github_app": GitHubSource(..., transport=MockTransport(fake))}`, two signed-in people via `tests/support/auth_store.py` sessions):
  - `test_linking_shows_a_code_then_the_persons_repositories` (start → 201 with `user_code`, no `device_code` key anywhere in the body; poll → 202; `fake.approve` → 200 `{"state":"linked","login":"weyenk"}`; repositories lists `weyenk/airlie`).
  - `test_noah_and_ashley_see_different_repositories`.
  - `test_another_persons_link_id_is_404`; `test_an_unknown_source_is_404`; `test_an_expired_code_is_410`; `test_a_declined_code_is_403`; `test_without_a_client_id_linking_is_503_with_the_fix`; `test_a_github_failure_on_start_is_502_without_secrets`.
  - `test_a_poll_before_the_interval_does_not_call_github`.
  - `test_refresh_true_lists_again`; `test_unlink_is_204_then_404_and_publishes_the_hint`.
  - `test_every_route_is_401_without_a_principal` (parametrized over the five routes).
  - `test_a_github_sourced_repository_cannot_be_pinned_as_an_extra` (422 with the fixed copy); `test_a_local_only_checkout_is_pinned_as_an_extra`.
- [ ] **Step 2: Run** `uv run pytest tests/functional/test_chat_sources.py tests/functional/test_chat_sessions.py -q` → FAIL.
- [ ] **Step 3: Implement** the router and the wiring; `create_app`'s `pinnable` keeps its meaning (the local names, default `repo_names_known`) and feeds `RepoAccess.local_names`.
- [ ] **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** (`chat: link GitHub by device flow and list a person's repositories`).

### Task 5: Authorize every repository-chat action per person; fetch through the source

**Files:**
- Modify: `src/airlie/chat/api.py:305-330,353-490,583-640` (`chat_router(..., access: RepoAccess | None)` replaces `pinnable`), `src/airlie/chat/repo_session.py:73-180,299-372`, `src/airlie/chat/repo_tools.py:24-150`, `src/airlie/boxes/mirror.py:21-75`, `src/airlie/serve/app.py`
- Test: `tests/functional/test_repo_chat_turn.py`, `tests/functional/test_chat_sessions.py`, `tests/functional/test_mirror_git.py`, `tests/unit/test_repo_session.py`, `tests/support/repo_chat.py`

**Interfaces:**
- Consumes: `RepoAccess.access`, `RepoAccess.may_push` (Task 3).
- Produces:

```python
# repo_session.py
Authorize = Callable[[str, str], Awaitable[Access | None]]          # (owner, repo)
MayPush = Callable[[str, str], Awaitable[str | None]]               # (owner, repo) -> refusal
class NoAccess(EngineError): ...
RepoSessions.__init__(..., authorize: Authorize, may_push: MayPush)  # open(): NoAccess when authorize is None
repo_mirrors(root, publisher, *, run, local_checkout) -> Callable[[str, Access], Mirror]
# RepoChat gains
may_publish: Callable[[], Awaitable[str | None]]   # may_push(owner, repo), bound by open()
# mirror.py: Mirror(root, repo, publisher, *, run, local_checkout) as today; an "extra" mirror is made with
# publisher.app None (read from local_checkout, can_push False); refresh() re-points origin when its URL differs
```

- [ ] **Step 1: Write the failing tests**
  - `test_a_person_without_access_cannot_make_a_chat_of_it` (POST thread `{kind:"repository", repo:"weyenk/airlie"}` as ashley → 403 with the fixed copy; no row made).
  - `test_a_turn_after_access_was_lost_is_403_and_the_chat_stays_listed` (`fake.revoke_all`, `refresh=true`, then turn → 403; `GET /threads` still lists it).
  - `test_prewarm_skips_a_repository_the_person_cannot_use`.
  - `test_push_is_refused_before_the_prompt_without_push_permission` (the stream has a `tool-output-error` with the `may_push` copy and no `data-approval`).
  - `test_push_is_refused_after_the_yes_when_access_went_meanwhile` (approve after `revoke_all` → refused, nothing pushed).
  - `test_an_extra_reads_its_checkout_and_never_pushes` (`push_branch` → `"set story.github_app to push from chat"` is replaced by `"{repo} is a local checkout: airlie does not push it"`).
  - `test_a_github_repository_is_fetched_through_the_app_not_the_checkout` (a checkout exists; the mirror's `origin` is `https://github.com/o/r.git` and the fake saw `access_tokens`).
  - `test_a_mirror_that_switches_from_local_to_source_repoints_origin`.
- [ ] **Step 2: Run** the four test files → FAIL.
- [ ] **Step 3: Implement.** The 403 checks run before `repo_sessions.open` in `create_thread` and the turn route; `may_prewarm` asks `access()`; `PushBranch.subject` / `OpenPr.subject` and both `run_approved` call `chat.may_publish()` first and raise or return its refusal. `default_repo_sessions` takes `access` and builds `mirror_for(repo, kind)`; the local checkout is read only for `"extra"`. Update `tests/support/repo_chat.py` and `tests/e2e/test_chat_redesign.py`'s server to pass `access` (no source: today's behaviour).
- [ ] **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** (`chat: repository chats, pre-warm, push and PRs only for the person's own repositories`).

### Task 6: The panel: Connect GitHub, the person's repositories, Refresh, extras

**Files:**
- Create: `web/src/api/sources.ts` (+ `.test.ts`), `web/src/stores/repositories.ts` (+ `.test.ts`), `web/src/screens/chat/panel/ConnectSource.vue` (+ `.test.ts`)
- Modify: `web/src/screens/chat/panel/ChatPanel.vue` (+ test), `PinPicker.vue` (+ test), `PanelRepository.vue`, `web/src/stores/pins.ts`

**Interfaces:**
- Consumes: Task 4's routes.
- Produces:

```ts
// api/sources.ts
export type SourceRepo = { repo: string; private: boolean; push: boolean; url: string };
export type SourceView = { name: string; title: string; linked: boolean; login: string | null;
  repositories: SourceRepo[]; error: string | null; listed_at: number | null };
export type PersonRepos = { sources: SourceView[]; extras: string[]; extra_candidates: string[] };
export type LinkStart = { link_id: string; user_code: string; verification_uri: string; expires_at: number; interval: number };
export type LinkPoll = { state: "linked"; login: string } | { state: "pending"; interval: number };
export function personRepositories(refresh?: boolean): Promise<PersonRepos>;
export function startLink(source: string): Promise<LinkStart>;
export function pollLink(source: string, linkId: string): Promise<LinkPoll>;  // 404/410/403 reject with the server's error and status
export function unlink(source: string): Promise<void>;
// stores/repositories.ts
export const useRepositories: () => { view: PersonRepos | null; shown: string[] /* sources' repositories, then extras */;
  error: string | null; refresh(): Promise<void> /* ?refresh=true */; reload(): Promise<void> };  // liveResource("chat", …)
```

`ConnectSource.vue` props `{ source: SourceView }`, emits `linked`. States: idle (button "Connect GitHub", the `error` line when set), waiting (the `user_code` large and monospace with a Copy button, a link "Open {verification_uri}" with `target="_blank" rel="noopener"`, "Waiting for you to approve on GitHub…", Cancel), expired / gone (the reason and "Start again"), declined. It polls with `setTimeout(interval)` and at once on `visibilitychange` to visible. `ChatPanel` groups chats by `useRepositories().shown` (chats of anything else stay under "Other repositories"), shows one `ConnectSource` per unlinked source in place of its repositories, a "Refresh repositories" button, and "GitHub: @login · Disconnect GitHub" for a linked source. Unpin applies to extras only. `PinPicker` lists `extra_candidates` under "Pin a local checkout".

- [ ] **Step 1: Write the failing tests** (vitest, fake timers):
  - `test_an_unlinked_person_sees_connect_github_and_their_extras`.
  - `test_connect_shows_the_code_and_the_link_then_the_repositories_once_approved` (202, 202, 200 → the panel lists `weyenk/airlie`; the fetch stub saw polls `interval` seconds apart).
  - `test_a_slow_down_waits_the_new_interval`; `test_a_declined_code_says_so_and_offers_start_again`; `test_an_expired_code_offers_start_again`.
  - `test_a_link_lost_to_a_restart_offers_start_again` (404 on poll → "The code is gone: start again", no error banner).
  - `test_coming_back_to_the_page_polls_at_once`.
  - `test_refresh_repositories_asks_with_refresh_true`; `test_a_source_error_shows_its_line_and_keeps_the_last_list`.
  - `test_disconnect_github_returns_to_connect`.
  - `test_github_repositories_cannot_be_unpinned_but_extras_can`; `test_the_pin_picker_offers_only_local_checkouts`.
  - `test_chats_of_a_repository_no_longer_listed_stay_under_other_repositories`.
- [ ] **Step 2: Run** `task web:gate` → FAIL.
- [ ] **Step 3: Implement** within B2's rules (plain layout; detail in a hover card or the phone's bottom sheet); the code and its Copy button fit at 375 px.
- [ ] **Step 4: Run** `task web:gate` → PASS; gate.
- [ ] **Step 5: Commit** (`web: the chat panel's repositories come from GitHub, with Connect GitHub and extras`).

### Task 7: One e2e, and the docs

**Files:**
- Create: `tests/e2e/test_repos_from_github.py`
- Modify: `README.md` (the chat section), `docs/integrations/story.md` ("Publishing as a bot": Device Flow on, `client_id`, the client secret file, the token key file, `chat.sources`)

**Interfaces:**
- Consumes: `FakeGitHubUser` (Task 2), `_serve` and `Evidence` from `tests/e2e/conftest.py`, the repository fakes of `tests/support/repo_chat.py` (Task 5).

- [ ] **Step 1: Write the failing test** `test_a_person_connects_github_sees_their_repositories_and_opens_a_repository_chat` (`pytestmark = pytest.mark.e2e`; a server with `chat.sources: [github_app]`, `client_id` and a client secret file set, `repo_sources` over `FakeGitHubUser` granting `o/r`, FakeBox VMs, a scripted model that reads `README.md` and answers): open `/` → evidence "Connect GitHub" in the panel → click → evidence the user code and the link → the test calls `fake.approve(code, "weyenk")` → evidence `o/r` under GitHub → New under `o/r` → send "what is this repository?" → evidence the `read_file` tool block and the answer, the chat row showing `o/r`. No fixed waits.
- [ ] **Step 2: Run** `task test:e2e -- -k repos_from_github` → FAIL; then PASS after Tasks 4-6 are in, five runs green in a row.
- [ ] **Step 3: Write the docs** (what Noah sets in `airlie.local.yaml`: `story.github_app.client_id`, `client_secret_file`; that Device Flow must stay on; where the token key lives and that losing it means everyone reconnects).
- [ ] **Step 4: Gate** → PASS.
- [ ] **Step 5: Commit** (`e2e: connect GitHub and open a repository chat; docs for the App's user authorization`).

### Task 8: Live check (stop point)

- [ ] **Step 1: Stop and ask Noah** for `client_id` and a client secret in `~/.config/airlie/github-app-client-secret` (0600), and for his go.
- [ ] **Step 2: Live check** on this Mac and Noah's iPhone: connect from the phone (code, GitHub's page, back to airlie), Noah's list includes `weyenk/airlie`, Ashley's does not; a repository chat fetches through the App; a push asks and lands as the bot; Disconnect GitHub, then the panel shows Connect GitHub and his repository chats stay listed. Record it in `docs/bench/2026-11-1x-repos-from-github-check.md` with screenshots; commit (`docs: repositories from GitHub, live check`).

---

## Self-review

- **Spec coverage:** the seam and `chat.sources` (Tasks 1-2); device flow and per-person tokens, encrypted, refreshed, deleted on unlink (Tasks 1-4); the person's list from installations, cached, Refresh (Tasks 2-4, 6); extras limited to local-only checkouts (Tasks 3-4, 6); fetch through the source, push and PR as the bot only for the person's list, behind the prompt (Task 5); Connect GitHub in the panel, phone-friendly (Task 6); the e2e (Task 7); Controller calls 1-9 each land in a task (secret file: 1-2; config homes: 1; no source = today: 3, 5; extras: 3-4; push permission: 3, 5; client polls: 4, 6; any principal: 1; no hiding: 6; `cryptography`: 1).
- **Steps:** each test step names its tests and the values they assert; code steps give signatures and the choices tests leave open; no bodies.
- **Types:** `RepoSource`, `UserTokens`, `SourceRepo`, `RepoAccess.view/access/may_push/save/forget`, `Access`, `PersonRepos`, `SourceView` and the route shapes match between Tasks 2-6; `mirror_for(repo, kind)` is Task 5's alone.
- **Review Focus:** each of the five lines has its test in the owning task.
- **Proportion:** the plan fixes names, routes, copy and tests; the bodies are the builder's.
