"""The Vue app's Chat in Chromium (/): the greeting, a streamed reply, a reply that is a JSON plan staying
text, /clear opening a fresh chat, a cut stream, the thread the earlier app kept in the tab (read-only, as
Legacy chat), the phone layout, and the tabs that stay reachable from a chat. The repository chat with its
tool blocks and permission prompt is test_chat_redesign.py. Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json
import re

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


def open_chat(page: Page, server: Server) -> None:
    """A fresh, empty chat (the server is shared: the chat `/` opens may hold an earlier test's messages)."""
    page.goto(f"{server.url}/")
    page.get_by_label("Message", exact=True).wait_for()
    size = page.viewport_size
    if size is not None and size["width"] <= 640:  # a phone: the panel is a slide-over
        page.get_by_role("button", name="Show chats").click()
    here = page.url
    page.get_by_role("button", name="New chat", exact=True).click()
    page.wait_for_function("(was) => location.href !== was", arg=here)  # the new chat is open
    page.locator("[data-welcome]").wait_for()
    page.locator("[data-test=panel-slide-over]").wait_for(
        state="detached"
    )  # and a phone's slide-over is shut


def say(page: Page, text: str) -> None:
    page.get_by_label("Message", exact=True).fill(text)
    page.get_by_role("button", name="Send").click()


def reply(page: Page):  # type: ignore[no-untyped-def]
    return page.locator("[data-message=assistant]").last


def test_a_new_chat_greets_and_a_quick_reply_gets_a_streamed_answer(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "Nothing is running right now."
    open_chat(app_page, server)
    evidence.expect(app_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting").to_be_visible()
    quick = app_page.locator("[data-quick]")
    evidence.expect(quick, "three quick replies").to_have_text(
        ["What can you do?", "What's running right now?", "/story"]
    )
    quick.nth(1).click()
    evidence.expect(app_page.locator("[data-msg=user]"), "my question is in the thread").to_have_text(
        "What's running right now?"
    )
    evidence.expect(reply(app_page), "the reply is shown").to_contain_text("Nothing is running right now.")
    evidence.expect(app_page.locator("[data-welcome]"), "the greeting is gone").to_have_count(0)
    evidence.expect(
        app_page.get_by_role("button", name="Send"), "and Send is back once the reply is whole"
    ).to_be_visible()
    last = server.upstream.requests[-1]["messages"][-1]
    assert last["role"] == "user" and "What's running right now?" in json.dumps(last)


def test_a_cut_stream_says_so_and_offers_retry(app_page: Page, server: Server, evidence: Evidence) -> None:
    server.upstream.reply = "A whole answer."

    def cut(route: Route) -> None:
        """The server runs the turn and saves it; the page's stream ends before `finish` (a network drop)."""
        real = route.fetch()
        events = real.text().split("\n\n")
        kept = [e for e in events if e and "[DONE]" not in e and json.loads(e[5:]).get("type") != "finish"]
        route.fulfill(response=real, body="\n\n".join(kept) + "\n\n")

    app_page.route("**/v1/airlie/chat/turn", cut)
    open_chat(app_page, server)
    say(app_page, "tell me")
    evidence.expect(app_page.locator("[data-cut]"), "the cut is said, with a way on").to_be_visible()
    evidence.expect(app_page.get_by_role("button", name="Retry"), "Retry is offered").to_be_visible()
    evidence.expect(app_page.locator("[data-msg=user]"), "my message stays").to_have_text("tell me")


def test_a_reply_that_is_a_json_plan_is_text_never_a_plan_card(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = PLAN
    open_chat(app_page, server)
    say(app_page, "is there a way to clear the airlie chat?")
    evidence.expect(reply(app_page), "the plan JSON is shown as text").to_contain_text("software-engineer")
    evidence.expect(app_page.get_by_role("button", name="Approve"), "nothing to approve").to_have_count(0)


def test_clear_opens_a_fresh_chat_without_asking_the_model(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "An answer."
    open_chat(app_page, server)
    say(app_page, "is there a way to clear this chat")
    evidence.expect(reply(app_page), "the question was answered").to_contain_text("An answer.")
    before = app_page.url
    asked = len(server.upstream.requests)
    say(app_page, "/clear")
    app_page.wait_for_function("(was) => location.href !== was", arg=before)
    evidence.expect(app_page.locator("[data-msg=user]"), "the new chat is empty").to_have_count(0)
    evidence.expect(
        app_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting is back"
    ).to_be_visible()
    assert len(server.upstream.requests) == asked


def test_a_thread_written_by_the_earlier_app_is_legacy_chat_and_read_only(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    filler = [
        m
        for i in range(12)
        for m in (
            {"role": "user", "text": f"earlier {i}", "sent": f"earlier {i}"},
            {"role": "chat", "text": f"answer {i}", "streaming": False, "thinking": False, "action": ""},
        )
    ]
    earlier = [
        *filler,
        {"role": "card", "job_id": f"story-{STORY_KEY}", "kind": "story", "label": "airlie · story"},
        {"role": "user", "text": "what is AIR-30", "sent": "what is AIR-30"},
        {
            "role": "chat",
            "text": f"**{STORY_KEY}** is blocked.",
            "streaming": False,
            "thinking": True,
            "action": "",
        },
    ]
    app_page.add_init_script(
        f"if (!sessionStorage.getItem('airlie.chat')) "
        f"sessionStorage.setItem('airlie.chat', {json.dumps(json.dumps(earlier))});"
    )
    open_chat(app_page, server)
    app_page.locator("[data-test=legacy-row]").click()
    evidence.expect(app_page.locator("[data-test=chat-header] h1"), "the row opens Legacy chat").to_have_text(
        "Legacy chat"
    )
    evidence.expect(app_page.locator("[data-msg=user]").last, "my earlier question").to_have_text(
        "what is AIR-30"
    )
    evidence.expect(
        app_page.locator("[data-card] [data-card-link]"), "a job card from the earlier app renders"
    ).to_have_attribute("href", f"/workflows/{STORY_KEY}")
    bubble = app_page.locator("[data-msg=chat] [data-bubble]").last
    evidence.expect(bubble, "the earlier reply").to_contain_text("is blocked.")
    evidence.expect(
        bubble.get_by_role("link", name=STORY_KEY), "its story key links to the workflow"
    ).to_have_attribute("href", f"/workflows/{STORY_KEY}")
    evidence.expect(
        app_page.get_by_label("Message", exact=True), "nothing can be typed into it"
    ).to_have_count(0)
    evidence.expect(app_page.locator("[data-test=legacy-note]"), "it says it is read-only").to_contain_text(
        "read-only"
    )
    app_page.locator("[data-card] [data-card-link]").click()
    evidence.expect(app_page, "the story card opens the story's workflow view").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}$")
    )


def test_the_tabs_stay_reachable_from_a_chat_on_a_desktop_and_on_a_phone(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(app_page, server)
    tabs = app_page.locator("[data-test=tabs]")
    evidence.expect(tabs.locator("a[aria-current=page]"), "Chat is the current tab").to_have_text("Chat")
    for name, path in (
        ("Board", "/board"),
        ("Workflows", "/workflows"),
        ("Metrics", "/metrics"),
        ("Admin", "/admin"),
    ):
        tabs.get_by_role("link", name=name).click()
        evidence.expect(app_page, f"{name} opens from a chat").to_have_url(re.compile(rf"{path}$"))
        app_page.go_back()
    app_page.set_viewport_size({"width": 375, "height": 700})
    bar = app_page.locator("nav[data-test=tabbar]")
    evidence.expect(bar, "the phone tab bar stays under the chat").to_be_visible()
    bar.get_by_role("link", name="Board").click()
    evidence.expect(app_page, "Board opens from the phone tab bar").to_have_url(re.compile(r"/board$"))


def test_a_long_unbroken_path_and_a_wide_code_block_stay_inside_the_screen_on_a_phone(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    path = "/Users/someone/git/airlie/" + "very-long-directory-name/" * 12 + "file.py"
    wide = "x = [" + ", ".join(f"'item_number_{i}'" for i in range(40)) + "]"
    server.upstream.reply = f"See {path} and https://example.com/{'a' * 150}\n\n```\n{wide}\n```\n"
    mine = "m" * 200  # my own message, unbroken
    app_page.set_viewport_size({"width": 375, "height": 700})
    open_chat(app_page, server)
    say(app_page, mine)
    answer = reply(app_page)
    evidence.expect(answer, "the reply is shown").to_contain_text("very-long-directory-name")
    evidence.expect(answer.locator("pre"), "the code block is there").to_be_visible()
    fits = app_page.evaluate(
        """() => {
          const doc = document.documentElement;
          const scroller = document.querySelector('[data-test=thread-scroll]');
          const inside = (el) => {
            const r = el.getBoundingClientRect();
            return r.right <= window.innerWidth && r.left >= 0 && el.scrollWidth <= el.clientWidth;
          };
          const answer = [...document.querySelectorAll('[data-message=assistant]')].pop();
          return {
            mine: inside(document.querySelector('[data-msg=user] > div')),
            page: doc.scrollWidth <= doc.clientWidth,
            scroller: scroller.scrollWidth <= scroller.clientWidth,
            answer: answer.getBoundingClientRect().right <= window.innerWidth,
          };
        }"""
    )
    evidence.shot("nothing overflows sideways", held=all(fits.values()))
    assert fits == {"mine": True, "page": True, "scroller": True, "answer": True}
