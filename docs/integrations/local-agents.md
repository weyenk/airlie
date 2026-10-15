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

Verified 2026-10-14 on OpenCode 1.19.0 (a 1.19-era result; tried on OpenCode 2.0.29: tool calls fail, see the
known issues at the end of this section): `opencode run --model airlie/base "create hello.txt …, read it back …"`
wrote the file, read it and answered, through `POST /v1/chat/completions` on the engine.

```bash
mkdir -p ~/.config/opencode && cp docs/integrations/opencode.json ~/.config/opencode/opencode.json
cd /path/to/project && opencode            # or: opencode run --model airlie/base "task"
```

If you already have an `opencode.json`, merge the `provider.airlie` block into it and set `"model":
"airlie/base"` (and `agent.build.model`) to make it the default.

The sample also holds the `mcp.airlie` entry that starts airlie's MCP server (see the chief, below), with
`/path/to/airlie` where your airlie checkout goes (in `--project` and in `--config`). `task opencode:install` writes
the real command into `~/.config/opencode/opencode.json`: if you copied the sample there, it replaces the placeholder
command and keeps the rest of the entry. If you copy the sample or merge the entry by hand, edit that path yourself,
or OpenCode cannot start the server.

Pick `airlie/coder` for coding (Qwen3-Coder with the coder persona), `airlie/architect` for a plan,
`airlie/qa` for tests; the generalist roles (`docs`, `research`, `product`, `ux`, `design`) run on Qwen3.8.
Each role's prompt is prepended to OpenCode's own system prompt by `task serve`. The `limit.context` values
assume the studio profile (`max_kv_size: 131072`); under the single profile the engine caps at 65536. The thinking
models (`architect`, `research`, `generalist`) have `limit.output` 16384, as their `max_tokens` in `airlie.yaml`:
OpenCode sends it as `max_tokens`, which caps the thinking and the reply together, so at 4096 they could spend it
all thinking and return an empty reply (`finish_reason: "length"`). `base`, `generalist` and `small` are the raw
models (no persona). Everything goes through `task serve` rather than the engine directly because the role server
also repairs streamed tool-call chunks that arrive without an `id` (vllm-mlx occasionally sends them; OpenCode's AI
SDK rejects them with "Expected 'id' to be a string").

### The chief: delegation, the studio and stories

`task opencode:install` copies a `chief` agent (switch to it with Tab), four subagents and two commands into
`~/.config/opencode`, and registers airlie's MCP server in `~/.config/opencode/opencode.json`. Run it from the
checkout you keep, your main one (see *Install* below). Restart OpenCode after installing, and keep `task serve`
running. The agent and command files are copies: run the task again after you update them.

- The **chief** runs on the raw `generalist` (Qwen3.8). It never edits files. It hands small changes to a
  subagent with OpenCode's `task` tool, and to these four only: `coder`, `qa`, `docs` or `architect`, each on its
  role model, so `task serve` supplies the persona (not OpenCode's built-in `explore`, whose shell has no merge
  denies). Its shell runs these git forms without asking, and only these (no wildcard, so no other flag or argument
  rides along): `git status`, `git status --short`, `git diff`, `git diff --stat`, `git diff --cached` and
  `git log --oneline -20`; it also runs `gh pr view` and `gh stack view --json` or `--short`, which may take
  arguments. `--output` on `git diff` and `git log` is denied (it writes a file), as are `gh api` and the merge
  commands, and everything else asks you first.
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
  and `gh stack merge*`, and the chief delegates only to those four. That guards against a model's slip, not against a
  determined one: the rules match the command line, so a wrapper such as `sh -c "gh pr merge ..."` is not caught under
  `coder` or `qa`, whose shell allows most other commands and has your full environment. The studio's own coder has a
  shell tool that these rules do not govern at all, but that shell, its `run_tests` and `git` tools and the test gate
  start without your credentials (no gh token or login, no SSH agent, no git credential helper, no `ATLASSIAN_*`), so
  a model there cannot naively `gh pr merge` or `git push` as you. It can write the repository's git configuration,
  which airlie's own git reads: airlie pins hooks, fsmonitor, replaced objects, credential helpers, the ssh command,
  askpass, proxies, address lists, the TLS check, alternates, submodules and the transports over it. As the bot, each
  publish step also refuses any URL but the story's repository and uses a token for that repository alone; as you, a
  remote on this machine is refused (SSH and HTTPS only), but a push still goes where the remote points on those, and
  a setting for the remote's exact URL still applies (`story.md`, *What stops a merge*, says exactly what remains).
  None of that is an OS sandbox: a model that deliberately goes after what your macOS user stores (the keychain,
  `~/.ssh`) is not stopped by it. What really stops a merge is GitHub: a ruleset on the trunk that only you can
  update, which can refuse airlie when it publishes as a GitHub App's bot (`story.md`, *Publishing as a bot*) but not
  when it publishes as you.
- Without `--api-key`, any program on this machine can use `task serve`, and its jobs API starts studio runs (they
  commit on a local branch) and stories (they push branches and open draft PRs). Start it with
  `task serve -- --api-key SECRET`, and give OpenCode the same secret in the `airlie` provider's `apiKey` (chat)
  and, for the tools, in the `environment` described below.
- A `task serve` that is already running was started before the jobs API existed and answers 404 to the tools:
  restart it on this code once the branch is merged.

#### The `airlie_*` tools: an MCP server

OpenCode 2.0 no longer loads tool files, so the tools come from `airlie mcp`, a stdio MCP server that OpenCode
starts and you never run by hand. It serves `plan`, `run`, `status`, `cancel` and `story`, which OpenCode names
`airlie_plan` and so on, and each is a client of `task serve`'s jobs API.

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

Found in a live sandbox check on OpenCode 2.0.29 on 2026-10-15, and open:

- End-to-end tool calls on local models can fail with `OpenAI Chat tool call delta is missing id or name`.
  The engine's Qwen3-Coder tool-call parser drops malformed calls, and a fix is proposed separately.
- `opencode run --agent chief` used the config's default model rather than the agent's `airlie/generalist`; to be
  rechecked in the TUI.

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
