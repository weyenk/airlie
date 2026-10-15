"""`vllm-mlx serve` with airlie's tool and reasoning parsers registered first.

vllm-mlx 0.5.0 has no parser-plugin flag, so the managed engine starts as
`python -m airlie.engine.vllm_mlx_serve serve …` (`build_serve_command`). An unknown `--tool-call-parser`
would make vllm-mlx fall back to its generic parser without saying so; this entry point refuses to start."""

from __future__ import annotations

import sys

from airlie.engine.parsers import ensure_registered

_FLAG = "--tool-call-parser"


def _tool_parser_flag(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == _FLAG:
            return argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith(f"{_FLAG}="):
            return a.removeprefix(f"{_FLAG}=")
    return None


def main() -> int:
    ensure_registered()
    import vllm_mlx.cli
    from vllm_mlx.tool_parsers import ToolParserManager

    name = _tool_parser_flag(sys.argv)
    if name is not None:
        try:
            ToolParserManager.get_tool_parser(name)
        except KeyError:
            why = "neither vllm-mlx nor airlie registers it"
            print(f"airlie: unknown {_FLAG} '{name}': {why}", file=sys.stderr)
            return 2
    vllm_mlx.cli.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
