"""Live: a coder tool call arrives as a ToolCall through HTTPEngine. With `engines.main.server_parsers: true`
the server parses it and `parse_output` keeps it; with `false` the client parses the raw XML (checked by hand
on a no-parser server on 2026-10-13, see docs/bench/README.md)."""

import pytest

from airlie.cli import _http_engine
from airlie.config import load_config
from airlie.engine import GenRequest, Message, ToolSchema

pytestmark = pytest.mark.live


async def test_coder_tool_call_arrives_as_a_tool_call() -> None:
    cfg = load_config()
    eng = _http_engine(cfg, cfg.engine_for_role("coder"))
    tool = ToolSchema(
        "read_file",
        "Read a file",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    req = GenRequest(
        role="coder",
        messages=[Message(role="user", content="Read app.py with the tool. Do not answer in prose.")],
        sampling=cfg.roles["coder"].sampling,
        max_tokens=200,
        tools=[tool],
        thinking=False,
    )
    res = await eng.generate(req)
    assert res.finish_reason == "tool_calls" and res.tool_calls[0].name == "read_file"
    await eng.stop()
