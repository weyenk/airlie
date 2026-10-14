# Terminal coding agents on airlie's local models

These agents call your machine directly, so nothing leaves it. They bring their own system prompt and tool set.
Two ways to connect: the **raw catalog models** on the engine (port 8080), or the **roles** on `task serve`
(port 8090), where the role's prompt is placed in front of the client's own system prompt with a line telling
the model to use the client's tools. Start the engines first: `task restart -- --topology studio` (or `task up`);
for roles also `task serve`.

| agent | how it connects | config |
| --- | --- | --- |
| **OpenCode** | OpenAI-compatible provider → `task serve` (roles and raw models) | copy [`opencode.json`](opencode.json) to `~/.config/opencode/opencode.json` (global) or into a project root; `/models` lists the roles `coder`, `architect`, `qa`, `security`, `docs`, `research`, `product`, `ux`, `design` and the raw models `base`, `generalist`, `small` |
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

None of these run the studio: it is a topology, not a chat model. For a whole feature use
`task studio -- "goal" --repo /path/to/project --test-cmd "pytest -q"` and review the resulting
`airlie/<run>/studio` branch.
