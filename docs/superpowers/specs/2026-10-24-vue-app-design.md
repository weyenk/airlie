# The web app on Vue: live data and shared components

Status: agreed in conversation with Noah on 2026-10-24, including the reshaped Workflows area; builder check
done; awaiting his review of the full text. Parent: `2026-10-17-web-app-and-local-board-design.md`, whose "no
framework, no bundler, no node build" rule this reverses on purpose. Look reference:
`docs/superpowers/mockups/2026-10-24-vue-app/board-look-dark.html` and `board-look-light.html`. Approved screen
mockups, in the same folder: `workflows.html`, `workflow-story.html`, `workflow-runs.html`, `board.html` (each has
buttons to switch light and dark, and the board's to open a card or Create).

## Why

**Stale data.** The web app keeps showing old data until the page is reloaded. Each screen gets its data its
own way, and most never ask again:

| Screen | How it refreshes today |
| --- | --- |
| Board, Metrics, Admin | once, when opened (the board also after your own move) |
| Workflows list, one workflow | a timer, every 10 s |
| Chat status strip | a timer, every 15 s |
| One job, Topology | long polls on the run's event log (the only live screens) |

Cards the story pipeline moves, comments agents add, users another admin invites: none of it shows until a
reload. The server has no single way to tell an open page that something changed.

**Reinventing UI.** Every screen builds its DOM by hand with `h()` from `dom.js`. Apart from `cards.js` there
is no shared component set, so each new screen re-creates buttons, chips, modals and forms.

Noah wants pages that update on their own, and components built once and reused.

## Goals

1. Every screen shows a change within about 2 seconds without a reload, whoever made it: this server, the
   launchd story tick, a `task` command, or another person.
2. One shared component set that every screen is built from.
3. A new look: shadcn-vue's components in PrimeVue Aura's colours, dark (the default) and light.
4. Parity with today's app on desktop and phone, except where "The Workflows area" below reshapes it: every
   other screen and action that exists now exists after.
5. A Workflows area that helps you find a story quickly and see what is running at a glance.

## Non-goals

- New features beyond live updates, the new look, the reshaped Workflows area and the Board changes under "The
  Board". Other things today's app cannot do stay out.
- Running spike, bug, research and design work. The Create modal offers those types, but how airlie runs each one
  end to end (its steps, roles and artifacts) is its own design (proposed 2026-10-24 as a separate session); this
  app draws whatever steps that design's API describes.
- Topology in the new app, for now. Noah likes its visuals, but the workflow view already shows most of the
  same information. It keeps working in today's app until the switch and is not carried over.
- Changes to the core, the engines, or how jobs run.
- A separate frontend server, server-side rendering, or Nuxt. FastAPI keeps serving the app.
- Replacing the jobs API's long polls. OpenCode's `airlie_status` tool and other clients still use them.
- Teaching airlie's story pipeline to build Vue stories. Its pre-code checks only recognise `test_*.py` files
  (`airlie.story.layer_checks`, `airlie.tools.scope`) and its tester lint only fixes ruff
  (`airlie.story.tester_lint`), so a tester writing Vitest specs would block. That is a follow-up after the
  switch.

## Anti-goals

- Loading anything from the internet at runtime. The app must keep working on the tailnet with no internet.
- Two looks mixed in one app.
- A build step anyone runs by hand. Every action stays a `task`.
- Breaking today's app before the switch.
- A merge button. Only a human merges, in GitHub (unchanged from the parent design).

## Constraints

- Python 3.12 and uv; every action a Taskfile task; no Docker locally (Apple `container`; GitHub's e2e runner
  keeps using Docker as today).
- The repository's test gate (`AGENTS.md`) must stay runnable by airlie's own story pipeline, which reads it
  from the base commit and runs it for every layer.
- Sign-in stays as it is: the admin Bearer key, the Tailscale identity header, or the `airlie_session` cookie
  (`airlie.auth.principal`).
- Remote access goes through Tailscale serve and, for API clients, a Cloudflare quick tunnel (`task tunnel`).

## Decided by Noah

- **Vue**, as a proper app with a build step.
- **shadcn-vue** components, restyled with **PrimeVue Aura's palette**: emerald for actions, the active tab and
  progress; zinc greys in dark mode, slate greys in light; Aura's soft tinted status tags; shadcn's pill-shaped
  tabs. The exact values are in the look reference files. They are Tailwind's MIT palette, not PrimeVue code.
  PrimeVue itself is out: since version 5 it needs a yearly license key and forbids redistribution in a
  development tool.
- **Mockups first.** Noah approves mockups before anything is built.
- **A separate address.** The new app grows at `/next`. Today's app stays the default at `/`, untouched, until
  every screen is done; then `/` switches to the new app.
- **Clean paths** (`/board`, not `/#/board`). Old `#/` links redirect to their clean path.
- **Claude builds all of it.**
- **The Workflows area is reshaped, not ported** (details under "The Workflows area"): Running becomes the top
  card with a progress bar per item; the Stories list gains pages, filters and sorting; Needs you and Recent
  results go away; one workflow's view stays; runs of every topology (single, teams, fork and studio) open in
  that view too, with a studio run's plan approval and Stop on it; the job page goes away.
- **airlie's pipeline stays off web work until a follow-up teaches it Vue.** During the build, web stories on
  this repository stay off airlie's To Do. After the switch and until that follow-up lands, web stories here
  go to Claude. The test gate must still pass inside every story, so Python-only stories keep working.
- **Node stays separate from Python.** Its version is pinned only in the web app's `package.json` (the
  `engines` field) and checked by Taskfile tasks. Nothing in `pyproject.toml`, `uv.lock` or uv installs or
  pins node.

