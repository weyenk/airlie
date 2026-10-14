"""Live studio checks: Muse reviews a diff as JSON; the chief plans as JSON; a vision role sees an image."""

import struct
import zlib
from pathlib import Path

import pytest

from airlie.cli import _http_engine
from airlie.config import load_config
from airlie.engine import ContentPart, GenRequest, Message, image_part
from airlie.skills import prompt_text
from airlie.topology.review import Reviewer
from airlie.topology.studio_plan import Plan, parse_chief

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
DIFF = (
    "--- a/app/orders.py\n+++ b/app/orders.py\n@@ -1,3 +1,4 @@\n def unit_price(total, qty):\n"
    "-    return total / qty\n+    if qty < 0:\n+        raise ValueError\n+    return total / qty\n"
)
FILE = "def unit_price(total, qty):\n    if qty < 0:\n        raise ValueError\n    return total / qty\n"


async def test_reviewer_returns_the_json_contract_on_a_real_diff() -> None:
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("reviewer"))
    r = await Reviewer(eng, "reviewer", cfg.roles["reviewer"]).review(
        "Fix the ZeroDivisionError when qty is 0 in unit_price", DIFF, "1 passed", {"app/orders.py": FILE}
    )
    assert r.verdict in ("approve", "request_changes"), r.raw
    assert r.verdict == "request_changes", r.raw
    assert any("0" in f.claim or "zero" in f.claim.lower() for f in r.findings), r.raw
    await eng.stop()


async def test_chief_answers_with_a_plan_object() -> None:
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("chief"))
    role = cfg.roles["chief"]
    user = (
        "## Request\nAdd input validation to the signup form and tests for it.\n\n## Specialists\n"
        "- coder: Implement or change code\n- qa: Test strategy\n- ux: User flows and microcopy\n\n"
        'Reply with one JSON object: a plan ({"type": "plan", ...}) or {"type": "answer", ...}.'
    )
    req = GenRequest(
        role="chief",
        messages=[
            Message(role="system", content=prompt_text(role.prompt)),
            Message(role="user", content=user),
        ],
        sampling=role.sampling,
        max_tokens=role.max_tokens,
        thinking=role.thinking,
    )
    res = await eng.generate(req)
    p = parse_chief(res.text)
    assert isinstance(p, Plan) and p.subtasks, res.text
    await eng.stop()


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """A solid-colour PNG; no imaging library needed."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


async def test_vision_role_sees_the_image(tmp_path: Path) -> None:
    p = tmp_path / "red.png"
    p.write_bytes(_png(64, 64, (220, 20, 20)))
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    eng = _http_engine(cfg, cfg.engine_for_role("design"))
    role = cfg.roles["design"]
    text = "What single colour fills this image? One word."
    msg = Message(role="user", content=text, parts=(ContentPart("text", text=text), image_part(p)))
    req = GenRequest(
        role="design",
        messages=[Message(role="system", content=prompt_text(role.prompt)), msg],
        sampling=role.sampling,
        max_tokens=20,
        thinking=False,
    )
    res = await eng.generate(req)
    assert "red" in res.text.lower(), res.text
    await eng.stop()
