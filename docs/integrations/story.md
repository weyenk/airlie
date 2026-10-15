# Stories: a Jira issue → stacked draft PRs

`task story` takes a Jira issue (or a written request) through the whole loop on your local models and stops
at a stack of draft pull requests for you to review. **Only a human merges:** airlie has no code that merges;
*What stops a merge*, below, says what else stands in the way.

| stage | who | what you get |
| --- | --- | --- |
| intake | airlie | the issue moves to *In Progress* with a comment |
| refine | product (Qwen3.8) | a story: title, summary, Given/When/Then acceptance criteria |
| layers | architect (Qwen3.8) | at most `story.max_layers` layers, bottom first, every criterion assigned |
| build, per layer | studio: qa writes the failing tests, coder makes them pass, Muse Glimmer reviews both | branch `<ISSUE-KEY>-<story>-<NN>-<layer>`, one commit by `airlie`, gated by the repository's test gate (its `AGENTS.md`) |
| publish | `gh stack` | the layers stacked on `story.trunk`, pushed, opened as **draft** PRs with bodies |
| review | airlie | a comment with the PR links, the issue → *In Review* |

A layer that makes no acceptance criterion true (a foundation) has no test-first step: its one build step adds its
own tests. The reviewer's blocking findings get one fix round; whatever it still finds is listed in the PR for you
to check and does not block the layer. The test gate decides.

An issue is built in the repository its `repo:<owner>/<name>` label names (*Which repository*), and every layer
must pass the `## Test gate` of that repository's `AGENTS.md` (*The test gate*).

If a stage cannot finish, the story stops: the issue gets a comment that says why and goes to *Blocked*, and
running the same command again resumes it (see *When a story stops*).

## Setup (once)

1. **An API token.** At id.atlassian.com → Security → API tokens, create one. A scoped token also needs
   `story.jira.api_url` (below).
2. **`~/.config/airlie/.env`**, one file for every checkout (keep it private: `chmod 600`):

   ```bash
   ATLASSIAN_API_KEY=<the token>
   ATLASSIAN_EMAIL=<your Atlassian account email>
   ATLASSIAN_BASE_URL=https://NAME.atlassian.net
   ATLASSIAN_PROJECT_KEY=AIR
   ```

   To move a `.env` you keep beside `airlie.yaml` there:
   `mkdir -p ~/.config/airlie && mv <checkout>/.env ~/.config/airlie/.env && chmod 600 ~/.config/airlie/.env`.
   A `.env` beside `airlie.yaml` still works (git-ignored, and one per checkout: a new clone or git worktree has
   none). Each variable is looked up in the process environment, then in the checkout's `.env`, then in
   `~/.config/airlie/.env`, then in `story.jira` (`site`, `project`, `email`, `token`): the first that has it
   wins. airlie warns when group or others can read `~/.config/airlie/.env`.

   airlie reads these files itself, in memory, when it builds the board: no task loads or exports them, so the
   studio's shell tool, the test gate, `gh`, `git` and the engines never inherit your token, and `ps eww` does not
   show it. **Prefer the file to exporting these variables in your shell:** an exported variable reaches every
   process the shell starts, and a variable that is already set in the shell wins over both files. Lines are
   `KEY=VALUE`, with an optional `export ` prefix, quotes and a trailing ` # comment`; a line that is none of
   those is skipped with a warning that names its number. The token travels only in the `Authorization` header
   (HTTP Basic); `task config` never shows it, and errors name the variables (or a file and line number), never
   their values.
3. **Statuses.** airlie moves the issue between *To Do* (intake), *In Progress*, *In Review* and *Blocked*; *Done*
   is yours, after the merge. Moving an issue takes the workflow transition into the wanted status, so the
   workflow must offer one from where the issue is; a move it does not offer stops the story with the statuses it
   does offer. If your workflow has no *In Review* or *Blocked*, add it, or name a status it has under
   `story.jira.statuses` in `airlie.local.yaml` (`blocked: null` leaves a stopped issue where it is).
   `story.jira.jql` changes which issues `task story:tick` takes (default: the project's *To Do* issues, in rank
   order).
4. **Repositories:** clone each repository stories are built in under `~/git` (`story.repos_root`), give each
   issue a `repo:<owner>/<name>` label (*Which repository*), and give each repository an `AGENTS.md` with a
   `## Test gate` section on its trunk (*The test gate*). A written request (`--text`) is built in `--repo`, else
   `story.repo` in `airlie.local.yaml`.
