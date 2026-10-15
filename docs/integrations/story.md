# Stories: a Jira issue → stacked draft PRs

`task story` takes a Jira issue (or a written request) through the whole loop on your local models and stops
at a stack of draft pull requests for you to review. **Only a human merges:** airlie has no code that merges;
*What stops a merge*, below, says what else stands in the way.

| stage | who | what you get |
| --- | --- | --- |
| intake | airlie | the issue moves to *In Progress* with a comment |
| refine | product (Qwen3.8) | a story: title, summary, Given/When/Then acceptance criteria |
| layers | architect (Qwen3.8) | at most `story.max_layers` layers, bottom first, every criterion assigned |
| build, per layer | studio: qa writes the failing tests, coder makes them pass, Muse Glimmer reviews both | branch `<ISSUE-KEY>-<story>-<NN>-<layer>`, one commit by `airlie`, gated by `story.test_cmd` |
| publish | `gh stack` | the layers stacked on `story.trunk`, pushed, opened as **draft** PRs with bodies |
| review | airlie | a comment with the PR links, the issue → *In Review* |

A layer that makes no acceptance criterion true (a foundation) has no test-first step: its one build step adds its
own tests. The reviewer's blocking findings get one fix round; whatever it still finds is listed in the PR for you
to check and does not block the layer. The test command is the gate.

If a stage cannot finish, the story stops: the issue gets a comment that says why and goes to *Blocked*, and
running the same command again resumes it (see *When a story stops*).

## Setup (once)

1. **An API token.** At id.atlassian.com → Security → API tokens, create one. A scoped token also needs
   `story.jira.api_url` (below).
2. **`.env`** beside `airlie.yaml`, in the root of this checkout:

   ```bash
   ATLASSIAN_API_KEY=<the token>
   ATLASSIAN_EMAIL=<your Atlassian account email>
   ATLASSIAN_BASE_URL=https://NAME.atlassian.net
   ATLASSIAN_PROJECT_KEY=AIR
   ```

   airlie reads `.env` itself, in memory, when it builds the board: no task loads or exports it, so the studio's
   shell tool, the test gate, `gh`, `git` and the engines never inherit your token, and `ps eww` does not show
   it. **Prefer `.env` to exporting these variables in your shell:** an exported variable reaches every process
   the shell starts, and a variable that is already set in the shell still wins over `.env`. The file is
   git-ignored, so it is never committed (keep it private: `chmod 600 .env`). Lines are `KEY=VALUE`, with an
   optional `export ` prefix, quotes and a trailing ` # comment`; a line that is none of those is skipped with a
   warning that names its number. Each variable wins over the same setting in `story.jira` (`site`, `project`,
   `email`, `token`). The token travels only in the `Authorization` header (HTTP Basic); `task config` never shows
   it, and errors name the variables (or a `.env` line number), never their values.

   **`.env` is one per checkout.** A new clone or git worktree has none, so the checkout you merge into (your main
   one) needs its own, and so does whichever checkout runs `task serve` or the launchd agent: each reads the
   `.env` beside the `airlie.yaml` it was started from.
3. **Statuses.** airlie moves the issue between *To Do* (intake), *In Progress*, *In Review* and *Blocked*; *Done*
   is yours, after the merge. Moving an issue takes the workflow transition into the wanted status, so the
   workflow must offer one from where the issue is; a move it does not offer stops the story with the statuses it
   does offer. If your workflow has no *In Review* or *Blocked*, add it, or name a status it has under
   `story.jira.statuses` in `airlie.local.yaml` (`blocked: null` leaves a stopped issue where it is).
   `story.jira.jql` changes which issues `task story:tick` takes (default: the project's *To Do* issues, in rank
   order).
4. **Repository and tests** in `airlie.local.yaml`:

   ```yaml
   story:
     repo: ~/git/myproject
     test_cmd: "uv run pytest -q"
   ```

5. **GitHub:** `gh extension install github/gh-stack`, and either `gh auth status` (the PRs are yours) or a
   GitHub App whose bot publishes for you (*Publishing as a bot*, below). Optionally, install the *GitHub for
   Jira* app for the repository: branch names start with the issue key, so the issue lists its branches and pull
   requests.
6. `task story:check` lists anything still missing: roles, repository, test command, gh, gh-stack, the remote, your
   gh login or the bot's app, the Jira credentials, the project and its statuses. It prints `ready` when nothing
   is.

