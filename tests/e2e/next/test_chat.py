"""The Vue app's Chat in Chromium (/next/): the greeting, a streamed reply, a reply that is a JSON plan
staying text, /clear, a cut stream, a thread written by today's app, and the status strip. Each check
leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e

PLAN = json.dumps(
    {
        "type": "plan",
        "assumptions": ["a"],
        "subtasks": [{"id": "find-chat-clear", "role": "software-engineer", "brief": "b"}],
    }
)
RUNNING_KEY = "AIR-9"


def _write_story(state: Path, key: str, **fields: Any) -> None:
    """A story's state.json as the story tick leaves it, written straight into the file (no request)."""
    story = state / "stories" / key
    story.mkdir(parents=True, exist_ok=True)
    body = {"card": {"key": key, "title": "Context gathering"}, "repo": "weyenk/airlie", "stage": "refine"}
    (story / "state.json").write_text(json.dumps({**body, **fields}))


def open_chat(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/next/")
    page.get_by_label("Message", exact=True).wait_for()


def say(page: Page, text: str) -> None:
    page.get_by_label("Message", exact=True).fill(text)
    page.get_by_role("button", name="Send").click()


def test_a_new_chat_greets_and_a_quick_reply_gets_a_streamed_answer(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "Nothing is running right now."
    open_chat(next_page, server)
    evidence.expect(next_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting").to_be_visible()
    quick = next_page.locator("[data-quick]")
    evidence.expect(quick, "three quick replies").to_have_text(
        ["What can you do?", "What's running right now?", "/story"]
    )
    quick.nth(1).click()
    evidence.expect(next_page.locator("[data-msg=user]"), "my question is in the thread").to_have_text(
        "What's running right now?"
    )
    evidence.expect(
        next_page.locator("[data-msg=chat] [data-bubble]").last, "the reply is shown"
    ).to_contain_text("Nothing is running right now.")
    evidence.expect(next_page.locator("[data-thinking]"), "Thinking is gone").to_have_count(0)
    evidence.expect(next_page.locator("[data-welcome]"), "the greeting is gone").to_have_count(0)
    assert server.upstream.requests[-1]["messages"][-1] == {
        "role": "user",
        "content": "What's running right now?",
    }


def test_thinking_shows_until_the_first_token(next_page: Page, server: Server, evidence: Evidence) -> None:
    held: list[Route] = []
    next_page.route("**/v1/chat/completions", lambda route: held.append(route))
    open_chat(next_page, server)
    say(next_page, "hello")
    evidence.expect(next_page.locator("[data-thinking]"), "Thinking shows with no token yet").to_have_text(
        "Thinking"
    )
    frame = {"choices": [{"index": 0, "delta": {"content": "First words."}, "finish_reason": None}]}
    body = f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n"
    while not held:
        next_page.wait_for_timeout(50)
    held[0].fulfill(status=200, headers={"content-type": "text/event-stream"}, body=body)
    evidence.expect(next_page.locator("[data-thinking]"), "Thinking goes with the first token").to_have_count(
        0
    )
    evidence.expect(
        next_page.locator("[data-msg=chat] [data-bubble]").last, "the words are there"
    ).to_have_text("First words.")


def test_a_cut_stream_says_so_and_keeps_what_arrived(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    frame = {"choices": [{"index": 0, "delta": {"content": "Half an ans"}, "finish_reason": None}]}
    next_page.route(
        "**/v1/chat/completions",
        lambda route: route.fulfill(
            status=200, headers={"content-type": "text/event-stream"}, body=f"data: {json.dumps(frame)}\n\n"
        ),  # no [DONE]: the stream ends early
    )
    open_chat(next_page, server)
    say(next_page, "tell me")
    reply = next_page.locator("[data-msg=chat] [data-bubble]").last
    evidence.expect(reply, "what arrived stays").to_contain_text("Half an ans")
    evidence.expect(next_page.locator("[data-cut]"), "and the cut is said").to_have_text(
        "airlie stopped answering"
    )


def test_a_reply_that_is_a_json_plan_is_text_never_a_plan_card(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = PLAN
    open_chat(next_page, server)
    say(next_page, "is there a way to clear the airlie chat?")
    evidence.expect(
        next_page.locator("[data-msg=chat] [data-bubble]").last, "the plan JSON is shown as text"
    ).to_contain_text("software-engineer")
    evidence.expect(next_page.get_by_role("button", name="Approve"), "nothing to approve").to_have_count(0)
    sent = server.upstream.requests[-1]
    assert "skill" not in sent and sent["messages"][0]["content"].startswith("You are the chat role.")


def test_clear_starts_a_fresh_session_without_asking_the_model(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "An answer."
    open_chat(next_page, server)
    say(next_page, "is there a way to clear this chat")
    evidence.expect(next_page.locator("[data-msg=user]"), "the question is in the thread").to_have_count(1)
    asked = len(server.upstream.requests)
    say(next_page, "/clear")
    evidence.expect(next_page.locator("[data-msg=user]"), "the thread is empty").to_have_count(0)
    evidence.expect(
        next_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting is back"
    ).to_be_visible()
    assert len(server.upstream.requests) == asked
    stored = next_page.evaluate("sessionStorage.getItem('airlie.chat')")
    evidence.shot("the stored thread is empty", held=json.loads(stored) == [])
    assert json.loads(stored) == []


def test_a_thread_written_by_todays_app_carries_over_and_shift_enter_is_a_new_line(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    filler = [
        m
        for i in range(12)
        for m in (
            {"role": "user", "text": f"earlier {i}", "sent": f"earlier {i}"},
            {"role": "chat", "text": f"answer {i}", "streaming": False, "thinking": False, "action": ""},
        )
    ]
    todays = [
        *filler,
        {
            "role": "card",
            "job_id": f"story-{STORY_KEY}",
            "kind": "story",
            "label": "airlie · story",
        },  # noteJobInChat
        {"role": "user", "text": "what is AIR-30", "sent": "what is AIR-30"},
        {
            "role": "chat",
            "text": f"**{STORY_KEY}** is blocked.",
            "streaming": False,
            "thinking": True,
            "action": "",
        },
    ]
    next_page.add_init_script(
        f"if (!sessionStorage.getItem('airlie.chat')) "
        f"sessionStorage.setItem('airlie.chat', {json.dumps(json.dumps(todays))});"
    )
    open_chat(next_page, server)
    evidence.expect(next_page.locator("[data-msg=user]").last, "my earlier question").to_have_text(
        "what is AIR-30"
    )
    evidence.expect(
        next_page.locator("[data-card]"), "a job card from today's app renders"
    ).to_have_attribute("href", f"/next/workflows/{STORY_KEY}")
    evidence.expect(
        next_page.locator("[data-msg=chat]").last, "the thread opens at its end"
    ).to_be_in_viewport()
    reply = next_page.locator("[data-msg=chat] [data-bubble]").last
    evidence.expect(reply, "the earlier reply").to_contain_text("is blocked.")
    evidence.expect(
        reply.get_by_role("link", name=STORY_KEY), "its story key links to the workflow"
    ).to_have_attribute("href", f"/next/workflows/{STORY_KEY}")
    evidence.expect(next_page.locator("[data-welcome]"), "no greeting over a thread").to_have_count(0)
    box = next_page.get_by_label("Message", exact=True)
    box.fill("line one")
    box.press("Shift+Enter")
    box.press_sequentially("line two")
    evidence.expect(box, "Shift+Enter is a new line").to_have_value("line one\nline two")
    box.press("Enter")
    evidence.expect(next_page.locator("[data-msg=user]").last, "Enter sends it").to_have_text(
        "line one\nline two"
    )
    # the draft is gone, and the thread now holds four items in today's format
    stored = json.loads(next_page.evaluate("sessionStorage.getItem('airlie.chat')"))
    assert stored[-2] == {"role": "user", "text": "line one\nline two", "sent": "line one\nline two"}
    assert stored[24]["role"] == "card"  # the card is kept as it was
    evidence.expect(
        next_page.locator("[data-msg=chat]").last, "my own message scrolls to the answer"
    ).to_be_in_viewport()
    next_page.locator("[data-card]").click()
    evidence.expect(next_page, "the story card opens the story's workflow view").to_have_url(
        re.compile(rf"/next/workflows/{STORY_KEY}$")
    )


def test_the_status_strip_counts_what_needs_you_and_follows_what_runs(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    open_chat(next_page, own_server)
    strip = next_page.locator("[data-test=status-strip]")
    evidence.expect(strip.locator("[data-strip=needs]"), "the one blocked story needs you").to_have_text(
        "1 needs you"
    )
    # the seeded topology run is already running: the count is read, then must grow by one
    running = strip.locator("[data-strip=running]")
    evidence.expect(running, "the strip counts what runs").to_have_text(re.compile(r"\d+ running"))
    before = int((running.inner_text()).split()[0])
    evidence.expect(strip.locator("[data-strip=studio]"), "Studio is online").to_have_text("Studio online")
    _write_story(own_server.state, RUNNING_KEY)  # as the tick would
    evidence.expect(running, "a story starts: the strip follows with no reload").to_have_text(
        f"{before + 1} running"
    )
    strip.locator("[data-strip=running]").click()
    evidence.expect(next_page, "running opens the workflows").to_have_url(re.compile(r"/next/workflows$"))
    next_page.go_back()
    strip.locator("[data-strip=needs]").click()
    evidence.expect(next_page, "needs you opens the blocked ones").to_have_url(
        re.compile(r"/next/workflows\?blocked=true$")
    )


def test_a_long_unbroken_path_and_a_wide_code_block_stay_inside_the_bubble_on_a_phone(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    path = "/Users/someone/git/airlie/" + "very-long-directory-name/" * 12 + "file.py"
    wide = "x = [" + ", ".join(f"'item_number_{i}'" for i in range(40)) + "]"
    server.upstream.reply = f"See {path} and https://example.com/{'a' * 150}\n\n```\n{wide}\n```\n"
    mine = "m" * 200  # my own message, unbroken
    card = {"role": "card", "job_id": "run-7", "kind": "run", "label": "l" * 200}
    next_page.add_init_script(
        f"if (!sessionStorage.getItem('airlie.chat')) "
        f"sessionStorage.setItem('airlie.chat', {json.dumps(json.dumps([card]))});"
    )
    next_page.set_viewport_size({"width": 375, "height": 700})
    open_chat(next_page, server)
    say(next_page, mine)
    bubble = next_page.locator("[data-msg=chat] [data-bubble]").last
    evidence.expect(bubble, "the reply is shown").to_contain_text("very-long-directory-name")
    evidence.expect(bubble.locator("pre"), "the code block is there").to_be_visible()
    fits = next_page.evaluate(
        """() => {
          const doc = document.documentElement;
          const b = [...document.querySelectorAll('[data-msg=chat] [data-bubble]')].pop();
          const scroller = document.querySelector('[data-test=thread-scroll]');
          const inside = (el) => {
            const r = el.getBoundingClientRect();
            return r.right <= window.innerWidth && r.left >= 0 && el.scrollWidth <= el.clientWidth;
          };
          const r = b.getBoundingClientRect();
          return {
            mine: inside(document.querySelector('[data-msg=user] > div')),
            card: inside(document.querySelector('[data-card]')),
            page: doc.scrollWidth <= doc.clientWidth,
            scroller: scroller.scrollWidth <= scroller.clientWidth,
            bubble: r.right <= window.innerWidth && r.left >= 0,
            text: b.scrollWidth <= b.clientWidth,
          };
        }"""
    )
    evidence.shot("nothing overflows sideways", held=all(fits.values()))
    assert fits == {"mine": True, "card": True, "page": True, "scroller": True, "bubble": True, "text": True}
