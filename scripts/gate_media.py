"""Phase 4b gate, Step 4: one picture and one short video described by the chat model.

    uv run python scripts/gate_media.py --url http://127.0.0.1:8080/v1 --model generalist --scratch D
    uv run python scripts/gate_media.py --engine main --scratch D

The picture (a red circle left of a blue square on white, the text AIRLIE 42) and the video (3 s: a green ball
moving left to right on black) are drawn here, stored through the web app's own UploadStore, and turned into
content parts by its own `resolve` (the picture scaled, the video sampled into frames), exactly as the Chat
screen's attachments are; the parts then go straight to the engine, since `task serve` needs a signed-in user.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(add_completion=False)


def draw(scratch: Path) -> tuple[Path, Path]:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    pic = scratch / "picture.png"
    img = Image.new("RGB", (800, 500), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((80, 150, 280, 350), fill="red")
    d.rectangle((480, 150, 680, 350), fill="blue")
    d.text((300, 420), "AIRLIE 42", fill="black", font=ImageFont.load_default(size=48))
    img.save(pic)

    vid = scratch / "ball.mp4"
    w, h, fps, secs = 640, 360, 10, 3
    out = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for i in range(fps * secs):
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        x = int(40 + (w - 80) * i / (fps * secs - 1))
        cv2.circle(frame, (x, h // 2), 30, (0, 200, 0), -1)  # BGR green
        out.write(frame)
    out.release()
    return pic, vid


async def one(path: Path) -> AsyncIterator[bytes]:
    yield path.read_bytes()


@app.command()
def main(
    scratch: Annotated[Path, typer.Option()],
    engine: Annotated[str | None, typer.Option()] = None,
    url: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
    out: Annotated[Path | None, typer.Option()] = None,
) -> None:
    from airlie.config import load_config
    from airlie.serve.uploads import UploadStore, resolve

    cfg = load_config(ROOT / "airlie.yaml")
    key = None
    extra: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}
    if engine is not None:
        e = cfg.engines[engine]
        url, model, key = e.base_url, e.served_name(e.models[0]), e.api_key
        extra = {"enable_thinking": False}
    assert url and model
    scratch.mkdir(parents=True, exist_ok=True)
    pic, vid = draw(scratch)
    store = UploadStore(scratch / "uploads", cfg.chat.attachments)

    async def run() -> dict[str, Any]:
        up_pic = await store.save(pic.name, one(pic), "gate")
        up_vid = await store.save(vid.name, one(vid), "gate")
        asks = {
            "picture": (
                up_pic,
                "Describe this picture: the shapes, their colours, where they are, and any text.",
            ),
            "video": (up_vid, "Describe what happens in this video: what moves, its colour, and which way."),
        }
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        res: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=600, headers=headers) as h:
            for name, (u, q) in asks.items():
                msgs = resolve(
                    [
                        {
                            "role": "user",
                            "content": [
                                {"type": "airlie_upload", "upload": u.id},
                                {"type": "text", "text": q},
                            ],
                        }
                    ],
                    store,
                    can_see=lambda _u: True,
                    vision=True,
                )
                images = sum(p.get("type") == "image_url" for p in msgs[0]["content"])
                body = {"model": model, "messages": msgs, "max_tokens": 300, "temperature": 0, **extra}
                r = await h.post(f"{url}/chat/completions", json=body)
                if r.status_code != 200:
                    res[name] = {"images": images, "error": f"{r.status_code}: {r.text[:300]}"}
                    continue
                j = r.json()
                res[name] = {
                    "images": images,
                    "text": j["choices"][0]["message"].get("content"),
                    "usage": j.get("usage"),
                    "timings": {
                        k: v
                        for k, v in (j.get("timings") or {}).items()
                        if k in ("prompt_ms", "predicted_ms")
                    },
                }
        return res

    res = asyncio.run(run())
    typer.echo(json.dumps(res, indent=1))
    if out is not None:
        out.write_text(json.dumps({"url": url, "model": model, **res}, indent=1) + "\n")


if __name__ == "__main__":
    app()
