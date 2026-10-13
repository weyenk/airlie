"""Muse Glimmer channel format (design §4a/§4b): to=self reasoning, to=user answer, to=<tool> atem calls."""

from pathlib import Path

from airlie.engine.parsers import ensure_registered
from airlie.engine.parsers.muse_glimmer import (
    MuseGlimmerReasoningParser,
    MuseGlimmerToolParser,
    split_channels,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parsers"


def fx(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def test_split_channels_self_then_user() -> None:
    parts = split_channels(fx("muse_self_user"))
    assert [r for r, _ in parts] == ["self", "user"]
    assert parts[0][1] == "The guard checks qty < 0 so zero slips through."
    assert parts[1][1].startswith('{"verdict": "request_changes"') and parts[1][1].endswith("]}")


def test_reasoning_parser_moves_self_to_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_self_user"))
    assert reasoning == "The guard checks qty < 0 so zero slips through."
    assert content is not None and content.startswith('{"verdict": "request_changes"')


def test_bare_user_turn_has_no_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_bare_user"))
    assert reasoning is None and content == '{"verdict": "approve", "findings": []}'


def test_unterminated_self_block_is_all_reasoning() -> None:
    reasoning, content = MuseGlimmerReasoningParser().extract_reasoning(fx("muse_unterminated"))
    assert reasoning == "Still thinking about the edge case" and content is None


def test_tool_parser_reads_atem_invoke_and_ignores_self_channel() -> None:
    info = MuseGlimmerToolParser().extract_tool_calls(fx("muse_tool"), None)
    assert info.tools_called and len(info.tool_calls) == 1
    call = info.tool_calls[0]
    assert call["name"] == "read_file"
    assert call["arguments"] == '{"path": "app.py", "lines": [1, 40]}'
    assert info.content is None


def test_tool_parser_without_calls_reports_user_content() -> None:
    info = MuseGlimmerToolParser().extract_tool_calls(fx("muse_bare_user"), None)
    assert not info.tools_called and info.content == '{"verdict": "approve", "findings": []}'


def test_registered_under_muse_glimmer_in_both_registries() -> None:
    from vllm_mlx.reasoning import get_parser
    from vllm_mlx.tool_parsers import ToolParserManager

    ensure_registered()
    ensure_registered()  # idempotent
    assert get_parser("muse_glimmer") is MuseGlimmerReasoningParser
    assert ToolParserManager.get_tool_parser("muse_glimmer") is MuseGlimmerToolParser
