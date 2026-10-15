"""Live: a coder tool call arrives as a ToolCall through HTTPEngine. With `engines.main.server_parsers: true`
the server parses it (airlie's `qwen3_xml_raw`, loaded by `airlie.engine.vllm_mlx_serve`) and `parse_output`
keeps it; with `false` the client parses the raw XML (checked by hand on a no-parser server on 2026-10-13, see
docs/bench/README.md)."""

import json

import pytest

from airlie.cli import _http_engine
from airlie.config import Sampling, load_config
from airlie.engine import GenRequest, Message, ToolSchema
from airlie.tools.fs import WriteFile

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


async def test_code_with_shifts_and_comparisons_arrives_whole() -> None:
    """2026-10-14: vllm-mlx's `qwen3_coder` cut a value at `<<` and turned a bare `<function=` call (this
    prompt makes the model drop `<tool_call>`) into a nameless call with the markup in the text."""
    cfg = load_config()
    eng = _http_engine(cfg, cfg.engine_for_role("coder"))
    code = (
        'def check(a, b, c, d):\n    if a < b and c & d:\n        return (1 << 3) > 2\n    return "<none>"\n'
    )
    req = GenRequest(
        role="coder",
        messages=[
            Message(role="system", content="You are a coding agent. Change files only through tool calls."),
            Message(
                role="user",
                content="Create the file cmp.py with exactly this content, using write_file:\n\n"
                f"```python\n{code}```",
            ),
        ],
        sampling=Sampling(temperature=0.0, top_p=1.0, top_k=0, min_p=0.0, presence_penalty=0.0),
        max_tokens=400,
        tools=[ToolSchema(WriteFile.name, WriteFile.description, WriteFile.parameters)],
        thinking=False,
    )
    res = await eng.generate(req)
    await eng.stop()
    assert res.finish_reason == "tool_calls" and [c.name for c in res.tool_calls] == ["write_file"]
    args = json.loads(res.tool_calls[0].arguments)
    assert args["path"] == "cmp.py"
    assert "if a < b and c & d:" in args["content"] and "(1 << 3) > 2" in args["content"]
    assert args["content"].rstrip().endswith('return "<none>"')
    assert "<function=" not in res.text and "<parameter=" not in res.text