5. **GitHub:** `gh extension install github/gh-stack`, and either `gh auth status` (the PRs are yours) or a
   GitHub App whose bot publishes for you (*Publishing as a bot*, below). Optionally, install the *GitHub for
   Jira* app for the repository: branch names start with the issue key, so the issue lists its branches and pull
   requests.
6. `task story:check -- AIR-7` lists anything still missing for that issue: roles; its label, checkout, remote,
   trunk and test gate, which it prints, or the reason its story would be blocked; gh, gh-stack, your gh login or
   the bot's app, the Jira credentials, the project and its statuses. Without an issue, `task story:check` checks
   the repository of a written request (`--repo`, or `story.repo`) in its place. Neither runs a model or writes
   anything, and each prints `ready` when nothing is missing.

A scoped API token reaches Jira through `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3`; set that as
`story.jira.api_url` (https://NAME.atlassian.net/_edge/tenant_info shows the cloud id).

The other `story:` settings are overridden in `airlie.local.yaml` (`task config` prints the resolved values):

| setting | default | what it does |
| --- | --- | --- |
| `story.repos_root` | `~/git` | where an issue's `repo:<owner>/<name>` label finds its checkout: the directory named `<name>`, in any case (relative to `airlie.yaml`'s directory; `~` is expanded) |
| `story.repo` | none | the repository a written request (`--text`) is built in (`--repo` wins); an issue's comes from its label, never from here |
| `story.test_cmd` | none | not a story's: a story's gate is its repository's `AGENTS.md` (or `--test-cmd`); this is the command `airlie_run` accepts (`local-agents.md`) |
| `story.trunk`, `story.remote` | `main`, `origin` | the branch the stack is based on, and the remote it is pushed to |
| `story.max_layers` | 5 | the most layers the architect may plan |
| `story.stage_attempts` | 2 | tries for the story and for the layer plan before the story stops |
| `story.topology` | `studio` | the profile story runs load (start the engines with it: `task restart -- --topology studio`) |
| `story.product_role`, `architect_role`, `tests_role`, `build_role` | `product`, `architect`, `qa`, `coder` | which role does what |
| `story.jira.statuses` | *To Do*, *In Progress*, *In Review*, *Blocked* | the names behind `intake`, `building`, `review` and `blocked` |
| `story.github_app.app_id`, `key_file`, `api_url` | none, `~/.config/airlie/github-app.pem`, `https://api.github.com` | publish as that GitHub App's bot (*Publishing as a bot*); none: with your gh login and SSH key |

## Run one

```bash
task restart -- --topology studio
task story -- AIR-7
task story -- https://NAME.atlassian.net/browse/AIR-7
task story -- --text "Let users upload an avatar (PNG, max 2 MB)" --repo ~/git/myproject
```

`--test-cmd "uv run pytest -q"` replaces the repository's test gate for that run, and `--repo PATH` builds an issue
in PATH instead of the checkout its label names (PATH's remote must still name the label's repository).

Pass an issue (its key, a `/browse/AIR-7` link, or a board link with `selectedIssue=AIR-7` in it) or `--text`, not
both. The command prints the outcome and the PR links, then the paths of `state.json` and `events.jsonl`, and exits
0 when the story finished and 1 when it stopped.

A story's directory is `~/.local/state/airlie/stories/<issue key>/` (`text-<hash>` for a written request, so the
same text resumes the same story), with `state.json`, `events.jsonl` and the PR bodies; follow a running story with
`tail -f` on its `events.jsonl`. A `--text` story needs no board: its notes go to `notes.md` there.

Stories are built one at a time. `task story`, the tick and the stories `task serve` starts share a lock: while one
is in flight, `task story` says so and exits, and the tick logs `busy`.

## Which repository: the issue's `repo:` label

Label each issue with the GitHub repository it is for: one label `repo:<owner>/<name>`, such as
`repo:weyenk/OpenHand`. The prefix may be in any case (`REPO:`); owner and name are GitHub's characters (letters,
digits, `.`, `_` and `-`). Models cannot move a label: agents get no Jira credentials, and airlie itself only
comments on issues and moves them.

