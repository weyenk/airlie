# Qwen3-Coder tool calls through vllm-mlx 0.5.0, 2026-10-14: what corrupts them and the proposed fix

Engine as running: `vllm-mlx 0.5.0 serve --continuous-batching --enable-auto-tool-choice --tool-call-parser
qwen3_coder --reasoning-parser qwen3` (mlx 0.32.3, mlx-lm 0.32.0), `base` = Qwen3-Coder-30B-A3B-Instruct-4bit @
6e302ea. Symptoms from the studio trials (runs 20261014-144313-fdd7 and 20261014-165050-e4ef): a tool named `ggrep`,
`run_tests` carrying write_file's `path`/`content`, `GGOOD` for `GOOD`, `("cropping,)`, write/edit steps with
ok=false, one 13-minute step; `engines/main/server.log` holds 52,753 `qwen3_xml_tool_parser ... mismatched tag`
warnings.

**Verdict.** The calls are corrupted inside the engine by vllm-mlx's `qwen3_coder` parser (four separate defects),
plus one defect in airlie's own stream assembly; a corrupted call then poisons the conversation it is replayed into.
No vllm-mlx release fixes it. Proposed: an airlie-owned Qwen3 XML parser that keeps parameter bodies as raw text,
loaded into the engine (so `task serve` / OpenCode get it too) and used by `parse_output`, plus the HTTPEngine
assembly fix. Validated offline: 136 of 136 replays pass (the current parser: 72, or 65 as airlie assembles the
stream). **Implemented 2026-10-15 on Noah's go** (see "Fix" at the end); `server_parsers` stays `true`.

## Reproduction, live (temperature 0, model `base`, one `write_file` tool)

System `You are a coding agent. You change files only through tool calls.`; user `Create the file cmp.py with exactly
this content, using write_file:` + a fenced block. Raw text taken from `/v1/completions` on the same chat-template
prompt (346 prompt tokens on both endpoints, so the generations are identical).

| case | raw model text | `stream: false` | `stream: true` |
|---|---|---|---|
| `if a < b and c & d:` / `return "<none>"` | bare `<function=write_file>` block; no `<tool_call>` token among the 79 generated (78 re-tokenize the text) | correct (`<` and `&` survive; the file's final newline is lost) | one call with `index: -1`, no id, no name, arguments `{"path": "<parameter=content>`; the markup leaks into `content` with the `<parameter=…>` tags removed |
| f-string braces, `("cropping",)`, `"<b>"`, `1 << 3 == 8 and 3 <= 4` | bare block; the model itself writes `("cropping,)` | content silently cut at `assert 1 ` (everything from `<<` on is gone), call reported complete | same as above, content cut at `assert 1 ` |

## Root causes

1. **Split marker on the stream (bare `<function=`).** The server only calls the tool parser once the accumulated
   text contains a marker (`server.py` `stream_chat_completion`), and this parser consumes `delta_text` only.
   `<function=` is three tokens (`<`, `function`, `=`), so the parser never sees `<function` and parses from
   mid-tag: nameless calls at index −1, markup in `content`. With airlie's coder
   prompt the model wraps calls in `<tool_call>` (6 of 6 samples), so this mostly hits clients with their own
   prompts: OpenCode (the reason for `ToolCallStreamRepair` in `serve/app.py`) and minimal prompts (2 of 2).
2. **`<` followed by `<` in a value** (`1 << n`, heredocs `<<EOF`). `_find_next_complete_element` treats a lone `<`
   as a possible partial `<tool_call>` and waits for input that never comes; the rest of the value is dropped and
   the end-of-chunk fallback closes the call. Stream and non-stream; the tool runs and writes a truncated file.
3. **Tool markup inside a value** (a docstring mentioning `` `<function=...>` ``, a parser fixture, a test). The
   chunk is passed to expat as a real `<function>` element nested in the parameter; the next `</parameter>` raises
   `mismatched tag`, and an expat parser that has failed keeps failing: every later chunk raises again (same
   line/column thousands of times: `line 8, column 446` × 9,276) and is dropped, while fallbacks synthesize closes.
   Result: wrong names (`...`, `write_file...` when a client concatenates, `read_fileread_file`), calls merged or
   split (`{…}}, "path": …` in fdd7 step-016, where `GGOOD` appeared). `ggrep` (e4ef step 5) is probably the
   same corruption, but its trace is gone: fix rounds restart step numbering and overwrite
   `trace/<subtask>/step-NNN.json` (e4ef has 79 step events, 40 trace files). The replay found this trigger (airlie's
   own `serve/app.py` sets it off); the log's 52,753 warnings span every session since 2026-10-12, mostly the
   10-12/13 benchmarks (≈30,700 under `small`, ≈22,000 under `base`), and their other triggers are not identified.
4. **Hermes JSON in `<tool_call>`** and **array parameters** come out wrong on the stream. The parser flag is
   server-wide, so `small` (Qwen3-4B, hermes JSON) goes through it too: live, both repro prompts on `small` came
   back as one call with no name and empty arguments. `git` `args` (an array) is mangled.
5. **airlie, `http_engine.py:212`**: `arguments = args if args.startswith("{") else accumulated + args` assumes
   vllm-mlx sends whole arguments, but `qwen3_coder` streams fragments; any value fragment that starts with `{`
   (f-string `{self.id}`, a dict literal, JSON) replaces everything before it. fdd7 step-003 lost its `path`
   this way ("cut off" error).

**Amplifier: replayed history.** A call whose arguments are not JSON is replayed by vllm-mlx as
`{"value": "<the string>"}` (`engine/chat_template_safety.py`), so the template shows the model
`<parameter=value>` holding escaped JSON. The model copies it: e4ef's `run_tests` carried write_file's `path`/
`content` with 1, then 3, then 7 levels of `\"` escaping, and failed 19 times across both rounds. One corrupted
call keeps the rest of the run broken.

**Not the parser.** `("cropping,)` is in the model's own greedy output (raw `/v1/completions` above), then
copied on every rewrite. The 13-minute step (e4ef step 9, `run_tests`, 4096 output tokens = a runaway) ran while
OpenCode sessions on `generalist` (hybrid attention, no prefix cache) re-prefilled ~72k tokens per turn at ~96 s a
step; it decoded at 5.6 tok/s.

## Is there a newer vllm-mlx?

No. PyPI's latest is 0.5.0 (2026-09-17). On `main` the parser file was last changed 2026-06-28 (#597, bare
`<function=>` support); server changes since 0.5.0 (#742 schema-aware argument recovery, #833 duplicate calls for
other parsers) do not touch these paths, and no open issue or PR covers them. Upgrading fixes nothing today.

