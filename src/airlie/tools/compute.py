"""compute: run a short Python program and return what it prints, for roles that may only read.

The planning roles stated derived facts they never computed, and each cost a red build or worse (roman,
2026-10-29 and 30): "no canonical numeral exceeds four characters" (XVIII has five), every string of sixteen
or more characters swept as 7**16 cases, "no valid output exceeds 13 characters" (the longest is 15), MMMDCLX
called malformed (it is 3660). Each was one line of Python to check. The program runs as `python3 -I` (no
site directory) in a temporary directory the conversation keeps, so a module saved there can be imported by
a later call; with airlie's agent environment, under a sandbox that refuses every file write outside that
directory and any network use; a time limit ends it, and a conversation runs at most CALLS programs."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from airlie.subproc import agent_env, kill_group, started
from airlie.tools import confine
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap

TIMEOUT_S = 10.0
OUTPUT_CHARS = 4000
CALLS = 6
"""Programs one conversation may run (bundle 6 run 3, 2026-10-30: 40 calls, 72,000 tokens, each writing the
same reference implementation again). Past it the tool says to answer from what was checked."""


class Compute:
    name = "compute"
    distinct_by_arguments = True
    """Another program that prints the same is a different check, not a repeat (2026-10-31, roman: product's
    three different checks of one table printed the same and the repeat guard stopped its refine); the same
    program again is answered in `run`, and CALLS bounds the rest."""
    description = (
        "Run a short Python 3 program (standard library) and return what it prints. Use it to check a "
        "number before you state it: a bound, a count, a maximum, an example's value, a test's expected "
        "result. Files it writes stay for your later calls in this conversation (save a reference module "
        "once, import it after); it cannot change anything else or reach the network, and it stops after "
        "10 seconds. A few calls are enough: check the numbers you will state, then answer."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "the program; print what you need"}},
        "required": ["code"],
    }

    def __init__(self) -> None:
        self.calls = 0
        self._workspace: tempfile.TemporaryDirectory[str] | None = None
        self.ran: dict[str, int] = {}
        """Each program this conversation ran (its text, stripped) and the call that ran it."""

    def workspace(self) -> Path:
        """The directory this conversation's programs run in and keep files in, made on first use and removed
        with the tool."""
        if self._workspace is None:
            self._workspace = tempfile.TemporaryDirectory(prefix="airlie-compute-")
        return Path(self._workspace.name)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = str(args.get("code") or "")
        if not code.strip():
            return ToolResult(False, "error: no code given")
        again = self.ran.get(code.strip())
        if again is not None:
            # the same program again prints the same: say so instead of running it (2026-10-31, roman: product
            # re-ran one buggy program three times, unchanged, until the repeat guard stopped its refine)
            said = (
                f"you ran this exact program already (call {again}); it would print the same. If its output "
                "shows a bug in the program, change the program; otherwise answer from what it printed"
            )
            return ToolResult(False, said)
        if self.calls >= CALLS:
            return ToolResult(
                False,
                f"compute ran {CALLS} programs in this conversation already: answer from what they showed",
            )
        self.calls += 1
        self.ran[code.strip()] = self.calls
        rc, text = await run_python(code, self.workspace(), sandbox=ctx.sandbox)
        if rc is None:
            return ToolResult(False, text)
        body = cap(text, OUTPUT_CHARS) or "(it printed nothing: print what you need)"
        return ToolResult(rc == 0, body if rc == 0 else f"{body}\n[exit {rc}]")


async def run_python(code: str, where: Path, *, sandbox: bool = True) -> tuple[int | None, str]:
    """(the exit code, the output) of `code` run in `where`; (None, why) when it could not start or ran past
    TIMEOUT_S. Confined to writing in `where` and to no network when the sandbox is available."""
    command = [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, '.')\n{code}"]
    spawn: dict[str, Any] = dict(
        cwd=where,
        env=agent_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if sandbox and confine.available():
        roots = [Path(p) for p in (where.resolve(), "/dev")]
        command = confine.argv(command, roots, network=False)
    try:
        proc = await started(asyncio.create_subprocess_exec(*command, **spawn))
    except OSError as e:
        return None, f"error: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except TimeoutError:
        await kill_group(proc)
        return None, f"error: the program ran past {TIMEOUT_S:g}s; compute a smaller case or bound the loop"
    except BaseException:  # cancelled: nothing it started may outlive its caller
        await kill_group(proc)
        raise
    rc = proc.returncode or 0
    return rc, out.decode("utf-8", "replace")
