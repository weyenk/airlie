"""The two tools only a chat has: `skill` (a vendored superpowers skill's text, or a file beside it) and
`todo` (the chat's own checklist, replaced whole on each call as Claude Code's TodoWrite does)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from airlie.chat.superpowers import SuperpowersLibrary
from airlie.skills.library import Skill
from airlie.tools.protocol import ToolContext, ToolResult

TodoStatus = Literal["pending", "in_progress", "done"]
TODO_STATUSES: tuple[str, ...] = get_args(TodoStatus)
MAX_TODOS = 50
MAX_TODO_TEXT = 500
_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


@dataclass(frozen=True)
class TodoItem:
    id: str
    text: str
    status: TodoStatus


class TodoList:
    def __init__(self) -> None:
        self._items: list[TodoItem] = []

    def items(self) -> list[TodoItem]:
        return list(self._items)

    def replace(self, items: list[TodoItem]) -> None:
        self._items = list(items)

    @classmethod
    def load(cls, items: list[dict[str, Any]]) -> TodoList:
        """A stored list; a row that fails the todo tool's own checks is skipped, never an error."""
        out = cls()
        parsed = [parse_item(r, n) for n, r in enumerate(items, 1)]
        out.replace([p for p in parsed if isinstance(p, TodoItem)])
        return out


def parse_item(r: object, n: int) -> TodoItem | str:
    """One todo row as a TodoItem, or the error naming what is wrong with item `n`."""
    if not isinstance(r, dict):
        return f"error: item {n} must be an object with id, text and status"
    text = str(r.get("text") or "").strip()
    status = r.get("status")
    if not text:
        return f"error: item {n} has empty text"
    if len(text) > MAX_TODO_TEXT:
        return f"error: item {n} text is over {MAX_TODO_TEXT} characters"
    if status not in TODO_STATUSES:
        return f"error: item {n} has status {status!r}; use one of {', '.join(TODO_STATUSES)}"
    return TodoItem(str(r.get("id") or n), text, cast(TodoStatus, status))


def render(items: list[TodoItem]) -> str:
    if not items:
        return "the todo list is empty"
    return "\n".join(f"{_MARK[i.status]} {i.id}: {i.text}" for i in items)


class SkillTool:
    name = "skill"
    description = (
        "Read a skill: its SKILL.md text, or with `file` a file beside it (a path inside the skill's "
        "directory). Name a skill from the list in your instructions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "the skill's name"},
            "file": {"type": "string", "description": "a file inside the skill's directory (optional)"},
        },
        "required": ["name"],
    }

    def __init__(self, library: SuperpowersLibrary, skills_extra: Mapping[str, Skill] | None = None) -> None:
        self._library = library
        self._extra = dict(skills_extra or {})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "")
        file = args.get("file")
        library_names = {d.name for d in self._library.skills()}
        if name in self._extra and name not in library_names:
            if file:
                return ToolResult(False, f"error: the {name} skill has no files beside it")
            return ToolResult(True, self._extra[name].text)
        try:
            if file:
                return ToolResult(True, self._library.read_sibling(name, str(file)))
            return ToolResult(True, self._library.get(name).text)
        except KeyError:
            known = ", ".join(sorted(library_names | set(self._extra)))
            return ToolResult(False, f"error: no skill {name!r}; the skills are {known}")
        except (ValueError, FileNotFoundError) as e:
            return ToolResult(False, f"error: {e}")


class TodoTool:
    name = "todo"
    description = (
        "Keep your checklist for this conversation. Send the whole list every call (it replaces the last); "
        f"at most one item in_progress, at most {MAX_TODOS} items, each text up to {MAX_TODO_TEXT} characters."
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": list(TODO_STATUSES)},
                    },
                    "required": ["id", "text", "status"],
                },
            }
        },
        "required": ["items"],
    }

    def __init__(self, todos: TodoList) -> None:
        self._todos = todos

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args.get("items")
        if not isinstance(raw, list):
            return ToolResult(False, "error: items must be a list of {id, text, status}")
        if len(raw) > MAX_TODOS:
            return ToolResult(False, f"error: at most {MAX_TODOS} items; got {len(raw)}")
        items: list[TodoItem] = []
        for n, r in enumerate(raw, 1):
            item = parse_item(r, n)
            if isinstance(item, str):
                return ToolResult(False, item)
            items.append(item)
        busy = [i.id for i in items if i.status == "in_progress"]
        if len(busy) > 1:
            return ToolResult(False, f"error: only one item may be in_progress; got {', '.join(busy)}")
        self._todos.replace(items)
        return ToolResult(True, render(items))