## Options, replayed offline

Method: 17 cases × (`<tool_call>`-wrapped, bare), tokenized with the model's tokenizer and replayed in frames of 1,
2 and 3 tokens through vllm-mlx 0.5.0's own `stream_chat_completion` and `_parse_tool_calls_with_parser` (fake
engine, real server code); scored as an OpenAI client assembles the stream and as airlie's `HTTPEngine` +
`parse_tool_args` do (what a tool would run with). Cases: `<`/`&`, `<<`, brace-led fragments, HTML, generics and
`->`, indented `edit_file`, two calls, a JSON file, an array parameter, a heredoc, prose mentioning `<function=x>`,
four real files (`story/model.py` and its test from the fdd7 trace, `engine/http_engine.py`, `serve/app.py`),
hermes JSON, a truncated call. Plus the two live outputs and the thinking path (reasoning parser on).

| option | non-stream | stream (OpenAI client) | stream (airlie today) | why |
|---|---|---|---|---|
| keep `qwen3_coder` (today) | 28/34 | 44/102 | 37/102 | defects 1–5 above |
| switch the flag to vllm-mlx's `qwen` | 9/34 | 24/102 | 27/102 | `\s*(.*?)\s*` + `strip()` drop leading indentation and the final newline (indented `edit_file` breaks); string values go through `json.loads`/`literal_eval` (a JSON file is parsed and written back re-indented); prose `<function=name>` becomes a call |
| upgrade vllm-mlx | — | — | — | no release or `main` change addresses this |
| `server_parsers: false` | — | — | — | ruled out 2026-10-13: the managed engine renders tools in its bracket protocol and the coder's calls arrive argument-less; OpenCode would get no tool calls |
| non-streaming coder requests | 28/34 | — | — | removes only the stream defects (1, 4, 5); `<<` and markup-in-value remain; OpenCode untouched |
| **airlie raw-text parser (prototype)** | **34/34** | **102/102** | **102/102** | also both live outputs, hermes JSON, thinking path, truncated call → unterminated JSON (airlie's "cut off" message) |

The prototype (≈250 lines, regex/scan rather than XML): a value ends at the first `</parameter>` followed by
`<parameter=`, `</function>` or the end of the output; exactly one newline after the opening tag and one before
the closing tag are format (the inverse of the chat template's `<parameter=k>\n` + value + `\n</parameter>\n`);
string-typed values stay raw text, other types follow the tool schema (`json.loads`, then `ast.literal_eval`,
never `eval`); `<function=x>` followed by prose is not a call; on the stream it sees every delta from the first
token (`REQUIRES_EAGER_STREAMING`) and emits each call whole, with an id and its index, once `</function>` (and an
optional `</tool_call>`) arrives; `finalize_streaming` closes a call cut off at `max_tokens` with unterminated
arguments. Format limit, shared by every parser of this format: a value containing `</parameter>` immediately
followed by `<parameter=` or `</function>` cannot be told apart from its end.

## Recommended fix

1. `src/airlie/engine/parsers/qwen3_xml_raw.py`: the parser above, registered by `ensure_registered()` next to
   `muse_glimmer` (same precedent; delete it when a vllm-mlx release parses this format correctly).
2. Load it into the engine. vllm-mlx has no parser-plugin flag, so `build_serve_command` starts
   `python -m airlie.engine.vllm_mlx_serve serve …` (register airlie's parsers, then `vllm_mlx.cli.main()`; same
   argv otherwise), and `models.base.tool_call_parser` becomes `qwen3_xml_raw`, which changes
   `--tool-call-parser`. `models.generalist.tool_call_parser` too (Qwen3.8 emits the same XML; that key is the
   server flag under `studio-qwen38`, client-side otherwise). `server_parsers` stays `true`. The parser also
   reads hermes JSON, so `small` and other Qwen-family models served by the same process are covered. Fixes
   airlie, `task serve`/OpenCode and any other client.
3. `HTTPEngine` assembly: OpenAI semantics. A delta carrying a new id for a known index restarts that call (the
   whole-call repeat vllm-mlx sends); otherwise arguments append. Replaces the `startswith("{")` rule.
4. Break the replay loop: when a call's arguments do not parse, record it in the conversation with `{}` so the
   template never shows the model `<parameter=value>` junk; the observation already says what went wrong.
5. Tests: the replay corpus and the two live outputs as unit fixtures for the parser (stream and non-stream);
   HTTPEngine assembly cases (fragments starting with `{`, whole-call repeats); a `task test:live` case sending
   the `<<` + bare-call prompt and asserting exact arguments. `task check` green; `task mutate:changed` on the new
   modules.

Not in this change (follow-ups): keep fix-round traces from overwriting the first round's step files; report the
four engine defects upstream (waybarrios/vllm-mlx) with this repro; `ToolCallStreamRepair` becomes a no-op guard.

## Fix (2026-10-15)

What changed, as recommended: `src/airlie/engine/parsers/qwen3_xml_raw.py` (registered by `ensure_registered()`);
`src/airlie/engine/vllm_mlx_serve.py`, the engine's entry point (`registry.SERVE_ENTRY`, run inside `uv run`),
which registers airlie's parsers before vllm-mlx parses its flags and refuses an unknown `--tool-call-parser`
instead of letting vllm-mlx fall back to its generic parser; `models.base` and `models.generalist` use
`tool_call_parser: qwen3_xml_raw`; `HTTPEngine` appends argument fragments and lets a new id for a known index
restart that call; `NativeTeam` replays a call whose arguments do not parse with `{}`
(`jsonargs.history_arguments`).

Checks: `task check` green; the parser is enrolled in mutation testing with every mutant caught; the parser's
unit tests drive vllm-mlx 0.5.0's own `stream_chat_completion` with a bare call split across tokens. Live, after
`task restart` (engine argv now `python -m airlie.engine.vllm_mlx_serve serve … --tool-call-parser qwen3_xml_raw`):
`task test:live -- tests/live/test_parsers_live.py` 2 passed; both repro prompts from the table above (bare calls)
now give one named call at index 0 whose arguments equal the model's raw parameter text, streamed and not, with
`1 << 3` intact and no markup in the text; `small` (hermes JSON) streams a named call with its arguments; the
`coder` role through `task serve` (8090, OpenCode's door) returns an id'd `write_file` call; no parser warning
in `server.log`.
