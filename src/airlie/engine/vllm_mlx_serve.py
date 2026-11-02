"""`vllm-mlx serve` with airlie's tool and reasoning parsers registered first.

vllm-mlx 0.5.0 has no parser-plugin flag, so the managed engine starts as
`python -m airlie.engine.vllm_mlx_serve serve …` (`build_serve_command`). An unknown `--tool-call-parser`
would make vllm-mlx fall back to its generic parser without saying so; this entry point refuses to start.
`--buffer-cache-mb N` is airlie's own: it becomes MLX_BUFFER_CACHE_LIMIT before vllm-mlx is imported, and is
taken out of the arguments vllm-mlx parses."""

from __future__ import annotations

import os
import sys

from airlie.engine.parsers import ensure_registered

_FLAG = "--tool-call-parser"
_BUFFER_CACHE = "--buffer-cache-mb"


def _take_buffer_cache(argv: list[str]) -> list[str]:
    """`argv` without `--buffer-cache-mb N`; MLX_BUFFER_CACHE_LIMIT set to N MiB, in bytes, when it was
    there."""
    if _BUFFER_CACHE not in argv:
        return argv
    i = argv.index(_BUFFER_CACHE)
    if i + 1 < len(argv):
        os.environ["MLX_BUFFER_CACHE_LIMIT"] = str(int(argv[i + 1]) * 1024 * 1024)
    return argv[:i] + argv[i + 2 :]


def _tool_parser_flag(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == _FLAG:
            return argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith(f"{_FLAG}="):
            return a.removeprefix(f"{_FLAG}=")
    return None


def main() -> int:
    sys.argv = _take_buffer_cache(sys.argv)
    ensure_registered()
    from airlie.engine.patches.active_requests import apply as apply_active_requests
    from airlie.engine.patches.first_output import apply as apply_first_output
    from airlie.engine.patches.hybrid_prefix_cache import apply as apply_hybrid_prefix_cache
    from airlie.engine.patches.mtp_batch_join import apply as apply_mtp_batch_join

    apply_hybrid_prefix_cache()
    apply_mtp_batch_join()
    apply_first_output()
    apply_active_requests()
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
