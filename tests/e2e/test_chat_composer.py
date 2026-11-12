"""The Vue app's Chat composer in Chromium (/): the + and its chooser, chips with a thumbnail and the limit,
your picture in your bubble and again after a reload (the saved thread holds ids and names, never bytes), the
/ menu and /clear. Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence). (The
story card and Start story left with the redesign: a /story reply is text in the thread.)"""

from __future__ import annotations

import base64
import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
from playwright.sync_api import FilePayload, Page

from tests.e2e.conftest import KEY, Evidence, Server
from tests.e2e.test_chat import open_chat, say

pytestmark = pytest.mark.e2e


def png(w: int = 24, h: int = 16) -> bytes:
    """A real PNG, so the thumbnail draws."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes((217, 84, 110)) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def clip(folder: Path) -> bytes:
    """A real 2 s mp4 (a red square crossing a white frame), as a phone would hand over."""
    path = folder / "clip.mp4"
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    for i in range(20):
        frame = np.full((240, 320, 3), 255, np.uint8)
        cv2.rectangle(frame, (10 + i * 12, 90), (60 + i * 12, 140), (0, 0, 255), -1)
        out.write(frame)
    out.release()
    return path.read_bytes()


STORY = "Add a New chat button\n\nChat gets a button that clears the thread. Done when a click empties it."


def attach(page: Page, *files: FilePayload) -> None:
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Attach a file").click()
    chooser.value.set_files(list(files))


def chip(page: Page, name: str) -> Any:
    return page.get_by_role("listitem", name=name)


def last_user_parts(server: Server) -> list[dict[str, Any]]:
    """What the model was sent for the person's last message."""
    content = server.upstream.requests[-1]["messages"][-1]["content"]
    assert isinstance(content, list), content
    return content


def reply(page: Page) -> Any:
    return page.locator("[data-message=assistant]").last


