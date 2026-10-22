"""Chat attachments (design 2026-10-22-chat-attachments): a file a person attaches in Chat is stored under
observability.state_dir/uploads as `<id>` beside `<id>.json`, and each request that names it gets what the
chat model can take: a picture or a video as a data URL, a text file or a PDF's text in a fence, anything
else as one line saying what it is. The kind is read from the bytes, never taken from the browser."""

from __future__ import annotations

import base64
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
        """Delete uploads older than keep_days, and parts a crash left behind. Returns how many went."""
        self._swept = self.now()
        cutoff = self._swept - self.spec.keep_days * DAY
        gone = 0
        for p in self.root.iterdir():
            try:
                if p.stat().st_mtime >= cutoff:
                    continue
                p.unlink()
                gone += p.suffix == ""  # the upload itself; its .json and .txt go with it
            except OSError:
                continue
        return gone

    # ---- what the model gets ----
    def data_url(self, upload: Upload) -> str:
        data = base64.b64encode(self.path(upload).read_bytes()).decode("ascii")
        return f"data:{upload.mime};base64,{data}"

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
    if upload.kind == "image" and media:
        return [{"type": "image_url", "image_url": {"url": store.data_url(upload)}}]
    if upload.kind == "video" and media:
        return [{"type": "video_url", "video_url": {"url": store.data_url(upload)}}]
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
) -> tuple[list[dict[str, Any]], bool]:
    """Messages with every `airlie_upload` part replaced, and whether a video goes as video. The newest
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
    video = any((u := found[n]) is not None and u.kind == "video" for n in media)
    return out, video
