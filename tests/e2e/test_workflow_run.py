"""The Vue app's workflow view for a run in Chromium (/workflows/runs/<id>): each topology draws its
stages as boxes holding their jobs, the winner starred; a finished plan job shows the plan card, which can be
edited as JSON and approved; Stop asks first. Each check leaves its screenshot in test-results/evidence/
(conftest.Evidence).

The finished runs are seeded by conftest: TOPO_DONE (studio), RUN_TEAMS, RUN_FORK and RUN_SINGLE. The plan and
the run Approve starts come from conftest's fake studio, on a server of the test's own."""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import Locator, Page

from tests.e2e.conftest import KEY, RUN_FORK, RUN_SINGLE, RUN_TEAMS, TOPO_DONE, Evidence, Server

pytestmark = pytest.mark.e2e

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 375, "height": 812}


def open_run(page: Page, server: Server, run: str, path: str = "") -> None:
    page.set_viewport_size(DESKTOP)
    page.goto(f"{server.url}/workflows/runs/{run}{path}")


def stage(page: Page, node: str) -> Locator:
    return page.locator(f"[data-stage][data-node={node}]")


def stages(page: Page) -> list[str | None]:
    """The nodes of the stages drawn, left to right (once the first is on the page)."""
    boxes = page.locator("[data-stage]")
    boxes.first.wait_for()
    return [boxes.nth(i).get_attribute("data-node") for i in range(boxes.count())]


def start_plan(page: Page, server: Server) -> str:
    """Ask the server for a plan and wait until its plan job has finished; the run's id."""
    auth = {"Authorization": f"Bearer {KEY}"}
    repo = str(server.state.parent / "repo")
    started = page.request.post(
        f"{server.url}/v1/airlie/plans",
        headers=auth,
        data={"goal": "Fix the flaky upload test", "repo": repo},
    )
    assert started.status == 202
    run = started.json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        flow = page.request.get(f"{server.url}/v1/airlie/runs/{run}/flow", headers=auth).json()
        if flow["plan"] is not None:
            return run
        time.sleep(0.1)
    raise AssertionError("the plan job did not finish")


