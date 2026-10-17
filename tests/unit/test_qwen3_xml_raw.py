"""qwen3_xml_raw: Qwen3-Coder / Qwen3.8 tool calls with parameter bodies kept as raw text (bench 2026-10-14).

Every expected value below is written by hand from the model's chat template: `<parameter=k>\\n` + value +
`\\n</parameter>\\n`, so one newline on each side is format and everything else is the value."""

from __future__ import annotations

import json
import logging
import types
from pathlib import Path
from typing import Any

import pytest

from airlie.engine.parsers import ensure_registered
from airlie.engine.parsers.qwen3_xml_raw import Qwen3XmlRawToolParser

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parsers"


def _tool(name: str, **props: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": props}},
    }


S = {"type": "string"}
REQ: dict[str, Any] = {
    "tools": [
        _tool("write_file", path=S, content=S),
        _tool("edit_file", path=S, old=S, new=S),
        _tool("read_file", path=S),
        _tool("run_tests"),
        _tool("git", args={"type": "array", "items": S}),
        _tool(
            "typed",
            n={"type": "integer"},
            x={"type": "number"},
            flag={"type": "boolean"},
            opts={"type": "object"},
            maybe={"type": ["integer", "null"]},
            word={"type": "integer"},
            setish={"type": "array"},
            s=S,
        ),
        {"type": "function", "function": {"name": "noprops", "parameters": {"type": "object"}}},
    ]
}

CODE = "def check(a, b, c, d):\n    if a < b and c & d:\n        return \"GOOD\"\n    return '<none>'\n"
WRITE_BODY = (
    "<function=write_file>\n<parameter=path>\ncmp.py\n</parameter>\n<parameter=content>\n"
    + CODE
    + "\n</parameter>\n</function>"
)
WRAPPED = "I'll write it.\n\n<tool_call>\n" + WRITE_BODY + "\n</tool_call>"
BARE = "I'll write it.\n\n" + WRITE_BODY
WRITE = ("write_file", {"path": "cmp.py", "content": CODE})

SHIFT_CODE = "x = 1 << 3\ncat <<EOF > out.txt\nEOF\ny = a<<b\n"
SHIFT = (
    "<tool_call>\n<function=write_file>\n<parameter=path>\nbits.sh\n</parameter>\n<parameter=content>\n"
    + SHIFT_CODE
    + "\n</parameter>\n</function>\n</tool_call>"
)
# a value that talks about the format itself: a parser fixture, a docstring, an HTML string
MARKUP_CODE = (
    'RAW = "<tool_call>\\n<function=read_file>\\n<parameter=path>\\napp.py\\n</parameter>\\n</function>"\n'
    'END = "</parameter>" + "</function>"\n'
    "# a bare `<function=...>` opens a call\n"
    'HTML = "<b>GOOD</b>"\n'
)
MARKUP = (
    "<tool_call>\n<function=write_file>\n<parameter=path>\nt.py\n</parameter>\n<parameter=content>\n"
    + MARKUP_CODE
    + "\n</parameter>\n</function>\n</tool_call>"
)
EDIT = (
    "<tool_call>\n<function=edit_file>\n<parameter=path>\nm.py\n</parameter>\n"
    "<parameter=old>\n    return x\n\n</parameter>\n"
    "<parameter=new>\n    if x is None:\n        return 0\n    return x\n\n</parameter>\n"
    "</function>\n</tool_call>"
)
EDIT_CALL = (
    "edit_file",
    {"path": "m.py", "old": "    return x\n", "new": "    if x is None:\n        return 0\n    return x\n"},
)
TWO = (
    "<tool_call>\n<function=write_file>\n<parameter=path>\na.py\n</parameter>\n"
    "<parameter=content>\nx = {}\n</parameter>\n</function>\n</tool_call>\n"
    "<tool_call>\n<function=run_tests>\n</function>\n</tool_call>"
)
TWO_CALLS = [("write_file", {"path": "a.py", "content": "x = {}"}), ("run_tests", {})]
MENTION = (
    "Calls look like <function=name> blocks; here is mine.\n"
    "<tool_call>\n<function=read_file>\n<parameter=path>\nREADME.md\n</parameter>\n</function>\n</tool_call>"
)
TYPED = (
    "<tool_call>\n<function=typed>\n"
    "<parameter=n>\n42\n</parameter>\n<parameter=x>\n2.5\n</parameter>\n"
    "<parameter=flag>\n True\n</parameter>\n"
    '<parameter=opts>\n{"a": [1, 2]}\n</parameter>\n<parameter=maybe>\nnull\n</parameter>\n'
    "<parameter=word>\nforty\n</parameter>\n<parameter=setish>\n{1, 2}\n</parameter>\n"
    "<parameter=s>\n123\n</parameter>\n"
    '<parameter=extra>\n{"kept": "as text"}\n</parameter>\n'
    "</function>\n</tool_call>"
)
TYPED_ARGS = {
    "n": 42,
    "x": 2.5,
    "flag": True,
    "opts": {"a": [1, 2]},
    "maybe": None,
    "word": "forty",
    "setish": "{1, 2}",  # a Python set is no JSON value: kept as written
    "s": "123",
    "extra": '{"kept": "as text"}',
}
GIT = (
    '<tool_call>\n<function=git>\n<parameter=args>\n["status", "--short"]\n</parameter>\n</function>\n'
    "</tool_call>"
)
HERMES = 'Reading it.\n\n<tool_call>\n{"name": "read_file", "arguments": {"path": "x.py"}}\n</tool_call>'
NO_FUNCTION_CLOSE = "<function=read_file>\n<parameter=path>\na.py\n</parameter>\n"
TRUNCATED = (
    "<tool_call>\n<function=write_file>\n<parameter=path>\nbig.py\n</parameter>\n<parameter=content>\n"
    "größe = 1\ndef f():\n    "
)
TRUNCATED_ARGS = '{"path": "big.py", "content": "größe = 1\\ndef f():\\n    '
THREE = (
    TWO
    + "\n<tool_call>\n<function=read_file>\n<parameter=path>\nb.py\n</parameter>\n</function>\n</tool_call>"
)

