# The airlie web app (v1) and the local board

Status: agreed with Noah on 2026-10-17 (chat-first app; kanban built into airlie; mockups iterated on the
canvas). Jira stays as an optional board backend; the local board becomes the default once the HDS issues are
migrated.

Mockups: https://claude.ai/artifact/DfBHBeXP356eFkFSgEZCqP (desktop and phone, dark by default).
Parent design: `2026-10-16-remote-studio-design.md`. Jira epic: HDS-14; stories HDS-13, HDS-7, HDS-8, HDS-10.

## Why

Noah mostly wants to *talk* to airlie, from a laptop or a phone on his tailnet, and see what it is doing. Jira
Free cannot hold his hierarchy (initiative → epic → story → subtask), needs Atlassian credentials in airlie's
environment, and lives outside the VPN. A board inside airlie fixes all three and lets the agents write to it
as themselves.

## Goals

- A web app served by `task serve`, behind the same API key, usable at phone width.
- Chat with the chief as the main surface; jobs started, followed and approved from the app.
- A board with the four levels, stored on the Studio, with the story pipeline reading from it through the
  existing `Board` seam.

## Non-goals (v1)

- Per-person keys, Tailscale identity, Tart boxes: the remote-studio POC stories. v1 uses the one serve key.
- An agentic chat that starts jobs on its own. v1's chat is the chief role over `/v1/chat/completions`; jobs
  are started with explicit controls. Tool-driven chat is v2.
- The product/three-amigos/architect cascade over the board: its own design. v1 gives the board the levels
  and the API; the pipeline still builds one story at a time.
- Metrics (HDS-7), topology page (HDS-8), admin (HDS-10). The nav links to them; the pages come later.
- A node build. v1 is static files (ES modules, one stylesheet) served by FastAPI, with no bundler.

## Anti-goals

- A merge button for airlie's work. Only a human merges, in GitHub.
- The board's data living only in RAM. It is a file under `observability.state_dir`, backed up like the rest.

## Decisions

### Serving

- `GET /` and `/app/*` serve `src/airlie/serve/web/` (static). `/v1/*` stays the API. The key middleware keeps
  guarding `/v1` only: the static shell is harmless without a key.
- The app keeps the key in `localStorage` after sign-in and sends `Authorization: Bearer`. A 401 returns the
  user to sign-in.
- New API for the app, all under `/v1/airlie`:
  - `GET /jobs`: every job this serve process started (id, kind, state, started), newest first.
  - `GET /runs`: recent run directories under `state_dir/runs` (id, started, ok, goal) from their events,
    newest first, at most 50.
  - `GET /stories`: stories under `state_dir/stories` (card key, stage, blocked reason) from `state.json`.
- Chat: `POST /v1/chat/completions` with `model: chief`, `stream: true`; the app renders the SSE stream.

### The local board

- A package `airlie.board`: `model.py` (Item, Level, Status), `store.py` (SQLite, stdlib `sqlite3`),
  `api.py` (FastAPI router at `/v1/board`).
- **Levels:** `initiative`, `epic`, `story`, `subtask`. A parent must be exactly one level up; an initiative
  has none.
- **Statuses:** `todo`, `doing`, `review`, `blocked`, `done`. The pipeline's columns map onto them:
  intake=todo, building=doing, review=review, blocked=blocked.
- **Keys:** `<prefix>-<n>` with `board.prefix` (default `AL`), so they satisfy `story.model.ISSUE_KEY` and
  name story directories like Jira keys do.
- **Item fields:** key, level, title, description (Markdown), status, parent key, labels (the `repo:` label
  works as it does for Jira), comments (author, text, time), created_at, updated_at, created_by.
- **Storage:** `state_dir/board.sqlite`, WAL mode; one writer (serve). `task board:export` writes the whole
  board as JSON next to it for backups and diffs.
- **The Board seam:** `airlie.story.local_board.LocalBoard` implements `Board` over the store: `card(ref)`
  for a story key, `next_card()` = the oldest `story` in `todo` (never an epic or initiative), `move`,
  `comment` (author `airlie`). `story.board: jira | local` picks the backend (default `jira` until the
  migration; `make_board` reads it).

### The app's screens (v1)

Chat/Home, Jobs (list + one job), Board, Sign in. Phone layouts as on the canvas: a bottom tab bar, the chat
filling the screen. Metrics, Topology and Admin are nav entries that show "coming soon".

- **Chat:** a conversation with the chief (streamed), a composer with the repository picker, and a status
  strip (needs you, running). Plan cards and job cards are rendered from job events when a job the app started
  reports them.
- **Start new work** (from the composer's `+`): written request or board story, repository, story or studio
  run. It calls `/v1/airlie/stories` or `/v1/airlie/plans` then `/v1/airlie/runs` (plan approval = sending the
  plan back with the run).
- **Job:** the event stream rendered as the agent tree (nodes from `subtask_start`/`subtask_done`/`review`
  events) with the selected node's latest steps; Stop calls `DELETE /v1/airlie/jobs/{id}`.
- **Board:** columns by status, a level filter, drill-down by parent, create and edit items, move between
  statuses by buttons (drag-and-drop is v2).

### Build and test

- Python side: test-first, no live engine or network in tests, `task check` green.
- Web side: no framework and no bundler; small ES modules. Tests: the API routes (Python). The UI is checked
  by hand in the browser until a browser test harness is chosen (v2).

## Left to the builder

The exact SQLite schema; how the SSE stream is parsed; the look's details beyond the mockups; file layout
under `serve/web/`.