def test_a_studio_run_draws_plan_specialists_review_and_synthesis(
    app_page: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    open_run(app_page, server, TOPO_DONE)
    evidence.expect(
        app_page.get_by_role("heading", name="Fix the build"), "the goal is the heading"
    ).to_be_visible()
    evidence.expect(app_page.get_by_text("studio run"), "the kind tag").to_be_visible()
    evidence.expect(app_page.locator("[data-stage]"), "four stages").to_have_count(4)
    assert stages(app_page) == ["plan", "specialists", "review", "synthesis"]
    evidence.expect(
        stage(app_page, "specialists").locator("button[data-job]"), "both specialists in one box"
    ).to_have_count(2)
    evidence.expect(stage(app_page, "plan"), "the plan stage passed").to_have_attribute(
        "data-status", "passed"
    )
    evidence.expect(
        app_page.locator("[data-progress]"), "a finished run shows no progress figure"
    ).to_have_count(0)
    evidence.expect(app_page.locator("[data-stop]"), "this server runs no job for it: no Stop").to_have_count(
        0
    )
    evidence.expect(
        app_page.locator("section[aria-label=Plan]"), "a run, not a plan: no plan card"
    ).to_have_count(0)
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def test_a_teams_run_stacks_its_teams_with_the_winner_starred_then_the_judge(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_run(app_page, server, RUN_TEAMS)
    assert stages(app_page) == ["teams", "judge"]
    teams = stage(app_page, "teams")
    evidence.expect(teams.locator("button[data-job]"), "three teams in the one box").to_have_count(3)
    evidence.expect(teams.locator("[data-won]"), "one star").to_have_count(1)
    evidence.expect(teams.locator("button[data-job=t2] [data-won]"), "on the second team").to_have_text(
        "★ won"
    )
    evidence.expect(teams.locator("button[data-job=t1]"), "the first team failed").to_have_attribute(
        "data-status", "failed"
    )
    boxes = [teams.locator("button[data-job]").nth(i).bounding_box() for i in range(3)]
    assert all(b is not None for b in boxes)
    ys = [b["y"] for b in boxes if b]
    evidence.shot("the teams are stacked", held=ys == sorted(ys) and len(set(ys)) == 3)
    assert ys == sorted(ys) and len(set(ys)) == 3
    evidence.expect(app_page.locator("section[aria-label=Job] h2"), "the failed team is open").to_have_text(
        "Teams / Team 1"
    )
    teams.locator("button[data-job=t2]").click()
    evidence.expect(app_page, "a job box opens it in the address").to_have_url(
        re.compile(rf"/workflows/runs/{RUN_TEAMS}/teams/t2$")
    )
    evidence.expect(app_page.locator("section[aria-label=Job] h2"), "and its detail").to_have_text(
        "Teams / Team 2"
    )


def test_a_fork_run_draws_the_agent_then_its_forks_and_no_progress_once_done(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_run(app_page, server, RUN_FORK)
    assert stages(app_page) == ["agent", "forks"]
    evidence.expect(
        stage(app_page, "agent").locator("button[data-job]"), "the agent that got stuck"
    ).to_have_count(1)
    forks = stage(app_page, "forks")
    evidence.expect(forks.locator("button[data-job]"), "two forks").to_have_count(2)
    evidence.expect(forks.locator("button[data-job=b1] [data-won]"), "the first fork won").to_have_text(
        "★ won"
    )
    evidence.expect(app_page.locator("[data-progress]"), "no figure on a finished run").to_have_count(0)
    evidence.expect(app_page.locator("[role=progressbar]"), "nor a bar").to_have_count(0)


def test_a_single_run_draws_one_agent(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_run(app_page, server, RUN_SINGLE)
    assert stages(app_page) == ["agent"]
    evidence.expect(stage(app_page, "agent").locator("button[data-job]"), "one job").to_have_count(1)
    evidence.expect(app_page.get_by_text("single run"), "the kind tag").to_be_visible()
    evidence.expect(
        app_page.get_by_role("heading", name="Rename the repo map cache key"), "the goal"
    ).to_be_visible()


def test_a_run_that_does_not_exist_says_so(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_run(app_page, server, "nope")
    evidence.expect(app_page.get_by_text("No run nope."), "the missing run is named").to_be_visible()


def test_on_a_phone_the_stages_stack_and_the_page_does_not_scroll_sideways(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(PHONE)
    app_page.goto(f"{server.url}/workflows/runs/{RUN_TEAMS}")
    first, second = stage(app_page, "teams").bounding_box(), stage(app_page, "judge").bounding_box()
    assert first is not None and second is not None
    evidence.shot(
        "the judge sits under the teams", held=second["y"] > first["y"] and abs(second["x"] - first["x"]) < 2
    )
    assert second["y"] > first["y"] and abs(second["x"] - first["x"]) < 2
    width = app_page.evaluate("document.documentElement.scrollWidth")
    evidence.shot("no sideways page scroll", held=width <= PHONE["width"])
    assert width <= PHONE["width"]


def test_a_finished_plan_job_shows_the_plan_card_and_Change_it_checks_the_json(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    run = start_plan(app_page, own_server)
    open_run(app_page, own_server, run)
    card = app_page.locator("section[aria-label=Plan]")
    evidence.expect(card, "the plan card").to_be_visible()
    evidence.expect(
        app_page.get_by_text("waiting for your approval"), "the state says what is waited for"
    ).to_be_visible()
    evidence.expect(card.locator("[data-assumptions] li"), "both assumptions").to_have_count(2)
    rows = card.locator("[data-subtask]")
    evidence.expect(rows, "both subtasks").to_have_count(2)
    evidence.expect(rows.nth(1), "the second comes after the first").to_contain_text("retry-note")
    evidence.expect(rows.nth(1), "it names what it waits for").to_contain_text("fix")
    evidence.expect(app_page.locator("[data-stop]"), "the plan job is done: no Stop").to_have_count(0)

    card.get_by_role("button", name="Change it").click()
    box = app_page.get_by_label("Plan as JSON")
    evidence.expect(box, "the plan as JSON").to_be_visible()
    assert json.loads(box.input_value())["subtasks"][0]["id"] == "fix"
    box.fill('{"subtasks": [')
    evidence.expect(
        app_page.locator("[data-plan-problem]"), "bad JSON is explained in words"
    ).to_contain_text("That is not valid JSON")
    evidence.expect(
        app_page.get_by_role("button", name="Use this plan"), "and cannot be used"
    ).to_be_disabled()
    box.fill('{"subtasks": []}')
    evidence.expect(app_page.locator("[data-plan-problem]"), "an empty plan is explained too").to_have_text(
        "The plan needs a list of subtasks."
    )
    box.fill(
        json.dumps(
            {
                "assumptions": ["Edited."],
                "subtasks": [{"id": "only", "role": "coder", "brief": "Do the one thing."}],
            }
        )
    )
    evidence.expect(
        app_page.locator("[data-plan-problem]"), "a good plan clears the complaint"
    ).to_have_count(0)
    app_page.get_by_role("button", name="Use this plan").click()
    evidence.expect(card.locator("[data-subtask]"), "the card now shows the edit").to_have_count(1)
    evidence.expect(card.locator("[data-assumptions] li"), "with its assumption").to_have_text("Edited.")


def test_Approve_plan_starts_the_edited_plan_and_opens_the_new_run_where_Stop_asks_first(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    run = start_plan(app_page, own_server)
    open_run(app_page, own_server, run)
    card = app_page.locator("section[aria-label=Plan]")
    card.get_by_role("button", name="Change it").click()
    app_page.get_by_label("Plan as JSON").fill(
        json.dumps({"subtasks": [{"id": "only", "role": "coder", "brief": "Do the one thing."}]})
    )
    app_page.get_by_role("button", name="Use this plan").click()
    card.get_by_role("button", name="Approve plan").click()
    evidence.expect(app_page, "the new run's view opens").to_have_url(
        re.compile(r"/workflows/runs/(?!" + re.escape(run) + r"$)[^/]+$")
    )
    evidence.expect(
        stage(app_page, "specialists").locator("button[data-job]"), "it runs the edited plan: one subtask"
    ).to_have_count(1)
    evidence.expect(
        stage(app_page, "specialists").locator("button[data-job=only]"), "the one the edit named"
    ).to_have_count(1)
    stop = app_page.locator("[data-stop]")
    evidence.expect(stop, "the run is running here: Stop").to_be_visible()
    stop.click()
    evidence.expect(app_page.get_by_text("Are you sure?"), "Stop asks first").to_be_visible()
    app_page.locator("[data-confirm-cancel]").click()
    evidence.expect(stop, "No leaves the run going").to_be_visible()
    stop.click()
    app_page.locator("[data-confirm-ok]").click()
    evidence.expect(stop, "Yes stops it: no Stop").to_have_count(0)
    evidence.expect(app_page.get_by_text("stopped", exact=True), "the state says stopped").to_be_visible()
