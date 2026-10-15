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

Verified 2026-10-14 (OpenCode 1.19.0): `opencode run --model airlie/base "create hello.txt …, read it back …"`
wrote the file, read it and answered, through `POST /v1/chat/completions` on the engine.

```bash
mkdir -p ~/.config/opencode && cp docs/integrations/opencode.json ~/.config/opencode/opencode.json
cd /path/to/project && opencode            # or: opencode run --model airlie/base "task"
```

If you already have an `opencode.json`, merge the `provider.airlie` block into it and set `"model":
"airlie/base"` (and `agent.build.model`) to make it the default.

The sample also holds the `mcp.airlie` entry that starts airlie's MCP server (see the chief, below), with
`/path/to/airlie` where your airlie checkout goes. `task opencode:install` writes the real path into
`~/.config/opencode/opencode.json` (it replaces the placeholder, after a backup, if you copied the sample there). If
you copy the sample or merge the entry by hand, edit that path yourself, or OpenCode cannot start the server.

Pick `airlie/coder` for coding (Qwen3-Coder with the coder persona), `airlie/architect` for a plan,
`airlie/qa` for tests; the generalist roles (`docs`, `research`, `product`, `ux`, `design`) run on Qwen3.8.
Each role's prompt is prepended to OpenCode's own system prompt by `task serve`. The `limit.context` values
assume the studio profile (`max_kv_size: 131072`); under the single profile the engine caps at 65536. `base`,
`generalist` and `small` are the raw models (no persona). Everything goes through `task serve` rather than the
engine directly because the role server also repairs streamed tool-call chunks that arrive without an `id`
(vllm-mlx occasionally sends them; OpenCode's AI SDK rejects them with "Expected 'id' to be a string").

### The chief: delegation, the studio and stories

`task opencode:install` copies a `chief` agent (switch to it with Tab), four subagents and two commands into
`~/.config/opencode`, and registers airlie's MCP server in `~/.config/opencode/opencode.json`. Restart OpenCode
after installing, and keep `task serve` running. The agent and command files are copies: run the task again after
you update them.

- The **chief** runs on the raw `generalist` (Qwen3.8). It never edits files. It hands small changes to a
  subagent with OpenCode's `task` tool, and to these four only: `coder`, `qa`, `docs` or `architect`, each on its
  role model, so `task serve` supplies the persona (not OpenCode's built-in `explore`, whose shell has no merge
  denies). Its shell runs `git status`, `git diff`, `git log`, `gh pr view` and `gh stack view --json` (or
  `--short`) without asking; `--output` on `git diff` and `git log` is denied (it writes a file), as are `gh api`
  and the merge commands, and every other command asks you first.
- **Features:** `/studio <goal>` (or just ask). `airlie_plan` returns the studio chief's plan; you approve or
  edit it; `airlie_run` builds it on a new branch, committed locally (the roles you approved are pinned);
  `airlie_status` follows it, and `airlie_cancel` stops it.
- **Stories:** `/story <Jira issue key or link | request>` runs the story pipeline (`story.md`) and ends in
  stacked draft PRs. A story that stops is a finished job whose result carries the reason, so `airlie_status` shows
  `stopped: <reason>` and the chief reports it as stopped, with the reason instead of PR links.
- **Give `/story` and `/studio` an issue key or link, or a short request of your own, and do not paste an issue's
  body:** OpenCode runs `` !`cmd` `` spans in a command's arguments, so pasted text can run a shell command.
- **What stops a merge.** The agents airlie installs (the chief and its four subagents) deny `gh api*`,
  `gh pr merge*` and `gh stack merge*`, and the chief delegates only to those four. That guards against a model's
  slip, not against a determined one: the rules match the command line, so a wrapper such as
  `sh -c "gh pr merge ..."` is not caught under `coder` or `qa`, whose shell allows most other commands. What really
  stops a merge is GitHub: branch protection on the trunk (required reviews) and the scope of the token your `gh`
  login uses.
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

- **Install.** `task opencode:install` merges an `mcp.airlie` entry into `~/.config/opencode/opencode.json`:
  `uv run --quiet --project <this checkout> airlie mcp`, with `AIRLIE_URL` set to `http://127.0.0.1:8090`. A missing
  file is created; a file that already has exactly that entry is left alone; any other is first copied to
  `opencode.json.<date>-<time>.bak`. A file that is not strict JSON (a comment, say) is refused and left as it is, and
  the error prints the entry to add by hand. The task also deletes the `tool/airlie.ts` an earlier install copied.
  `airlie mcp config` prints the entry (`--install` merges it, as the task does).
- **Environment.** `airlie mcp` reads three variables, which you set in the `environment` object of its entry:
  `AIRLIE_URL` (`task serve`'s address), `AIRLIE_SERVE_API_KEY` (sent as `Authorization: Bearer`, and only to an
  `https` or loopback `AIRLIE_URL`) and `AIRLIE_TEST_CMDS` (test commands the tools accept, one per line). Installing
  again replaces an entry that differs from the generated one (after a backup), so put what you added to
  `environment` back, or copy it from the backup.
- **Test commands.** `airlie_run` and `airlie_story` pass on a `test_cmd` only if it is `story.test_cmd` (from the
  `airlie.yaml` `airlie mcp` finds above where it runs, else this checkout's) or a line of `AIRLIE_TEST_CMDS`; any
  other is refused, because the command runs in a shell on this machine and a model must not choose it. Leave
  `test_cmd` out to use `story.test_cmd`. The chief's file also sets `airlie_run` and `airlie_story` to `ask`; the
  refusal does not depend on that prompt.
- **Time.** An MCP client may cut a call off at 60 s, so every tool answers within about 45 s. `airlie_plan` waits up
  to 45 s for the plan; when the chief is still planning it returns the job id ("still planning"), and the chief calls
  `airlie_status` with it until the plan comes. `airlie_status` waits up to 45 s (at most 50) for news.
  `airlie_run` and `airlie_story` return at once with a job id.
- **Repository.** The tools work in the git repository `airlie mcp` was started in, or in the `repo` they are
  given; `airlie_story` falls back to `story.repo`.

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
