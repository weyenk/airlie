"""Chat attachments (design 2026-10-22-chat-attachments): POST/GET /v1/airlie/uploads, and the chat route
turning each `airlie_upload` part into what the chat model can take."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.serve.app import create_app
from airlie.serve.uploads import fence, sniff
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve_chat import chat_cfg

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64
ZIP = b"PK\x03\x04" + b"\x00\x01\x02\xff" * 32


def tiny_pdf(text: str | None) -> bytes:
    """A one-page PDF whose page says `text` in Helvetica (None: a page with no text layer, like a scan)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def setup(tmp_path: Path, **attachments: Any) -> tuple[TestClient, FakeUpstream, Any]:
    cfg = chat_cfg(tmp_path)
    cfg.roles["chat"].vision = True
    for k, v in attachments.items():
        setattr(cfg.chat.attachments, k, v)
    up = FakeUpstream()
    return TestClient(create_app(cfg, transport=httpx.MockTransport(up))), up, cfg


def put(client: TestClient, name: str, data: bytes, **kw: Any) -> httpx.Response:
    return client.post("/v1/airlie/uploads", params={"name": name}, content=data, **kw)


def ask(
    client: TestClient, *parts: dict[str, Any], model: str = "chat", history: list[Any] | None = None
) -> Any:
    messages = [*(history or []), {"role": "user", "content": [{"type": "text", "text": "look"}, *parts]}]
    return client.post("/v1/chat/completions", json={"model": model, "messages": messages})


def ref(uid: str, name: str = "f") -> dict[str, Any]:
    return {"type": "airlie_upload", "upload": uid, "name": name}


def sent(up: FakeUpstream) -> dict[str, Any]:
    return json.loads(up.requests[-1].content)


def user_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for m in body["messages"] if m["role"] == "user" for p in m["content"]]


# ---- the kind comes from the bytes ----
@pytest.mark.parametrize(
    ("data", "name", "kind", "mime"),
    [
        (PNG, "a.png", "image", "image/png"),
        (JPEG, "IMG_0001.JPG", "image", "image/jpeg"),
        (MP4, "clip.mp4", "video", "video/mp4"),
        (MOV, "IMG_0002.MOV", "video", "video/quicktime"),
        (WEBM, "screen.webm", "video", "video/webm"),
        (tiny_pdf("hi"), "doc.pdf", "pdf", "application/pdf"),
        (b"print('hi')\n", "x.py", "text", "text/x-python"),
        (b"plain words", "notes", "text", "text/plain"),
        (ZIP, "shot.png", "file", "image/png"),  # a .png name on zip bytes is a file
        (HEIC, "IMG_0003.HEIC", "file", "image/heic"),
    ],
)
def test_the_kind_is_read_from_the_bytes_not_the_name(data: bytes, name: str, kind: str, mime: str) -> None:
    assert sniff(data, name) == (kind, mime)


def test_text_cut_inside_a_multibyte_character_is_still_text() -> None:
    assert sniff("café".encode()[:-1], "a")[0] == "text"


# ---- POST /v1/airlie/uploads ----
def test_an_upload_stores_the_bytes_and_a_meta_file(tmp_path: Path) -> None:
    client, _, cfg = setup(tmp_path)
    r = put(client, "../../etc/shot.png", PNG, headers={"Content-Type": "text/html"})
    assert r.status_code == 201
    out = r.json()
    assert out.keys() == {"id", "name", "kind", "mime", "size"} and len(out["id"]) == 32
    assert (
        out["name"] == "shot.png" and out["kind"] == "image" and out["mime"] == "image/png"
    )  # not the header
    root = cfg.observability.state_dir.expanduser() / "uploads"
    assert (root / out["id"]).read_bytes() == PNG and out["size"] == len(PNG)
    meta = json.loads((root / f"{out['id']}.json").read_text())
    assert meta["owner"] == "bootstrap" and meta["kind"] == "image"


def test_ids_are_random_not_a_hash_of_the_content(tmp_path: Path) -> None:
    client, _, _ = setup(tmp_path)
    assert put(client, "a.png", PNG).json()["id"] != put(client, "a.png", PNG).json()["id"]


@pytest.mark.parametrize(
    ("data", "limit", "what"),
    [
        (PNG + b"\x00" * 5000, {"max_image_mb": 0.001}, "a picture may be up to 0.001 MB"),
        (
            MP4 + b"\x00" * 300_000,
            {"max_video_mb": 0.1},
            "a video may be up to 0.1 MB",
        ),  # past the 64 KB head
        (b"x" * 5000, {"max_file_mb": 0.001}, "a file may be up to 0.001 MB"),
    ],
)
def test_a_file_past_its_kinds_limit_is_413_naming_it_and_leaves_nothing(
    tmp_path: Path, data: bytes, limit: dict[str, float], what: str
) -> None:
    client, _, cfg = setup(tmp_path, **limit)
    r = put(client, "big", data)
    assert r.status_code == 413 and what in r.json()["detail"]
    assert list((cfg.observability.state_dir.expanduser() / "uploads").iterdir()) == []


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    client, _, _ = setup(tmp_path)
    r = put(client, "empty.txt", b"")
    assert r.status_code == 400 and "empty" in r.json()["detail"]