## Decided by Claude (Noah can overrule)

- TypeScript, Vite, Vue Router in history mode, Pinia for state, Tailwind v4 (shadcn-vue needs it), Vitest
  with Vue Test Utils. npm, because it comes with node; the lockfile is committed.
- The pin is node 26, the newest even-numbered line, which node marks long-term support each October. It is
  also the node the launchd story tick already finds (Homebrew's), so the tick needs no change.
- Source in `web/` at the repository root. The build writes into the Python package under `src/airlie/serve/`,
  is gitignored, and is served by FastAPI.
- System font stack, as today. No web fonts are fetched or bundled.
- Every screen is mocked up before the first screen is built, so the shared components are designed from the
  whole app, not grown screen by screen.
- Board cards show the fields they show today: key, level, title and labels. The look reference's extra card
  contents (a progress bar, the role at work, PR counts, "needs a decision") are example data, not board
  features. The progress bar does appear, on the Running card in Workflows. Anything else new that the step-1
  mockups add and that needs server work becomes its own follow-up.
- In the Workflows area (Noah confirmed these when they were played back): the Stories list shows 25 stories
  a page, filters by stage, repository and blocked, searches key and title, and sorts by last activity by
  default, or by key or stage. Running lists every story and run in progress, whoever started it.
  Progress counts finished stages and layers against the total. The Start new work button stays.
- The parity target is `main` when the build starts, which includes AIR-17's centred item modal and
  click-to-edit (PRs 140 to 143, merged 2026-10-24).

## How it works

### The live feed

A new endpoint, `GET /v1/events`, is a server-sent event stream. It sits under `/v1`, so it takes the same
sign-in as the rest of the API.

- **Events are hints, not data.** Each says what changed, for example `{"topic": "board", "key": "AIR-17"}`
  or `{"topic": "board"}` when the key is unknown. The page then refetches through the GET endpoints it
  already uses. Those endpoints stay the one source of truth and the one place permissions are checked.
- **Topics** cover everything a screen shows: board items and their comments, jobs, runs, stories and their
  flow, users and invites, and whatever else the parity list needs. The builder fixes the exact list.
- **Changes made inside the server** publish at once: the board API, the jobs manager and the auth API emit an
  event when they write.
- **Changes made by other processes** are caught by a watcher. The launchd story tick, `task run` and
  `task board:import-jira` write the same files from outside the server. While at least one page is connected,
  the watcher checks about once a second:
  - SQLite's `PRAGMA data_version` on the board and auth databases, read on a connection the watcher keeps
    open (the number changes whenever another connection commits). A change found this way carries no key,
    so it publishes a topic-wide event and the page refetches that whole list.
  - The size of run and story event logs under `observability.state_dir`, the same files the long polls
    already read.
- **Staying connected through proxies.** The server sends a comment line every 15 seconds and the headers
  `Cache-Control: no-cache` and `X-Accel-Buffering: no`, so Tailscale serve and Cloudflare neither buffer the
  stream nor close it as idle.
- **No replay.** The server keeps no event history. When a page connects or reconnects, it refetches
  everything it is showing, so nothing missed while disconnected stays stale.

### The page side

- **The feed client reads the stream with `fetch`,** not the browser's `EventSource`, because `EventSource`
  cannot send the Bearer key. `api.js`'s chat `stream()` already parses server-sent events from a `fetch`
  body and shows the way. One stream per tab, reconnecting with backoff.
- **The top bar shows the stream's state:** "Live" while connected, "Reconnecting" otherwise (see the look
  reference).