The story is built in the checkout of that repository under `story.repos_root` (default `~/git`): the directory
named `<name>`, in any case (`~/git/openhand` for `repo:weyenk/OpenHand`). It must be a git repository whose
`story.remote` (`origin`), as written in its configuration (`remote.origin.url`, its first URL; an `insteadOf` rule
does not count), names that repository on github.com, as `git@github.com:<owner>/<name>.git`,
`ssh://git@github.com/<owner>/<name>.git` or `https://github.com/<owner>/<name>.git` (`.git` optional, any case).

- `task story -- AIR-7 --repo PATH` builds in PATH instead (another clone, say); its remote must still name the
  label's repository.
- A written request (`--text`) has no labels: it is built in `--repo`, else `story.repo`. `story.repo` is for
  written requests only, and an issue never uses it. A written request is pinned to the repository its remote
  names, as written, when it starts (*Publishing as a bot*).
- The label is also the repository the story is pinned to and publishes to. A label changed after the story
  started (to another repository, to none, or to several) stops it: the issue gets `the issue's repo: label
  changed after its story started, from repo:A to repo:B` and is set aside (below). Put the label back, or delete
  the story's `state.json` to start over.

## The test gate: `AGENTS.md`

Every layer must pass its repository's test gate: the commands under a `Test gate` heading of the `AGENTS.md` at
the repository root. OpenHand's, for example:

~~~markdown
## Test gate

```sh
pnpm install --frozen-lockfile --prefer-offline
pnpm static:ratchet
node scripts/run-impacted-tests.mjs
```
~~~

