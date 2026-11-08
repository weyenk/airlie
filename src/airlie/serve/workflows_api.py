"""The workflows the builder edits, under /v1/workflows (task serve's key middleware guards it like the rest
of /v1): the repo's workflows (config/workflows/*.yaml, read when the config loaded) and the board's drafts,
listed and read; drafts saved, deleted, validated and published as a PR; and the catalog of what a node may
name (docs/superpowers/specs/2026-11-07-workflow-builder-design.md: "What it looks like", "Drafts",
"Validation")."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, get_args

import yaml
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from airlie.board.model import WorkflowDraft
from airlie.board.store import BoardStore, BoardStoreError
from airlie.config import AirlieConfig
from airlie.flow.spec import Format, NodeType, WorkflowSpec
from airlie.prompts.hub import load as hub_prompts
from airlie.story.flow import STORY_CATALOG  # the catalog, with the story's checks declared on it
from airlie.story.publish import PublishError
from airlie.story.workflow_publish import NothingToPublish
from airlie.story.workflows import (
    DraftPrompts,
    WorkflowError,
    bundle_users,
    draft_body,
    draft_spec,
    edits_file,
    used_by,
    workflow_problems,
)

KINDS = ("text", "number", "bool", "list of …", "one of …")
"""The field kinds an output's field list may use (`airlie.flow.outputs.parse_kind`), as the builder offers
them; any kind may end in `?` for an optional field."""

RESERVED = frozenset({"catalog", "validate", "drafts"})
"""Names a draft may not take: the router's own paths under /v1/workflows, which `GET /{name}` would never
reach."""


def _not_reserved(name: str) -> None:
    if name in RESERVED:
        raise HTTPException(422, f"{name!r} is reserved")


Publisher = Callable[[WorkflowDraft], Awaitable[tuple[int, str]]]
"""Opens the PR that puts a draft in the repo: its number and URL."""


class SaveDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str | None
    yaml: str
    prompts: dict[str, str] = {}


class Validate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yaml: str
    prompts: dict[str, str] | None = None  # None: the prompt files of the saved draft of that name


def _layout(spec: WorkflowSpec) -> dict[str, dict[str, float]]:
    return {node: {"x": at.x, "y": at.y} for node, at in spec.layout.items()}


def _pr(draft: WorkflowDraft) -> dict[str, Any] | None:
    if draft.published_pr is None:
        return None
    return {"number": draft.published_pr, "url": draft.published_url}


def workflows_router(
    cfg: AirlieConfig,
    store: BoardStore,
    publish: Callable[[str, str | None], None],
    publisher: Publisher,
) -> APIRouter:
    """`publish` is told `("workflows", name)` after each save, delete and publish that succeeded; task serve
    passes its feed's. `publisher` opens a published draft's PR."""
    router = APIRouter(prefix="/v1/workflows", tags=["workflows"])

    def texts() -> dict[str, str]:
        return {d.name: d.yaml for d in store.drafts()}

    def prompt_files() -> dict[str, Mapping[str, str]]:
        return {d.name: d.prompts for d in store.drafts()}

    def problems(
        name: str, drafts: Mapping[str, str] | None = None, prompts: DraftPrompts | None = None
    ) -> list[str]:
        """What stops draft `name` resolving over the drafts (their YAML, and the prompt files they carry), as
        saved unless given. A draft that edits a file is checked as its own text, though the file runs."""
        return workflow_problems(
            cfg,
            texts() if drafts is None else drafts,
            name,
            prompts=prompt_files() if prompts is None else prompts,
            own=True,
        )

    def file_text(name: str, spec: WorkflowSpec) -> str:
        if cfg.source_path is not None:
            path = Path(cfg.source_path).parent / "config" / "workflows" / f"{name}.yaml"
            if path.is_file():
                return path.read_text()
        dumped = {"workflows": {name: spec.model_dump(mode="json", exclude_none=True)}}
        return yaml.safe_dump(dumped, sort_keys=False)

    def draft_detail(draft: WorkflowDraft) -> dict[str, Any]:
        try:
            layout = _layout(draft_spec(draft.name, draft.yaml))
        except WorkflowError:  # a draft that does not parse shows no positions; its problems say why
            layout = {}
        return {
            "name": draft.name,
            "type": draft.type,
            "source": "draft",
            "yaml": draft.yaml,
            "layout": layout,
            "problems": problems(draft.name),
            "prompts": dict(draft.prompts),
            "pr": _pr(draft),
            "edits_file": edits_file(cfg, draft.name),
        }

    def get_draft(name: str) -> WorkflowDraft:
        try:
            return store.draft(name)
        except KeyError:
            raise HTTPException(404, f"no draft workflow {name!r}") from None

    def in_use(name: str) -> None:
        """Refuse (409, with the users) taking away draft `name` while another workflow's bundle node runs
        it. A draft that edits a file goes freely: the file still serves its users."""
        if not edits_file(cfg, name) and (users := used_by(cfg, texts(), name)):
            raise HTTPException(409, {"used_by": users})

    @router.get("")
    async def list_workflows() -> dict[str, Any]:
        rows = store.drafts()
        drafts = {d.name: d.yaml for d in rows}
        prompts = {d.name: d.prompts for d in rows}
        users = bundle_users(cfg, drafts)
        files: list[dict[str, Any]] = [
            {
                "name": name,
                "type": spec.type,
                "source": "file",
                "bundle": spec.type is None,
                "problems": [],
                "pr": None,
                "used_by": users.get(name, []),
            }
            for name, spec in cfg.workflows.items()
        ]
        listed = [
            {
                "name": d.name,
                "type": d.type,
                "source": "draft",
                "bundle": d.type is None,
                "problems": problems(d.name, drafts, prompts),
                "pr": _pr(d),
                # an edit of a file waiting for its PR (or a merge the serve has not loaded): never run
                "superseded": edits_file(cfg, d.name),
                "used_by": users.get(d.name, []),
            }
            for d in rows
        ]
        return {"workflows": files + listed}

    @router.get("/catalog")
    async def catalog() -> dict[str, Any]:
        return {
            "roles": sorted(cfg.roles),
            "types": list(get_args(NodeType)),
            "formats": list(get_args(Format)),
            "kinds": list(KINDS),
            "checks": STORY_CATALOG.describe(),
            "prompt_files": sorted(p.id for p in hub_prompts().values() if p.kind == "rule"),
        }

    @router.post("/validate")
    async def validate(body: Validate) -> dict[str, Any]:
        try:
            name, _ = draft_body(body.yaml)
        except WorkflowError as e:
            return {"problems": [str(e)]}
        _not_reserved(name)
        prompts = prompt_files()
        if body.prompts is not None:
            prompts[name] = body.prompts
        return {"problems": problems(name, {**texts(), name: body.yaml}, prompts)}

    @router.get("/{name}")
    async def read_workflow(name: str) -> dict[str, Any]:
        """A draft first: one named as a file is that file's edit in progress, which the builder opens."""
        with contextlib.suppress(KeyError):
            return draft_detail(store.draft(name))
        if (spec := cfg.workflows.get(name)) is not None:
            return {
                "name": name,
                "type": spec.type,
                "source": "file",
                "yaml": file_text(name, spec),
                "layout": _layout(spec),
                "problems": [],
                "prompts": {},
                "pr": None,
                "edits_file": False,
            }
        return draft_detail(get_draft(name))

    @router.put("/drafts/{name}")
    async def save_draft(name: str, body: SaveDraft) -> dict[str, Any]:
        _not_reserved(name)  # a file's name is allowed: the draft is an edit of that file (`edits_file`)
        with contextlib.suppress(WorkflowError):  # a save naming another workflow renames this one
            if draft_body(body.yaml)[0] != name:
                in_use(name)
        try:
            draft_body(body.yaml, name)
            saved = store.save_draft(name, type=body.type, yaml=body.yaml, prompts=body.prompts)
        except (WorkflowError, BoardStoreError) as e:
            raise HTTPException(422, str(e)) from None
        publish("workflows", name)
        return draft_detail(saved)

    @router.delete("/drafts/{name}", status_code=204)
    async def delete_draft(name: str) -> Response:
        get_draft(name)  # 404 before anything else
        in_use(name)
        try:
            store.delete_draft(name)
        except KeyError:
            raise HTTPException(404, f"no draft workflow {name!r}") from None
        publish("workflows", name)
        return Response(status_code=204)

    @router.post("/{name}/publish")
    async def publish_draft(name: str) -> dict[str, Any]:
        draft = get_draft(name)
        if found := problems(name):
            raise HTTPException(422, {"problems": found})
        # published already: a new push to the same branch, and the PR gh finds on it (the same one while
        # it is open) is the one recorded
        try:
            number, url = await publisher(draft)
        except NothingToPublish as e:  # the trunk holds the draft as it is
            raise HTTPException(409, str(e)) from None
        except PublishError as e:  # a git or gh step that failed, or gh not installed (CannotStart)
            raise HTTPException(502, str(e)) from None
        try:
            store.mark_published(name, number, url)
        except KeyError:
            raise HTTPException(404, "draft vanished while publishing") from None
        publish("workflows", name)
        return {"number": number, "url": url}

    return router
