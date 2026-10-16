# Terminal coding agents on airlie's local models

These agents call your machine directly, so nothing leaves it. They bring their own system prompt and tool set.
Two ways to connect: the **raw catalog models** on the engine (port 8080), or the **roles** on `task serve`
(port 8090), where the role's prompt is placed in front of the client's own system prompt with a line telling
the model to use the client's tools. Start the engines first: `task restart -- --topology studio` (or `task up`);
for roles also `task serve`.

| agent | how it connects | config |
| --- | --- | --- |
| **OpenCode** | OpenAI-compatible provider → `task serve` (roles and raw models) | copy [`opencode.json`](opencode.json) to `~/.config/opencode/opencode.json` (global) or into a project root; `/models` lists the roles `coder`, `architect`, `qa`, `security`, `docs`, `research`, `product`, `ux`, `design` and the raw models `base`, `generalist`, `small`; `task opencode:install` adds the `chief` agent with subagents, `/studio` and `/story`, and registers `airlie mcp`, the MCP server behind the `airlie_*` tools |
| **Claude Code** | Anthropic-compatible `/v1/messages` (vllm-mlx serves it) | environment variables below |
| **Qwen Code** | OpenAI-compatible | `OPENAI_BASE_URL=http://127.0.0.1:8080/v1 OPENAI_API_KEY=airlie-local OPENAI_MODEL=base qwen` |

## OpenCode

Verified 2026-10-14 on OpenCode 1.19.0: `opencode run --model airlie/base "create hello.txt …, read it back …"`
wrote the file, read it and answered, through `POST /v1/chat/completions` on the engine. OpenCode 2.0.29 was
checked on 2026-10-15; the known issues at the end of this section say what holds there.

```bash
mkdir -p ~/.config/opencode && cp docs/integrations/opencode.json ~/.config/opencode/opencode.json
cd /path/to/project && opencode            # or: opencode run --model airlie/base "task"
```

If you already have an `opencode.json`, merge the `provider.airlie` block into it and set `"model":
"airlie/generalist"` to make it the default, as the sample does: it is the chief's model, and `opencode run --agent
chief` takes the config's default rather than the agent's own.

The sample also holds the `mcp.airlie` entry that starts airlie's MCP server (see the chief, below), with
`/path/to/airlie` where your airlie checkout goes (in `--project` and in `--config`). `task opencode:install` writes
the real command into `~/.config/opencode/opencode.json`: if you copied the sample there, it replaces the placeholder
command and keeps the rest of the entry. If you copy the sample or merge the entry by hand, edit that path yourself,
or OpenCode cannot start the server.

The sample's default model is `airlie/generalist`, the chief's (see below). Pick `airlie/coder` for coding
(Qwen3-Coder with the coder persona), `airlie/architect` for a plan, `airlie/qa` for tests; the generalist roles
(`docs`, `research`, `product`, `ux`, `design`) run on Qwen3.8.
Each role's prompt is prepended to OpenCode's own system prompt by `task serve`. The `limit.context` values
assume the studio profile (`max_kv_size: 131072`); under the single profile the engine caps at 65536. The thinking
models (`architect`, `research`, `generalist`) have `limit.output` 16384, as their `max_tokens` in `airlie.yaml`:
OpenCode sends it as `max_tokens`, which caps the thinking and the reply together, so at 4096 they could spend it
all thinking and return an empty reply (`finish_reason: "length"`). For a role that thinks, `task serve` also sends
its `thinking_budget` as `thinking_token_budget`, the per-request field vllm-mlx reads to cap reasoning tokens (one
the client sends itself wins). `base`, `generalist` and `small` are the raw models (no persona). Everything goes
through `task serve` rather than the engine directly because the role server also repairs streamed tool-call chunks
that arrive without an `id` (vllm-mlx occasionally sends them; OpenCode's AI SDK rejects them with "Expected 'id' to
be a string").

### The chief: delegation, the studio and stories

`task opencode:install` copies a `chief` agent (switch to it with Tab), four subagents and two commands into
`~/.config/opencode`, and registers airlie's MCP server in `~/.config/opencode/opencode.json`. Run it from the
checkout you keep, your main one (see *Install* below). Restart OpenCode after installing, and keep `task serve`
running. The agent and command files are copies: run the task again after you update them.

- The **chief** runs on the raw `generalist` (Qwen3.8), the sample's default model (`opencode run --agent chief`
  takes the config's default, not the agent's own: see the known issues). It never edits files. It hands small
  changes to a subagent with OpenCode's `subagent` tool (its permission key is still `task`), and to these four
  only: `coder`, `qa`, `docs` or `architect`, whose files name their role models, so `task serve` supplies the
  persona (a hand-off to `coder` was seen to run on `airlie/coder`: see the known issues), not OpenCode's built-in
  `explore`, whose shell has no merge denies. It asks you before it hands work to `coder` or `qa`, which run any
  shell command without asking; `docs` and `architect` ask before each of theirs, so a hand-off to them does not.
  It asks you before every shell command of its own, the read-only ones too: a redirect after a compound statement
  ran unasked under per-command allows, so no allow list stays read-only. `--output` on `git diff` and `git log`,
  `gh api` and the merge commands are denied outright.
- **Features:** `/studio <goal>` (or just ask). `airlie_plan` returns the studio chief's plan; you approve or
  edit it; `airlie_run` builds it on a new branch, committed locally (the roles you approved are pinned);
  `airlie_status` follows it, and `airlie_cancel` stops it.
- **Stories:** `/story <Jira issue key or link | request>` runs the story pipeline (`story.md`) and ends in
  stacked draft PRs. An issue is built in the repository its `repo:<owner>/<name>` label names, and every layer
  must pass the `## Test gate` of that repository's `AGENTS.md`. A story that stops is a finished job whose result
  carries the reason, so `airlie_status` shows `stopped: <reason>` and the chief reports it as stopped, with the
  reason instead of PR links.