# (text, expected calls, expected prose) — every complete case is checked non-stream and streamed
CASES: dict[str, tuple[str, list[tuple[str, dict[str, Any]]], str]] = {
    "wrapped": (WRAPPED, [WRITE], "I'll write it."),
    "bare": (BARE, [WRITE], "I'll write it."),
    "shift": (SHIFT, [("write_file", {"path": "bits.sh", "content": SHIFT_CODE})], ""),
    "markup_in_value": (MARKUP, [("write_file", {"path": "t.py", "content": MARKUP_CODE})], ""),
    "edit_indent": (EDIT, [EDIT_CALL], ""),
    "two_calls": (TWO, TWO_CALLS, ""),
    "prose_mention": (
        MENTION,
        [("read_file", {"path": "README.md"})],
        "Calls look like <function=name> blocks; here is mine.",
    ),
    "typed": (TYPED, [("typed", TYPED_ARGS)], ""),
    "array_param": (GIT, [("git", {"args": ["status", "--short"]})], ""),
    "hermes_json": (HERMES, [("read_file", {"path": "x.py"})], "Reading it."),
    "no_function_close": (NO_FUNCTION_CLOSE, [("read_file", {"path": "a.py"})], ""),
    "closed_by_tool_call_end": (
        "<tool_call>\n<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</tool_call>",
        [("read_file", {"path": "a.py"})],
        "",
    ),
    "next_call_without_function_close": (
        "<function=read_file>\n<parameter=path>\na.py\n</parameter>\n<function=run_tests>\n</function>",
        [("read_file", {"path": "a.py"}), ("run_tests", {})],
        "",
    ),
    "three_calls": (THREE, [*TWO_CALLS, ("read_file", {"path": "b.py"})], ""),
    "hermes_json_unclosed": (
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "x.py"}}',
        [("read_file", {"path": "x.py"})],
        "",
    ),
    "no_properties": (
        "<function=noprops>\n<parameter=x>\n1\n</parameter>\n</function>",
        [("noprops", {"x": "1"})],
        "",
    ),
}


def parse(text: str, request: dict[str, Any] | None = REQ) -> tuple[list[tuple[str, Any]], str | None]:
    info = Qwen3XmlRawToolParser().extract_tool_calls(text, request)
    assert info.tools_called is bool(info.tool_calls)
    return [(c["name"], json.loads(c["arguments"])) for c in info.tool_calls], info.content


def stream(text: str, size: int, request: dict[str, Any] | None = REQ) -> tuple[list[dict[str, Any]], str]:
    """Feed text in chunks of `size` as vllm-mlx's server does (accumulated text + delta), then finalize."""
    p = Qwen3XmlRawToolParser()
    calls: list[dict[str, Any]] = []
    content: list[str] = []

    def take(out: dict[str, Any] | None) -> None:
        if out:
            calls.extend(out.get("tool_calls") or [])
            content.append(out.get("content") or "")

    prev = ""
    for i in range(0, len(text), size):
        cur = text[: i + size]
        take(p.extract_tool_calls_streaming(prev, cur, cur[len(prev) :], request=request))
        prev = cur
    take(p.finalize_streaming(text))
    return calls, "".join(content)


