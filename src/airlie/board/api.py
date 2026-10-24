"""The board over HTTP, under /v1/board (task serve's key middleware guards it like the rest of /v1): list and
read items, create, change (fields, status, parent), move (status and place in a column), comment, and the
export. v1 has one person: every comment and item is noah's; a later per-person auth sets the author."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from airlie.board.model import Level, Status
from airlie.board.store import BoardStore, BoardStoreError

PERSON = "noah"


class CreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Level
    title: str
    description: str = ""
    parent: str | None = None
    labels: Sequence[str] = ()


class ChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    description: str | None = None
    status: Status | None = None
    parent: str | None = None
    labels: Sequence[str] | None = None


class MoveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
    before: str | None = None  # the card to land above; none puts it at the bottom of the column


class AddComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class EditComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def board_router(store: BoardStore) -> APIRouter:
    router = APIRouter(prefix="/v1/board", tags=["board"])

    def item(key: str) -> dict[str, Any]:
        try:
            return store.get(key).to_dict()
        except KeyError:
            raise HTTPException(404, f"no item {key} on the board") from None

    @router.get("/items")
    async def list_items(
        level: Level | None = None,
        status: Status | None = None,
        parent: str | None = None,
    ) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in store.list(level=level, status=status, parent=parent)]}

    @router.post("/items", status_code=201)
    async def create_item(body: CreateItem) -> dict[str, Any]:
        try:
            made = store.create(
                level=body.level,
                title=body.title,
                description=body.description,
                parent=body.parent,
                labels=body.labels,
                created_by=PERSON,
            )
        except BoardStoreError as e:
            raise HTTPException(422, str(e)) from None
        return made.to_dict()

    @router.get("/items/{key}")
    async def get_item(key: str) -> dict[str, Any]:
        return item(key)

    @router.patch("/items/{key}")
    async def change_item(key: str, body: ChangeItem) -> dict[str, Any]:
        item(key)  # 404 before anything else
        try:
            changed = store.update(
                key,
                title=body.title,
                description=body.description,
                status=body.status,
                parent=body.parent,
                labels=body.labels,
            )
        except BoardStoreError as e:
            raise HTTPException(422, str(e)) from None
        return changed.to_dict()

    @router.post("/items/{key}/move")
    async def move_item(key: str, body: MoveItem) -> dict[str, Any]:
        item(key)
        try:
            return store.move(key, body.status, body.before).to_dict()
        except BoardStoreError as e:
            raise HTTPException(422, str(e)) from None

    @router.post("/items/{key}/comments")
    async def add_comment(key: str, body: AddComment) -> dict[str, Any]:
        item(key)
        return store.comment(key, PERSON, body.text).to_dict()

    @router.patch("/items/{key}/comments/{comment_id}")
    async def edit_comment(key: str, comment_id: int, body: EditComment, request: Request) -> dict[str, Any]:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "not signed in")
        item(key)  # 404 before anything else
        comments = [c for c in item(key)["comments"] if c["id"] == comment_id]
        if not comments:
            raise HTTPException(404, f"no comment {comment_id} on {key}")
        comment = comments[0]
        if comment["author"] not in (principal.name, principal.user_id):
            raise HTTPException(403, "you can only edit your own comments")
        return store.edit_comment(key, comment_id, body.text).to_dict()

    @router.get("/export")
    async def export() -> dict[str, Any]:
        return store.export()

    return router
