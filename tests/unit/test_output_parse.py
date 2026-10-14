"""parse_output: vllm-mlx's parser classes applied on the client (design §4b). Server-parsed calls win."""

from pathlib import Path

from airlie.config import ModelSpec
from airlie.engine import GenResult, ToolCall
from airlie.engine.output_parse import parse_output

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parsers"


def result(text: str, **kw) -> GenResult:  # type: ignore[no-untyped-def]
    base = dict(
        text=text, reasoning="", tool_calls=[], finish_reason="stop", prompt_tokens=1, completion_tokens=1
    )
    return GenResult(**{**base, **kw})  # type: ignore[arg-type]


def model(**kw) -> ModelSpec:  # type: ignore[no-untyped-def]
    return ModelSpec(repo="org/m", revision="r", **kw)


def test_qwen3_coder_xml_becomes_a_tool_call() -> None:
    out = parse_output(
        model(tool_call_parser="qwen3_coder"), result((FIX / "qwen3_coder_xml.txt").read_text())
    )
    assert out.finish_reason == "tool_calls"
    assert [(c.name, c.arguments) for c in out.tool_calls] == [("read_file", '{"path": "app.py"}')]
    assert out.text == "I'll look at the file first."
    assert out.tool_calls[0].id.startswith("call_")


def test_qwen_json_call_and_think_block() -> None:
    out = parse_output(
        model(tool_call_parser="qwen", reasoning_parser="qwen3"), result((FIX / "qwen_json.txt").read_text())
    )
    assert out.reasoning == "The user wants the file."
    assert out.tool_calls[0].name == "read_file" and out.tool_calls[0].arguments == '{"path": "app.py"}'


def test_harmony_reasoning_and_final() -> None:
    out = parse_output(model(reasoning_parser="harmony"), result((FIX / "harmony.txt").read_text()))
    assert out.reasoning == "Need to read app.py" and out.text == "Reading it now."