- **Pinia stores** hold the data, one per resource. A store subscribes to its topics and refetches the item
  or list an event names. Screens read from stores, so they redraw by themselves.
- **A card changed by someone else is highlighted briefly** ("updated just now" in the look reference). The
  builder picks the duration.

### Components

- shadcn-vue's components are copied into `web/` (that is how shadcn-vue works) and restyled through its CSS
  variables to Aura's values. Only the components the screens use are copied.
- On top of them, airlie's own shared components, at least: the app shell (top nav on desktop, tab bar on
  phones, theme toggle, live indicator), the status chip for board statuses and job states, the item card,
  the item modal, the role avatar, empty and error states, a confirm dialog, the markdown view and the chat
  composer.
- The markdown view must stay safe the way `md.js` is today: text never becomes HTML.
- A screen does not hand-style something a shared component covers.

### The Workflows area

Today it has four sections (Stories, Running, Needs you, Recent results), one workflow's view
(`workflow.js`), and a separate job page (`job.js`) that is the only view a studio run has. Runs of the
other topologies (single, teams and fork) are shown only by the Topology page, which is not carried over, so
without this design they would have no view at all in the new app. After:

- **Running, the top card.** One row per story or run in progress, of any topology, whoever started it: the
  web app, the launchd story tick, a `task` command or `POST /v1/airlie/run`. Today's Running section lists
  only jobs this server started, so it misses the tick's stories. A row shows the key (or the run's goal) and
  title, a progress bar, and nothing else. Clicking it opens that work's workflow view. Rows appear, advance
  and leave live, through the feed.
- **Stories, below Running.** The list stays, with pages, filters, search and sorting (the choices are under
  "Decided by Claude"). Paging, filtering and sorting happen in the API, so the list stays fast with hundreds
  of stories.
- **Needs you and Recent results** go away.
- **One workflow's view** stays as it is today, in the new look: the stages left to right, a lane of jobs per
  layer, and the selected job's status, role, retries, input, output and steps.
- **Runs of every topology open in the same view,** with stages shaped by the topology that ran it:
  - **single:** the one agent's work, then its result;
  - **teams:** a lane per team working in parallel, then the judge's pick;
  - **fork:** the agent until it got stuck, a lane per fork, then the winner;
  - **studio:** the chief's plan, the specialists, the reviewer, then the synthesis.

  A story is not a fifth topology. The story pipeline runs on top of the studio: its build stage runs one
  studio run per layer, which is why a story's view has a lane of jobs per layer. A standalone run's view
  shows its own topology's stages instead. The builder settles the exact stages from the run events. A studio run's plan is approved on this view,
  before anything is built; only studio plans first. Stop is on the view while the work runs, for
  jobs this server started through the jobs API (plans, runs and stories), as on the job page today. A story
  the launchd tick runs belongs to another process, and `POST /v1/airlie/run` holds its request open until
  the run ends, so neither has a Stop.
