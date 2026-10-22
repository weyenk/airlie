"""Chat attachments (design 2026-10-22-chat-attachments): a file a person attaches in Chat is stored under
observability.state_dir/uploads as `<id>` beside `<id>.json`, and each request that names it gets what the
chat model can take: a picture scaled to max_image_px, a video as frames airlie samples from it (vllm-mlx
0.5.0's batched engine refuses video parts), a text file or a PDF's text in a fence, anything else as one
line saying what it is. The kind is read from the bytes, never taken from the browser. What is derived from
an upload (a scaled picture, a video's frames, a PDF's text) is kept beside it as `<id>.*`."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from airlie.config.schema import AttachmentsSpec

Kind = Literal["image", "video", "text", "pdf", "file"]
MEDIA: tuple[Kind, ...] = ("image", "video")
ID = re.compile(r"^[0-9a-f]{32}$")
HEAD = 64 * 1024
"""Bytes read before the kind is decided: enough to tell text from binary."""
DAY = 86400.0

_HEIF = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1", b"avif", b"avis"}
_AUDIO = {b"M4A ", b"M4B ", b"M4P ", b"F4A "}


class UploadTooLarge(Exception):
    """The body ran past its kind's limit; the message names the limit."""


class Unreadable(ValueError):
    """A picture or video whose bytes could not be decoded; the message says why."""


@dataclass(frozen=True)
class Upload:
    id: str
    name: str
    mime: str
    size: int
    kind: Kind
    owner: str
    at: float

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "mime": self.mime, "size": self.size, "kind": self.kind}


def sniff(head: bytes, name: str) -> tuple[Kind, str]:
    """(kind, mime) from the first bytes, with the name's extension only for a text file's or a stored
    file's mime. A `.png` name on zip bytes is a file."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIF:  # HEIC/AVIF stills: iOS sends JPEG for a picked photo; a raw .heic is stored
            return "file", "image/heic" if brand != b"avif" else "image/avif"
        if brand in _AUDIO:
            return "file", "audio/mp4"
        return "video", "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm"
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    guessed = mimetypes.guess_type(name)[0]
    if _is_text(head):
        return "text", guessed if guessed and guessed.startswith("text/") else "text/plain"
    return "file", guessed or "application/octet-stream"


def _is_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    for cut in range(4):  # a multi-byte character split at the end of the head is still text
        try:
            head[: len(head) - cut].decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    return False


def clean_name(raw: str) -> str:
    """The file's own name, without a path or control characters."""
    name = re.split(r"[\\/]", raw)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:255] or "file"


def _mb(n: float) -> str:
    return f"{n:g} MB"