- **Where:** the first heading, at any level (`#` to `######`), whose text is `Test gate`, in any case; then the
  first fenced code block (```` ``` ```` or `~~~`) after it and before the next heading of its level or a higher
  one.
- **The commands:** each line of that block that is not blank and does not start with `#` is one command, one per
  line (no `\` continuation lines). They run in order, in the layer's worktree and without your credentials,
  joined with ` && `: the first that fails fails the layer. CRLF line ends are fine.
- **Read from the trunk:** `AGENTS.md` is read with git from the story's base commit (the trunk as fetched when the
  story started), never from a worktree: a model that edits `AGENTS.md` in its layer, or an uncommitted edit in
  your checkout, changes nothing. A symbolic link to another file of the commit is followed.
- **When:** at the start of the story, once its repository and base commit are known and before refine, so a
  repository without a gate costs no model time.
- **Kept for the story's whole life:** the commands and the blob of `AGENTS.md` they came from are kept in
  `state.json` (`gate`). Every resume uses that gate, whatever `AGENTS.md` says by then; to give a story the new
  one, delete its `state.json` and start it over.
- **`--test-cmd`** on `task story` wins over `AGENTS.md` for that run (it is not kept: the next run without it
  uses the story's gate). Nothing else gives a story its gate: `POST /v1/airlie/stories` and `airlie_story` take
  no test command, and `story.test_cmd` and `AIRLIE_TEST_CMDS` are only for `airlie_run`.
- The coder's brief for each layer says to follow `AGENTS.md` at the repository root.

## What stops a story before it starts

Each of these needs a person: the label is how a person says which repository an issue is for, and the gate is
what a person says every change must pass. A story without either cannot be built safely, so airlie says why on
the issue and moves it out of *To Do*, and the tick goes on to the next issue.

| the problem | the comment says |
| --- | --- |
| no `repo:` label | `the issue names no repository: add the label repo:<owner>/<name> ...` |
| two `repo:` labels, or a malformed one | the labels, quoted |
| no directory named `<name>` in `story.repos_root`, or two (on a case-sensitive volume) | the root, and for none the `git clone` command that puts it there |
| that directory is not a git repository, has no `story.remote`, or its remote names another repository | which, with the clone or `git remote add` command to run, or the URL found and the one the label wants |
| the label changed after the story started | from what to what |
| no test gate on the trunk: no `AGENTS.md`, no `Test gate` section, or no command in its block | the repository, the trunk and its commit, and what to add, with a three-line example |

The label and checkout problems are found before the story is opened, so nothing has started: the issue gets
`cannot be picked up: <reason>` and `Needs a person: fix that, then resume with ...`, and moves to *Blocked* (to
*In Progress* when `story.jira.statuses.blocked` is null, with a note that says so, to keep `task story:tick` from
taking it again). `task story` and `POST /v1/airlie/stories` tell the issue the same way, then say why (exit 1, or a
400). A missing gate is found once the story has started and fetched its trunk: the story stops after `'intake'`
like any other stop, and the issue goes to *Blocked* (with `blocked: null` it stays in *In Progress*, where the
pick-up put it). To retry, fix the cause and move the issue back to *To Do*, or run `task story -- AIR-7`.

`task story:check -- AIR-7` shows all of it without running anything: the label, the checkout and its remote, the
trunk as last fetched, and the gate, or the reason the story would be blocked. No model runs, nothing is fetched,
and nothing is written or posted.

## What you see on the issue

The story comments on the issue at each step (every comment starts with **airlie**) and moves it at the start and
the end:

| when | the comment says | the issue |
| --- | --- | --- |
| picked up | `picked this up; progress in .../events.jsonl` | *To Do* → *In Progress* |
| refined | `story: <title>` and one line per acceptance criterion (Given/When/Then) | |
| layers planned | `layers, bottom first:` and a numbered list: each layer's title and the criteria it owns (`enabling: no criterion` for a foundation) | |
| a layer built | `layer 01/03 built: <title> (<the test gate's commands> passes)`, one comment per layer | |
| published | `stacked draft PRs, bottom layer first:` and the numbered PR links | → *In Review* |

airlie's comments never feed a later story's brief. Comments from people do: they go to product with the
issue's description.

## When a story stops

A stop is never silent. The issue gets `stopped after '<stage>': <reason>` and `Resume: ...`, and goes to *Blocked*.
With `blocked: null` it is left where it was: in *In Progress* once the story was picked up, but still in *To Do* if
the pick-up itself failed, and then every beat tries it again. The stage is the last one that finished, so the
failure is in the next one:

| stopped after | the failure was in |
| --- | --- |
| `'new'` | the pick-up itself (Jira refused the move to *In Progress*, or the first comment), or publishing as the bot could not start (its remote, its installation, its key) |
| `'intake'` | refining the story, or finding the trunk or its test gate |
| `'refine'` | planning the layers, or posting the story's comment |
| `'layers'` | building a layer, or posting the layer plan's comment or a built layer's |
| `'build'` | publishing the PRs |
| `'publish'` | telling the issue (the PR links or the move to *In Review*): the PRs are open |

The refined story, the layer plan and each built layer are saved before their comments are posted, so a comment
that Jira refuses stops the story as `after 'refine'` or `after 'layers'` (the reason names Jira) and costs no work:
the story, the plan and the built layers are kept, and the model is not asked again. The pick-up and the final
note work the other way round: they are posted before their stage is saved, so when Jira refuses one the stop reads
`after 'new'` or `after 'publish'`, and the next run posts it again. The reason is cut to 800 characters, and a
`user:password@` in a URL is masked. The usual reasons:

- product says the request cannot be built as written, and names the decision a person must make: edit the issue
  or comment on it;
- a model's reply was unusable after `story.stage_attempts` tries (the last problem is the reason);
- the repository has no test gate on the trunk (*What stops a story before it starts*);
- a layer changed nothing, or the test gate fails on it. Its branch and worktree are kept for you to look at, and
  the comment names them;
- `story.trunk` does not exist in the repository, `git` or `gh` fails or runs over 10 minutes (a push, a PR), or Jira
  refuses a move;
- publishing as the bot: the remote is not a github.com repository, the app is not installed on it or its key is
  wrong (each stops the story before any model works, and says how to fix it), or the remote now leads somewhere
  else than the repository the story is pinned to (a step refuses; see *Publishing as a bot*).

**Resume** by fixing the cause and running the same command again (`task story -- AIR-7`; for a request, the same
`--text`). Finished stages are skipped, the issue goes back to *In Progress* with `resumed after '<stage>'`, and only
the stopped stage runs again. A failed layer's kept worktree and branch are removed first and the layer is rebuilt
from its base, so copy anything you want from them before you run it. The refined story, the layer plan, the commit
the story builds on (the trunk as fetched when it started) and its test gate are kept, so editing the issue after
the refine, or `AGENTS.md` after the start, changes nothing: to start over, delete the story's `state.json` (its
events, notes and PR bodies stay beside it) and the layer branches it made. To make the tick retry a blocked story,
move the issue back to *To Do*.

**A signal or a cancel** (Ctrl-C, SIGTERM, SIGHUP, or `DELETE` on a `task serve` job) stops the story at once. airlie
kills the `gh` it started so nothing keeps pushing, comments `stopped by a signal or a cancel during '<stage>'; it
is back in To Do and the next run resumes it`, moves the issue to *To Do* (waiting at most 10 s for Jira) and
exits. The tick takes *To Do* issues, so its next beat resumes the story from its last finished stage.

**A killed airlie** (SIGKILL, a crash, a power cut) cannot say so: the issue stays where the last step left it
(normally *In Progress*), with no stop comment. Run the same command to resume.

## From OpenCode

`/story <issue key, issue link or request>` in OpenCode (it runs on the `chief` agent; see `local-agents.md`). The
chief calls `airlie_story`, a tool of the `airlie mcp` server that `task opencode:install` registers in OpenCode's
`opencode.json`; it starts the same pipeline through `task serve`, and the chief follows it with `airlie_status`. An
issue is built in the repository its `repo:` label names (the tool sends a `repo` only when it is given one); a
written request in the git repository `airlie mcp` was started in, or in the `repo` the tool is given, or else in
`story.repo`. `airlie_story` takes no test command: the gate is the repository's `AGENTS.md`. A story that stops is
a finished job whose result carries the reason, so the chief reports it as stopped, with the reason instead of PR
links, and says it resumes when the same command runs again.

**Give `/story` an issue key or link, or a short request of your own, and do not paste an issue's body:** OpenCode
runs `` !`cmd` `` spans in a command's arguments, so pasted text can run a shell command.

How far OpenCode's rules go in keeping an agent from merging is under *What stops a merge*.

## Over HTTP (`task serve`)

```bash
curl -s http://127.0.0.1:8090/v1/airlie/stories -H 'content-type: application/json' -d '{"card": "AIR-7"}'
curl -s 'http://127.0.0.1:8090/v1/airlie/jobs/story-AIR-7?since=0&wait=60'
curl -s -X DELETE http://127.0.0.1:8090/v1/airlie/jobs/story-AIR-7
```

`POST /v1/airlie/stories` takes exactly one of `card` (an issue key or link) or `text`, and optionally `repo`: a
written request's repository (`story.repo` otherwise), or for an issue the checkout to use in place of the one its
label names. A body with anything else, such as a `test_cmd`, is a `422`: a story's gate is its repository's
`AGENTS.md`, never the request's. It answers `202` with `{"job_id": "story-AIR-7", "kind": "story"}`. A bad issue
reference, or a story that cannot start (no repository for a written request, an issue whose label or checkout
cannot be used, a role that is not a specialist), is a `400` with the reason before anything runs; an issue whose
label or checkout cannot be used is also told so, and moved out of *To Do* (*What stops a story before it starts*).
The same story already running is a `409`. `GET /v1/airlie/jobs/{id}?since=N&wait=S` holds its reply until there is
a new event, the job ends or `S` seconds pass (at most 300), and answers `state` (`running`, `done`, `failed`,
`cancelled`), `events`, `next` (pass it as the next `since`) and, at the end, `result` or `error`. A story that
stops is a `done` job whose `result.blocked` holds the reason (`result` also has `stage`, `prs` and `dir`); a second
story while one is in flight is accepted and then fails with `StoryBusy`. `DELETE` cancels, as above. Jobs live in
the `task serve` process: restarting it forgets them, and the story's directory stays.

**Without `--api-key`, `task serve` lets any program on this machine start stories**, which push branches and open
draft PRs. Start it with `task serve -- --api-key SECRET` (it also reads `AIRLIE_SERVE_API_KEY`) to require
`Authorization: Bearer SECRET` on every `/v1` route. OpenCode then needs the same secret twice: `AIRLIE_SERVE_API_KEY`
in the `environment` of the `mcp.airlie` entry of its `opencode.json` (for the `airlie_*` tools; see
`local-agents.md`), and `apiKey` in the `airlie` provider (for chat).

**A `task serve` that is already running (port 8090) must be restarted on this code.** It was started before the
jobs API and stories existed, so it answers 404 to `/v1/airlie/plans`, `/runs`, `/stories` and `/jobs`. Stop it
and run `task serve` again from the checkout that has the code (once the branch is merged, your main checkout).

## Automatic pickup (launchd)

`task story:tick` is one beat, by hand: it builds the top issue of the intake query (the project's *To Do* issues by
rank, or `story.jira.jql`), unless a story is already in flight or the engine is down. `task story:launchd:install`
renders `docs/integrations/story-tick.plist` with this checkout's path and your home, writes it to
`~/Library/LaunchAgents/dev.airlie.story-tick.plist` and loads it; the agent then runs the beat every 15 minutes
(log: `~/.local/state/airlie/story-tick.log`). `task story:launchd:uninstall` unloads and removes it.

- **What it runs:** `/bin/zsh -lc 'cd "<this checkout>" && exec uv run --quiet airlie story tick'`, not `task`.
  launchd stops a job with SIGTERM, go-task does not pass that signal on, and uv does: `exec` makes uv the process
  launchd owns, so the signal reaches airlie, which stops the story and kills `gh`'s process group. The login shell
  puts `uv`, `gh` and `git` on the PATH, but only from the files a login shell reads (`~/.zshenv`, `~/.zprofile`,
  `/etc/zprofile`), not from `~/.zshrc`.
- **Install it from the checkout you keep** (your main one, not a temporary worktree). The beat reads that
  checkout's `airlie.yaml`, `airlie.local.yaml` (`story.repos_root`, if your clones are not in `~/git`) and `.env`
  (or `~/.config/airlie/.env`). Each issue it takes is built in the repository its `repo:` label names.
- **It never starts the engines:** run `task restart -- --topology studio` first, or the beat logs `engine down`.
- **It runs only while you are logged in.** launchd skips a beat while the Mac sleeps and while the previous beat is
  still running, and a beat that takes an issue runs the whole story. Installing again, or uninstalling, while a
  story is being built stops it (the job gets a SIGTERM): the issue goes back to *To Do*, and the next beat, or
  `task story -- AIR-7`, resumes it.

Each beat appends one line to the log: a timestamp, then one of

| the line says | meaning |
| --- | --- |
| `idle: no issue matches the intake query` | nothing in *To Do* (or nothing `story.jira.jql` returns) |
| `busy: a story is in flight` | another `task story`, tick or `task serve` story holds the lock |
| `engine down: start it with ...` | the engines are not up |
| `no board configured (...)` | no Jira site and project in the environment, this checkout's `.env`, `~/.config/airlie/.env` or `story.jira` |
| `AIR-7 <title>: review` | the story finished: PRs open, the issue in *In Review* |
| `AIR-7 <title>: blocked (<reason>)` | the story stopped; see *When a story stops* |
| `AIR-7 <title>: cannot start (...)` | the story could not be placed (its label or checkout) or opened; set aside (below) |
| `AIR-7 <title>: already in review (nothing to do) ...` | a finished story's issue was in *To Do*; moved on (below) |

A beat that fails instead of finishing writes its message with no timestamp: the error itself (a role that is not a
specialist, Jira refusing the credentials or not answering; `task story:check` names what is missing), or
`stopped by SIGTERM: ...` when launchd stopped it.

An issue that a beat leaves in *To Do* is taken again by every later beat, and nothing behind it would ever be built.
So the tick moves two kinds of issue out of *To Do*:

- **A story it cannot place or open:** the issue's label or the checkout it names cannot be used (*What stops a
  story before it starts*), or its `state.json` cannot be read or is damaged, its `events.jsonl` cannot be
  appended to, the story was started in another repository, or (for a written request, until it has published)
  the remote no longer names the GitHub repository the story publishes to. The issue gets `cannot be picked up:
  <reason>` and `Needs a person`, and goes to *Blocked* (to *In Progress* when `blocked: null`, with a note that
  says so). The reason says what to fix: the label, the checkout or its remote, or the file (fix it or delete
  it), or to resume the story in the repository it was started in.
- **A story that already finished**, whose issue was put back in *To Do*: nothing is built again. The issue gets
  `this story is already in review` with the PR links, and moves to *In Review*.

## What lands where

- Branches: one per layer, each based on the layer below, named `<ISSUE-KEY>-<story>-<NN>-<layer>` (a written
  request has no key prefix). Your own checkout is never switched: `gh stack` runs in a worktree of its own, because
  `gh stack init` switches the checkout it runs in.
- PRs: drafts, titled `[n/total] <story>: <layer>`, each with a link to the issue, the layer's criteria, a table of
  how it was built, the reviewer's findings to check, and the stack. Review and merge bottom-up. Text the models
  wrote is defused (a zero-width space goes after `@`, `<` and `&`, and between `#` and the digits after it), so a PR
  notifies no one, links no `#123` reference and renders no HTML from it.
- Commits: one per layer, authored and committed by `airlie <airlie@local>`, or by the bot when you publish as one,
  so AI work is visible in history. The PRs are opened with your own `gh` login, or by the bot.
- The issue: the comments and moves in *What you see on the issue* and *When a story stops*.

## Publishing as a bot

By default the publish steps (the trunk fetch, `gh stack init` and `submit`, each `gh pr view` and `gh pr edit`) run
with your own `gh` login and SSH key: the PRs are yours, and a ruleset that lets you merge lets anything holding
your login merge too. With `story.github_app.app_id` set, they run as a GitHub App's bot instead, which a ruleset
can refuse.

1. **Create a GitHub App** (GitHub → Settings → Developer settings → GitHub Apps → New GitHub App): repository
   permissions *Contents* and *Pull requests* read and write (*Metadata* read comes with them), no webhook (untick
   *Active*), and no *Workflows* permission. Generate a private key, save it as `~/.config/airlie/github-app.pem`
   and keep it to yourself: `chmod 600 ~/.config/airlie/github-app.pem`.
2. **Install it on the repositories stories publish to** (the app's page → *Install App*). *Only select
   repositories* keeps it away from the others; an installation on all of them works too (each story is pinned to
   one repository, below), and `task story:check` says when it can reach them all.
3. **Set `story.github_app.app_id`** to the app's id, in `airlie.local.yaml` (this checkout's `airlie.yaml` names
   Noah's app). `key_file` is the key's path (`~` is expanded when it is read) and `api_url` GitHub's API.
4. `task story:check` mints a token and prints `publishing to OWNER/REPO as <app>[bot]`. It names what the
   installation or the token lacks, and warns of a key that group or others can read, of permissions the app
   holds beyond contents, pull requests and metadata, and of an installation on all your repositories. It also
   names a rule, yours or the repository's (`url.<base>.insteadOf`, `pushInsteadOf`, a `pushurl`), that would send
   the bot's fetch or push anywhere but `https://github.com/OWNER/REPO`: a common global one,
   `url.git@github.com:.insteadOf https://github.com/`, does, and the bot cannot use SSH. gh needs no login of
   yours, but gh and gh-stack must be installed.

What changes:

- The PRs are opened by `<app>[bot]`, and each layer's commit is authored and committed by it
  (`<id>+<app>[bot]@users.noreply.github.com`, which GitHub shows as the bot). Without the *Workflows*
  permission, a layer that changes `.github/workflows/` cannot be pushed: the story stops at publish and says so.
- Your credentials are not used. Each step gets a token minted at its start, for the story's repository alone and
  with contents and pull requests write alone (whatever more the app holds), valid for an hour and kept nowhere
  (not in the state, an event or a file); gh gets an empty configuration directory of the step's own, so it never
  reads your login or the keychain; there is no SSH agent and no git over SSH: GitHub's SSH URLs
  (`git@github.com:`, `ssh://git@github.com/`) are fetched and pushed over HTTPS, and git uses HTTPS and nothing
  else. An app that cannot get a token stops the story before any model works.
- **Each story is pinned to one repository:** an issue's is the one its `repo:` label names; a written request's,
  the one its remote named, as written (before any `insteadOf`), when the story started. `state.json` keeps it.
  Before every step airlie works out where git would fetch from and push to, with every rule applied, and refuses
  anything but that repository's `https://github.com/` URL, before a token is minted. A later run that finds the
  remote naming another repository, or none, cannot open the story until it has published (an issue's checkout is
  checked at every run: *Which repository*), and `task story:tick` sets the issue aside (*Blocked*, with the
  reason).
- **Protect the trunk:** a ruleset on `main` that only you may update keeps the bot, and anything that gets one of
  its tokens, from merging. A private repository on GitHub Free cannot have one: there the bot could merge, and the
  only guard is that airlie has no code that merges.

## What stops a merge

airlie has no code that merges, and the PRs it opens are drafts.

In a story (and in any studio run), whatever runs code a model chose or wrote starts without your credentials: the
coder's shell, `run_tests` and `git` tools, and the test gate. They get no gh token and no gh login (gh points at
an empty configuration), no SSH agent, no git over SSH, no git credential helper, and none of `ATLASSIAN_*` or
`AIRLIE_SERVE_API_KEY`. So a model cannot `gh pr merge` or `git push` through its environment and its tools; only
airlie's publish steps push.

A model can still write the repository's git configuration (`.git/config`, which every worktree shares) and its
files, and airlie's own git reads them. So airlie gives its own git these settings on the command line or in its
environment, which outrank the repository's configuration:

- its commits, the squash and every publish step: no repository hook (`core.hooksPath`) and no fsmonitor;
- every publish step, as you or as the bot:
  - no credential helper but yours, as your global and system configuration name them, or, as the bot, gh's
    alone, for github.com; no ssh command but yours (`core.sshCommand` from your global or system configuration,
    else `ssh`) or, as the bot, none (`GIT_SSH_COMMAND=false`); no askpass and no prompt (`GIT_ASKPASS`,
    `SSH_ASKPASS`, `GIT_TERMINAL_PROMPT`);
  - no command that lists an alternate's refs (`core.alternateRefsCommand`), no proxy (`http.proxy`,
    `remote.<name>.proxy` and `GIT_PROXY_COMMAND`, all empty: a proxy `HTTPS_PROXY` names is off too), no list of
    addresses for hosts (`http.curloptResolve`), the TLS check on (`http.sslVerify`), and no going into
    submodules (`fetch.recurseSubmodules`, `push.recurseSubmodules`, `submodule.recurse`);
  - transports: as you, SSH and HTTPS remotes only (`GIT_ALLOW_PROTOCOL=ssh:https`: no remote on this machine,
    whose `receivepack` or `uploadpack` command git would run itself, and no `ext::`, `git://` or remote
    helper); as the bot, HTTPS only;
