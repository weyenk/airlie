"""Shared helpers extracted from tests/unit/test_story_board.py for tests.support.story_board."""

from __future__ import annotations

from typing import Any

import httpx

from airlie.config import JiraSpec
from airlie.story.board import note
from airlie.story.jira import JiraBoard, adf_doc

SITE = "https://acme.atlassian.net"

EMAIL, TOKEN = "me@example.org", "t0ken"


def doc(*paragraphs: str) -> dict[str, Any]:
    """Plain paragraphs as ADF, the way Jira returns a description or a comment."""
    content = [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]
    return {"type": "doc", "version": 1, "content": content}


ISSUE = {
    "id": "10007",
    "key": "AIR-7",
    "fields": {
        "summary": "Avatar upload",
        "description": doc("Users want avatars"),
        "status": {"name": "To Do"},
        # the repository: a checkout named "repo" (test_story_checkout)
        "labels": ["repo:o/repo", "frontend"],
        "comment": {  # oldest first, as Jira returns them
            "comments": [
                {"body": adf_doc(note("picked this up"))},
                {"body": doc("PNG only")},
                {"body": doc("Max 2 MB")},
            ]
        },
    },
}

TRANSITIONS = [
    {"id": "11", "name": "Back to do", "to": {"name": "To Do"}},
    {"id": "21", "name": "Start", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Ready for review", "to": {"name": "In Review"}},
    {"id": "41", "name": "Block", "to": {"name": "Blocked"}},
]


class FakeJira:
    """Jira Cloud's REST API v3 as JiraBoard uses it; a non-200 `status` answers every request with it."""

    def __init__(self, status: int = 200, current: str = "To Do", issues: list[Any] | None = None) -> None:
        self.status, self.current = status, current
        self.issues = [ISSUE] if issues is None else issues
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errorMessages": ["Client must be authenticated"]})
        method, path = request.method, request.url.path.split("/rest/api/3", 1)[1]
        if method == "GET" and path == "/issue/AIR-7":
            return httpx.Response(
                200, json={**ISSUE, "fields": {**ISSUE["fields"], "status": {"name": self.current}}}
            )
        if method == "GET" and path == "/search/jql":
            return httpx.Response(200, json={"issues": self.issues, "isLast": True})
        if method == "GET" and path == "/issue/AIR-7/transitions":
            return httpx.Response(200, json={"transitions": TRANSITIONS})
        if method == "POST" and path == "/issue/AIR-7/transitions":
            return httpx.Response(204)
        if method == "POST" and path == "/issue/AIR-7/comment":
            return httpx.Response(201, json={"id": "100"})
        if path == "/myself":
            return httpx.Response(200, json={"accountId": "a1"})
        if path == "/project/AIR":
            return httpx.Response(200, json={"key": "AIR"})
        if path == "/project/AIR/statuses":
            names = ["To Do", "In Progress", "In Review", "Blocked", "Done"]
            return httpx.Response(200, json=[{"name": "Task", "statuses": [{"name": n} for n in names]}])
        return httpx.Response(404, json={"errorMessages": [f"no route {method} {path}"]})


def board(fake: FakeJira, **statuses: str | None) -> JiraBoard:
    spec = JiraSpec.model_validate({"site": SITE, "project": "AIR", "statuses": statuses})
    return JiraBoard(spec, email=EMAIL, token=TOKEN, transport=httpx.MockTransport(fake))
