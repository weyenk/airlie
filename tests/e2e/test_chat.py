"""AIR-36 in Chromium: Chat is just chat. A reply that is a JSON plan is shown as text, / opens the menu of
chat skills, /story gives a story card, and Start story posts the story only when a repository is chosen.
Each check leaves its screenshot in test-results/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

PLAN = json.dumps(
    {
        "type": "plan",
        "assumptions": ["a"],
        "subtasks": [{"id": "find-chat-clear", "role": "software-engineer", "brief": "b"}],
    }
)
REPO = "/work/airlie"
STORY = "Add a New chat button\n\nChat gets a button that clears the thread. Done when a click empties it."


def open_chat(page: Page, server: Server, repos: list[str]) -> list[dict[str, object]]:
    """The chat screen with the repository list stubbed; returns the story requests the page makes."""
    posted: list[dict[str, object]] = []
    page.route("**/v1/airlie/repos", lambda route: route.fulfill(json={"repos": repos}))

    def stories(route: Route) -> None:
        if route.request.method == "POST":
            posted.append(route.request.post_data_json)
            route.fulfill(status=202, json={"job_id": "story-1", "kind": "story"})
        else:
            route.fallback()

    page.route("**/v1/airlie/stories", stories)
    page.goto(f"{server.url}/#/")
    return posted


def say(page: Page, text: str) -> None:
    page.get_by_label("Message", exact=True).fill(text)
    page.get_by_role("button", name="Send").click()


def test_a_reply_that_is_a_json_plan_is_text_never_a_plan_card(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = PLAN
    open_chat(signed_in, server, [REPO])
    say(signed_in, "is there a way to clear the airlie chat?")
    evidence.expect(signed_in.locator(".them").last, "the plan JSON is shown as text").to_contain_text(
        "software-engineer"
    )
    evidence.expect(signed_in.locator(".plan-card"), "no plan card").to_have_count(0)
    evidence.expect(signed_in.get_by_role("button", name="Approve"), "nothing to approve").to_have_count(0)
    sent = server.upstream.requests[-1]
    assert "skill" not in sent and sent["messages"][0]["content"].startswith("You are the chat role.")


def test_typing_a_slash_shows_the_menu_and_choosing_story_gives_a_story_card(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    open_chat(signed_in, server, [REPO])
    box = signed_in.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    menu = signed_in.get_by_role("listbox", name="Commands")
    evidence.expect(menu.get_by_role("option", name="/story"), "the menu offers /story").to_be_visible()
    box.press_sequentially("zzz")
    evidence.expect(menu, "a prefix nothing matches closes the menu").to_be_hidden()
    box.fill("/st")
    menu.get_by_role("option", name="/story").click()
    evidence.expect(box, "choosing it fills the box").to_have_value("/story ")
    say(signed_in, "/story the chat gets a New chat button")
    card = signed_in.locator(".story-card")
    evidence.expect(card.get_by_label("Story request"), "the reply is an editable story card").to_have_value(
        STORY
    )
    evidence.expect(card.get_by_role("button", name="Start story"), "with Start story").to_be_visible()
    sent = server.upstream.requests[-1]
    assert "skill" not in sent  # the route strips it
    assert "the chat gets a New chat button" in sent["messages"][-1]["content"]
    assert sent["messages"][-1]["content"] == "the chat gets a New chat button"
    assert "Turn the request" not in sent["messages"][-1]["content"]


def test_start_story_posts_the_text_and_repository_and_shows_a_job_card(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    posted = open_chat(signed_in, server, [REPO])
    say(signed_in, "/story the chat gets a New chat button")
    card = signed_in.locator(".story-card")
    card.get_by_label("Story request").fill(STORY + "\nEdited.")
    card.get_by_role("button", name="Start story").click()
    evidence.expect(signed_in.locator("a.job-card"), "a job card replaces the story card").to_contain_text(
        "Story started"
    )
    evidence.expect(signed_in.locator(".story-card"), "the story card is gone").to_have_count(0)
    assert posted == [{"text": STORY + "\nEdited.", "repo": REPO}]


def test_the_composer_has_no_repository_row_and_the_story_card_has_the_picker(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    open_chat(signed_in, server, [REPO, "/work/other"])
    evidence.expect(
        signed_in.get_by_label("Repository"), "no repository picker under the message box"
    ).to_have_count(0)
    say(signed_in, "/story the chat gets a New chat button")
    picker = signed_in.locator(".story-card").get_by_label("Repository")
    evidence.expect(picker, "the story card has its own picker").to_be_visible()
    evidence.expect(picker.locator("option"), "one option per repository").to_have_count(2)


def test_clear_starts_a_fresh_session_without_asking_the_model(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "An answer."
    open_chat(signed_in, server, [REPO])
    say(signed_in, "is there a way to clear this chat")
    evidence.expect(signed_in.locator(".me"), "the question is in the thread").to_have_count(1)
    asked = len(server.upstream.requests)
    box = signed_in.get_by_label("Message", exact=True)
    box.press_sequentially("/cl")
    signed_in.get_by_role("listbox", name="Commands").get_by_role("option", name="/clear").click()
    box.press("Enter")
    evidence.expect(signed_in.locator(".me"), "the thread is empty").to_have_count(0)
    evidence.expect(
        signed_in.get_by_text("Hi, I'm airlie's assistant"), "the welcome is back"
    ).to_be_visible()
    assert len(server.upstream.requests) == asked


def test_start_story_without_a_repository_says_to_pick_one_and_posts_nothing(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    posted = open_chat(signed_in, server, [])
    say(signed_in, "/story the chat gets a New chat button")
    signed_in.locator(".story-card").get_by_role("button", name="Start story").click()
    evidence.expect(signed_in.locator(".story-card .error"), "it asks for a repository").to_contain_text(
        "Pick a repository"
    )
    evidence.expect(signed_in.locator("a.job-card"), "no job card").to_have_count(0)
    assert posted == []