- **The job page goes away.** Its agent tree and event timeline are not carried over. Links that pointed to
  it, including the chat's "job started" cards, point to the work's workflow view instead.

What the API gains for this ("exposing more on the API"):

- The stories list takes page, page size, filters, search and sort, and returns the total count and each
  story's last-activity time. Today it returns every story with key, title, repository, stage and blocked
  reason, and no time.
- A running list: every story and run in progress, each with its progress worked out on the server.
- A flow for a run of any topology, in the same shape as a story's flow, so one view draws them all. It is
  built from the run's `events.jsonl`, the same log the Topology page reads; `airlie.serve.tree` already tells
  the four topologies apart from it. The job detail under it (input, output, steps) works for run jobs as it
  does for story jobs.

### The Board

From the mockup review on 2026-10-24 (`board.html`):

- **Columns fit the window.** The six status columns are as tall as the window less a buffer, and each column
  scrolls on its own; the page itself does not scroll sideways or down to reach cards.
- **Create replaces New initiative.** The modal opens with a type picker: initiative, epic, story, subtask, spike,
  research, design, bug. The button names the type ("Create story"). The board model gains the four new types;
  where they sit in the hierarchy and how they run come from the work-types design. Until it lands, the picker
  offers only the four levels the model has.
- **The level filter becomes a Type dropdown**, since eight types do not fit as buttons. Each type has a colour
  square on its cards and in the picker.
