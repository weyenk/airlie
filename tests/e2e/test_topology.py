"""The Topology screen in Chromium: a finished studio run's tree with every hand-off, a running one whose
tree moves as its log grows, a circle's detail on tap, and the phone width. Each check leaves its screenshot
(see conftest.Evidence)."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import TOPO_DONE, TOPO_LIVE, Evidence, Server

pytestmark = pytest.mark.e2e


def node(page: Page, nid: str):  # type: ignore[no-untyped-def]
    return page.locator(f'g.topo-node[data-node="{nid}"]')


def test_a_finished_studio_run_shows_its_agents_and_every_hand_off(
    signed_in: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    signed_in.goto(f"{server.url}/#/topology/{TOPO_DONE}")
    evidence.expect(signed_in.get_by_role("heading", name="Topology"), "the page heading").to_be_visible()
    evidence.expect(
        signed_in.locator("g.topo-node"), "chief, coder, docs, reviewer, synthesis"
    ).to_have_count(5)
    for nid in ("chief", "fix", "note", "reviewer", "synthesis"):
        evidence.expect(node(signed_in, nid), f"{nid} finished").to_have_class(re.compile(r"\bdone\b"))
    evidence.expect(signed_in.locator("path.topo-seg.on"), "nothing is working now").to_have_count(0)
    handoffs = signed_in.locator(".topo-handoffs li")
    evidence.expect(handoffs, "one line per hand-off").to_have_count(7)
    evidence.expect(handoffs.first, "newest first: the chief's answer").to_contain_text("Chief → Synthesis")
    evidence.expect(signed_in.locator(".topo-handoffs"), "what the brief said").to_contain_text(
        "make the check pass"
    )
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def test_tapping_a_circle_shows_its_role_model_steps_and_tokens_and_its_hand_offs(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/topology/{TOPO_DONE}")
    node(signed_in, "fix").click()
    detail = signed_in.locator("section[aria-live=polite]")
    evidence.expect(detail, "its role").to_contain_text("coder")
    evidence.expect(detail, "its model").to_contain_text("base")
    evidence.expect(detail, "its skill").to_contain_text("python")
    evidence.expect(detail, "its tokens").to_contain_text("2,400 in · 300 out")
    evidence.expect(
        signed_in.get_by_role("heading", name="Hand-offs of Coder"), "hand-offs for it"
    ).to_be_visible()
    evidence.expect(signed_in.locator(".topo-handoffs li"), "the coder's four hand-offs").to_have_count(4)


def test_a_running_run_marks_who_works_and_moves_when_its_log_grows(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    page = signed_in
    page.goto(f"{own_server.url}/#/topology/{TOPO_LIVE}")
    evidence.expect(node(page, "fix"), "the coder is working").to_have_class(re.compile(r"\bactive\b"))
    evidence.expect(node(page, "note"), "the docs subtask waits").to_have_class(re.compile(r"\bwaiting\b"))
    evidence.expect(page.locator("path.topo-seg.on"), "the chief's brief is the live path").to_have_count(1)
    log = own_server.state / "runs" / TOPO_LIVE / "events.jsonl"
    more = [
        {
            "ts": 1792750005.0,
            "run_id": TOPO_LIVE,
            "kind": "handoff",
            "src": "fix",
            "dst": "reviewer",
            "what": "diff",
            "text": "1 file changed: PASS",
        },
    ]
    with log.open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in more))
    evidence.expect(node(page, "reviewer"), "the reviewer works now").to_have_class(re.compile(r"\bactive\b"))
    evidence.expect(node(page, "fix"), "the coder waits on it").to_have_class(re.compile(r"\bidle\b"))
    evidence.expect(page.locator(".topo-handoffs li").first, "the new hand-off on top").to_contain_text(
        "Coder → Reviewer"
    )


def test_the_tree_fits_a_phone(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.set_viewport_size({"width": 375, "height": 812})
    signed_in.goto(f"{server.url}/#/topology/{TOPO_DONE}")
    evidence.expect(signed_in.locator("g.topo-node"), "every circle drawn").to_have_count(5)
    wide = signed_in.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    evidence.shot("no sideways scroll", held=not wide)
    assert not wide


def test_the_topology_tab_opens_the_newest_run(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/topology")
    evidence.expect(signed_in, "a run is opened").to_have_url(re.compile(r"#/topology/[^/]+$"))
    evidence.expect(signed_in.get_by_label("Run"), "the picker lists runs").to_be_visible()
