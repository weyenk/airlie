"""`airlie board import-jira`: a Jira project onto the local board. Epics become epics, stories, tasks,
features and bugs stories, sub-tasks subtasks, each under its Jira parent; the issue keeps its number under
board.prefix (HDS-5 → AIR-5; a number another item holds is reported and skipped), and every item carries a
`jira:<key>` label, which a re-run matches to update titles, descriptions, statuses, labels and parents and to
add only the comments it has not got. A dry run reports and writes nothing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from airlie.board.model import LEVELS, Item, Level, Status, parent_level
from airlie.board.store import BoardStore
from airlie.config import JiraSpec, JiraStatusesSpec
from airlie.story.jira import adf_text

ORIGIN = "jira:"
"""The label an imported item carries: jira:<its Jira key>."""
_LEVEL_OF: dict[str, Level] = {
    "epic": "epic",
    "story": "story",
    "task": "story",
    "feature": "story",
    "bug": "story",
    "sub-task": "subtask",
    "subtask": "subtask",
}


class JiraClient(Protocol):
    """What the import needs of airlie.story.jira.JiraBoard."""

    spec: JiraSpec

    async def project_issues(self) -> list[dict[str, Any]]: ...


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    """One line per item created or updated, and per thing worth knowing: "<jira key>: <what>"."""
    dry_run: bool = False

    def summary(self) -> str:
        if self.dry_run:
            return (
                f"dry run: {self.created} would be created, {self.updated} updated, {self.skipped} unchanged"
            )
        return f"{self.created} created, {self.updated} updated, {self.skipped} unchanged"


@dataclass(frozen=True)
class _Issue:
    key: str
    number: int
    level: Level
    title: str
    description: str
    status: str
    labels: tuple[str, ...]
    parent: str | None
    comments: tuple[tuple[str, str, float], ...]
    """(author, text, time), oldest first."""
    notes: tuple[str, ...]


def _when(raw: Any, fallback: float) -> float:
    """A Jira timestamp (2026-10-17T10:00:00.000+0000) as seconds since the epoch; `fallback` when it is
    not one."""
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    return fallback


def _parse(issue: dict[str, Any], now: float) -> _Issue:
    key = str(issue.get("key") or "")
    f = issue.get("fields") or {}
    type_name = str((f.get("issuetype") or {}).get("name") or "")
    level = _LEVEL_OF.get(type_name.casefold())
    notes: list[str] = []
    if level is None:
        level = "story"
        notes.append(f"{key}: issue type {type_name!r} is not one airlie knows; imported as a story")
    labels = f.get("labels")
    parent = (f.get("parent") or {}).get("key")
    created = _when(f.get("created"), now)
    comments: list[tuple[str, str, float]] = []
    for c in (f.get("comment") or {}).get("comments") or []:
        text = adf_text(c.get("body"))
        if not text:
            continue
        author = str((c.get("author") or {}).get("displayName") or "unknown")
        comments.append((author, text, _when(c.get("created"), created)))
    return _Issue(
        key=key,
        number=int(key.rsplit("-", 1)[1]) if "-" in key and key.rsplit("-", 1)[1].isdigit() else 0,
        level=level,
        title=str(f.get("summary") or key),
        description=adf_text(f.get("description")),
        status=str((f.get("status") or {}).get("name") or ""),
        labels=tuple(x for x in labels if isinstance(x, str)) if isinstance(labels, list) else (),
        parent=str(parent) if parent else None,
        comments=tuple(comments),
        notes=tuple(notes),
    )


def status_map(statuses: JiraStatusesSpec) -> dict[str, Status]:
    """Jira status name (case-folded) → board status, from story.jira.statuses."""
    pairs: list[tuple[str | None, Status]] = [
        (statuses.intake, "todo"),
        (statuses.building, "doing"),
        (statuses.review, "review"),
        (statuses.blocked, "blocked"),
        (statuses.done, "done"),
    ]
    return {name.casefold(): status for name, status in pairs if name is not None}


def _origin(item: Item) -> str | None:
    return next((x[len(ORIGIN) :] for x in item.labels if x.startswith(ORIGIN)), None)


def _an(level: str) -> str:
    return f"an {level}" if level[0] in "aeiou" else f"a {level}"


async def import_jira(
    store: BoardStore, client: JiraClient, project_key: str, *, dry_run: bool = False
) -> ImportReport:
    """Every issue of the project onto `store`: see the module. Items are made epics first, then stories,
    then subtasks, so a parent is on the board before its children. `project_key` is the Jira project, for the
    report."""
    report = ImportReport(dry_run=dry_run)
    now = datetime.now().timestamp()
    issues = sorted(
        (_parse(i, now) for i in await client.project_issues()),
        key=lambda i: (LEVELS.index(i.level), i.number),
    )
    statuses = status_map(client.spec.statuses)
    existing: dict[str, Item] = {}
    on_board: dict[str, Item] = {}
    for item in store.list():
        on_board[item.key] = item
        origin = _origin(item)
        if origin is not None:
            existing.setdefault(origin, item)
    board_key: dict[str, str] = {i.key: existing[i.key].key for i in issues if i.key in existing}
    level_of: dict[str, Level] = {i.key: i.level for i in issues}
    for issue in issues:
        report.notes.extend(issue.notes)
        parent = _parent(issue, project_key, board_key, level_of, report)
        status = statuses.get(issue.status.casefold())
        if status is None:
            report.notes.append(
                f"{issue.key}: status {issue.status!r} is not one of story.jira.statuses; set to todo"
            )
            status = "todo"
        labels = (*(x for x in issue.labels if x != f"{ORIGIN}{issue.key}"), f"{ORIGIN}{issue.key}")
        if issue.key in existing:
            _update(store, existing[issue.key], issue, parent, status, labels, report)
        else:
            _create(store, issue, parent, status, labels, on_board, board_key, report)
    return report


def _parent(
    issue: _Issue,
    project_key: str,
    board_key: dict[str, str],
    level_of: dict[str, Level],
    report: ImportReport,
) -> str | None:
    """The board key of the issue's Jira parent: one in the project, one level up and on the board; else None
    and a note."""
    if issue.parent is None:
        return None
    want = parent_level(issue.level)
    if issue.parent not in level_of:
        report.notes.append(f"{issue.key}: parent {issue.parent} is not in project {project_key}; no parent")
        return None
    if want is None or level_of[issue.parent] != want:
        have = _an(level_of[issue.parent])
        report.notes.append(
            f"{issue.key}: parent {issue.parent} is {have}, not {_an(want or 'nothing')}; no parent"
        )
        return None
    if issue.parent not in board_key:  # skipped: its number was taken
        report.notes.append(f"{issue.key}: parent {issue.parent} was not imported; no parent")
        return None
    return board_key[issue.parent]


def _create(
    store: BoardStore,
    issue: _Issue,
    parent: str | None,
    status: Status,
    labels: tuple[str, ...],
    on_board: dict[str, Item],
    board_key: dict[str, str],
    report: ImportReport,
) -> None:
    key = f"{store.prefix}-{issue.number}"
    taken = on_board.get(key)
    if taken is not None:  # another item holds the number: not this issue's (existing would have matched)
        origin = _origin(taken)
        whose = f"is from {origin}" if origin else "is not from Jira"
        report.skipped += 1
        report.notes.append(f"{issue.key}: skipped; {key} is already on the board and {whose}")
        return
    report.created += 1
    if report.dry_run:
        board_key[issue.key] = key
        report.notes.append(f"{issue.key}: would create {key}")
        return
    item = store.create(
        level=issue.level,
        title=issue.title,
        description=issue.description,
        parent=parent,
        labels=labels,
        created_by="jira",
        number=issue.number,
    )
    if status != "todo":
        store.update(item.key, status=status)
    for author, text, when in issue.comments:
        store.comment(item.key, author, text, at=when)
    board_key[issue.key] = item.key
    report.notes.append(f"{issue.key}: created as {item.key}")


def _update(
    store: BoardStore,
    item: Item,
    issue: _Issue,
    parent: str | None,
    status: Status,
    labels: tuple[str, ...],
    report: ImportReport,
) -> None:
    changes: dict[str, Any] = {}
    if item.title != issue.title:
        changes["title"] = issue.title
    if item.description != issue.description:
        changes["description"] = issue.description
    if item.status != status:
        changes["status"] = status
    if item.labels != labels:
        changes["labels"] = labels
    if parent is not None and item.parent != parent:
        changes["parent"] = parent
    have = {(c.author, c.text, round(c.at, 3)) for c in item.comments}
    new = [(a, t, w) for a, t, w in issue.comments if (a, t, round(w, 3)) not in have]
    if not changes and not new:
        report.skipped += 1
        return
    report.updated += 1
    what = [*changes, *([f"{len(new)} new comment{'s' if len(new) != 1 else ''}"] if new else [])]
    if report.dry_run:
        report.notes.append(f"{issue.key}: would update {item.key} ({', '.join(what)})")
        return
    if changes:
        store.update(item.key, **changes)
    for author, text, when in new:
        store.comment(item.key, author, text, at=when)
    report.notes.append(f"{issue.key}: updated {item.key} ({', '.join(what)})")