- **The repository field searches as you type.** Typing shows the top three matches below the field, the typed
  part in bold and the rest of the top match greyed in the field; Tab completes it, the arrow keys pick another.
  It is the same field in Create and in the card modal's click-to-edit. The API gains a list of known repositories
  as `owner/name`, read from each checkout's origin remote under `story.repos_root` and from the board's existing
  `repo:` labels (today's `GET /v1/airlie/repos` returns local paths).
- **On the phone**, the card modal's Acceptance criteria and Workflow sections start folded and open on a tap; a
  strip of status pills with counts replaces the six columns, one column at a time.

### Screens

Each at `/next` plus its clean path, desktop and phone:

| Today | New |
| --- | --- |
| `#/` Chat | `/next/` |
| `#/workflows` | `/next/workflows`, reshaped as above |
| `#/workflows/{key}/{node}/{job}` | `/next/workflows/{key}/{node}/{job}` |
| (none: runs only had the job page or Topology) | a workflow view for a run of any topology; the builder picks a path that cannot clash with a story key |
| `#/jobs/{id}` | gone; redirects to the workflow view of the work it ran |
| `#/start` | `/next/start` |
| `#/board/{key}` (with the item modal, comment edit and drag-and-drop) | `/next/board/{key}`, with the changes under "The Board" |
| `#/topology/{run}` | not carried over; stays in today's app until the switch |
| `#/metrics` | `/next/metrics` |
| `#/admin` | `/next/admin` |
| `#/join/{token}` (public) | `/next/join/{token}` |
| `#/signin` (public, passkeys) | `/next/signin` |

`#/jobs` redirects to workflows today and keeps doing so. Chat keeps its thread in `sessionStorage`, and its
plan cards can still approve a plan in the chat.

### Serving

- FastAPI serves the built files under `/next`, and returns the new app's page for any other `/next/...` path
  so the router can draw it. `/v1`, `/health` and today's `/` and `/app` are unchanged until the switch.
- **The switch** moves the new app to `/`, returns its page for every path that is not the API, a file or the
  health check, and removes today's app and its tests. `/next/...` redirects to the same path without the
  prefix. The new app's page turns an old `#/x` link into `/x`. Invite links, made by `task auth:invite` and
  `POST /v1/auth/invites`, change from `/#/join/<token>` to `/join/<token>` at the same time.

### Build and toolchain

- **Taskfile tasks** for installing, building, checking and a dev server: Vite's dev server proxies `/v1` to
  `task serve` on port 8090. `task setup` installs and builds. `task serve` builds first, and skips the build
  when nothing changed.
- **Node's version check.** `task doctor` and every web task compare the node on the path with the major
  version in `package.json`'s `engines` field. On a mismatch they stop and say which node they found and which
  one is needed. They never install node. On Noah's Mac today, interactive shells find nvm's node 24 and
  login shells, including the launchd story tick, find Homebrew's node 26. With the pin at 26, nvm's default
  has to move to 26 (or nvm step aside) before the web tasks run from an interactive shell.
- **The test gate** in `AGENTS.md` gains the web steps after the Python ones: install from the lockfile, type
  check, lint, unit tests and the build. Every command runs from the repository root. The builder writes the
  exact lines.
- **The web steps skip themselves when nothing changed.** They run as one Taskfile task whose sources are
  `web/` and the lockfile, so `task` skips it when those are unchanged since it last passed in that checkout.
  airlie's scoped `run_tests` narrows only the pytest step and runs every other gate step in full on every
  call. Without the skip, a Python-only story would install, type check, lint, test and build the web app
  after every change its tester or coder makes. With it, the story pays that once per worktree.
- **airlie's story pipeline must be able to run that gate.** Agent commands may only write inside an allow
  list (`airlie.tools.confine`): the worktree, temporary directories and uv's cache. npm's cache is not on
  it, so the gate's install step would fail inside every story on this repository. npm's cache joins the list
  the way uv's did.
- **CI** installs the pinned node in the shared setup action. The static job runs the web checks. The e2e
  image (`ops/e2e/Containerfile`) gains a node build stage, so the container serves the built app.

### Testing

- **Python**, with no network: the feed needs sign-in, sends heartbeats, publishes in-process writes, and
  catches a write from another connection to the board database and growth of an event log. Serving: `/next`
  returns the page for unknown paths and the files under their own paths. At the switch: the redirects. The
  Workflows API: the stories list's paging, filters, search, sort, count and last-activity time; the running
  list including a story the tick started in another process, with its progress; a run's flow for each of
  the four topologies, from recorded event logs.
- **Vitest:** the shared components, and a store refetching when its topic arrives.
- **The browser suite** (`tests/e2e`, Playwright in Apple `container`) covers `/next` the way it covers `/`
  today, with evidence screenshots. The key check: with the board open, a change made from outside the page
  shows within about 2 seconds without a reload, both through the API and as a direct write to the board
  database from another process.
- **A screen is done only when the browser suite passes on it.** Text-matching the served page is not enough.

## Order of work

Each step is its own pull request, except the mockups.

1. **Mockups of every screen**, desktop and phone, in the approved look, shown in the browser for Noah to
   approve. The approved files are committed next to the look reference.
2. **Foundation:** `web/`, the Taskfile tasks, the test gate, npm's cache on the allow list, CI and the e2e
   image, and `/next` serving an app shell.
3. **The live feed**, server and page side.
4. **The shared components.**
5. **The board**, first, because it is the stalest screen and proves the feed and the components.
6. **The Workflows area:** the API's stories options, running list and run flow, then the screens, including
   plan approval and Stop on the workflow view.
7. **The remaining screens.**
8. **The switch.**

## Risks

- **A slower story pipeline.** Every story worktree on this repository installs, checks and builds the web app
  the first time its gate runs. npm's shared cache keeps the install to copying, not downloading, and the
  skip-when-unchanged task keeps it to once per worktree.
- **Burst writes from another process** collapse into one `data_version` change, so the page refetches a whole
  list instead of one item. That is correct, only less precise.
- **Two apps for a while.** With web stories off airlie's To Do, today's app only gets bug fixes during the
  build. Each one has to be carried into the new app too.

## Left to the builder

The exact event topics and payloads, the watcher's interval, file layout under `web/`, the build output
directory's name, the exact gate lines, lint rules, the highlight's duration, which shadcn-vue components to
copy, how the Pinia stores are split, the path of a run's workflow view, each topology's exact stages, the
exact progress formula, and the API's parameter names for paging, filters and sorting.
