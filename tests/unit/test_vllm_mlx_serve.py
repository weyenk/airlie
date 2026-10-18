"""The engine entry point: vllm-mlx's CLI with airlie's parsers registered first (vllm-mlx 0.5.0 has no parser
plugin flag), refusing a parser name that vllm-mlx would silently replace with its generic parser."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import vllm_mlx.cli
from vllm_mlx.tool_parsers import ToolParserManager

import airlie.engine.parsers as parsers
from airlie.config import load_config
from airlie.engine import vllm_mlx_serve
from airlie.engine.parsers.qwen3_xml_raw import Qwen3XmlRawToolParser
from airlie.engine.process import RealRunner
from airlie.engine.registry import build_serve_command

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml
ENTRY = ["python", "-m", "airlie.engine.vllm_mlx_serve"]


@pytest.fixture
def fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """As in a new engine process: airlie's parsers are not registered yet."""
    monkeypatch.setattr(parsers, "_done", False)
    for name in ("qwen3_xml_raw", "muse_glimmer"):
        monkeypatch.delitem(ToolParserManager.tool_parsers, name, raising=False)


def cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> list[dict[str, Any]]:
    """Replace vllm-mlx's CLI entry with a recorder of what it would have started with."""
    started: list[dict[str, Any]] = []

    def main() -> None:
        started.append({"argv": list(sys.argv), "parsers": dict(ToolParserManager.tool_parsers)})

    monkeypatch.setattr(vllm_mlx.cli, "main", main)
    monkeypatch.setattr(sys, "argv", ["vllm_mlx_serve", *argv])
    return started


@pytest.mark.usefixtures("fresh_process")
def test_vllm_mlx_starts_with_airlie_parsers_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    started = cli(monkeypatch, "serve", "--tool-call-parser", "qwen3_xml_raw", "--port", "8080")
    assert vllm_mlx_serve.main() == 0
    assert [s["argv"] for s in started] == [
        ["vllm_mlx_serve", "serve", "--tool-call-parser", "qwen3_xml_raw", "--port", "8080"]
    ]
    assert started[0]["parsers"]["qwen3_xml_raw"] is Qwen3XmlRawToolParser


@pytest.mark.parametrize(
    "flag", [["--tool-call-parser", "qwen3_xml_rwa"], ["--tool-call-parser=qwen3_xml_rwa"]]
)
def test_an_unknown_tool_parser_stops_the_engine_before_it_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    started = cli(monkeypatch, "serve", *flag)
    assert vllm_mlx_serve.main() == 2
    assert started == []
    assert capsys.readouterr().err == (
        "airlie: unknown --tool-call-parser 'qwen3_xml_rwa': neither vllm-mlx nor airlie registers it\n"
    )


def test_a_known_parser_given_with_equals_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started = cli(monkeypatch, "serve", "--tool-call-parser=qwen3_coder")
    assert vllm_mlx_serve.main() == 0 and len(started) == 1


def test_a_flag_without_its_value_is_left_for_vllm_mlx_to_report(monkeypatch: pytest.MonkeyPatch) -> None:
    started = cli(monkeypatch, "serve", "--tool-call-parser")
    assert vllm_mlx_serve.main() == 0 and len(started) == 1


def test_serve_without_a_tool_parser_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started = cli(monkeypatch, "serve", "--port", "8080")
    assert vllm_mlx_serve.main() == 0 and len(started) == 1


def test_the_serve_command_starts_vllm_mlx_through_airlie_entry_point() -> None:
    argv = build_serve_command(load_config(ROOT / "airlie.yaml", NO_LOCAL), "main", Path("/r.yaml"))
    assert argv[: len(ENTRY) + 3] == [*ENTRY, "serve", "--models-config", "/r.yaml"]


def test_the_engine_runs_inside_the_project_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`uv run` makes `python` the project's interpreter, the one with vllm-mlx and airlie installed."""
    spawned: list[list[str]] = []

    class Popen:
        def __init__(self, cmd: list[str], **kw: Any) -> None:
            spawned.append(cmd)
            self.pid = 7

    monkeypatch.setattr(subprocess, "Popen", Popen)
    assert RealRunner().spawn([*ENTRY, "serve"], tmp_path / "server.log", tmp_path) == 7
    RealRunner().spawn(["llama-server", "-m", "k.gguf"], tmp_path / "server.log", tmp_path)
    assert spawned == [["uv", "run", "--quiet", *ENTRY, "serve"], ["llama-server", "-m", "k.gguf"]]


@pytest.mark.parametrize("topology", [None, "studio"])
def test_the_shipped_engine_parser_keeps_code_intact(topology: str | None) -> None:
    """The parser airlie.yaml gives the engine keeps an indented `<<` line exactly (bench 2026-10-14)."""
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology=topology)
    argv = build_serve_command(cfg, "main", Path("/r.yaml"))
    parsers.ensure_registered()
    parser = ToolParserManager.get_tool_parser(argv[argv.index("--tool-call-parser") + 1])()
    text = (
        "<tool_call>\n<function=write_file>\n<parameter=path>\nb.py\n</parameter>\n"
        "<parameter=content>\n    x = 1 << 3\n\n</parameter>\n</function>\n</tool_call>"
    )
    call = parser.extract_tool_calls(text, None).tool_calls[0]
    assert json.loads(call["arguments"]) == {"path": "b.py", "content": "    x = 1 << 3\n"}


def test_every_parser_named_in_airlie_yaml_resolves() -> None:
    from vllm_mlx.reasoning import get_parser

    parsers.ensure_registered()
    for m in load_config(ROOT / "airlie.yaml", NO_LOCAL).models.values():
        if m.tool_call_parser:
            ToolParserManager.get_tool_parser(m.tool_call_parser)
        if m.reasoning_parser:
            get_parser(m.reasoning_parser)
