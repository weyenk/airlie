"""Shared helpers extracted from tests/unit/test_jira_import.py for tests.support.jira_import."""

from __future__ import annotations

from typing import Any

from airlie.config import JiraSpec
from tests.support.story_board import SITE, doc

T1, T2 = "2026-10-01T09:00:00.000+0000", "2026-10-02T10:30:00.000+0000"


def issue(
    key: str,
    kind: str,
    summary: str,
    *,
    status: str = "To Do",
    parent: str | None = None,
    labels: tuple[str, ...] = (),
    description: str | None = None,
    comments: tuple[tuple[str, str, str], ...] = (),
) -> dict[str, Any]:
    """An issue as search/jql returns it with the import's fields."""
    fields: dict[str, Any] = {
        "summary": summary,
        "issuetype": {"name": kind, "subtask": kind == "Sub-task"},
        "status": {"name": status},
        "labels": list(labels),
        "description": doc(description) if description else None,
        "created": T1,
        "comment": {
            "comments": [
                {"author": {"displayName": author}, "body": doc(text), "created": when}
                for author, text, when in comments
            ]
        },
    }
    if parent is not None:
        fields["parent"] = {"key": parent, "fields": {"summary": "parent"}}
    return {"id": key.split("-")[1], "key": key, "fields": fields}


HDS = [  # scrambled on purpose: the import orders epics before stories before subtasks itself
    issue("HDS-16", "Sub-task", "Write the tests", parent="HDS-7", status="In Progress"),
    issue(
        "HDS-7",
        "Story",
        "Board screen",
        status="In Review",
        parent="HDS-14",
        labels=("repo:weyenk/airlie", "web"),
        description="Columns by status",
        comments=(("Noah", "Keep it small", T1), ("airlie", "picked this up", T2)),
    ),
    issue("HDS-14", "Epic", "Web app", status="To Do"),
    issue("HDS-5", "Task", "Fix the parser", status="Done"),
    issue("HDS-9", "Bug", "Crash on save", status="Waiting", parent="HDS-14"),
]


class FakeClient:
    def __init__(self, issues: list[dict[str, Any]], **statuses: str | None) -> None:
        self.issues = issues
        self.spec = JiraSpec.model_validate({"site": SITE, "project": "HDS", "statuses": statuses})
        self.project = "HDS"
        self.calls = 0

    async def project_issues(self) -> list[dict[str, Any]]:
        self.calls += 1
        return self.issues