A scoped API token reaches Jira through `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3`; set that as
`story.jira.api_url` (https://NAME.atlassian.net/_edge/tenant_info shows the cloud id).

The other `story:` settings are overridden in `airlie.local.yaml` (`task config` prints the resolved values):

| setting | default | what it does |
| --- | --- | --- |
| `story.repo`, `story.test_cmd` | none | the repository, and the gate every layer must pass (`--repo` and `--test-cmd` win) |
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
task story -- --text "Let users upload an avatar (PNG, max 2 MB)" --repo ~/git/myproject --test-cmd "uv run pytest -q"
```

Pass an issue (its key, a `/browse/AIR-7` link, or a board link with `selectedIssue=AIR-7` in it) or `--text`, not
both. The command prints the outcome and the PR links, then the paths of `state.json` and `events.jsonl`, and exits
0 when the story finished and 1 when it stopped.

A story's directory is `~/.local/state/airlie/stories/<issue key>/` (`text-<hash>` for a written request, so the
same text resumes the same story), with `state.json`, `events.jsonl` and the PR bodies; follow a running story with
`tail -f` on its `events.jsonl`. A `--text` story needs no board: its notes go to `notes.md` there.

Stories are built one at a time. `task story`, the tick and the stories `task serve` starts share a lock: while one
is in flight, `task story` says so and exits, and the tick logs `busy`.

## What you see on the issue

The story comments on the issue at each step (every comment starts with **airlie**) and moves it at the start and
the end:

| when | the comment says | the issue |
| --- | --- | --- |
| picked up | `picked this up; progress in .../events.jsonl` | *To Do* → *In Progress* |
| refined | `story: <title>` and one line per acceptance criterion (Given/When/Then) | |
| layers planned | `layers, bottom first:` and a numbered list: each layer's title and the criteria it owns (`enabling: no criterion` for a foundation) | |
| a layer built | `layer 01/03 built: <title> (<test command> passes)`, one comment per layer | |
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
| `'new'` | the pick-up itself (Jira refused the move to *In Progress*, or the first comment) |
| `'intake'` | refining the story (or finding the trunk) |
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
- a layer changed nothing, or `story.test_cmd` fails on it. Its branch and worktree are kept for you to look at, and
  the comment names them;
- `story.trunk` does not exist in the repository, `git` or `gh` fails or runs over 10 minutes (a push, a PR), or Jira
  refuses a move;
- publishing as the bot: the remote is not a github.com repository (the story stops before anything runs), or the
  app is not installed on it, or its key is wrong (the layers stop before they are built; the stop says how to fix
  it).

**Resume** by fixing the cause and running the same command again (`task story -- AIR-7`; for a request, the same
`--text`). Finished stages are skipped, the issue goes back to *In Progress* with `resumed after '<stage>'`, and only
the stopped stage runs again. A failed layer's kept worktree and branch are removed first and the layer is rebuilt
from its base, so copy anything you want from them before you run it. The refined story, the layer plan and the
commit the story builds on (the trunk as fetched when it started) are kept, so editing the issue after the refine
changes nothing: to start over, delete the story's `state.json` (its events, notes and PR bodies stay beside it)
and the layer branches it made. To make the tick retry a blocked story, move the issue back to *To Do*.

**A signal or a cancel** (Ctrl-C, SIGTERM, SIGHUP, or `DELETE` on a `task serve` job) stops the story at once. airlie
kills the `gh` it started so nothing keeps pushing, comments `stopped by a signal or a cancel during '<stage>'; it
is back in To Do and the next run resumes it`, moves the issue to *To Do* (waiting at most 10 s for Jira) and
exits. The tick takes *To Do* issues, so its next beat resumes the story from its last finished stage.

**A killed airlie** (SIGKILL, a crash, a power cut) cannot say so: the issue stays where the last step left it
(normally *In Progress*), with no stop comment. Run the same command to resume.

## From OpenCode

`/story <issue key, issue link or request>` in OpenCode (it runs on the `chief` agent; see `local-agents.md`). The
chief calls `airlie_story`, a tool of the `airlie mcp` server that `task opencode:install` registers in OpenCode's
`opencode.json`; it starts the same pipeline through `task serve`, and the chief follows it with `airlie_status`. The
story is built in the git repository `airlie mcp` was started in, or in the `repo` the tool is given, or else in
`story.repo`. `airlie_story` passes on a test command only if it is `story.test_cmd` or a line of `AIRLIE_TEST_CMDS`
in the server's environment, and refuses any other; left out, it uses `story.test_cmd`. A story that stops is a
finished job whose result carries the reason, so the chief reports it as stopped, with the reason instead of PR
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

`POST /v1/airlie/stories` takes exactly one of `card` (an issue key or link) or `text`, and optionally `repo` and
`test_cmd` (`story.repo` and `story.test_cmd` otherwise). It answers `202` with
`{"job_id": "story-AIR-7", "kind": "story"}`. A bad issue reference, or a story that cannot start (no repository,
no test command, a role that is not a specialist), is a `400` with the reason before anything runs; the same story
already running is a `409`. `GET /v1/airlie/jobs/{id}?since=N&wait=S` holds its reply until there is a new event,
the job ends or `S` seconds pass (at most 300), and answers `state` (`running`, `done`, `failed`, `cancelled`),
`events`, `next` (pass it as the next `since`) and, at the end, `result` or `error`. A story that stops is a `done`
job whose `result.blocked` holds the reason (`result` also has `stage`, `prs` and `dir`); a second story while one
is in flight is accepted and then fails with `StoryBusy`. `DELETE` cancels, as above. Jobs live in the `task serve`
process: restarting it forgets them, and the story's directory stays.

**Without `--api-key`, `task serve` lets any program on this machine start stories**, which push branches and open
draft PRs, and the request's `test_cmd` runs in a shell. Start it with `task serve -- --api-key SECRET` (it also
reads `AIRLIE_SERVE_API_KEY`) to require `Authorization: Bearer SECRET` on every `/v1` route. OpenCode then needs the
same secret twice: `AIRLIE_SERVE_API_KEY` in the `environment` of the `mcp.airlie` entry of its `opencode.json` (for
the `airlie_*` tools; see `local-agents.md`), and `apiKey` in the `airlie` provider (for chat).

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
  checkout's `airlie.yaml`, `airlie.local.yaml` (`story.repo` and `story.test_cmd` must be set there) and `.env`.
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
| `no board configured (...)` | no Jira site and project in the environment, this checkout's `.env` or `story.jira` |
| `AIR-7 <title>: review` | the story finished: PRs open, the issue in *In Review* |
| `AIR-7 <title>: blocked (<reason>)` | the story stopped; see *When a story stops* |
| `AIR-7 <title>: cannot start (...)` | the story could not be opened; set aside (below) |
| `AIR-7 <title>: already in review (nothing to do) ...` | a finished story's issue was in *To Do*; moved on (below) |

A beat that fails instead of finishing writes its message with no timestamp: the error itself (no `story.repo` or
`story.test_cmd`, a role that is not a specialist, Jira refusing the credentials or not answering; `task
story:check` names what is missing), or `stopped by SIGTERM: ...` when launchd stopped it.

An issue that a beat leaves in *To Do* is taken again by every later beat, and nothing behind it would ever be built.
So the tick moves two kinds of issue out of *To Do*:

- **A story it cannot open** (its `state.json` cannot be read or is damaged, or the story was started in another
  repository): the issue gets `cannot be picked up: <reason>` and `Needs a person`, and goes to *Blocked* (to
  *In Progress* when `blocked: null`, with a note that says so). The reason names the `state.json` and says whether
  to fix or delete it, or to resume the story in the repository it was started in.
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
2. **Install it on the story repository only** (the app's page → *Install App* → *Only select repositories*).
3. **Set `story.github_app.app_id`** to the app's id, in `airlie.local.yaml` (this checkout's `airlie.yaml` names
   Noah's app). `key_file` is the key's path (`~` is expanded when it is read) and `api_url` GitHub's API.
4. `task story:check` mints a token and prints `publishing to OWNER/REPO as <app>[bot]`; it warns of a key that
   group or others can read, and names what the installation or the token lacks. gh needs no login of yours, but gh
   and gh-stack must be installed.

What changes:

- The PRs are opened by `<app>[bot]`, and each layer's commit is authored and committed by it
  (`<id>+<app>[bot]@users.noreply.github.com`, which GitHub shows as the bot). Without the *Workflows*
  permission, a layer that changes `.github/workflows/` cannot be pushed: the story stops at publish and says so.
- Your credentials are not used. Each step gets a token minted at its start, for the story's repository alone,
  valid for an hour and kept nowhere (not in the state, an event or a file); gh gets an empty configuration
  directory of the step's own, so it never reads your login or the keychain; there is no SSH agent and no git
  over SSH: GitHub's SSH URLs (`git@github.com:`, `ssh://git@github.com/`) are fetched and pushed over HTTPS, and
  git uses HTTPS and nothing else. The repository is the one the remote's URL names when a run starts, before any
  model works in it (the URL as written, before any `insteadOf`).
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

A model can still write the repository's git configuration (`.git/config`, which every worktree shares), and
airlie's own git reads it. So airlie gives its own git these settings on the command line, which outranks the
repository's configuration:

- its commits, the squash and every publish step: no repository hook (`core.hooksPath`) and no fsmonitor;
- the publish steps: no credential helper but yours, as your global and system configuration name them, or, as
  the bot, gh's alone, for github.com; no ssh command but yours (`core.sshCommand` from your global or system
  configuration, else `ssh`) or, as the bot, none (`GIT_SSH_COMMAND=false`); no askpass and no prompt
  (`GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_TERMINAL_PROMPT`);
- as the bot, also: a token for the story's repository alone, and git over HTTPS only.

A push still reads the rest of the repository's configuration: the remote's URLs (`remote.<name>.url` and
`pushurl`), `url.<base>.insteadOf` and `pushInsteadOf`, and git's other settings. As you, a model that redirects
the remote has your branches pushed where it points, with your credentials, and a command it names for a remote
on this machine (`remote.<name>.receivepack`) runs with your environment. As the bot, a redirect to another
GitHub repository is refused (the token covers the story's repository alone), one to another host gets no
credential, and a remote on this machine is refused with the rest of what is not HTTPS.

None of this is an OS sandbox: those processes still run as your macOS user, and a model that deliberately goes
after what is stored for that user (the keychain, `~/.ssh`, the app's key) is not stopped by it.

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
