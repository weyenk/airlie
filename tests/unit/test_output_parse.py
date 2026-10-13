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
