"""Chat attachments in Chromium (design 2026-10-22-chat-attachments): the + opens the file chooser, a picked
file uploads as a chip, and the chat route hands the model a picture or video as itself, a text file as its
text, anything else as a line saying what it is. Each check leaves its screenshot in test-results/evidence/
(conftest.Evidence)."""

from __future__ import annotations

import base64
import re
import struct
import zlib
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page

from tests.e2e.conftest import Evidence, Server
from tests.e2e.test_chat import open_chat, say

pytestmark = pytest.mark.e2e

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200


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


def attach(page: Page, *files: FilePayload) -> None:
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Attach a file").click()
    chooser.value.set_files(list(files))


def chip(page: Page, name: str) -> Any:
    return page.get_by_role("listitem", name=name)


def last_user_parts(server: Server) -> list[dict[str, Any]]:
    return server.upstream.requests[-1]["messages"][-1]["content"]


def test_the_plus_opens_the_file_chooser_and_start_new_work_is_a_quick_reply(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(signed_in, server, [])
    plus = signed_in.get_by_role("button", name="Attach a file")
    evidence.expect(plus, "the + is Attach").to_be_visible()
    with signed_in.expect_file_chooser() as chooser:
        plus.click()
    assert chooser.value.is_multiple()
    evidence.expect(signed_in, "the + does not leave Chat").to_have_url(re.compile(r"#/$"))
    signed_in.get_by_role("button", name="Start new work").click()
    evidence.expect(signed_in, "the quick reply still opens Start new work").to_have_url(
        re.compile(r"#/start$")
    )


def test_a_picture_shows_as_a_chip_then_in_my_bubble_and_reaches_the_model_as_itself(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "A pink rectangle."
    open_chat(signed_in, server, [])
    data = png()
    attach(signed_in, {"name": "shot.png", "mimeType": "image/png", "buffer": data})
    evidence.expect(chip(signed_in, "shot.png").locator("img"), "the chip shows the picture").to_be_visible()
    evidence.expect(
        signed_in.get_by_role("button", name="Send"), "Send is on once it is uploaded"
    ).to_be_enabled()
    say(signed_in, "what is this?")
    evidence.expect(signed_in.locator(".me img"), "my bubble shows the picture").to_be_visible()
    evidence.expect(signed_in.get_by_role("listitem", name="shot.png"), "the tray is empty").to_have_count(0)
    evidence.expect(signed_in.locator(".them").last, "the reply").to_contain_text("A pink rectangle.")
    parts = last_user_parts(server)
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()},
    }


def test_after_a_reload_the_picture_is_still_there_and_the_thread_holds_no_bytes(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    server.upstream.reply = "Seen."
    open_chat(signed_in, server, [])
    attach(signed_in, {"name": "kept.png", "mimeType": "image/png", "buffer": png()})
    evidence.expect(signed_in.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(signed_in, "keep this")
    evidence.expect(signed_in.locator(".them").last, "answered").to_contain_text("Seen.")
    signed_in.reload()
    img = signed_in.locator(".me img")
    evidence.expect(img, "the picture is fetched again after the reload").to_be_visible()
    signed_in.wait_for_function("() => document.querySelector('.me img')?.naturalWidth > 0")
    stored = signed_in.evaluate("() => sessionStorage.getItem('airlie.chat')")
    evidence.shot("sessionStorage keeps the id and name, never base64", img, held="base64" not in stored)
    assert "base64" not in stored and "kept.png" in stored


def test_a_video_reaches_the_model_as_video_with_the_frame_settings(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(signed_in, server, [])
    attach(signed_in, {"name": "clip.mp4", "mimeType": "video/mp4", "buffer": MP4})
    evidence.expect(chip(signed_in, "clip.mp4"), "the chip names the video").to_be_visible()
    evidence.expect(signed_in.get_by_role("button", name="Send"), "uploaded").to_be_enabled()
    say(signed_in, "what happens here?")
    evidence.expect(signed_in.locator(".me video"), "my bubble has the player").to_have_count(1)
    body = server.upstream.requests[-1]
    assert body["messages"][-1]["content"][1]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert body["video_fps"] == 1.0 and body["video_max_frames"] == 32


def test_a_text_file_reaches_the_model_as_its_text_and_a_zip_as_a_line(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(signed_in, server, [])
    attach(
        signed_in,
        {"name": "server.log", "mimeType": "text/plain", "buffer": b"ERROR disk full\n"},
        {"name": "site.zip", "mimeType": "application/zip", "buffer": b"PK\x03\x04" + b"\x00\xff" * 40},
    )
    evidence.expect(chip(signed_in, "server.log"), "a chip for the log").to_be_visible()
    evidence.expect(chip(signed_in, "site.zip"), "a chip for the zip").to_be_visible()
    evidence.expect(signed_in.get_by_role("button", name="Send"), "both uploaded").to_be_enabled()
    say(signed_in, "")
    evidence.expect(signed_in.locator(".me .me-file"), "my bubble names both files").to_have_count(2)
    parts = last_user_parts(server)
    assert parts[0]["text"].startswith("Attached file `server.log`") and "ERROR disk full" in parts[0]["text"]
    assert (
        parts[1]["text"]
        == "Attached file `site.zip` (application/zip, 84 B): its contents can't be read from chat."
    )


def test_a_file_over_the_limit_says_so_keeps_send_off_and_x_clears_it(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    open_chat(signed_in, server, [])
    asked = len(server.upstream.requests)
    attach(
        signed_in,
        {"name": "huge.bin", "mimeType": "application/octet-stream", "buffer": b"\x00" * (2 * 1024 * 1024)},
    )
    big = chip(signed_in, "huge.bin")
    evidence.expect(big, "the chip says it is too large").to_contain_text(
        "too large: a file may be up to 1 MB"
    )
    evidence.expect(signed_in.get_by_role("button", name="Send"), "Send stays off").to_be_disabled()
    signed_in.get_by_label("Message", exact=True).fill("send anyway")
    signed_in.get_by_label("Message", exact=True).press("Enter")
    evidence.expect(signed_in.locator(".me"), "nothing was sent").to_have_count(0)
    signed_in.get_by_role("button", name="Remove huge.bin").click()
    evidence.expect(big, "× clears it").to_have_count(0)
    evidence.expect(signed_in.get_by_role("button", name="Send"), "Send is back on").to_be_enabled()
    assert len(server.upstream.requests) == asked