def test_the_limits_endpoint_tells_the_composer_what_it_may_attach(tmp_path: Path) -> None:
    client, _, _ = setup(tmp_path, max_video_mb=50)
    assert client.get("/v1/airlie/chat/attachments").json() == {
        "max_per_message": 10,
        "max_image_mb": 20,
        "max_video_mb": 50,
        "max_file_mb": 25,
    }


# ---- who may read an upload ----
@pytest.fixture
def people(tmp_path: Path) -> dict[str, Any]:
    cfg = chat_cfg(tmp_path)
    store = AuthStore(tmp_path / "auth.sqlite")
    ana, ben = store.add_user("Ana", "member"), store.add_user("Ben", "member")
    app = create_app(cfg, transport=httpx.MockTransport(FakeUpstream()), api_key="k", auth_store=store)
    cookies = {u.name: {SESSION_COOKIE: store.make_session(u.id, 1)} for u in (ana, ben)}
    return {"client": TestClient(app), "cookies": cookies}


def test_uploads_need_a_sign_in(people: dict[str, Any]) -> None:
    client = people["client"]
    assert put(client, "a.png", PNG).status_code == 401
    assert client.get(f"/v1/airlie/uploads/{'0' * 32}").status_code == 401


def test_only_the_uploader_or_an_admin_reads_an_upload(people: dict[str, Any]) -> None:
    client, cookies = people["client"], people["cookies"]
    client.cookies.update(cookies["Ana"])
    uid = put(client, "a.png", PNG).json()["id"]
    assert client.get(f"/v1/airlie/uploads/{uid}").content == PNG
    client.cookies.clear()
    client.cookies.update(cookies["Ben"])
    theirs = client.get(f"/v1/airlie/uploads/{uid}")
    unknown = client.get(f"/v1/airlie/uploads/{'0' * 32}")
    assert theirs.status_code == unknown.status_code == 404 and theirs.json() == unknown.json()
    client.cookies.clear()
    assert client.get(f"/v1/airlie/uploads/{uid}", headers={"Authorization": "Bearer k"}).content == PNG


def test_only_a_picture_or_video_is_served_inline(tmp_path: Path) -> None:
    client, _, _ = setup(tmp_path)
    pic = client.get(f"/v1/airlie/uploads/{put(client, 'a.png', PNG).json()['id']}")
    assert pic.headers["content-type"] == "image/png" and pic.headers["content-disposition"].startswith(
        "inline"
    )
    page = client.get(
        f"/v1/airlie/uploads/{put(client, 'x.html', b'<script>alert(1)</script>').json()['id']}"
    )
    assert page.headers["content-type"] == "application/octet-stream"
    assert page.headers["content-disposition"].startswith("attachment")
    for r in (pic, page):
        assert (
            r.headers["x-content-type-options"] == "nosniff"
            and r.headers["content-security-policy"] == "sandbox"
        )


def test_a_bad_id_is_404_and_never_a_path(tmp_path: Path) -> None:
    client, _, _ = setup(tmp_path)
    assert client.get("/v1/airlie/uploads/..%2Fauth.sqlite").status_code == 404


def test_uploads_older_than_keep_days_are_deleted(tmp_path: Path) -> None:
    client, _, cfg = setup(tmp_path, keep_days=1)
    old, new = put(client, "old.png", PNG).json()["id"], put(client, "new.png", PNG).json()["id"]
    root = cfg.observability.state_dir.expanduser() / "uploads"
    past = time.time() - 2 * 86400
    for p in root.glob(f"{old}*"):
        os.utime(p, (past, past))
    TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream())))  # a start sweeps
    assert sorted(p.name for p in root.iterdir()) == sorted([new, f"{new}.json"])
    assert client.get(f"/v1/airlie/uploads/{old}").status_code == 404


# ---- what the chat model gets ----
def test_a_picture_reaches_the_engine_as_an_image_url_data_url(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "a.png", PNG).json()["id"]
    assert ask(client, ref(uid)).status_code == 200
    body = sent(up)
    parts = user_parts(body)
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
    }
    assert "video_fps" not in body  # no video in this request


def test_a_video_goes_as_video_url_with_the_frame_settings(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path, video_fps=0.5, video_max_frames=16)
    uid = put(client, "clip.mov", MOV).json()["id"]
    ask(client, ref(uid))
    body = sent(up)
    assert user_parts(body)[1]["video_url"]["url"].startswith("data:video/quicktime;base64,")
    assert body["video_fps"] == 0.5 and body["video_max_frames"] == 16


def test_a_text_file_is_fenced_with_its_name(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "server.log", b"ERROR boom\n```\nnot a fence end\n").json()["id"]
    ask(client, ref(uid))
    text = user_parts(sent(up))[1]["text"]
    assert text.startswith("Attached file `server.log` (")
    assert "````\nERROR boom\n```\nnot a fence end\n\n````" in text  # a longer fence than the file's own