@pytest.mark.parametrize("name", list(CASES))
def test_complete_output_parses_to_the_exact_calls_and_prose(name: str) -> None:
    text, want_calls, want_prose = CASES[name]
    calls, prose = parse(text)
    assert calls == want_calls
    assert prose == (want_prose or None)


@pytest.mark.parametrize("size", [1, 2, 3, 7, 64, 10_000])
@pytest.mark.parametrize("name", list(CASES))
def test_streamed_output_matches_at_every_chunk_size(name: str, size: int) -> None:
    text, want_calls, want_prose = CASES[name]
    calls, prose = stream(text, size)
    assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == want_calls
    assert [c["index"] for c in calls] == list(range(len(want_calls)))
    assert all(c["type"] == "function" and c["id"].startswith("call_") for c in calls)
    assert prose.strip() == want_prose
    assert "tool_call>" not in prose and "</function>" not in prose  # no markup leaks into content


def test_calls_get_distinct_ids() -> None:
    info = Qwen3XmlRawToolParser().extract_tool_calls(TWO, REQ)
    ids = [c["id"] for c in info.tool_calls]
    assert len(set(ids)) == 2 and all(i.startswith("call_") for i in ids)


def test_values_are_raw_text_without_a_schema() -> None:
    calls, _ = parse(TYPED, None)
    assert calls[0][1]["n"] == "42" and calls[0][1]["opts"] == '{"a": [1, 2]}'


def test_output_without_a_call_is_returned_unchanged() -> None:
    text = "Just prose: a < b && <b>bold</b>\n"
    info = Qwen3XmlRawToolParser().extract_tool_calls(text, REQ)
    assert (info.tools_called, info.tool_calls, info.content) == (False, [], text)


def test_tool_call_wrapper_without_a_function_or_json_is_not_a_call() -> None:
    for text in ("<tool_call>\nnot a call\n</tool_call>", "<tool_call>\n{'name': 'x'}\n</tool_call>"):
        info = Qwen3XmlRawToolParser().extract_tool_calls(text, REQ)
        assert (info.tools_called, info.content) == (False, text)


def test_hermes_json_without_a_name_is_not_a_call() -> None:
    text = '<tool_call>\n{"arguments": {"path": "x"}}\n</tool_call>'
    assert Qwen3XmlRawToolParser().extract_tool_calls(text, REQ).tools_called is False


def test_think_block_is_dropped_before_parsing() -> None:
    calls, prose = parse("<think>\nI will write a < b.\n</think>\n\n" + WRAPPED)
    assert calls == [WRITE] and prose == "I'll write it."


def test_a_call_cut_off_mid_value_keeps_its_arguments_unterminated() -> None:
    """Cut off at max_tokens: never a parseable call that writes half a file; airlie reports a runaway."""
    info = Qwen3XmlRawToolParser().extract_tool_calls(TRUNCATED, REQ)
    assert info.tools_called and info.tool_calls[0]["name"] == "write_file"
    assert info.tool_calls[0]["arguments"] == TRUNCATED_ARGS
    calls, _ = stream(TRUNCATED, 5)
    assert [c["function"]["arguments"] for c in calls] == [TRUNCATED_ARGS]


def test_a_call_cut_off_inside_a_tag_is_unterminated_too() -> None:
    text = "<function=write_file>\n<parameter=path>\nx.py\n</parameter>\n<parameter=cont"
    info = Qwen3XmlRawToolParser().extract_tool_calls(text, REQ)
    assert info.tool_calls[0]["arguments"] == '{"path": "x.py'


def test_a_call_cut_off_after_its_name_has_no_arguments() -> None:
    info = Qwen3XmlRawToolParser().extract_tool_calls("Reading.\n<function=read_file>\n", REQ)
    assert [(c["name"], c["arguments"]) for c in info.tool_calls] == [("read_file", "{}")]