def test_the_plus_opens_the_file_chooser_and_the_story_quick_reply_fills_the_box(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(app_page, server)
    plus = app_page.get_by_role("button", name="Attach a file")
    evidence.expect(plus, "the + is Attach").to_be_visible()
    with app_page.expect_file_chooser() as chooser:
        plus.click()
    assert chooser.value.is_multiple()
    evidence.expect(app_page, "the + does not leave Chat").to_have_url(re.compile(r"/(chat/[^/]+)?$"))
    app_page.locator("[data-quick]", has_text="/story").click()
    evidence.expect(
        app_page.get_by_label("Message", exact=True), "the quick reply puts /story in the box"
    ).to_have_value("/story ")


def test_a_picture_shows_as_a_chip_then_in_my_bubble_and_reaches_the_model_as_itself(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "A pink rectangle."
    open_chat(app_page, server)
    data = png()
    attach(app_page, {"name": "shot.png", "mimeType": "image/png", "buffer": data})
    evidence.expect(chip(app_page, "shot.png").locator("img"), "the chip shows the picture").to_be_visible()
    evidence.expect(chip(app_page, "shot.png"), "the chip says its size").to_contain_text(
        re.compile(r"\d+ B")
    )
    evidence.expect(
        app_page.get_by_role("button", name="Send"), "Send is on once it is uploaded"
    ).to_be_enabled()
    say(app_page, "what is this?")
    evidence.expect(app_page.locator("[data-msg=user] img"), "my bubble shows the picture").to_be_visible()
    evidence.expect(chip(app_page, "shot.png"), "the tray is empty").to_have_count(0)
    evidence.expect(reply(app_page), "the reply").to_contain_text("A pink rectangle.")
    parts = last_user_parts(server)
    assert {"type": "text", "text": "what is this?"} in parts
    assert {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()},
    } in parts


def test_after_a_reload_the_picture_is_still_there_and_the_thread_holds_no_bytes(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "Seen."
    open_chat(app_page, server)
    attach(app_page, {"name": "kept.png", "mimeType": "image/png", "buffer": png()})
    evidence.expect(app_page.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(app_page, "keep this")
    evidence.expect(reply(app_page), "answered").to_contain_text("Seen.")
    app_page.reload()
    img = app_page.locator("[data-msg=user] img")
    evidence.expect(img, "the picture is fetched again after the reload").to_be_visible()
    app_page.wait_for_function("() => document.querySelector('[data-msg=user] img')?.naturalWidth > 0")
    kept = app_page.evaluate(
        "() => JSON.stringify([...Object.entries(sessionStorage), ...Object.entries(localStorage)])"
    )
    evidence.shot("the tab keeps no picture bytes", img, held="base64" not in kept and "blob:" not in kept)
    assert "base64" not in kept and "blob:" not in kept
    with httpx.Client(base_url=server.url, headers={"Authorization": f"Bearer {KEY}"}) as c:
        saved = c.get("/v1/airlie/chat/threads").json()
        thread = c.get(f"/v1/airlie/chat/threads/{saved[0]['id']}").json()
    parts = thread["messages"][0]["parts"]
    assert "kept.png" in json.dumps(parts) and "base64" not in json.dumps(parts)


def test_a_video_reaches_the_model_as_frames_airlie_sampled(
    app_page: Page, server: Server, evidence: Evidence, tmp_path: Path
) -> None:
    open_chat(app_page, server)
    attach(app_page, {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": clip(tmp_path)})
    evidence.expect(chip(app_page, "clip.mp4"), "the chip names the video").to_be_visible()
    evidence.expect(app_page.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(app_page, "what happens here?")
    evidence.expect(app_page.locator("[data-msg=user] video"), "my bubble has the player").to_have_count(1)
    parts = last_user_parts(server)
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert "Video `clip.mp4` (2.0 s): 2 frames, one every 1.0 s, in order." in texts
    assert [t for t in texts if t.startswith("t=")] == ["t=0.5 s", "t=1.5 s"]
    frames = [p for p in parts if p["type"] == "image_url"]
    assert len(frames) == 2 and all(
        p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in frames
    )


def test_a_text_file_reaches_the_model_as_its_text_and_a_zip_as_a_line(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(app_page, server)
    attach(
        app_page,
        {"name": "server.log", "mimeType": "text/plain", "buffer": b"ERROR disk full\n"},
        {"name": "site.zip", "mimeType": "application/zip", "buffer": b"PK\x03\x04" + b"\x00\xff" * 40},
    )
    evidence.expect(chip(app_page, "server.log"), "a chip for the log").to_be_visible()
    evidence.expect(chip(app_page, "site.zip"), "a chip for the zip").to_be_visible()
    evidence.expect(app_page.get_by_role("button", name="Send"), "both uploaded").to_be_enabled()
    say(app_page, "")
    evidence.expect(
        app_page.locator("[data-msg=user] [data-file]"), "my bubble names both files"
    ).to_have_count(2)
    texts = [p["text"] for p in last_user_parts(server) if p["type"] == "text"]
    assert any(t.startswith("Attached file `server.log`") and "ERROR disk full" in t for t in texts)
    assert "Attached file `site.zip` (application/zip, 84 B): its contents can't be read from chat." in texts


def test_a_file_over_the_limit_says_so_keeps_send_off_and_x_clears_it(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(app_page, server)
    asked = len(server.upstream.requests)
    attach(
        app_page,
        {"name": "huge.bin", "mimeType": "application/octet-stream", "buffer": b"\x00" * (2 * 1024 * 1024)},
    )
    big = chip(app_page, "huge.bin")
    evidence.expect(big, "the chip says it is too large").to_contain_text(
        "too large: a file may be up to 1 MB"
    )
    evidence.expect(app_page.get_by_role("button", name="Send"), "Send stays off").to_be_disabled()
    box = app_page.get_by_label("Message", exact=True)
    box.fill("send anyway")
    box.press("Enter")
    evidence.expect(app_page.locator("[data-msg=user]"), "nothing was sent").to_have_count(0)
    app_page.get_by_role("button", name="Remove huge.bin").click()
    evidence.expect(big, "× clears it").to_have_count(0)
    evidence.expect(app_page.get_by_role("button", name="Send"), "Send is back on").to_be_enabled()
    assert len(server.upstream.requests) == asked


def test_typing_a_slash_shows_the_menu_and_a_story_command_sends_its_skill(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = STORY
    open_chat(app_page, server)
    box = app_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    menu = app_page.get_by_role("listbox", name="Commands")
    story, clear = "/story", "/clear"
    evidence.expect(menu.get_by_role("option", name=story), "the menu offers /story").to_be_visible()
    evidence.expect(menu.get_by_role("option", name=clear), "and /clear").to_be_visible()
    box.press_sequentially("zzz")
    evidence.expect(
        app_page.get_by_text("No command matches"), "a prefix nothing matches says so"
    ).to_be_visible()
    box.fill("/st")
    evidence.expect(menu.get_by_role("option"), "the menu narrows as you type").to_have_count(1)
    menu.get_by_role("option", name="/story").click()
    evidence.expect(box, "choosing it fills the box").to_have_value("/story ")
    evidence.expect(menu, "and closes the menu").to_have_count(0)
    say(app_page, "/story the chat gets a New chat button")
    evidence.expect(app_page.locator("[data-msg=user]"), "my bubble holds the request alone").to_have_text(
        "the chat gets a New chat button"
    )
    evidence.expect(reply(app_page), "the reply is the story request, as text").to_contain_text(
        "Add a New chat button"
    )
    sent = json.dumps(server.upstream.requests[-1])
    assert "story pipeline will build" in sent  # the story skill's prompt went with the turn


def test_the_menu_is_driven_from_the_keyboard(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_chat(app_page, server)
    box = app_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    options = app_page.get_by_role("listbox", name="Commands").get_by_role("option")
    evidence.expect(options.first, "the first command is active").to_have_attribute("aria-selected", "true")
    box.press("ArrowDown")
    evidence.expect(options.nth(1), "ArrowDown moves").to_have_attribute("aria-selected", "true")
    box.press("Escape")
    evidence.expect(options, "Escape closes the menu").to_have_count(0)
    box.fill("/")
    box.press("Enter")
    evidence.expect(box, "Enter chooses the active command").to_have_value(re.compile(r"^/\w+ $"))


def test_clear_from_the_menu_opens_a_fresh_chat_and_drops_unsent_files(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "An answer."
    open_chat(app_page, server)
    say(app_page, "is there a way to clear this chat")
    evidence.expect(app_page.locator("[data-msg=user]"), "the question is in the thread").to_have_count(1)
    evidence.expect(reply(app_page), "and answered").to_contain_text("An answer.")
    attach(app_page, {"name": "later.log", "mimeType": "text/plain", "buffer": b"x"})
    evidence.expect(chip(app_page, "later.log"), "a file waits in the tray").to_be_visible()
    asked = len(server.upstream.requests)
    before = app_page.url
    box = app_page.get_by_label("Message", exact=True)
    box.press_sequentially("/cl")
    menu = app_page.get_by_role("listbox", name="Commands")
    menu.get_by_role("option", name="/clear").click()
    box.press("Enter")
    app_page.wait_for_function("(was) => location.href !== was", arg=before)
    evidence.expect(app_page.locator("[data-msg=user]"), "the new chat is empty").to_have_count(0)
    evidence.expect(chip(app_page, "later.log"), "the unsent file is dropped").to_have_count(0)
    evidence.expect(
        app_page.get_by_text("Hi, I'm airlie's assistant"), "the greeting is back"
    ).to_be_visible()
    assert len(server.upstream.requests) == asked


def test_the_slash_menu_and_the_tray_fit_on_a_phone(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size({"width": 375, "height": 700})
    open_chat(app_page, server)
    long_name = "a-very-long-name-" + "x" * 80 + ".log"
    attach(app_page, {"name": long_name, "mimeType": "text/plain", "buffer": b"x"})
    box = app_page.get_by_label("Message", exact=True)
    box.press_sequentially("/")
    menu = app_page.locator("[data-test=slash-menu]")
    evidence.expect(menu, "the menu is there").to_be_visible()
    box_edges = menu.bounding_box()
    assert box_edges is not None and box_edges["x"] >= 0 and box_edges["x"] + box_edges["width"] <= 375
    box.fill("")
    fits = app_page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    evidence.shot("a long file name does not widen the page", held=bool(fits))
    assert fits


def test_a_failed_upload_says_why_and_x_clears_it(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_chat(app_page, server)
    app_page.route(
        "**/v1/airlie/uploads?*", lambda route: route.fulfill(status=507, json={"detail": "the disk is full"})
    )
    attach(app_page, {"name": "notes.log", "mimeType": "text/plain", "buffer": b"x"})
    evidence.expect(chip(app_page, "notes.log"), "the chip says why").to_contain_text("the disk is full")
    evidence.expect(app_page.get_by_role("button", name="Send"), "Send stays off").to_be_disabled()
    app_page.get_by_role("button", name="Remove notes.log").click()
    evidence.expect(chip(app_page, "notes.log"), "× clears it").to_have_count(0)
    evidence.expect(app_page.get_by_role("button", name="Send"), "Send is back on").to_be_enabled()