- **Give `/story` an issue key or link, or a short request of your own, and `/studio` a short goal in your own
  words. Do not paste an issue's body, or any text you did not write, into either:** OpenCode runs `` !`cmd` ``
  spans in a command's arguments, so pasted text can run a shell command.
- **What stops a merge.** The agents airlie installs (the chief and its four subagents) deny `gh api*`, `gh pr merge*`
  and `gh stack merge*`, and the chief delegates only to those four, and asks you before it hands work to `coder` or
  `qa`. That guards against a model's slip, not against a determined one: the rules match the command line, so a
  wrapper such as `sh -c "gh pr merge ..."` is not caught under `coder` or `qa`, whose shell allows most other
  commands and has your full environment. The studio's own coder has a shell tool that these rules do not govern at
  all, but that shell, its `run_tests` and `git` tools and the test gate start without your credentials (no gh token
  or login, no SSH agent, no git credential helper, no `ATLASSIAN_*`), so a model there cannot naively `gh pr merge`
  or `git push` as you. It can write the repository's git configuration, which airlie's own git reads: airlie pins
  hooks, fsmonitor, replaced objects, credential helpers, the ssh command, askpass, proxies, address lists, the TLS
  check, alternates, submodules and the transports over it. Each publish
  step, as you or as the bot, also refuses a remote, `pushurl` or `insteadOf` rule that would send git anywhere but
  the story's repository on github.com (as the bot, its HTTPS URL alone), and tells gh that repository. As the bot
  it uses a token for that repository alone; as you, a remote on this machine is refused (SSH and HTTPS only), a
  written request's remote that names no github.com repository is not checked, and a setting for the remote's exact
  URL still applies (`story.md`, *What stops a merge*, says exactly what remains). None of that is an OS sandbox: a
  model that deliberately goes after what your macOS user stores (the keychain, `~/.ssh`, `~/.config/airlie/.env`
  and the checkout's `.env` with your Jira token, the GitHub App's private key) is not stopped by it. What really
  stops a merge is GitHub: a ruleset on the trunk that only you can update, which can refuse airlie when it publishes
  as a GitHub App's bot (`story.md`, *Publishing as a bot*) but not when it publishes as you.
- Without `--api-key`, any program on this machine can use `task serve`, and its jobs API starts studio runs (they
  commit on a local branch) and stories (they push branches and open draft PRs). Start it with
  `task serve -- --api-key SECRET`, and give OpenCode the same secret in the `airlie` provider's `apiKey` (chat)
  and, for the tools, in the `environment` described below.
- A `task serve` that is already running was started before the jobs API existed and answers 404 to the tools:
  restart it on this code once the branch is merged.

#### The `airlie_*` tools: an MCP server

OpenCode 2.0 no longer loads tool files, so the tools come from `airlie mcp`, a stdio MCP server that OpenCode
starts and you never run by hand. It serves `plan`, `run`, `status`, `cancel` and `story`, which OpenCode names
`airlie_plan` and so on (in its permission prompts and in the chief's `permission` block; the model reaches them
through `execute` as `tools.airlie.plan(…)`, see the known issues), and each is a client of `task serve`'s jobs API.