@pytest.mark.parametrize(
    ("body", "arguments"),
    [
        ('{"name": "f"}', "{}"),
        ('{"name": "f", "arguments": "{\\"a\\": 1}"}', '{"a": 1}'),
        ('{"name": "f", "arguments": {"t": "größe"}}', '{"t": "größe"}'),
    ],
)
def test_hermes_json_arguments_are_passed_on_as_json_text(body: str, arguments: str) -> None:
    info = Qwen3XmlRawToolParser().extract_tool_calls(f"<tool_call>\n{body}\n</tool_call>", REQ)
    assert [(c["name"], c["arguments"]) for c in info.tool_calls] == [("f", arguments)]


@pytest.mark.parametrize(
    "body",
    [
        '{"name": "read_file", "parameters": {"path": "src/a.py"}}',
        '{"name": "read_file", "parameters": "{\\"path\\": \\"src/a.py\\"}"}',
        '{"name": "read_file", "arguments": {"path": "src/a.py"}, "parameters": {"path": "other.py"}}',
        '{"name": "read_file", "parameters": {"path": "o.py"}, "arguments": "{\\"path\\": \\"src/a.py\\"}"}',
    ],
    ids=["parameters_object", "parameters_string", "arguments_win", "arguments_string_win"],
)
def test_hermes_json_parameters_is_read_as_arguments(body: str) -> None:
    """HDS-5: the coder wrote `parameters`; dropping it left read_file without a path."""
    text = f"<tool_call>\n{body}\n</tool_call>"
    assert parse(text) == ([("read_file", {"path": "src/a.py"})], None)
    for size in (1, 7, 10_000):
        calls, _ = stream(text, size)
        assert [(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
            ("read_file", {"path": "src/a.py"})
        ]


EMPTY_CALLS = [
    '<tool_call>\n{"name": "read_file"}\n</tool_call>',
    '<tool_call>\n{"name": "read_file", "arguments": {}}\n</tool_call>',
    "<function=read_file></function>",
    "Reading.\n<tool_call>\n<function=read_file>\n</function>\n</tool_call>",
]


@pytest.mark.parametrize("text", EMPTY_CALLS)
def test_a_call_without_arguments_is_logged_with_its_raw_text(
    text: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="airlie.engine.parsers.qwen3_xml_raw")
    assert parse(text)[0] == [("read_file", {})]  # still returned as an empty call
    raw = repr(text[text.index("<") :])  # one log line: newlines escaped
    want = [("airlie.engine.parsers.qwen3_xml_raw", logging.WARNING)]
    assert [(r.name, r.levelno) for r in caplog.records] == want
    msg = caplog.records[0].getMessage()
    assert msg == "airlie qwen3_xml_raw: read_file call has no arguments; raw call: " + raw
    assert "Reading." not in msg  # the call, not the prose around it
    for size in (1, 10_000):
        caplog.clear()
        calls, _ = stream(text, size)
        assert [c["function"]["arguments"] for c in calls] == ["{}"]
        assert [(r.name, r.levelno) for r in caplog.records] == want
        assert raw in caplog.records[0].getMessage()


def test_a_call_with_arguments_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="airlie.engine.parsers.qwen3_xml_raw")
    for text in (WRAPPED, HERMES, EDIT):
        parse(text)
        stream(text, 3)
    assert caplog.records == []


def test_the_logged_raw_call_is_capped(caplog: pytest.LogCaptureFixture) -> None:
    pad = " " * 5000
    text = '<tool_call>\n{"name": "read_file",' + pad + '"x": 1}\n</tool_call>'
    caplog.set_level(logging.WARNING, logger="airlie.engine.parsers.qwen3_xml_raw")
    parse(text)
    msg = caplog.records[0].getMessage()
    assert msg.endswith(repr(text[:2048]))


def test_a_typed_value_cut_off_mid_call_stays_text_and_unterminated() -> None:
    text = "<function=typed>\n<parameter=n>\n42\n</parameter>\n<parameter=x>\n2."
    info = Qwen3XmlRawToolParser().extract_tool_calls(text, REQ)
    assert info.tool_calls[0]["arguments"] == '{"n": 42, "x": "2.'


def test_prose_is_streamed_as_it_arrives() -> None:
    p = Qwen3XmlRawToolParser()
    assert p.extract_tool_calls_streaming("", "Hel", "Hel", request=REQ) == {"content": "Hel"}
    assert p.extract_tool_calls_streaming("Hel", "Hello wor", "lo wor", request=REQ) == {"content": "lo wor"}


@pytest.mark.parametrize(
    "text", ["<tool_call>\nno call", "x <function=name> blocks", "see <function=foo bar", "<tool_call>\n<fx"]
)
def test_a_marker_that_turns_out_to_be_prose_is_streamed_at_once(text: str) -> None:
    p = Qwen3XmlRawToolParser()
    assert p.extract_tool_calls_streaming("", text, text, request=REQ) == {"content": text}