- as the bot, also: a token for the story's repository alone, minted per step with contents and pull requests
  write alone; a step that refuses unless git would fetch from and push to that repository's own URL; and the TLS
  check and the address list given again for that exact URL.

What planted repository configuration can still do during a publish step:

- **As you,** where the remote points is the repository's to say, within those transports: a rewritten remote or
  `pushurl` on this machine is refused, so no command it names runs, but one on another SSH or HTTPS host is not.
  A model that rewrites it during a run has that run push your branches there, with your credentials (for a
  remote on github.com, the next run finds another repository and sets the story aside: an issue's at every run,
  a written request's until it has published). Settings for your remote's exact URL (`http.<url>.sslVerify`,
  `http.<url>.curloptResolve`) outrank the general ones above, so over HTTPS a model that also leaves a listener
  running on this machine could read what your credential helper sends.
- **As the bot,** git's other settings still apply, but as far as these settings and the URL check reach, none
  can send the token anywhere but GitHub's address for the story's repository: the proxy is off, the address list
  empty and TLS checked for that exact URL, and any other URL is refused.
- **Either way,** a filter or a diff driver that attributes name (`filter.<driver>.clean`, `smudge`, `process`)
  runs with the step's environment if the step's git reads the working tree's files; the attributes can come from
  a committed `.gitattributes`, from `.git/info/attributes`, or from a file a planted `core.attributesFile` names.
  gh-stack's `init`, `push` and `view` were seen not to read them (a planted clean filter did not run); `submit`
  talks to GitHub and was not checked.

The backstop is GitHub: the token's scope (one repository, one hour, contents and pull requests write) and a
ruleset on the trunk. None of this is an OS sandbox either: those processes still run as your macOS user, and a
model that deliberately goes after what is stored for that user (the keychain, `~/.ssh`, the app's key) is not
stopped by it.

Publishing as you ignores a `core.sshCommand` or `credential.helper` in the repository's own configuration, so a
deploy key set up there for one repository stops being used. To keep one, put those settings in a file your
global configuration includes for that repository's directory, which airlie reads as yours:

```ini
[includeIf "gitdir:~/git/myproject/"]
    path = ~/.gitconfig-myproject
```

In OpenCode, the agents airlie installs (the chief and its four subagents, `coder`, `qa`, `docs` and `architect`)
deny the commands `gh api*`, `gh pr merge*` and `gh stack merge*`, and the chief delegates only to those four. That
guards against a model's slip, not against a determined one: the rules match the command line, so a wrapper such
as `sh -c "gh pr merge ..."` is not caught under `coder` or `qa`, whose shell allows most other commands and runs
with your full environment (OpenCode's shells are not airlie's).

What really stops a merge is GitHub: a ruleset (or branch protection) on the trunk that only you can update. It
can refuse a bot but not you: publish as a bot (above), and it keeps everything but you from merging.

## Not yet

An amigos deliberation (product, qa and architect agreeing a contract), an architect review loop across the
whole stack, several stories at once, a Vikunja board, and answering review comments automatically.
