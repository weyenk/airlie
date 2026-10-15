"""Parsers airlie adds to vllm-mlx's registries (client-side parsing, design 2026-10-13 §4b)."""

from __future__ import annotations

_done = False


def ensure_registered() -> None:
    global _done
    if _done:
        return
    from vllm_mlx.reasoning import register_parser
    from vllm_mlx.tool_parsers import ToolParserManager

    from airlie.engine.parsers.muse_glimmer import MuseGlimmerReasoningParser, MuseGlimmerToolParser
    from airlie.engine.parsers.qwen3_xml_raw import Qwen3XmlRawToolParser

    register_parser("muse_glimmer", MuseGlimmerReasoningParser)
    ToolParserManager.register_module("muse_glimmer", MuseGlimmerToolParser)
    ToolParserManager.register_module("qwen3_xml_raw", Qwen3XmlRawToolParser)
    _done = True
