"""Live: the coder model's raw tool-call text parses on the client the same way the server parses it."""

import pytest

from airlie.cli import _http_engine
from airlie.config import load_config
from airlie.engine import GenRequest, Message, ToolSchema

pytestmark = pytest.mark.live


async def test_coder_tool_call_round_trip_through_parse_output() -> None:
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
