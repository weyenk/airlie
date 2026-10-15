# Terminal coding agents on airlie's local models

These agents call your machine directly, so nothing leaves it. They bring their own system prompt and tool set.
Two ways to connect: the **raw catalog models** on the engine (port 8080), or the **roles** on `task serve`
(port 8090), where the role's prompt is placed in front of the client's own system prompt with a line telling
the model to use the client's tools. Start the engines first: `task restart -- --topology studio` (or `task up`);
for roles also `task serve`.

| agent | how it connects | config |
| --- | --- | --- |
| **OpenCode** | OpenAI-compatible provider → `task serve` (roles and raw models) | copy [`opencode.json`](opencode.json) to `~/.config/opencode/opencode.json` (global) or into a project root; `/models` lists the roles `coder`, `architect`, `qa`, `security`, `docs`, `research`, `product`, `ux`, `design` and the raw models `base`, `generalist`, `small`; `task opencode:install` adds the `chief` agent with subagents, `/studio`, `/story` and the `airlie_*` tools |
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

Pick `airlie/coder` for coding (Qwen3-Coder with the coder persona), `airlie/architect` for a plan,
`airlie/qa` for tests; the generalist roles (`docs`, `research`, `product`, `ux`, `design`) run on Qwen3.8.
Each role's prompt is prepended to OpenCode's own system prompt by `task serve`. The `limit.context` values
assume the studio profile (`max_kv_size: 131072`); under the single profile the engine caps at 65536. `base`,
`generalist` and `small` are the raw models (no persona). Everything goes through `task serve` rather than the
engine directly because the role server also repairs streamed tool-call chunks that arrive without an `id`
(vllm-mlx occasionally sends them; OpenCode's AI SDK rejects them with "Expected 'id' to be a string").

### The chief: delegation, the studio and stories

`task opencode:install` copies a `chief` agent (switch to it with Tab), four subagents, two commands and five
tools (`airlie_plan`, `airlie_run`, `airlie_status`, `airlie_cancel`, `airlie_story`) into `~/.config/opencode`.
Restart OpenCode after installing, and keep `task serve` running. The files are copies: run the task again after
you update them.

- The **chief** runs on the raw `generalist` (Qwen3.8). It never edits files. It hands small changes to a
  subagent with OpenCode's `task` tool: `coder`, `qa`, `docs` or `architect`, each on its role model, so `task
  serve` supplies the persona.
- **Features:** `/studio <goal>` (or just ask). `airlie_plan` returns the studio chief's plan; you approve or
  edit it; `airlie_run` builds it on a new branch (the roles you approved are pinned); `airlie_status` follows
  it, and `airlie_cancel` stops it.
- **Stories:** `/story <Jira issue key or link | request>` runs the story pipeline (`story.md`) and ends in
  stacked draft PRs. The story is built in the repository OpenCode is open in. A story that stops is a finished
  job whose result carries the reason, so `airlie_status` shows `stopped: <reason>` and the chief reports it as
  stopped, with the reason instead of PR links.
- A test command the model picks for `airlie_run` or `airlie_story` runs in a shell, so OpenCode asks you
  first, as it does for any bash command.
- No agent can merge: each one denies `gh pr merge*`, `gh stack merge*` and `gh api*` (a pull request can also
  be merged through the API).
- The tools call `task serve` at `http://127.0.0.1:8090` (`AIRLIE_URL` in OpenCode's environment changes it).
  Without `--api-key`, any program on this machine can use that server, and its jobs API starts studio runs and
  stories that push branches and open draft PRs; start it with `task serve -- --api-key SECRET`, put the same
  secret in `AIRLIE_SERVE_API_KEY` for OpenCode and in the `apiKey` of the `airlie` provider in `opencode.json`.
- A `task serve` that is already running was started before the jobs API existed and answers 404 to these tools:
  restart it on this code once the branch is merged.

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
