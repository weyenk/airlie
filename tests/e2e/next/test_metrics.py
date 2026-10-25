"""The Vue app's Metrics screen in Chromium (/next/metrics): the tiles and the chart against step events
seeded into the state directory with timestamps relative to now. Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

HOUR = 3600
DAY = 86400


def _step(
    ts: float, *, role: str, model: str | None, gen_ms: float, prefill_ms: float, **more: Any
) -> dict[str, Any]:
    """A step event of 100 completion tokens over `gen_ms` and 1000 prompt tokens over `prefill_ms`."""
    row: dict[str, Any] = {
        "ts": ts,
        "kind": "step",
        "role": role,
        "run_id": "metrics-now",
        "team_id": "tests" if role == "tester" else "build",
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prefill_ms": prefill_ms,
        "gen_ms": gen_ms,
        **more,
    }
    if model is not None:
        row["model"] = model
    return row


def _seed_metrics(state: Path) -> None:
    """The last 7 days: the coder at 50 tok/s and the tester at 25, three days ago; the coder again 2 hours
    ago (no model recorded). The 7 days before: one step at 40 generation and 1000 prefill tok/s.
    Whole window: generation 300 tokens / 8 s = 37.5, prefill 3000 tokens / 2 s = 1,500, and one fix round
    over two layers (0.5). Stories: two in review and one blocked touched in the window, one in review
    10 days ago."""
    for seeded in ("runs", "stories"):  # the shared seed's own steps (some recent) would add to the sums
        shutil.rmtree(state / seeded, ignore_errors=True)
    now = time.time()
    rows = [
        _step(
            now - 3 * DAY, role="coder", model="base", gen_ms=2000, prefill_ms=500, story="AIR-9", layer="01"
        ),
        _step(now - 3 * DAY, role="tester", model="other", gen_ms=4000, prefill_ms=1000, story="AIR-9"),
        _step(
            now - 2 * HOUR, role="coder", model=None, gen_ms=2000, prefill_ms=500, story="AIR-10", layer="02"
        ),
        {"ts": now - 3 * DAY, "kind": "fix_round", "story": "AIR-9", "layer": "01"},
        _step(now - 9 * DAY, role="coder", model="base", gen_ms=2500, prefill_ms=1000),
    ]
    run = state / "runs" / "metrics-now"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for key, stage, blocked, age in (
        ("AIR-9", "publish", None, 0),
        ("AIR-10", "review", None, 0),
        ("AIR-11", "layers", "the tester stopped", 0),
        ("AIR-12", "review", None, 10 * DAY),
    ):
        story = state / "stories" / key
        story.mkdir(parents=True)
        (story / "state.json").write_text(
            json.dumps(
                {
                    "card": {"key": key, "title": key},
                    "repo": "weyenk/airlie",
                    "stage": stage,
                    "blocked": blocked,
                }
            )
        )
        if age:
            os.utime(story / "state.json", (now - age, now - age))


def test_the_tiles_show_the_numbers_and_the_change_against_the_week_before(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _seed_metrics(own_server.state)
    next_page.goto(own_server.url + "/next/metrics")
    gen = next_page.locator("[data-tile=generation]")
    evidence.expect(gen.locator("[data-value]"), "generation tok/s over the 7 days").to_have_text("37.5tok/s")
    evidence.expect(gen.locator("[data-delta]"), "down 6% from 40").to_have_text("▼ 6% vs the 7 days before")
    prefill = next_page.locator("[data-tile=prefill]")
    evidence.expect(prefill.locator("[data-value]"), "prefill tok/s").to_have_text("1,500tok/s")
    evidence.expect(prefill.locator("[data-delta]"), "up 50% from 1,000").to_have_text(
        "▲ 50% vs the 7 days before"
    )
    stories = next_page.locator("[data-tile=stories]")
    evidence.expect(stories.locator("[data-value]"), "three stories active in the 7 days").to_have_text("3")
    evidence.expect(stories.locator("[data-delta]"), "the breakdown adds up to the three").to_have_text(
        "2 in review · 1 blocked"
    )
    fix = next_page.locator("[data-tile=fix-rounds]")
    evidence.expect(fix.locator("[data-value]"), "one fix round over two layers").to_have_text("0.5")
    evidence.expect(fix.locator("[data-delta]"), "no layers the week before").to_have_text(
        "no data for the 7 days before"
    )
    evidence.expect(next_page.locator("body"), "no NaN or Infinity anywhere").not_to_contain_text(
        re.compile("NaN|Infinity")
    )


def test_the_chart_draws_a_line_per_role_and_a_bucket_opens_the_stories_active_then(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _seed_metrics(own_server.state)
    next_page.goto(own_server.url + "/next/metrics")
    legend = next_page.locator("[data-legend]")
    evidence.expect(legend, "the legend names the roles").to_contain_text("coder")
    evidence.expect(legend, "both of them").to_contain_text("tester")
    evidence.expect(next_page.locator("[data-line]"), "one line each").to_have_count(2)
    evidence.expect(next_page.locator("[data-line=coder]"), "in the first series colour").to_have_attribute(
        "stroke", "#34d399"
    )
    evidence.expect(next_page.locator("[data-line=tester]"), "and the second").to_have_attribute(
        "stroke", "#38bdf8"
    )
    plot = next_page.locator("[data-plot]")
    plot.focus()
    plot.press("ArrowRight")
    tip = next_page.locator("[data-tip]")
    evidence.expect(tip, "the first bucket's tooltip has the coder at 50").to_contain_text(
        re.compile(r"coder\s*50")
    )
    evidence.expect(tip, "and the tester at 25").to_contain_text(re.compile(r"tester\s*25"))
    plot.press("Enter")
    evidence.expect(next_page, "the bucket opens the Workflows list for its window").to_have_url(
        re.compile(r"/next/workflows\?active_from=\d+&active_to=\d+$")
    )
    q = re.search(r"active_from=(\d+)&active_to=(\d+)", next_page.url)
    assert q is not None
    assert int(q[2]) - int(q[1]) == 6 * HOUR
    assert abs(int(q[1]) - (time.time() - 3 * DAY)) < 6 * HOUR + 60  # the bucket three days ago
    evidence.expect(next_page.locator("[data-window]"), "the list says it is narrowed").to_contain_text(
        "Active"
    )


def test_slicing_by_model_names_a_missing_model_unknown(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _seed_metrics(own_server.state)
    next_page.goto(own_server.url + "/next/metrics")
    next_page.locator("[data-slice]").select_option("model")
    legend = next_page.locator("[data-legend]")
    evidence.expect(legend, "the legend follows the slice").to_contain_text("base")
    evidence.expect(legend, "with the other model").to_contain_text("other")
    evidence.expect(legend, "and a step with no model reads unknown").to_contain_text("unknown")
    evidence.expect(next_page.get_by_text("Generation speed by model"), "the title follows").to_be_visible()


def test_every_slice_on_offer_draws_without_an_error(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _seed_metrics(own_server.state)
    next_page.goto(own_server.url + "/next/metrics")
    options = next_page.locator("[data-slice] option").evaluate_all("els => els.map(e => e.value)")
    assert options == ["role", "model", "topology", "task", "agent"]
    expected = {
        "role": "coder",
        "model": "base",
        "topology": "unknown",
        "task": "metrics-now",
        "agent": "build",
    }
    for option in options:
        next_page.locator("[data-slice]").select_option(option)
        evidence.expect(
            next_page.get_by_text(f"Generation speed by {option}"), f"{option}: the chart is titled"
        ).to_be_visible()
        evidence.expect(
            next_page.locator("[data-legend]"), f"{option}: the legend names its slices"
        ).to_contain_text(expected[option])
        evidence.expect(next_page.get_by_role("alert"), f"{option}: no error banner").to_have_count(0)


def test_a_range_with_no_runs_says_so(next_page: Page, own_server: Server, evidence: Evidence) -> None:
    for seeded in ("runs", "stories"):  # the shared seed may hold recent steps
        shutil.rmtree(own_server.state / seeded, ignore_errors=True)
    next_page.goto(own_server.url + "/next/metrics")
    next_page.locator("[data-range='4h']").click()
    evidence.expect(next_page.get_by_text("No runs in this range"), "nothing ran in 4 hours").to_be_visible()
    evidence.expect(next_page.locator("[data-line]"), "and no line is drawn").to_have_count(0)
    evidence.expect(
        next_page.locator("[data-tile=generation] [data-value]"), "the speed is unknown, not 0"
    ).to_have_text("unknown")


def test_the_phone_lays_the_tiles_out_two_by_two_without_scrolling_sideways(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _seed_metrics(own_server.state)
    next_page.set_viewport_size({"width": 375, "height": 812})
    next_page.goto(own_server.url + "/next/metrics")
    evidence.expect(next_page.locator("[data-plot]"), "the chart draws").to_be_visible()
    tiles = next_page.locator("[data-tile]").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().top)"
    )
    assert tiles[0] == tiles[1] and tiles[2] == tiles[3] and tiles[2] > tiles[0]
    sideways = next_page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
    evidence.shot("the phone page does not scroll sideways", held=not sideways)
    assert not sideways
