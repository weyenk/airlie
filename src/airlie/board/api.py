"""The board over HTTP, under /v1/board (task serve's key middleware guards it like the rest of /v1): list and
read items, create, change (fields, status, parent), move (status and place in a column), comment, and the
export. v1 has one person: every comment and item is noah's; a later per-person auth sets the author."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from airlie.board.model import Level, Status
from airlie.board.store import BoardStore, BoardStoreError
from airlie.story.workflows import workflow_label

PERSON = "noah"
STARTED = "the workflow of a started card cannot be changed; make a new card for the other workflow"


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


def board_router(
    store: BoardStore,
    publish: Callable[[str, str | None], None] | None = None,
    *,
    workflow_check: Callable[[str], str | None] | None = None,
    started: Callable[[str], bool] | None = None,
) -> APIRouter:
    """`publish`, when given, is told `("board", key)` right after each write that succeeded (a refused one
    publishes nothing): task serve passes its feed's, so an open page refetches that one item at once.
    `workflow_check`, when given, says what stops a workflow resolving (None when nothing does): a card is
    not given a `workflow:` label, nor a story carrying one moved to approved, while its workflow has a
    problem (a 422 with it), so the board never holds a card the line could not run (the builder's anti-goal:
    nothing saves what the composer refuses). A card with no such label runs the story's: never refused.
    `started`, when given, says whether the card `key` has a saved state (it has started, and runs the
    workflow it snapshotted, D21): its `workflow:` label is then never added, removed or changed (a 422),
    and `GET /items/{key}` says `started`."""
    router = APIRouter(prefix="/v1/board", tags=["board"])

    def check_workflow(labels: Sequence[str]) -> None:
        name = workflow_label(labels)  # as the line reads it: the card's own (`card_workflow`)
        if workflow_check is not None and name is not None and (problem := workflow_check(name)) is not None:
            raise HTTPException(422, problem)

    def changed(key: str) -> None:
        if publish is not None:
            publish("board", key)

    def mover(request: Request) -> str:
        """Who a status change is recorded as made by: the request's principal, else the one person (v1)."""
        principal = getattr(request.state, "principal", None)
        return str(principal.name) if principal is not None else PERSON

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
        changed(made.key)
        return made.to_dict()

    @router.get("/items/{key}")
    async def get_item(key: str) -> dict[str, Any]:
        found = item(key)
        if started is not None:
            found["started"] = started(key)
        return found

    @router.patch("/items/{key}")
    async def change_item(key: str, body: ChangeItem, request: Request) -> dict[str, Any]:
        found = item(key)  # 404 before anything else
        relabelled = body.labels is not None and workflow_label(body.labels) != workflow_label(
            found["labels"]
        )
        if relabelled and started is not None and started(key):
            raise HTTPException(422, STARTED)
        if relabelled or (body.status == "approved" and found["level"] == "story"):
            check_workflow(found["labels"] if body.labels is None else body.labels)
        try:
            updated = store.update(
                key,
                title=body.title,
                description=body.description,
                status=body.status,
                parent=body.parent,
                labels=body.labels,
                by=mover(request),
            )
        except BoardStoreError as e:
            raise HTTPException(422, str(e)) from None
        changed(key)
        return updated.to_dict()

    @router.post("/items/{key}/move")
    async def move_item(key: str, body: MoveItem, request: Request) -> dict[str, Any]:
        found = item(key)
        if body.status == "approved" and found["level"] == "story":
            check_workflow(found["labels"])
        try:
            moved = store.move(key, body.status, body.before, by=mover(request))
        except BoardStoreError as e:
            raise HTTPException(422, str(e)) from None
        changed(key)
        return moved.to_dict()

    @router.post("/items/{key}/comments")
    async def add_comment(key: str, body: AddComment) -> dict[str, Any]:
        item(key)
        commented = store.comment(key, PERSON, body.text)
        changed(key)
        return commented.to_dict()

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
        edited = store.edit_comment(key, comment_id, body.text)
        changed(key)
        return edited.to_dict()

    @router.delete("/items/{key}/comments/{comment_id}")
    async def delete_comment(key: str, comment_id: int, request: Request) -> dict[str, Any]:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise HTTPException(401, "not signed in")
        item(key)  # 404 before anything else
        comments = [c for c in item(key)["comments"] if c["id"] == comment_id]
        if not comments:
            raise HTTPException(404, f"no comment {comment_id} on {key}")
        comment = comments[0]
        if comment["author"] not in (principal.name, principal.user_id):
            raise HTTPException(403, "you can only delete your own comments")
        deleted = store.delete_comment(key, comment_id)
        changed(key)
        return deleted.to_dict()

    @router.get("/export")
    async def export() -> dict[str, Any]:
        return store.export()

    return router
