"""`airlie board import-jira`: a Jira project onto the local board, keys kept, re-runnable, dry-runnable. The
Jira client is a fake that answers with recorded JSON shapes: no network."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.board.jira_import import ImportReport, import_jira
from airlie.config import JiraSpec, JiraStatusesSpec
from tests.unit.test_story_board import SITE, doc

T1, T2 = "2026-10-01T09:00:00.000+0000", "2026-10-02T10:30:00.000+0000"


def at(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


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


def store(tmp_path: Path, prefix: str = "HDS") -> BoardStore:
    return BoardStore(tmp_path / "board.sqlite", prefix=prefix)


def test_the_jira_statuses_know_done() -> None:
    assert JiraStatusesSpec().done == "Done"


async def test_import_keeps_jira_numbers_levels_parents_and_statuses(tmp_path: Path) -> None:
    s = store(tmp_path)
    report = await import_jira(s, FakeClient(HDS), "HDS")
    assert isinstance(report, ImportReport)
    assert (report.created, report.updated, report.skipped) == (5, 0, 0)
    by_key = {i.key: i for i in s.list()}
    assert sorted(by_key) == ["HDS-14", "HDS-16", "HDS-5", "HDS-7", "HDS-9"]
    assert by_key["HDS-14"].level == "epic" and by_key["HDS-14"].parent is None
    assert by_key["HDS-7"].level == "story" and by_key["HDS-7"].parent == "HDS-14"
    assert by_key["HDS-16"].level == "subtask" and by_key["HDS-16"].parent == "HDS-7"
    assert by_key["HDS-5"].level == "story" and by_key["HDS-5"].parent is None  # a top-level task
    assert by_key["HDS-9"].level == "story" and by_key["HDS-9"].parent == "HDS-14"
    assert {k: i.status for k, i in by_key.items()} == {
        "HDS-14": "todo",
        "HDS-7": "review",
        "HDS-16": "doing",
        "HDS-5": "done",
        "HDS-9": "todo",  # 'Waiting' is no status airlie knows
    }
    assert any(n.startswith("HDS-9: status 'Waiting'") and "todo" in n for n in report.notes)
    assert s.create(level="initiative", title="next").key == "HDS-17"  # the counter sits past the import


async def test_import_carries_title_description_labels_and_comments(tmp_path: Path) -> None:
    s = store(tmp_path)
    await import_jira(s, FakeClient(HDS), "HDS")
    item = s.get("HDS-7")
    assert item.title == "Board screen" and item.description == "Columns by status"
    assert item.labels == ("repo:weyenk/airlie", "web", "jira:HDS-7")
    assert [(c.author, c.text, c.at) for c in item.comments] == [
        ("Noah", "Keep it small", at(T1)),
        ("airlie", "picked this up", at(T2)),
    ]
    assert s.get("HDS-14").labels == ("jira:HDS-14",)
    assert s.get("HDS-5").comments == () and s.get("HDS-5").description == ""


async def test_import_allocates_keys_when_the_prefix_differs_and_labels_the_origin(tmp_path: Path) -> None:
    s = store(tmp_path, prefix="AL")
    report = await import_jira(s, FakeClient(HDS), "HDS")
    assert report.created == 5
    items = s.list()
    assert [i.key for i in items] == ["AL-1", "AL-2", "AL-3", "AL-4", "AL-5"]
    assert [i.level for i in items] == ["epic", "story", "story", "story", "subtask"]
    of = {next(x for x in i.labels if x.startswith("jira:")): i for i in items}
    assert of["jira:HDS-14"].key == "AL-1"
    assert of["jira:HDS-7"].parent == "AL-1" and of["jira:HDS-16"].parent == of["jira:HDS-7"].key
    assert "HDS-14: created as AL-1" in report.notes


async def test_rerun_updates_changed_items_adds_only_new_comments_and_skips_the_rest(
    tmp_path: Path,
) -> None:
    s = store(tmp_path)
    await import_jira(s, FakeClient(HDS), "HDS")
    changed = [
        issue("HDS-16", "Sub-task", "Write the tests", parent="HDS-7", status="In Progress"),
        issue(
            "HDS-7",
            "Story",
            "Board screen v2",
            status="Done",
            parent="HDS-14",
            labels=("repo:weyenk/airlie", "web", "shipped"),
            description="Columns by status",
            comments=(
                ("Noah", "Keep it small", T1),
                ("airlie", "picked this up", T2),
                ("Noah", "Merged", "2026-10-03T08:00:00.000+0000"),
            ),
        ),
        issue("HDS-14", "Epic", "Web app", status="To Do"),
        issue("HDS-5", "Task", "Fix the parser", status="Done"),
        issue("HDS-9", "Bug", "Crash on save", status="Waiting", parent="HDS-14"),
    ]
    report = await import_jira(s, FakeClient(changed), "HDS")
    assert (report.created, report.updated, report.skipped) == (0, 1, 4)
    item = s.get("HDS-7")
    assert (item.title, item.status) == ("Board screen v2", "done")
    assert item.labels == ("repo:weyenk/airlie", "web", "shipped", "jira:HDS-7")
    assert [c.text for c in item.comments] == ["Keep it small", "picked this up", "Merged"]
    assert [i.key for i in s.list()] == ["HDS-5", "HDS-7", "HDS-9", "HDS-14", "HDS-16"]  # nothing twice
    again = await import_jira(s, FakeClient(changed), "HDS")
    assert (again.created, again.updated, again.skipped) == (0, 0, 5)
    assert [c.text for c in s.get("HDS-7").comments] == ["Keep it small", "picked this up", "Merged"]


async def test_rerun_moves_an_item_under_a_new_parent(tmp_path: Path) -> None:
    s = store(tmp_path)
    await import_jira(s, FakeClient(HDS), "HDS")
    moved = [*HDS[:3], issue("HDS-5", "Task", "Fix the parser", status="Done", parent="HDS-14"), HDS[4]]
    report = await import_jira(s, FakeClient(moved), "HDS")
    assert report.updated == 1 and s.get("HDS-5").parent == "HDS-14"


async def test_a_parent_outside_the_project_or_at_the_wrong_level_leaves_the_item_parentless(
    tmp_path: Path,
) -> None:
    s = store(tmp_path)
    issues = [
        issue("HDS-2", "Story", "under a story", parent="HDS-1"),
        issue("HDS-1", "Task", "a task"),
        issue("HDS-3", "Epic", "under something elsewhere", parent="OTHER-9"),
    ]
    report = await import_jira(s, FakeClient(issues), "HDS")
    assert report.created == 3
    assert s.get("HDS-2").parent is None and s.get("HDS-3").parent is None
    assert any(n.startswith("HDS-2: parent HDS-1 is a story, not an epic") for n in report.notes)
    assert any(n.startswith("HDS-3: parent OTHER-9 is not in project HDS") for n in report.notes)


async def test_the_status_names_come_from_story_jira_statuses(tmp_path: Path) -> None:
    s = store(tmp_path)
    issues = [
        issue("HDS-1", "Story", "a", status="Code Review"),
        issue("HDS-2", "Story", "b", status="Blocked"),
    ]
    report = await import_jira(s, FakeClient(issues, review="Code Review", blocked=None), "HDS")
    assert s.get("HDS-1").status == "review"
    assert s.get("HDS-2").status == "todo" and any(
        n.startswith("HDS-2: status 'Blocked'") for n in report.notes
    )


async def test_an_unknown_issue_type_is_a_story_with_a_note(tmp_path: Path) -> None:
    s = store(tmp_path)
    report = await import_jira(s, FakeClient([issue("HDS-1", "Spike", "look")]), "HDS")
    assert s.get("HDS-1").level == "story"
    assert any(n.startswith("HDS-1: issue type 'Spike'") and "story" in n for n in report.notes)


async def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    report = await import_jira(s, FakeClient(HDS), "HDS", dry_run=True)
    assert (report.created, report.updated, report.skipped) == (5, 0, 0) and report.dry_run
    assert s.list() == []
    assert "HDS-14: would create HDS-14" in report.notes
    await import_jira(s, FakeClient(HDS), "HDS")
    changed = [*HDS[1:], issue("HDS-16", "Sub-task", "Write the tests again", parent="HDS-7")]
    report = await import_jira(s, FakeClient(changed), "HDS", dry_run=True)
    assert (report.created, report.updated, report.skipped) == (0, 1, 4)
    assert s.get("HDS-16").title == "Write the tests" and s.get("HDS-16").status == "doing"
    assert any(n.startswith("HDS-16: would update HDS-16 (title, status)") for n in report.notes)


def test_the_report_summarises_itself() -> None:
    r = ImportReport(created=2, updated=1, skipped=3, notes=["HDS-1: created as HDS-1"])
    assert r.summary() == "2 created, 1 updated, 3 unchanged"
    assert (
        ImportReport(dry_run=True, created=1).summary()
        == "dry run: 1 would be created, 0 updated, 0 unchanged"
    )
