"""Run the diagnosis rules over every recorded block and print what they make of them.

    uv run python scripts/calibrate_diagnosis.py [STATE_DIR]

For every story under STATE_DIR (default ~/.local/state/airlie), every block is gathered and classed; the
output is the distribution by class and by kind, the count per story, and one line of evidence per block, so a
rule that misreads shows. A rule is adjusted only with a fixture (scripts/block_fixture.py) that pins the
change (2026-10-25, P5a)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from airlie.story.diagnosis import _events as read_events
from airlie.story.diagnosis import blocks_of, classify, gather


def main() -> None:
    state = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.local/state/airlie").expanduser()
    by_class: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_story: dict[str, Counter[str]] = {}
    lines: list[str] = []
    for story in sorted((state / "stories").glob("*")):
        events = read_events(story / "events.jsonl")
        for n in range(1, len(blocks_of(events)) + 1):
            pack = gather(state, story.name, n=n)
            got = classify(pack)
            by_class[got.class_] += 1
            by_kind[f"{got.class_}/{got.kind}"] += 1
            by_story.setdefault(story.name, Counter())[got.class_] += 1
            lines.append(
                f"{story.name} #{n} layer {pack.layer or '-'}: {got.signature} [{got.rule}] "
                f"stop={pack.stop_reason} refused={pack.refused_calls} | "
                f"{got.evidence[0][:90] if got.evidence else ''}"
            )
    total = sum(by_class.values())
    print(f"{total} blocks in {state}\n")
    print("by class:")
    for k, v in by_class.most_common():
        print(f"  {k:12} {v:3}")
    print("\nby class and kind:")
    for k, v in by_kind.most_common():
        print(f"  {k:32} {v:3}")
    print("\nby story:")
    for k, c in by_story.items():
        print(f"  {k:34} " + ", ".join(f"{cl} {n}" for cl, n in c.most_common()))
    print("\nblocks:")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