@pytest.mark.parametrize(
    ("text", "now", "at_end"),
    [
        ("a <", "a ", "<"),
        ("a < b <tool_", "a < b ", "<tool_"),
        ("a <tool_call>", "a ", "<tool_call>"),
        ("a <tool_call>\n<func", "a ", "<tool_call>\n<func"),
        ("a <function=foo", "a ", "<function=foo"),
        ('a <tool_call>\n{"name": "x", ', "a ", '<tool_call>\n{"name": "x", '),
    ],
)
def test_a_fragment_held_back_at_the_end_of_the_output_is_flushed_as_prose(
    text: str, now: str, at_end: str
) -> None:
    p = Qwen3XmlRawToolParser()
    assert p.extract_tool_calls_streaming("", text, text, request=REQ) == {"content": now}
    assert p.finalize_streaming(text) == {"content": at_end}


def test_a_possible_marker_is_held_back_until_it_resolves() -> None:
    p = Qwen3XmlRawToolParser()
    assert p.extract_tool_calls_streaming("", "a <", "a <", request=REQ) == {"content": "a "}
    assert p.extract_tool_calls_streaming("a <", "a <fun", "fun", request=REQ) is None
    assert p.extract_tool_calls_streaming("a <fun", "a <funny", "ny", request=REQ) == {"content": "<funny"}


def test_nothing_is_emitted_inside_a_call_and_the_call_arrives_whole() -> None:
    p = Qwen3XmlRawToolParser()
    outs = []
    prev = ""
    for i in range(len(WRAPPED)):
        cur = WRAPPED[: i + 1]
        outs.append(p.extract_tool_calls_streaming(prev, cur, cur[-1], request=REQ))
        prev = cur
    start = WRAPPED.index("<tool_call>")
    assert all(o is None or set(o) == {"content"} for o in outs[: len(WRAPPED) - 1])
    assert all(o is None for o in outs[start : len(WRAPPED) - 1])
    last = outs[-1]
    assert last is not None and set(last) == {"tool_calls"} and len(last["tool_calls"]) == 1
    call = last["tool_calls"][0]
    assert (call["index"], call["type"], call["function"]["name"]) == (0, "function", "write_file")
    assert json.loads(call["function"]["arguments"]) == WRITE[1]
    assert p.finalize_streaming(WRAPPED) is None  # already emitted: nothing twice


def test_a_bare_call_at_the_end_of_the_output_is_emitted_by_finalize() -> None:
    p = Qwen3XmlRawToolParser()
    assert p.extract_tool_calls_streaming("", BARE, BARE, request=REQ) == {"content": "I'll write it.\n\n"}
    out = p.finalize_streaming(BARE)
    assert out is not None and [c["function"]["name"] for c in out["tool_calls"]] == ["write_file"]


def test_a_new_response_starts_from_a_clean_state() -> None:
    p = Qwen3XmlRawToolParser()
    first = p.extract_tool_calls_streaming("", TWO, TWO, request=REQ)
    p.finalize_streaming(TWO)
    again = p.extract_tool_calls_streaming("", TWO, TWO, request=REQ)
    assert first is not None and again is not None
    assert [c["index"] for c in first["tool_calls"]] == [0, 1]
    assert [c["index"] for c in again["tool_calls"]] == [0, 1]


def test_reset_clears_the_stream_position() -> None:
    p = Qwen3XmlRawToolParser()
    first = p.extract_tool_calls_streaming("x", "x" + TWO, TWO, request=REQ)
    assert first is not None and first["content"] == "x"
    assert [c["index"] for c in first["tool_calls"]] == [0, 1]
    p.reset()
    out = p.extract_tool_calls_streaming("x", "x" + TWO, TWO, request=REQ)
    assert out is not None and out["content"] == "x"
    assert [c["index"] for c in out["tool_calls"]] == [0, 1]


def test_live_bare_call_from_2026_10_14_parses_exactly() -> None:
    """Recorded raw output of `base` at temperature 0 (no `<tool_call>` token): the stream used to lose it."""
    raw = (FIX / "qwen3_coder_bare_live.txt").read_text()
    calls, prose = parse(raw)
    assert calls == [
        (
            "write_file",
            {
                "path": "cmp.py",
                "content": 'def check(a, b, c, d):\n    if a < b and c & d:\n        return "GOOD"\n'
                '    return "<none>"',
            },
        )
    ]
    assert (
        prose == "I'll create the file `cmp.py` with the specified content using the `write_file` function."
    )
    assert [c["function"]["name"] for c in stream(raw, 2)[0]] == ["write_file"]