- **Install.** `task opencode:install` puts an `mcp.airlie` entry into `~/.config/opencode/opencode.json`:
  `uv run --quiet --project <this checkout> airlie mcp --config <this checkout>/airlie.yaml`, with `AIRLIE_URL` set
  to `http://127.0.0.1:8090`. A missing file is created. An `mcp.airlie` you already have is merged into, not
  replaced: its `type` and `command` are set to these, and every other key and every `environment` entry you added
  stays (`AIRLIE_URL` is added only when it is missing). The task says `created`, `added`, `updated` or `unchanged`.
  When it changes an existing file it first copies it to `opencode.json.<date>-<time>.bak` (an unchanged file gets no
  backup), keeps the file's mode and follows a symlink, and writes the result beside the file and moves it into
  place, so a failed write cannot truncate your config. A file that is not strict JSON (a comment, say), or an
  `mcp`, `mcp.airlie` or `environment` that is not an object, is refused and left as it is, and the error prints the
  entry to add by hand. The task also deletes the `tool/airlie.ts` an earlier install copied. `airlie mcp config`
  prints the entry (`--install` merges it, as the task does).
- **Install from the checkout you keep.** The entry points at the checkout the task ran from, so run it from your
  main checkout. From a `.claude/worktrees/` checkout the task still installs but warns that the checkout
  disappears once its branch is merged, after which OpenCode cannot start `airlie mcp`; run it again from the main
  checkout then.
- **Environment.** `airlie mcp` reads three variables, which you set in the `environment` object of its entry:
  `AIRLIE_URL` (`task serve`'s address), `AIRLIE_SERVE_API_KEY` (sent as `Authorization: Bearer`, and only to an
  `https` or loopback `AIRLIE_URL`) and `AIRLIE_TEST_CMDS` (test commands `airlie_run` accepts, one per line).
  Installing again keeps what you put there; it only resets `type` and `command`.
- **Config.** The installed command pins `--config <this checkout>/airlie.yaml`, so the `story.test_cmd` that
  `airlie_run` accepts is the one in that file (with its `airlie.local.yaml`) wherever OpenCode runs, never an
  `airlie.yaml` that happens to be above the project. An entry made by hand without `--config` takes
  `AIRLIE_CONFIG`, else an `airlie.yaml` above where `airlie mcp` runs, else this checkout's own.
- **Test commands.** `airlie_run` passes on a `test_cmd` only if it is `story.test_cmd` or a line of
  `AIRLIE_TEST_CMDS`; any other is refused, because the command runs in a shell on this machine and a model must not
  choose it. Left out, `airlie_run` uses `story.test_cmd`, else the first `AIRLIE_TEST_CMDS` line, and with neither
  the run has no test gate. `airlie_story` takes no `test_cmd` at all: a story's gate is the `## Test gate` of its
  repository's `AGENTS.md`, read from the trunk (`story.md`, *The test gate*). The chief's file also sets
  `airlie_run` and `airlie_story` to `ask`; the refusal does not depend on that prompt.
- **Time.** An MCP client may cut a call off at 60 s, so every tool returns within about 45 s, even when
  `task serve` hangs. `airlie_plan` waits for the plan for most of that (its request included); when the chief is
  still planning it returns the job id ("still planning"), and the chief calls `airlie_status` with it until the
  plan comes. `airlie_status` waits 40 s by default (at most 43) for news. `airlie_run` and `airlie_story` return
  as soon as the job has started, with its id. When `task serve` does not answer in time, a tool says so: it names
  the job when it knows its id and points at `airlie_status`, and after a request to start a job that timed out
  it warns that the job may have started anyway.
- **Repository.** The tools work in the git repository `airlie mcp` was started in, or in the `repo` they are
  given; for a written request `airlie_story` falls back to `story.repo`. For a Jira issue, `airlie_story` sends no
  repository and refuses a `repo`: the issue's `repo:` label names its repository, and only a person's
  `task story --repo` builds it elsewhere.

#### Known issues

Checked live on OpenCode 2.0.29 on 2026-10-15, against the real engines with the engine's tool-call parser fixed:

- **Tool calls work.** A local model's calls reach OpenCode's tools and, through `airlie mcp`, `task serve`; the
  earlier `OpenAI Chat tool call delta is missing id or name` is gone with the parser fix.