def test_a_long_text_is_cut_with_a_note(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path, max_text_kb=1)
    uid = put(client, "big.txt", b"a" * 5000).json()["id"]
    ask(client, ref(uid))
    text = user_parts(sent(up))[1]["text"]
    assert "(cut at 1 KB of 5 KB)" in text and "a" * 1024 in text and "a" * 1025 not in text


def test_a_pdf_reaches_the_model_as_its_text(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "spec.pdf", tiny_pdf("Hello from a PDF")).json()["id"]
    ask(client, ref(uid))
    text = user_parts(sent(up))[1]["text"]
    assert text.startswith("Attached file `spec.pdf`") and "Hello from a PDF" in text


def test_a_pdf_with_no_text_layer_says_so(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "scan.pdf", tiny_pdf(None)).json()["id"]
    ask(client, ref(uid))
    assert "no text layer" in user_parts(sent(up))[1]["text"]


def test_a_broken_pdf_says_its_text_could_not_be_read(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "broken.pdf", b"%PDF-1.4\nnot really\n").json()["id"]
    assert ask(client, ref(uid)).status_code == 200
    assert "could not be read" in user_parts(sent(up))[1]["text"]


def test_any_other_file_is_one_line_saying_it_cannot_be_read(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "site.zip", ZIP).json()["id"]
    ask(client, ref(uid))
    line = user_parts(sent(up))[1]["text"]
    assert line == "Attached file `site.zip` (application/zip, 132 B): its contents can't be read from chat."


def test_a_missing_or_someone_elses_upload_becomes_a_placeholder(tmp_path: Path) -> None:
    client, up, cfg = setup(tmp_path)
    theirs = put(client, "theirs.png", PNG).json()["id"]
    meta = cfg.observability.state_dir.expanduser() / "uploads" / f"{theirs}.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "owner": "someone-else"}))
    store = AuthStore(tmp_path / "people.sqlite")
    me = store.add_user("Me", "member")
    app = create_app(cfg, transport=httpx.MockTransport(up), api_key="k", auth_store=store)
    mine = TestClient(app, cookies={SESSION_COOKIE: store.make_session(me.id, 1)})
    assert ask(mine, ref(theirs, "theirs.png"), ref("f" * 32, "gone.txt")).status_code == 200
    parts = user_parts(sent(up))
    assert parts[1:] == [
        {"type": "text", "text": "[attachment `theirs.png` is no longer available]"},
        {"type": "text", "text": "[attachment `gone.txt` is no longer available]"},
    ]


def test_only_the_newest_pictures_and_video_go_as_pixels(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path, history_images=2, history_videos=1)
    pics = [put(client, f"p{i}.png", PNG).json()["id"] for i in range(3)]
    vids = [put(client, f"v{i}.mp4", MP4).json()["id"] for i in range(2)]
    history = [
        {"role": "user", "content": [ref(pics[0], "p0.png"), ref(vids[0], "v0.mp4")]},
        {"role": "assistant", "content": "seen"},
    ]
    ask(client, ref(pics[1]), ref(pics[2]), ref(vids[1]), history=history)
    kinds = [p["type"] if p["type"] != "text" else p["text"] for p in user_parts(sent(up))]
    assert kinds == [
        "[earlier image `p0.png`]",
        "[earlier video `v0.mp4`]",
        "look",
        "image_url",
        "image_url",
        "video_url",
    ]


def test_a_role_without_vision_is_told_it_cannot_see_the_picture(tmp_path: Path) -> None:
    client, up, cfg = setup(tmp_path)
    cfg.roles["chat"].vision = False
    uid = put(client, "a.png", PNG).json()["id"]
    ask(client, ref(uid, "a.png"))
    assert user_parts(sent(up))[1]["text"].endswith("this model can't see pictures.")


def test_attachments_are_for_the_chat_role_only(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    uid = put(client, "a.png", PNG).json()["id"]
    for model in ("chief", "base"):
        r = ask(client, ref(uid), model=model)
        assert r.status_code == 400 and "chat role" in r.json()["detail"]
    assert up.requests == []


def test_a_message_may_carry_up_to_max_per_message_attachments(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path, max_per_message=2)
    uid = put(client, "a.png", PNG).json()["id"]
    r = ask(client, ref(uid), ref(uid), ref(uid))
    assert r.status_code == 400 and "up to 2" in r.json()["detail"] and up.requests == []


def test_a_chat_without_attachments_is_unchanged(tmp_path: Path) -> None:
    client, up, _ = setup(tmp_path)
    client.post(
        "/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
    )
    body = sent(up)
    assert body["messages"][1] == {"role": "user", "content": "hi"} and "video_fps" not in body


def test_a_fence_outlasts_the_longest_run_of_backticks_inside() -> None:
    assert fence("a") == "```\na\n```"
    assert fence("x `````` y").startswith("```````\n")