def test_ensure_registered_serves_it_by_name() -> None:
    from vllm_mlx.tool_parsers import ToolParserManager

    ensure_registered()
    assert ToolParserManager.get_tool_parser("qwen3_xml_raw") is Qwen3XmlRawToolParser


# ---- contract with vllm-mlx 0.5.0's server: the code paths that ran the old parser into the ground ----


class _Engine:
    """Replays deltas through vllm-mlx's streaming handler (its own code, no model)."""

    model_name = "base"
    tokenizer = None
    _tokenizer = None

    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas

    async def stream_chat(self, messages: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
        for i, d in enumerate(self.deltas):
            yield types.SimpleNamespace(
                new_text=d, finished=False, prompt_tokens=3, completion_tokens=i + 1, finish_reason=None
            )
        yield types.SimpleNamespace(
            new_text="",
            finished=True,
            prompt_tokens=3,
            completion_tokens=len(self.deltas),
            finish_reason="stop",
        )


# importing vllm-mlx's server models raises its own pydantic deprecation warning; not ours to fix
VLLM_SERVER = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    import vllm_mlx.server as srv

    ensure_registered()
    monkeypatch.setattr(srv, "_enable_auto_tool_choice", True)
    monkeypatch.setattr(srv, "_tool_call_parser", "qwen3_xml_raw")
    monkeypatch.setattr(srv, "_tool_parser_instance", None)
    monkeypatch.setattr(srv, "_reasoning_parser", None)
    return srv


def _request(srv: Any, stream: bool) -> Any:
    from vllm_mlx.api.models import ChatCompletionRequest

    return ChatCompletionRequest(
        model="base",
        messages=[{"role": "user", "content": "go"}],
        tools=REQ["tools"],
        stream=stream,
        chat_template_kwargs={"enable_thinking": False},
    )


@VLLM_SERVER
async def test_vllm_mlx_stream_delivers_a_bare_call_split_across_tokens(server: Any) -> None:
    # the live tokenization of a bare call: `<`, `function`, `=` arrive in different frames
    deltas = ["I'll write it.\n\n<", "function", "=write", "_file>\n<", "parameter", "=path>\ncmp.py\n</"]
    rest = WRITE_BODY[WRITE_BODY.index("parameter>\n<parameter=content>") :]
    deltas += [rest[i : i + 2] for i in range(0, len(rest), 2)]
    assert "".join(deltas) == BARE
    sse = [
        s
        async for s in server.stream_chat_completion(
            _Engine(deltas), [], _request(server, True), chat_template_kwargs={"enable_thinking": False}
        )
    ]
    frames = [json.loads(s[6:]) for s in sse if s.startswith("data: {")]
    calls = [tc for f in frames for ch in f["choices"] for tc in (ch["delta"].get("tool_calls") or [])]
    content = "".join(ch["delta"].get("content") or "" for f in frames for ch in f["choices"])
    assert [(c["index"], c["function"]["name"]) for c in calls] == [(0, "write_file")]
    assert json.loads(calls[0]["function"]["arguments"]) == WRITE[1]
    assert content.strip() == "I'll write it."
    assert [ch["finish_reason"] for f in frames for ch in f["choices"] if ch["finish_reason"]] == [
        "tool_calls"
    ]


@VLLM_SERVER
def test_vllm_mlx_replays_earlier_calls_through_the_model_template(server: Any) -> None:
    """Native format: history tool calls reach Qwen's own template, not vllm-mlx's bracket text."""
    assert server._detect_native_tool_support() is True


@VLLM_SERVER
async def test_vllm_mlx_non_stream_parses_with_it(server: Any) -> None:
    text, calls = server._parse_tool_calls_with_parser(SHIFT, _request(server, False), None)
    assert [(c.function.name, json.loads(c.function.arguments)) for c in calls] == CASES["shift"][1]


def test_the_example_call_in_the_correction_message_parses() -> None:
    from airlie.teams.native_team import MALFORMED

    out = Qwen3XmlRawToolParser().extract_tool_calls(MALFORMED, REQ)
    assert out.tools_called and [c["name"] for c in out.tool_calls] == ["read_file"]
    assert json.loads(out.tool_calls[0]["arguments"]) == {"path": "README.md"}
