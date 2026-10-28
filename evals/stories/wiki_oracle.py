"""The wiki case's oracle: the request's spec, checked in the built app apart from the tests the run wrote.

Run as `python3 -I wiki_oracle.py <checkout>`; prints one JSON object: {"spec": [[name, ok], ...],
"extra": [[name, ok], ...]}. The checks are wiki_oracle.test.ts beside this file: it is copied into the
checkout's web/src and run with the checkout's own Vitest (its node_modules installed with `npm ci` when the
checkout has none), so they mount the app's real routes in jsdom. A check whose test did not run (the file
could not load: no router, no wiki) counts as failed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHECKS = Path(__file__).with_name("wiki_oracle.test.ts")
PLACED = "src/__airlie_oracle__.test.ts"


def titles() -> list[str]:
    """The checks' titles, in file order, as Vitest reports them (without the describe)."""
    lines = [line.strip() for line in CHECKS.read_text().splitlines()]
    return [line.split('it("', 1)[1].split('"', 1)[0] for line in lines if line.startswith('it("')]


def main(checkout: Path) -> dict[str, list[list[object]]]:
    web = checkout / "web"
    shutil.copy2(CHECKS, web / PLACED)
    if not (web / "node_modules").exists():
        npm_ci = ["npm", "ci", "--no-audit", "--no-fund"]
        subprocess.run(npm_ci, cwd=web, check=True, capture_output=True, timeout=600)
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            ["npx", "vitest", "run", PLACED, "--reporter=json", f"--outputFile={report}"],
            cwd=web,
            capture_output=True,
            timeout=600,
        )
        got = json.loads(report.read_text()) if report.is_file() else {}
    passed = {
        case["title"]
        for result in got.get("testResults") or []
        for case in result.get("assertionResults") or []
        if case.get("status") == "passed"
    }
    out: dict[str, list[list[object]]] = {"spec": [], "extra": []}
    for title in titles():
        kind, _, name = title.partition(": ")
        out[kind].append([name, title in passed])
    return out


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]))))