def human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class UploadStore:
    def __init__(self, root: Path, spec: AttachmentsSpec, now: Callable[[], float] = time.time) -> None:
        self.root, self.spec, self.now = root, spec, now
        self.root.mkdir(parents=True, exist_ok=True)
        self._swept = 0.0
        self.sweep()

    def limit(self, kind: Kind) -> float:
        s = self.spec
        return {"image": s.max_image_mb, "video": s.max_video_mb}.get(kind, s.max_file_mb)

    async def save(self, raw_name: str, chunks: AsyncIterator[bytes], owner: str) -> Upload:
        """Stream `chunks` to disk; past the kind's limit nothing is kept and UploadTooLarge is raised."""
        if self.now() - self._swept > DAY:
            self.sweep()
        name = clean_name(raw_name)
        uid = secrets.token_hex(16)
        part = self.root / f"{uid}.part"
        head, size, kind, mime, cap = b"", 0, None, "", 0
        try:
            with part.open("wb") as out:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if kind is None:
                        head += chunk
                        if len(head) < HEAD:
                            continue
                        kind, mime, cap = self._decide(head, name)
                        out.write(head)
                    else:
                        out.write(chunk)
                    if size > cap:
                        raise UploadTooLarge(self._too_large(kind, size))
                if kind is None:  # the whole file fit in the head
                    kind, mime, cap = self._decide(head, name)
                    out.write(head)
                    if size > cap:
                        raise UploadTooLarge(self._too_large(kind, size))
            if size == 0:
                raise ValueError("the file is empty")
            part.rename(self.root / uid)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        upload = Upload(uid, name, mime, size, kind, owner, self.now())
        (self.root / f"{uid}.json").write_text(json.dumps(asdict(upload)))
        return upload

    def _decide(self, head: bytes, name: str) -> tuple[Kind, str, int]:
        kind, mime = sniff(head, name)
        return kind, mime, int(self.limit(kind) * 1024 * 1024)

    def _too_large(self, kind: Kind, size: int) -> str:
        what = {"image": "a picture", "video": "a video"}.get(kind, "a file")
        return f"too large: {what} may be up to {_mb(self.limit(kind))} (this one is over {human(size)})"

    def get(self, uid: str) -> Upload | None:
        if not ID.match(uid):
            return None
        try:
            meta = json.loads((self.root / f"{uid}.json").read_text())
        except (OSError, ValueError):
            return None
        if not (self.root / uid).is_file():
            return None
        return Upload(**meta)

    def path(self, upload: Upload) -> Path:
        return self.root / upload.id

    def sweep(self) -> int:
        """Delete uploads older than keep_days with everything derived from them, and parts a crash left
        behind. An upload's age is its .json's. Returns how many uploads went."""
        self._swept = self.now()
        cutoff = self._swept - self.spec.keep_days * DAY

        def old(p: Path) -> bool:
            try:
                return p.stat().st_mtime < cutoff
            except OSError:
                return False

        files = list(self.root.iterdir())
        expired = {p.stem for p in files if p.suffix == ".json" and old(p)}
        gone = 0
        for p in files:
            uid = p.name[:32]
            if uid in expired or (not (self.root / f"{uid}.json").exists() and old(p)):
                p.unlink(missing_ok=True)
                gone += p.name == uid
        return gone

    # ---- what the model gets ----
    def picture(self, upload: Upload) -> str:
        """A picture as a data URL, at most max_image_px on its long edge and turned upright (EXIF). One
        that already fits is sent as it is; the scaled copy is kept as `<id>.px<N>.<ext>`."""
        px = self.spec.max_image_px
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
            cached = self.root / f"{upload.id}.px{px}.{ext}"
            if cached.is_file():
                return _data_url(mime, cached.read_bytes())
        from PIL import Image, ImageOps, UnidentifiedImageError

        try:
            with Image.open(self.path(upload)) as img:
                upright = img.getexif().get(0x0112, 1) in (0, 1)
                if max(img.size) <= px and upright and upload.mime in ("image/png", "image/jpeg"):
                    return _data_url(upload.mime, self.path(upload).read_bytes())
                img.seek(0)  # a GIF's first frame
                out = ImageOps.exif_transpose(img)
                out.thumbnail((px, px), Image.Resampling.LANCZOS)
                alpha = out.mode in ("RGBA", "LA") or (out.mode == "P" and "transparency" in out.info)
                ext, mime, fmt = ("png", "image/png", "PNG") if alpha else ("jpg", "image/jpeg", "JPEG")
                out = out.convert("RGBA" if alpha else "RGB")
                buf = io.BytesIO()
                out.save(buf, fmt, **({} if alpha else {"quality": 85}))
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
            raise Unreadable(f"it could not be read as a picture ({e})") from e
        (self.root / f"{upload.id}.px{px}.{ext}").write_bytes(buf.getvalue())
        return _data_url(mime, buf.getvalue())

    def frames(self, upload: Upload) -> tuple[float, list[tuple[float, str]]]:
        """(duration in seconds, [(t, JPEG data URL)]): video_fps frames a second, at most video_max_frames
        spread evenly over the clip, each at most video_frame_px on its long edge. Kept as
        `<id>.frames-<fps>-<max>-<px>.json`, so a video in the history is decoded once."""
        s = self.spec
        cache = self.root / f"{upload.id}.frames-{s.video_fps:g}-{s.video_max_frames}-{s.video_frame_px}.json"
        if cache.is_file():
            got = json.loads(cache.read_text())
            return float(got["duration"]), [(float(t), str(u)) for t, u in got["frames"]]
        import cv2

        cap = cv2.VideoCapture(str(self.path(upload)))
        try:
            if not cap.isOpened():
                raise Unreadable("it could not be read as a video")
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            picked: list[tuple[float, Any]] = []
            if fps > 0 and count > 0:
                duration = count / fps
                n = max(1, min(s.video_max_frames, int(duration * s.video_fps)))
                for i in range(n):
                    t = (i + 0.5) * duration / n
                    cap.set(cv2.CAP_PROP_POS_FRAMES, min(count - 1, int(t * fps)))
                    ok, frame = cap.read()
                    if ok:
                        picked.append((t, frame))
            else:  # no frame count (some webm): read through, keeping one frame in every step
                native = fps if fps > 0 else 30.0
                step, i = max(1, round(native / s.video_fps)), 0
                while len(picked) < s.video_max_frames:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if i % step == 0:
                        picked.append((i / native, frame))
                    i += 1
                duration = i / native
        finally:
            cap.release()
        if not picked:
            raise Unreadable("it could not be read as a video (no frame decoded)")
        out = [(t, _data_url("image/jpeg", _jpeg(cv2, frame, s.video_frame_px))) for t, frame in picked]
        cache.write_text(json.dumps({"duration": duration, "frames": out}))
        return duration, out

    def text_of(self, upload: Upload) -> tuple[str, bool]:
        """(text, cut): a text file's or a PDF's text, at most max_text_kb of it."""
        limit = self.spec.max_text_kb * 1024
        if upload.kind == "pdf":
            text = self._pdf_text(upload)
        else:
            with self.path(upload).open("rb") as f:
                text = f.read(limit + 4).decode("utf-8", errors="replace")
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text, False
        return raw[:limit].decode("utf-8", errors="ignore"), True

    def _pdf_text(self, upload: Upload) -> str:
        cache = self.root / f"{upload.id}.txt"  # read once; a PDF stays in the history for many turns
        if cache.is_file():
            return cache.read_text()
        from pypdf import PdfReader

        limit = self.spec.max_text_kb * 1024
        pages: list[str] = []
        total = 0
        for page in PdfReader(self.path(upload)).pages:
            t = (page.extract_text() or "").strip()
            pages.append(t)
            total += len(t.encode("utf-8"))
            if total > limit:
                break
        text = "\n\n".join(p for p in pages if p)
        cache.write_text(text)
        return text


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _jpeg(cv2: Any, frame: Any, px: int) -> bytes:
    h, w = frame.shape[:2]
    scale = px / max(h, w)
    if scale < 1:
        frame = cv2.resize(
            frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA
        )
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise Unreadable("a frame could not be encoded")
    return bytes(buf.tobytes())


