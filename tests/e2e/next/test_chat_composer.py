"""The Vue app's Chat composer in Chromium (/next/): the + and its chooser, chips with a thumbnail and the
limit, your picture in your bubble and again after a reload (the thread holds ids and names, never bytes),
the / menu, /story as an editable story card with the repository type-ahead, and the job card a started
story becomes. Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page, Route

from tests.e2e.conftest import Evidence, Server
from tests.e2e.next.test_chat import _write_story, say
from tests.e2e.next.test_chat import open_chat as open_plain
from tests.e2e.test_chat_attachments import clip, png

pytestmark = pytest.mark.e2e

REPO = "weyenk/airlie"
STORY = "Add a New chat button\n\nChat gets a button that clears the thread. Done when a click empties it."
STARTED = "AIR-9"


def open_chat(
    page: Page, server: Server, repos: list[str], *, job: str = f"story-{STARTED}"
) -> list[dict[str, object]]:
    """The Vue chat with the repository names stubbed; returns the story requests the page makes."""
    posted: list[dict[str, object]] = []
    page.route("**/v1/airlie/repos/names", lambda route: route.fulfill(json={"repos": repos}))

    def stories(route: Route) -> None:
        if route.request.method == "POST":
            posted.append(route.request.post_data_json)
            route.fulfill(status=202, json={"job_id": job, "kind": "story"})
        else:
            route.fallback()

    page.route("**/v1/airlie/stories", stories)
    open_plain(page, server)
    return posted


def attach(page: Page, *files: FilePayload) -> None:
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Attach a file").click()
    chooser.value.set_files(list(files))


def chip(page: Page, name: str) -> Any:
    return page.get_by_role("listitem", name=name)


def last_user_parts(server: Server) -> list[dict[str, Any]]:
    return server.upstream.requests[-1]["messages"][-1]["content"]


def reply(page: Page) -> Any:
    return page.locator("[data-msg=chat] [data-bubble]").last


def test_the_plus_opens_the_file_chooser_and_the_story_quick_reply_fills_the_box(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(next_page, server, [])
    plus = next_page.get_by_role("button", name="Attach a file")
    evidence.expect(plus, "the + is Attach").to_be_visible()
    with next_page.expect_file_chooser() as chooser:
        plus.click()
    assert chooser.value.is_multiple()
    evidence.expect(next_page, "the + does not leave Chat").to_have_url(re.compile(r"/next/$"))
    next_page.locator("[data-quick]", has_text="/story").click()
    evidence.expect(
        next_page.get_by_label("Message", exact=True), "the quick reply puts /story in the box"
    ).to_have_value("/story ")


def test_a_picture_shows_as_a_chip_then_in_my_bubble_and_reaches_the_model_as_itself(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "A pink rectangle."
    open_chat(next_page, server, [])
    data = png()
    attach(next_page, {"name": "shot.png", "mimeType": "image/png", "buffer": data})
    evidence.expect(chip(next_page, "shot.png").locator("img"), "the chip shows the picture").to_be_visible()
    evidence.expect(chip(next_page, "shot.png"), "the chip says its size").to_contain_text(
        re.compile(r"\d+ B")
    )
    evidence.expect(
        next_page.get_by_role("button", name="Send"), "Send is on once it is uploaded"
    ).to_be_enabled()
    say(next_page, "what is this?")
    evidence.expect(next_page.locator("[data-msg=user] img"), "my bubble shows the picture").to_be_visible()
    evidence.expect(chip(next_page, "shot.png"), "the tray is empty").to_have_count(0)
    evidence.expect(reply(next_page), "the reply").to_contain_text("A pink rectangle.")
    parts = last_user_parts(server)
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()},
    }


def test_after_a_reload_the_picture_is_still_there_and_the_thread_holds_no_bytes(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "Seen."
    open_chat(next_page, server, [])
    attach(next_page, {"name": "kept.png", "mimeType": "image/png", "buffer": png()})
    evidence.expect(next_page.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(next_page, "keep this")
    evidence.expect(reply(next_page), "answered").to_contain_text("Seen.")
    next_page.reload()
    img = next_page.locator("[data-msg=user] img")
    evidence.expect(img, "the picture is fetched again after the reload").to_be_visible()
    next_page.wait_for_function("() => document.querySelector('[data-msg=user] img')?.naturalWidth > 0")
    stored = next_page.evaluate("() => sessionStorage.getItem('airlie.chat')")
    evidence.shot("sessionStorage keeps the id and name, never base64", img, held="base64" not in stored)
    assert "base64" not in stored and "blob:" not in stored and "kept.png" in stored
    kept = json.loads(stored)[0]["attachments"][0]
    assert set(kept) == {"id", "name", "mime", "size", "kind"}


def test_a_video_reaches_the_model_as_frames_airlie_sampled(
    next_page: Page, server: Server, evidence: Evidence, tmp_path: Path
) -> None:
    open_chat(next_page, server, [])
    attach(next_page, {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": clip(tmp_path)})
    evidence.expect(chip(next_page, "clip.mp4"), "the chip names the video").to_be_visible()
    evidence.expect(next_page.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(next_page, "what happens here?")
    evidence.expect(next_page.locator("[data-msg=user] video"), "my bubble has the player").to_have_count(1)
    parts = last_user_parts(server)[1:]
    assert parts[0] == {
        "type": "text",
        "text": "Video `clip.mp4` (2.0 s): 2 frames, one every 1.0 s, in order.",
    }
    assert [p["text"] for p in parts[1::2]] == ["t=0.5 s", "t=1.5 s"]
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in parts[2::2])


def test_a_text_file_reaches_the_model_as_its_text_and_a_zip_as_a_line(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(next_page, server, [])
    attach(
        next_page,
        {"name": "server.log", "mimeType": "text/plain", "buffer": b"ERROR disk full\n"},
        {"name": "site.zip", "mimeType": "application/zip", "buffer": b"PK\x03\x04" + b"\x00\xff" * 40},
    )
    evidence.expect(chip(next_page, "server.log"), "a chip for the log").to_be_visible()
    evidence.expect(chip(next_page, "site.zip"), "a chip for the zip").to_be_visible()
    evidence.expect(next_page.get_by_role("button", name="Send"), "both uploaded").to_be_enabled()
    say(next_page, "")
    evidence.expect(
        next_page.locator("[data-msg=user] [data-file]"), "my bubble names both files"
    ).to_have_count(2)
    parts = last_user_parts(server)
    assert parts[0]["text"].startswith("Attached file `server.log`") and "ERROR disk full" in parts[0]["text"]
    assert (
        parts[1]["text"]
        == "Attached file `site.zip` (application/zip, 84 B): its contents can't be read from chat."
    )


def test_a_file_over_the_limit_says_so_keeps_send_off_and_x_clears_it(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(next_page, server, [])
    asked = len(server.upstream.requests)
    attach(
        next_page,
        {"name": "huge.bin", "mimeType": "application/octet-stream", "buffer": b"\x00" * (2 * 1024 * 1024)},
    )
    big = chip(next_page, "huge.bin")
    evidence.expect(big, "the chip says it is too large").to_contain_text(
        "too large: a file may be up to 1 MB"
    )
    evidence.expect(next_page.get_by_role("button", name="Send"), "Send stays off").to_be_disabled()
    box = next_page.get_by_label("Message", exact=True)
    box.fill("send anyway")
    box.press("Enter")
    evidence.expect(next_page.locator("[data-msg=user]"), "nothing was sent").to_have_count(0)
    next_page.get_by_role("button", name="Remove huge.bin").click()
    evidence.expect(big, "× clears it").to_have_count(0)
    evidence.expect(next_page.get_by_role("button", name="Send"), "Send is back on").to_be_enabled()
    assert len(server.upstream.requests) == asked


def test_typing_a_slash_shows_the_menu_and_choosing_story_gives_a_story_card(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    open_chat(next_page, server, [REPO])
    box = next_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    menu = next_page.get_by_role("listbox", name="Commands")
    story, clear = "/story", "/clear"
    evidence.expect(menu.get_by_role("option", name=story), "the menu offers /story").to_be_visible()
    evidence.expect(menu.get_by_role("option", name=clear), "and /clear").to_be_visible()
    box.press_sequentially("zzz")
    evidence.expect(
        next_page.get_by_text("No command matches"), "a prefix nothing matches says so"
    ).to_be_visible()
    box.fill("/st")
    evidence.expect(menu.get_by_role("option"), "the menu narrows as you type").to_have_count(1)
    menu.get_by_role("option", name="/story").click()
    evidence.expect(box, "choosing it fills the box").to_have_value("/story ")
    evidence.expect(menu, "and closes the menu").to_have_count(0)
    say(next_page, "/story the chat gets a New chat button")
    card = next_page.locator("[data-story-card]")
    evidence.expect(card.get_by_label("Story request"), "the reply is an editable story card").to_have_value(
        STORY
    )
    evidence.expect(card.get_by_role("button", name="Start story"), "with Start story").to_be_visible()
    sent = server.upstream.requests[-1]
    assert "skill" not in sent  # the route strips it
    assert sent["messages"][-1]["content"] == "the chat gets a New chat button"
    assert "Turn the request" not in sent["messages"][-1]["content"]


def test_the_menu_is_driven_from_the_keyboard(next_page: Page, server: Server, evidence: Evidence) -> None:
    open_chat(next_page, server, [])
    box = next_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    options = next_page.get_by_role("listbox", name="Commands").get_by_role("option")
    evidence.expect(options.first, "the first command is active").to_have_attribute("aria-selected", "true")
    box.press("ArrowDown")
    evidence.expect(options.nth(1), "ArrowDown moves").to_have_attribute("aria-selected", "true")
    box.press("Escape")
    evidence.expect(options, "Escape closes the menu").to_have_count(0)
    box.fill("/")
    box.press("Enter")
    evidence.expect(box, "Enter chooses the active command").to_have_value(re.compile(r"^/\w+ $"))


def test_clear_from_the_menu_starts_a_fresh_session_and_drops_unsent_files(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "An answer."
    open_chat(next_page, server, [])
    say(next_page, "is there a way to clear this chat")
    evidence.expect(next_page.locator("[data-msg=user]"), "the question is in the thread").to_have_count(1)
    attach(next_page, {"name": "later.log", "mimeType": "text/plain", "buffer": b"x"})
    evidence.expect(chip(next_page, "later.log"), "a file waits in the tray").to_be_visible()
    asked = len(server.upstream.requests)
    box = next_page.get_by_label("Message", exact=True)
    box.press_sequentially("/cl")
    menu = next_page.get_by_role("listbox", name="Commands")
    menu.get_by_role("option", name="/clear").click()
    box.press("Enter")
    evidence.expect(next_page.locator("[data-msg=user]"), "the thread is empty").to_have_count(0)
    evidence.expect(chip(next_page, "later.log"), "the unsent file is dropped").to_have_count(0)
    evidence.expect(
        next_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting is back"
    ).to_be_visible()
    assert len(server.upstream.requests) == asked


def test_start_story_posts_the_text_and_repository_and_shows_a_job_card(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    posted = open_chat(next_page, server, [REPO, "weyenk/other"])
    say(next_page, "/story the chat gets a New chat button")
    card = next_page.locator("[data-story-card]")
    card.get_by_label("Story request").fill(STORY + "\nEdited.")
    picker = card.get_by_role("combobox", name="Repository")
    picker.fill("weyenk/")
    evidence.expect(
        card.get_by_role("option"), "the type-ahead offers one match per repository"
    ).to_have_count(2)
    picker.fill(REPO)
    card.get_by_role("button", name="Start story").click()
    job = next_page.locator("[data-card]")
    evidence.expect(job, "a job card replaces the story card").to_contain_text("story started")
    evidence.expect(job, "it names the repository").to_contain_text("airlie · story")
    evidence.expect(next_page.locator("[data-story-card]"), "the story card is gone").to_have_count(0)
    evidence.expect(
        job.get_by_role("link", name=re.compile("Open workflow")), "it opens the workflow"
    ).to_have_attribute("href", f"/next/workflows/{STARTED}")
    assert posted == [{"text": STORY + "\nEdited.", "repo": REPO}]
    # the card is part of the thread: it is there after a reload, and the repository is the default next time
    next_page.reload()
    evidence.expect(next_page.locator("[data-card]"), "the job card is there after a reload").to_be_visible()
    assert next_page.evaluate("localStorage.getItem('airlie.repo')") == REPO


def test_an_edited_story_and_the_last_repository_come_back(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    next_page.add_init_script(
        f"if (!localStorage.getItem('airlie.repo')) localStorage.setItem('airlie.repo', {REPO!r});"
    )
    posted = open_chat(next_page, server, [REPO])
    say(next_page, "/story the chat gets a New chat button")
    card = next_page.locator("[data-story-card]")
    evidence.expect(
        card.get_by_role("combobox", name="Repository"), "the last repository is the default"
    ).to_have_value(REPO)
    card.get_by_label("Story request").fill("An edit that stays.")
    next_page.reload()
    evidence.expect(
        next_page.locator("[data-story-card]").get_by_label("Story request"), "the edit survives a reload"
    ).to_have_value("An edit that stays.")
    assert posted == []  # nothing starts without the click


def test_start_story_without_a_repository_says_to_pick_one_and_posts_nothing(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    posted = open_chat(next_page, server, [])
    say(next_page, "/story the chat gets a New chat button")
    next_page.locator("[data-story-card]").get_by_role("button", name="Start story").click()
    evidence.expect(
        next_page.locator("[data-story-card] [role=alert]"), "it asks for a repository"
    ).to_have_text("Pick a repository first.")
    evidence.expect(next_page.locator("[data-card]"), "no job card").to_have_count(0)
    assert posted == []


def test_the_composer_has_no_repository_row_and_the_story_card_has_the_picker(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    open_chat(next_page, server, [REPO, "weyenk/other"])
    evidence.expect(
        next_page.get_by_label("Repository"), "no repository picker under the message box"
    ).to_have_count(0)
    say(next_page, "/story the chat gets a New chat button")
    evidence.expect(
        next_page.locator("[data-story-card]").get_by_label("Repository"), "the story card has its own picker"
    ).to_be_visible()


def test_the_job_card_follows_the_story_with_no_reload(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    own_server.upstream.reply = STORY
    open_chat(next_page, own_server, [REPO])
    say(next_page, "/story the chat gets a New chat button")
    card = next_page.locator("[data-story-card]")
    card.get_by_role("combobox", name="Repository").fill(REPO)
    card.get_by_role("button", name="Start story").click()
    job = next_page.locator("[data-card]")
    evidence.expect(job.locator("[data-stage]"), "nothing has run yet").to_have_text("starting")
    evidence.expect(job.get_by_role("progressbar"), "so no progress").to_have_count(0)
    _write_story(own_server.state, STARTED)  # as the tick would
    evidence.expect(job.locator("[data-stage]"), "the stage follows the story").to_have_text(
        "planning layers"
    )
    evidence.expect(job.get_by_role("progressbar"), "and the progress shows").to_be_visible()
    job.get_by_role("link", name=re.compile("Open workflow")).click()
    evidence.expect(next_page, "Open workflow opens the story").to_have_url(
        re.compile(rf"/next/workflows/{STARTED}$")
    )


def test_the_slash_menu_and_the_tray_fit_on_a_phone(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    next_page.set_viewport_size({"width": 375, "height": 700})
    open_chat(next_page, server, [])
    long_name = "a-very-long-name-" + "x" * 80 + ".log"
    attach(next_page, {"name": long_name, "mimeType": "text/plain", "buffer": b"x"})
    box = next_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    menu = next_page.locator("[data-test=slash-menu]")
    evidence.expect(menu, "the menu is there").to_be_visible()
    box_edges = menu.bounding_box()
    assert box_edges is not None and box_edges["x"] >= 0 and box_edges["x"] + box_edges["width"] <= 375
    box.fill("")
    fits = next_page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    evidence.shot("a long file name does not widen the page", held=bool(fits))
    assert fits
