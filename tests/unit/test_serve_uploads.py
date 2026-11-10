"""The attachment sweep that deletes uploads older than keep_days (design 2026-10-22-chat-attachments). Kept
in the unit layer as unit-after-fix: it ages files by the real time.time() (quarantined clock), to be given a
clock it can control. The rest of the uploads behaviour runs in tests/functional/test_serve_uploads.py."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from airlie.serve.app import create_app
from tests.support.serve import FakeUpstream
from tests.support.serve_chat import chat_cfg
from tests.unit.serve_helpers import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def setup(tmp_path: Path, **attachments: Any) -> tuple[TestClient, FakeUpstream, Any]:
    cfg = chat_cfg(tmp_path)
    cfg.roles["chat"].vision = True
    for k, v in attachments.items():
        setattr(cfg.chat.attachments, k, v)
    up = FakeUpstream()
    return TestClient(create_app(cfg, transport=httpx.MockTransport(up))), up, cfg


def put(client: TestClient, name: str, data: bytes, **kw: Any) -> httpx.Response:
    return client.post("/v1/airlie/uploads", params={"name": name}, content=data, **kw)


def test_uploads_older_than_keep_days_are_deleted(tmp_path: Path) -> None:
    client, _, cfg = setup(tmp_path, keep_days=1)
    old, new = put(client, "old.png", PNG).json()["id"], put(client, "new.png", PNG).json()["id"]
    root = cfg.observability.state_dir.expanduser() / "uploads"
    past = time.time() - 2 * 86400
    for p in root.glob(f"{old}*"):
        os.utime(p, (past, past))
    (root / f"{old}.px1536.jpg").write_bytes(b"made today, from an old upload")  # goes with its upload
    (root / f"{'a' * 32}.part").write_bytes(b"a crash's leftover")
    os.utime(root / f"{'a' * 32}.part", (past, past))
    TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream())))  # a start sweeps
    assert sorted(p.name for p in root.iterdir()) == sorted([new, f"{new}.json"])
    assert client.get(f"/v1/airlie/uploads/{old}").status_code == 404
