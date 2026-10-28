"""LHR authoring tools: HTTP client to lhr-office /api/authoring (Supabase bearer).

Pattern matches web.py (injectable client, no stdio MCP). Photos use airlie upload ids."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from airlie.config import AirlieConfig
from airlie.serve.uploads import UploadStore
from airlie.tools.lhr_session import LhrSessionStore
from airlie.tools.protocol import Tool, ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry

SIGN_IN = (
    "You are not signed in to Love Heat Relationship yet. Run "
    "`uv run airlie lhr set-token --token YOUR_SUPABASE_ACCESS_TOKEN` "
    "(or set LHR_SUPABASE_ACCESS_TOKEN), then try again."
)


class AuthoringClient(Protocol):
    async def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]: ...


class HttpxAuthoringClient:
    """Calls {base_url}/api/authoring{path} with Authorization: Bearer from the session store."""

    def __init__(
        self,
        base_url: str,
        session: LhrSessionStore,
        *,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._timeout = timeout_s
        self._transport = transport

    async def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        token = self._session.token()
        if not token:
            return 401, {"error": "not signed in"}
        url = f"{self._base}/api/authoring{path}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(self._timeout, connect=10.0)
        ) as client:
            r = await client.request(method, url, json=json_body, headers=headers)
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body


def _cap(text: str, ctx: ToolContext) -> str:
    if len(text) <= ctx.output_cap_chars:
        return text
    return text[: ctx.output_cap_chars] + f"\n… [truncated {len(text) - ctx.output_cap_chars} chars]"


def _format_json(status: int, body: Any) -> str:
    if isinstance(body, (dict, list)):
        return f"HTTP {status}\n{json.dumps(body, indent=2)}"
    return f"HTTP {status}\n{body}"


class _LhrTool:
    def __init__(self, client: AuthoringClient) -> None:
        self._client = client

    async def _run_auth(
        self, args: dict[str, Any], ctx: ToolContext, method: str, path: str, body: dict[str, Any] | None
    ) -> ToolResult:
        status, payload = await self._client.request(method, path, json_body=body)
        if status == 401:
            return ToolResult(False, SIGN_IN)
        ok = 200 <= status < 300
        return ToolResult(ok, _cap(_format_json(status, payload), ctx))


class LhrCreateDraft(_LhrTool):
    name = "lhr_create_draft"
    description = "Start a new recipe or article draft on the LHR site. Returns draftId."
    parameters = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["recipe", "article"]},
            "title": {"type": "string", "description": "Optional initial title"},
        },
        "required": ["type"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        post_type = str(args.get("type") or "")
        title = str(args.get("title") or "")
        return await self._run_auth(args, ctx, "POST", "/drafts", {"type": post_type, "title": title})


class LhrListDrafts(_LhrTool):
    name = "lhr_list_drafts"
    description = "List your unfinished LHR post drafts."
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self._run_auth(args, ctx, "GET", "/drafts", None)


class LhrUpdateSection(_LhrTool):
    name = "lhr_update_section"
    description = (
        "Update an article draft: set title and/or append one section (heading + body). "
        "Use lhr_set_recipe for recipe drafts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string"},
            "title": {"type": "string"},
            "heading": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["draft_id"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft_id = str(args.get("draft_id") or "")
        status, existing = await self._client.request("GET", f"/drafts/{draft_id}")
        if status == 401:
            return ToolResult(False, SIGN_IN)
        if status != 200 or not isinstance(existing, dict):
            return ToolResult(False, _format_json(status, existing))
        draft = existing.get("draft")
        if not isinstance(draft, dict):
            return ToolResult(False, "error: draft payload missing")
        patch: dict[str, Any] = {}
        if args.get("title") is not None:
            patch["title"] = str(args["title"])
        heading, body = args.get("heading"), args.get("body")
        if heading is not None and body is not None:
            sections = list(draft.get("sections") or [])
            sections.append({"heading": str(heading), "body": str(body)})
            patch["sections"] = sections
        if not patch:
            return ToolResult(False, "error: provide title and/or heading+body")
        return await self._run_auth(args, ctx, "PATCH", f"/drafts/{draft_id}", patch)


class LhrSetRecipe(_LhrTool):
    name = "lhr_set_recipe"
    description = (
        "Update a recipe draft: title, narrative story, and/or append one ingredient and/or one step."
    )
    parameters = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string"},
            "title": {"type": "string"},
            "narrative_body": {"type": "string", "description": "The story around the recipe (markdown)"},
            "ingredient_item": {"type": "string"},
            "ingredient_amount": {"type": "string"},
            "step": {"type": "string"},
        },
        "required": ["draft_id"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft_id = str(args.get("draft_id") or "")
        status, existing = await self._client.request("GET", f"/drafts/{draft_id}")
        if status == 401:
            return ToolResult(False, SIGN_IN)
        if status != 200 or not isinstance(existing, dict):
            return ToolResult(False, _format_json(status, existing))
        draft = existing.get("draft")
        if not isinstance(draft, dict):
            return ToolResult(False, "error: draft payload missing")
        patch: dict[str, Any] = {}
        if args.get("title") is not None:
            patch["title"] = str(args["title"])
        if args.get("narrative_body") is not None:
            patch["narrativeBody"] = str(args["narrative_body"])
        ing_item = args.get("ingredient_item")
        if ing_item is not None:
            ingredients = list(draft.get("ingredients") or [])
            entry: dict[str, str] = {"item": str(ing_item)}
            if args.get("ingredient_amount") is not None:
                entry["amount"] = str(args["ingredient_amount"])
            ingredients.append(entry)
            patch["ingredients"] = ingredients
        if args.get("step") is not None:
            steps = list(draft.get("steps") or [])
            steps.append(str(args["step"]))
            patch["steps"] = steps
        if not patch:
            return ToolResult(
                False,
                "error: provide title, narrative_body, ingredient_item, and/or step",
            )
        return await self._run_auth(args, ctx, "PATCH", f"/drafts/{draft_id}", patch)


class LhrUploadPhoto(_LhrTool):
    name = "lhr_upload_photo"
    description = "Attach a photo already uploaded in Chat (upload_id from the attachment) to an LHR draft."
    parameters = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string"},
            "upload_id": {"type": "string"},
            "caption": {"type": "string"},
        },
        "required": ["draft_id", "upload_id"],
    }

    def __init__(
        self,
        client: AuthoringClient,
        uploads: UploadStore,
        can_see: Callable[[Any], bool],
    ) -> None:
        super().__init__(client)
        self._uploads = uploads
        self._can_see = can_see

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft_id = str(args.get("draft_id") or "")
        upload_id = str(args.get("upload_id") or "")
        found = self._uploads.get(upload_id)
        if found is None or not self._can_see(found):
            return ToolResult(False, f"error: no such upload {upload_id!r} (attach it in Chat first)")
        if found.kind != "image":
            return ToolResult(False, "error: upload must be a picture")
        path = self._uploads.path(found)
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ToolResult(False, f"error: could not read upload: {e}")
        body = {
            "contentType": found.mime or "image/jpeg",
            "imageBase64": base64.b64encode(raw).decode("ascii"),
        }
        if args.get("caption") is not None:
            body["caption"] = str(args["caption"])
        return await self._run_auth(args, ctx, "POST", f"/drafts/{draft_id}/photo", body)


def _summarize_draft(row: dict[str, Any]) -> str:
    draft = row.get("draft") if isinstance(row.get("draft"), dict) else row
    if not isinstance(draft, dict):
        return json.dumps(row, indent=2)
    post_type = draft.get("postType", "?")
    title = draft.get("title") or "(untitled)"
    lines = [f"Type: {post_type}", f"Title: {title}"]
    if post_type == "recipe":
        lines.append(f"Ingredients: {len(draft.get('ingredients') or [])}")
        lines.append(f"Steps: {len(draft.get('steps') or [])}")
    else:
        lines.append(f"Sections: {len(draft.get('sections') or [])}")
    lines.append(f"Photos: {len(draft.get('photos') or [])}")
    narrative = draft.get("narrativeBody") or ""
    if narrative:
        snippet = narrative if len(narrative) <= 80 else f"{narrative[:80]}..."
        lines.append(f"Narrative: {snippet}")
    site = row.get("previewUrl")
    if isinstance(site, str) and site:
        lines.append(f"Preview: {site}")
    return "\n".join(lines)


class LhrPreview(_LhrTool):
    name = "lhr_preview"
    description = "Summarize a draft for the author and return a preview URL when the office is configured."
    parameters = {
        "type": "object",
        "properties": {"draft_id": {"type": "string"}},
        "required": ["draft_id"],
    }

    def __init__(self, client: AuthoringClient, *, site_base_url: str | None) -> None:
        super().__init__(client)
        self._site = (site_base_url or "").rstrip("/")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft_id = str(args.get("draft_id") or "")
        status, draft_row = await self._client.request("GET", f"/drafts/{draft_id}")
        if status == 401:
            return ToolResult(False, SIGN_IN)
        if status != 200 or not isinstance(draft_row, dict):
            return ToolResult(False, _format_json(status, draft_row))
        tok_status, token_body = await self._client.request("POST", f"/drafts/{draft_id}/preview-token")
        if tok_status == 401:
            return ToolResult(False, SIGN_IN)
        preview_url = ""
        if tok_status == 200 and isinstance(token_body, dict):
            token = str(token_body.get("token") or "")
            expires = token_body.get("expiresAt")
            if self._site and token and expires is not None:
                preview_url = f"{self._site}/preview/{draft_id}?token={token}&expires={expires}"
        summary_row = {**draft_row, "previewUrl": preview_url} if preview_url else draft_row
        return ToolResult(True, _cap(_summarize_draft(summary_row), ctx))


class LhrPublish(_LhrTool):
    name = "lhr_publish"
    description = (
        "Publish a draft to the live site. Call lhr_preview first and only publish after the author "
        "explicitly confirms (e.g. says yes, publish)."
    )
    parameters = {
        "type": "object",
        "properties": {"draft_id": {"type": "string"}},
        "required": ["draft_id"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        draft_id = str(args.get("draft_id") or "")
        return await self._run_auth(args, ctx, "POST", f"/drafts/{draft_id}/publish", {})


def lhr_tool_instances(
    cfg: AirlieConfig,
    session: LhrSessionStore,
    uploads: UploadStore,
    can_see_upload: Callable[[Any], bool],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Tool]:
    client: AuthoringClient = HttpxAuthoringClient(
        cfg.lhr.office_url,
        session,
        timeout_s=cfg.lhr.timeout_s,
        transport=transport,
    )
    return [
        LhrCreateDraft(client),
        LhrListDrafts(client),
        LhrUpdateSection(client),
        LhrSetRecipe(client),
        LhrUploadPhoto(client, uploads, can_see_upload),
        LhrPreview(client, site_base_url=cfg.lhr.site_base_url),
        LhrPublish(client),
    ]


def attach_lhr(
    registry: ToolRegistry,
    cfg: AirlieConfig,
    uploads: UploadStore,
    can_see_upload: Callable[[Any], bool],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    session = LhrSessionStore(cfg.observability.state_dir)
    for tool in lhr_tool_instances(cfg, session, uploads, can_see_upload, transport=transport):
        registry.add(tool)


def lhr_registry(
    cfg: AirlieConfig,
    ctx: ToolContext,
    uploads: UploadStore,
    can_see_upload: Callable[[Any], bool],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolRegistry:
    reg = ToolRegistry([], ctx)
    attach_lhr(reg, cfg, uploads, can_see_upload, transport=transport)
    return reg