def test_muse_review_one_shot() -> None:
    out = parse_output(
        model(tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer"),
        result((FIX / "muse_self_user.txt").read_text()),
    )
    assert out.reasoning.startswith("The guard") and out.text.startswith('{"verdict"') and not out.tool_calls


def test_muse_tool_channel_without_user_block() -> None:
    out = parse_output(
        model(tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer"),
        result((FIX / "muse_tool.txt").read_text()),
    )
    assert out.text == "" and out.tool_calls[0].name == "read_file" and out.finish_reason == "tool_calls"


def test_server_parsed_calls_win() -> None:
    already = [ToolCall(id="call_1", name="read_file", arguments='{"path": "x"}')]
    raw = (FIX / "qwen3_coder_xml.txt").read_text()
    out = parse_output(
        model(tool_call_parser="qwen3_coder"), result(raw, tool_calls=already, finish_reason="tool_calls")
    )
    assert out.tool_calls == already and out.text == raw


def test_model_without_parsers_is_untouched() -> None:
    raw = (FIX / "qwen3_coder_xml.txt").read_text()
    out = parse_output(model(), result(raw))
    assert out.text == raw and not out.tool_calls


def test_server_reasoning_is_not_reparsed() -> None:
    out = parse_output(model(reasoning_parser="qwen3"), result("<think>x</think>y", reasoning="server"))
    assert out.reasoning == "server" and out.text == "<think>x</think>y"


def test_muse_bare_user_turn_through_parse_output() -> None:
    out = parse_output(
        model(tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer"),
        result((FIX / "muse_bare_user.txt").read_text()),
    )
    assert out.reasoning == "" and out.text == '{"verdict": "approve", "findings": []}' and not out.tool_calls


def test_empty_think_block_is_removed_from_text() -> None:
    out = parse_output(model(reasoning_parser="qwen3"), result("<think>\n\n</think>\n\nanswer"))
    assert out.text == "answer"


# ---- stubbed parsers: pin the glue (ids, argument coercion, finish reason, text handling) ----


class _Info:
    def __init__(self, calls: list[dict], content: str | None = None) -> None:
        self.tools_called = bool(calls)
        self.tool_calls = calls
        self.content = content


class _StubToolParser:
    def __init__(self, calls: list[dict], content: str | None = None) -> None:
        self._calls, self._content, self.resets, self.seen = calls, content, 0, []

    def reset(self) -> None:
        self.resets += 1

    def extract_tool_calls(self, text: str, request: object) -> _Info:
        self.seen.append((text, request))
        return _Info(self._calls, self._content)


class _StubReasoningParser:
    def __init__(self, reasoning: str | None, content: str | None) -> None:
        self._out = (reasoning, content)

    def extract_reasoning(self, text: str) -> tuple[str | None, str | None]:
        return self._out


def _stub_tools(monkeypatch, parser: _StubToolParser) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("airlie.engine.output_parse._tool_parser", lambda name: parser)


def _stub_reasoning(monkeypatch, reasoning: str | None, content: str | None) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "airlie.engine.output_parse._reasoning_parser", lambda name: _StubReasoningParser(reasoning, content)
    )


def test_given_call_id_is_kept_and_missing_id_is_generated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    parser = _StubToolParser([{"id": "abc", "name": "a"}, {"name": "b"}, {"id": "", "name": "c"}])
    _stub_tools(monkeypatch, parser)
    out = parse_output(model(tool_call_parser="x"), result("go"))
    assert out.tool_calls[0].id == "abc"
    for c in out.tool_calls[1:]:
        assert c.id.startswith("call_") and len(c.id) == len("call_") + 8
    assert out.tool_calls[1].id != out.tool_calls[2].id


def test_argument_coercion_dict_string_and_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    parser = _StubToolParser(
        [
            {"name": "a", "arguments": {"path": "x"}},
            {"name": "b", "arguments": '{"raw": true}'},
            {"name": "c"},
        ]
    )
    _stub_tools(monkeypatch, parser)
    out = parse_output(model(tool_call_parser="x"), result("go"))
    assert [c.arguments for c in out.tool_calls] == ['{"path": "x"}', '{"raw": true}', "{}"]
    assert [c.name for c in out.tool_calls] == ["a", "b", "c"]


def test_parser_is_reset_called_with_text_and_no_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    parser = _StubToolParser([{"name": "a"}], content="  leftover \n")
    _stub_tools(monkeypatch, parser)
    out = parse_output(model(tool_call_parser="x"), result("the raw text"))
    assert parser.resets == 1 and parser.seen == [("the raw text", None)]
    assert out.text == "leftover" and out.finish_reason == "tool_calls"


def test_tool_call_with_no_leftover_content_clears_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_tools(monkeypatch, _StubToolParser([{"name": "a"}], content=None))
    assert parse_output(model(tool_call_parser="x"), result("go")).text == ""


def test_cancelled_finish_reason_survives_tool_parsing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_tools(monkeypatch, _StubToolParser([{"name": "a"}]))
    out = parse_output(model(tool_call_parser="x"), result("go", finish_reason="cancelled"))
    assert out.finish_reason == "cancelled" and len(out.tool_calls) == 1


def test_no_tools_called_leaves_text_and_finish_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_tools(monkeypatch, _StubToolParser([], content="ignored"))
    out = parse_output(model(tool_call_parser="x"), result("plain", finish_reason="length"))
    assert out.text == "plain" and out.finish_reason == "length" and not out.tool_calls


def test_reasoning_with_no_content_empties_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reasoning(monkeypatch, "thinking", None)
    out = parse_output(model(reasoning_parser="x"), result("<think>thinking</think>"))
    assert out.reasoning == "thinking" and out.text == ""


def test_no_reasoning_and_no_content_keeps_the_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reasoning(monkeypatch, None, None)
    out = parse_output(model(reasoning_parser="x"), result("untouched"))
    assert out.reasoning == "" and out.text == "untouched"


def test_content_identical_to_text_keeps_the_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reasoning(monkeypatch, None, "same")
    assert parse_output(model(reasoning_parser="x"), result("same")).text == "same"


def test_content_differing_from_text_is_taken_even_without_reasoning(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_reasoning(monkeypatch, None, "cleaned")
    out = parse_output(model(reasoning_parser="x"), result("raw"))
    assert out.text == "cleaned" and out.reasoning == ""