def _seconds(t: float) -> str:
    return f"{t:.1f} s" if t < 60 else f"{int(t // 60)} min {t % 60:.0f} s"


def fence(text: str) -> str:
    """A fence longer than any run of backticks inside, so a file's own fences cannot close it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def file_line(upload: Upload, why: str) -> str:
    return f"Attached file `{upload.name}` ({upload.mime}, {human(upload.size)}): {why}"


def as_parts(store: UploadStore, upload: Upload, *, media: bool) -> list[dict[str, Any]]:
    """The content parts the model gets for one attachment. `media`: send a picture or video as itself
    (False past the history caps, or for a role without vision)."""
    if upload.kind in MEDIA and media:
        try:
            if upload.kind == "image":
                return [{"type": "image_url", "image_url": {"url": store.picture(upload)}}]
            return video_parts(upload, *store.frames(upload))
        except Exception as e:  # a broken picture or clip: say so, the conversation goes on
            why = str(e) if isinstance(e, Unreadable) else f"it could not be read ({e})"
            return [{"type": "text", "text": file_line(upload, f"{why}.")}]
    if upload.kind in MEDIA:
        return [{"type": "text", "text": f"[earlier {upload.kind} `{upload.name}`]"}]
    if upload.kind in ("text", "pdf"):
        try:
            text, cut = store.text_of(upload)
        except Exception as e:  # a broken or encrypted PDF: say so, the conversation goes on
            return [{"type": "text", "text": file_line(upload, f"its text could not be read ({e}).")}]
        if not text.strip():
            why = "it has no text layer (a scan?), so its contents can't be read from chat."
            return [{"type": "text", "text": file_line(upload, why)}]
        note = f" (cut at {store.spec.max_text_kb} KB of {human(upload.size)})" if cut else ""
        head = f"Attached file `{upload.name}` ({human(upload.size)}){note}:"
        return [{"type": "text", "text": f"{head}\n{fence(text)}"}]
    return [{"type": "text", "text": file_line(upload, "its contents can't be read from chat.")}]


def video_parts(upload: Upload, duration: float, frames: list[tuple[float, str]]) -> list[dict[str, Any]]:
    """A note on what the frames are, then each frame after its time: the model sees a video as pictures."""
    n = len(frames)
    every = f", one every {_seconds(duration / n)}" if n > 1 else ""
    note = f"Video `{upload.name}` ({_seconds(duration)}): {n} frame{'s' if n != 1 else ''}{every}, in order."
    parts: list[dict[str, Any]] = [{"type": "text", "text": note}]
    for t, url in frames:
        parts += [
            {"type": "text", "text": f"t={_seconds(t)}"},
            {"type": "image_url", "image_url": {"url": url}},
        ]
    return parts


def has_uploads(messages: list[Any]) -> bool:
    return any(
        isinstance(m, dict)
        and isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "airlie_upload" for p in m["content"])
        for m in messages
    )


def resolve(
    messages: list[dict[str, Any]],
    store: UploadStore,
    *,
    can_see: Callable[[Upload], bool],
    vision: bool,
) -> list[dict[str, Any]]:
    """Messages with every `airlie_upload` part replaced. The newest
    history_images pictures and history_videos videos go as pixels; older ones become a note. An id that
    is gone, or not this person's, becomes a placeholder: the conversation goes on."""
    spec = store.spec
    budget = {"image": spec.history_images if vision else 0, "video": spec.history_videos if vision else 0}
    found: dict[int, Upload | None] = {}
    media: set[int] = set()
    refs = [
        (i, j, p)
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), list)
        for j, p in enumerate(m["content"])
        if isinstance(p, dict) and p.get("type") == "airlie_upload"
    ]
    for n, (_, _, p) in reversed(list(enumerate(refs))):
        u = store.get(str(p.get("upload", "")))
        u = u if u is not None and can_see(u) else None
        found[n] = u
        if u is not None and u.kind in MEDIA and budget[u.kind] > 0:
            budget[u.kind] -= 1
            media.add(n)
    out = [dict(m) for m in messages]
    replaced: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for n, (i, j, p) in enumerate(refs):
        u = found[n]
        if u is None:
            name = clean_name(str(p.get("name") or "file"))
            replaced[(i, j)] = [{"type": "text", "text": f"[attachment `{name}` is no longer available]"}]
        elif u.kind in MEDIA and not vision:
            why = f"this model can't see {'pictures' if u.kind == 'image' else 'video'}."
            replaced[(i, j)] = [{"type": "text", "text": file_line(u, why)}]
        else:
            replaced[(i, j)] = as_parts(store, u, media=n in media)
    for i in {i for i, _ in replaced}:
        parts: list[dict[str, Any]] = []
        for j, p in enumerate(messages[i]["content"]):
            parts.extend(replaced.get((i, j), [p]))
        out[i]["content"] = parts
    return out
