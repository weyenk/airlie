"""The median wall-clock and tokens of each stage over the recorded cards (planning-line P5b, D8).

    uv run python scripts/stage_medians.py [STATE_DIR]

For every card under STATE_DIR (default ~/.local/state/airlie: stories, epics, initiatives), each stage's
figures are folded from the card's events and the runs they name (`StageMeter.fold`, the same fold the line
records with); a stage a card never ran is left out. The output is, per stage, the sample size, the median
wall-clock (seconds) and tokens (prompt plus completion), and those medians doubled: the defaults
`airlie.yaml`'s `line.budgets` carries, with the date and the sample size written beside them."""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from airlie.story.budgets import REPEATING, STAGES, StageMeter


def main() -> None:
    state = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.local/state/airlie").expanduser()
    samples: dict[str, list[tuple[float, int]]] = {stage: [] for stage in STAGES}
    for kind in ("stories", "epics", "initiatives"):
        for folder in sorted((state / kind).glob("*")):
            if not (folder / "events.jsonl").is_file():
                continue
            meter = StageMeter(
                state, folder.name, {}, lambda *a, **k: None, reviewers=frozenset({"reviewer"})
            )
            for stage in STAGES:
                # a stage a story runs for each layer is a sample for each layer; the rest one for the card
                found = meter.runs(stage) if stage in REPEATING else [meter.fold(stage)]
                for figures in found:
                    if figures.steps or figures.wall_s:
                        samples[stage].append((figures.wall_s, figures.tokens))
    print(
        f"{'stage':10} {'n':>3} {'median s':>9} {'median tokens':>14} {'budget s':>9} {'budget tokens':>14}"
    )
    for stage in STAGES:
        rows = samples[stage]
        if not rows:
            print(f"{stage:10} {0:>3}")
            continue
        wall = statistics.median(w for w, _ in rows)
        tokens = statistics.median(t for _, t in rows)
        print(f"{stage:10} {len(rows):>3} {wall:>9.0f} {tokens:>14.0f} {2 * wall:>9.0f} {2 * tokens:>14.0f}")


if __name__ == "__main__":
    main()