- **Code mode.** OpenCode 2.0 shows the model its own tools and one `execute` tool, and airlie's tools are
  reachable only inside `execute`'s JavaScript: `await tools.airlie.plan({goal})`, `tools.airlie.run({goal, plan})`,
  `tools.airlie.status({job_id})`, `tools.airlie.cancel({job_id})` and `tools.airlie.story({card})` (or `{text}`).
  The chief, `/studio` and `/story` say so, because asked for `airlie_run` by name the model answers that it has
  no such tool. Permissions hold there: `tools.airlie.run` inside `execute` asked as `airlie_run`, so the keys in
  the chief's `permission` block (`airlie_run`, `airlie_story`) stay as they are.
- **A non-interactive `opencode run` auto-rejects every ask,** the chief's `airlie_run` and `airlie_story` and every
  shell command it runs among them: `git status` under the chief asked, and was auto-rejected. Approve those in the
  TUI.
- **A subagent runs on its own model.** The chief handed `coder` a task, and the coder's own session ran on
  `airlie/coder`, its own model, not the chief's. That was checked for `coder` only.
- **`opencode run --agent chief` uses the config's default model, not the agent's own** (`opencode debug agents`
  resolves the chief to `airlie/generalist` all the same). On `airlie/coder` the chief degenerated after a
  rejection and repeated one shell command, so the sample's default is now `airlie/generalist`, and with it
  `opencode run --agent chief` without `--model` ran the chief on `airlie/generalist`. An `opencode.json` copied
  from an earlier sample still defaults to `airlie/coder`, and `task opencode:install` does not change the model:
  set `"model": "airlie/generalist"` there, or pass `--model airlie/generalist` to `opencode run`.
- **No allow list stays read-only, so the chief asks before every shell command.** `gh pr view 1 > pr.txt` ran
  unasked under `gh pr view *` (its `*` matched the redirect), and a redirect after a compound statement ran
  unasked under the plain git allows: `(git status) > comp1.txt`, `{ git status; } > comp2.txt` and
  `for i in 1; do git status; done > comp3.txt` each wrote their file. OpenCode matches each command inside a
  compound statement by its own text and the redirect is part of none of them, so no rule can close it (and
  `edit: deny` does not cover a shell redirect). What held: `gh pr view 1 & touch F` was split into two commands and
  asked, and so were `git diff > F` and `GIT_EXTERNAL_DIFF=cat git diff`.
- **A redirect with no command runs without asking, under every agent's rules.** Under the chief's `"*": ask`,
  `> empty1.txt` created an empty file unasked, and `> existing.txt` truncated an existing file to zero bytes.
  OpenCode asks only about commands, and that line has none, so no rule closes it; what it writes is always empty.

Not from that check, but what the rules leave:

- **In the OpenCode door, the person approving is the control.** Every OpenCode agent runs as you. What you
  approve: each shell command the chief runs, each hand-off to `coder` or `qa`, and `airlie_run` and
  `airlie_story` (a hand-off to `docs` or `architect` does not ask: their shells ask before each command). Once you
  approve a hand-off, `coder` and `qa` run any command as you, without asking, with your `gh` login, SSH agent and
  keychain. So approve a hand-off only for a brief you have read. The unattended story pipeline is different: it
  starts its agents without your credentials, and publishes as the bot when `story.github_app.app_id` is set
  (`story.md`, *Publishing as a bot*). *What stops a merge*, above, says what the rules do and do not stop.

## Claude Code

Verified 2026-10-14 (Claude Code 2.1.285, vllm-mlx 0.5.0): a print-mode task that creates a file, reads it
back and summarises completed on `base` with the 20 built-in tools.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=airlie-local
export ANTHROPIC_MODEL=base
export ANTHROPIC_SMALL_FAST_MODEL=small
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=131072     # the engine's window; silences the "unknown model" notice
claude --strict-mcp-config --mcp-config '{"mcpServers":{}}'
```

The two MCP flags matter: with your MCP servers attached, Claude Code sends every MCP tool definition (257 in
one test) and vllm-mlx's Anthropic adapter rejects any tool whose name is longer than 64 characters or contains
characters outside `[A-Za-z0-9_-]` (several Vercel MCP tool names are), which comes back to Claude Code as a
500 on every turn. Leave MCP out of local-model sessions, or pass a `--mcp-config` holding only servers whose
tool names fit.

Claude Code sends its own (large) system prompt and tool definitions; a 30B local model follows them less
reliably than Claude does, so expect more retries on complex edits. `generalist` works the same way with
`ANTHROPIC_MODEL=generalist`.

## The studio

A chat model does not run the studio: it is a topology, not a model. OpenCode's `chief` starts it for you
(`/studio`, above); from the other agents, for a whole feature use
`task studio -- "goal" --repo /path/to/project --test-cmd "pytest -q"` and review the resulting
`airlie/<run>/studio` branch.
