"""The chat's skill and todo tools."""

from pathlib import Path
from typing import Any

import pytest

from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.tools import MAX_TODOS, SkillTool, TodoItem, TodoList, TodoTool
from airlie.skills.library import Skill
from airlie.tools.protocol import ToolContext

CTX = ToolContext(worktree=Path("."))


@pytest.fixture
def library(tmp_path: Path) -> SuperpowersLibrary:
    for name, body in (("alpha", "Alpha body.\n"), ("zeta", "Zeta body.\n")):
        d = tmp_path / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use when {name}.\n---\n\n{body}")
    (tmp_path / "skills" / "alpha" / "prompt.md").write_text("sibling text\n")
    (tmp_path / "secret.txt").write_text("outside\n")
    return SuperpowersLibrary(tmp_path)


async def test_skill_returns_the_skill_md(library: SuperpowersLibrary) -> None:
    result = await SkillTool(library).run({"name": "alpha"}, CTX)

    assert result.ok
    assert result.output.strip() == "Alpha body."


async def test_skill_returns_a_sibling_file_when_asked(library: SuperpowersLibrary) -> None:
    result = await SkillTool(library).run({"name": "alpha", "file": "prompt.md"}, CTX)

    assert (result.ok, result.output) == (True, "sibling text\n")


async def test_skill_unknown_name_is_an_error_result_naming_the_known_skills(
    library: SuperpowersLibrary,
) -> None:
    result = await SkillTool(library).run({"name": "nope"}, CTX)

    assert not result.ok
    assert result.output == "error: no skill 'nope'; the skills are alpha, zeta"


async def test_skill_refuses_paths_outside_the_skill_directory(library: SuperpowersLibrary) -> None:
    tool = SkillTool(library)

    up = await tool.run({"name": "alpha", "file": "../../secret.txt"}, CTX)
    absolute = await tool.run({"name": "alpha", "file": "/etc/passwd"}, CTX)
    missing = await tool.run({"name": "alpha", "file": "none.md"}, CTX)

    assert [r.ok for r in (up, absolute, missing)] == [False, False, False]
    assert all(r.output.startswith("error: ") and "outside" not in r.output for r in (up, absolute, missing))
    assert "leaves the alpha skill directory" in up.output


async def test_skill_serves_an_extra_skill_by_name_and_lists_it_when_unknown(
    library: SuperpowersLibrary, tmp_path: Path
) -> None:
    story = Skill("story", "Tell a story", "Story text.", tmp_path / "story.md")
    tool = SkillTool(library, {"story": story})

    served = await tool.run({"name": "story"}, CTX)
    with_file = await tool.run({"name": "story", "file": "x.md"}, CTX)
    unknown = await tool.run({"name": "nope"}, CTX)

    assert (served.ok, served.output) == (True, "Story text.")
    assert (with_file.ok, with_file.output) == (False, "error: the story skill has no files beside it")
    assert unknown.output == "error: no skill 'nope'; the skills are alpha, story, zeta"


async def test_a_library_skill_wins_over_an_extra_of_the_same_name(library: SuperpowersLibrary) -> None:
    clash = Skill("alpha", "x", "Extra text.", Path("x.md"))

    result = await SkillTool(library, {"alpha": clash}).run({"name": "alpha"}, CTX)

    assert result.output.strip() == "Alpha body."


def _items(*rows: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"id": i, "text": t, "status": s} for i, t, s in rows]


async def test_todo_replaces_the_list_and_renders_a_checklist() -> None:
    todos = TodoList()
    todos.replace([TodoItem("old", "old item", "pending")])

    result = await TodoTool(todos).run(
        {"items": _items(("1", "plan", "done"), ("2", "build", "in_progress"), ("3", "ship", "pending"))},
        CTX,
    )

    assert result.ok
    assert result.output == "[x] 1: plan\n[~] 2: build\n[ ] 3: ship"
    assert todos.items() == [
        TodoItem("1", "plan", "done"),
        TodoItem("2", "build", "in_progress"),
        TodoItem("3", "ship", "pending"),
    ]


async def test_todo_an_empty_list_clears_it() -> None:
    todos = TodoList()
    todos.replace([TodoItem("1", "x", "pending")])

    result = await TodoTool(todos).run({"items": []}, CTX)

    assert (result.ok, result.output) == (True, "the todo list is empty")
    assert todos.items() == []


async def test_todo_refuses_two_in_progress_items() -> None:
    todos = TodoList()

    result = await TodoTool(todos).run(
        {"items": _items(("a", "x", "in_progress"), ("b", "y", "in_progress"))}, CTX
    )

    assert (result.ok, result.output) == (False, "error: only one item may be in_progress; got a, b")
    assert todos.items() == []


async def test_todo_refuses_empty_text() -> None:
    result = await TodoTool(TodoList()).run({"items": _items(("a", "  ", "pending"))}, CTX)

    assert (result.ok, result.output) == (False, "error: item 1 has empty text")


async def test_todo_refuses_a_bad_status() -> None:
    result = await TodoTool(TodoList()).run({"items": _items(("a", "x", "started"))}, CTX)

    assert not result.ok
    assert result.output == "error: item 1 has status 'started'; use one of pending, in_progress, done"


async def test_todo_refuses_more_than_fifty_items_and_takes_exactly_fifty() -> None:
    many = _items(*[(str(n), "x", "pending") for n in range(MAX_TODOS + 1)])
    todos = TodoList()

    refused = await TodoTool(todos).run({"items": many}, CTX)
    taken = await TodoTool(todos).run({"items": many[:MAX_TODOS]}, CTX)

    assert (refused.ok, refused.output) == (False, "error: at most 50 items; got 51")
    assert taken.ok and len(todos.items()) == 50


async def test_todo_refuses_items_that_are_not_a_list_of_objects() -> None:
    tool = TodoTool(TodoList())

    not_list = await tool.run({"items": "do it"}, CTX)
    not_obj = await tool.run({"items": ["do it"]}, CTX)

    assert not_list.output == "error: items must be a list of {id, text, status}"
    assert not_obj.output == "error: item 1 must be an object with id, text and status"


async def test_todo_numbers_an_item_sent_without_an_id() -> None:
    todos = TodoList()

    await TodoTool(todos).run({"items": [{"text": "x", "status": "pending"}]}, CTX)

    assert todos.items() == [TodoItem("1", "x", "pending")]


def test_a_todo_list_round_trips_through_load() -> None:
    todos = TodoList()
    todos.replace([TodoItem("1", "a", "done"), TodoItem("2", "b", "in_progress")])
    stored = [{"id": i.id, "text": i.text, "status": i.status} for i in todos.items()]

    assert TodoList.load(stored).items() == todos.items()


def test_load_skips_stored_rows_that_fail_the_tools_checks() -> None:
    stored: list[Any] = [
        {"id": "1", "text": "keep", "status": "done"},
        {"text": "no id gets its number", "status": "pending"},
        {"id": "3", "status": "pending"},
        {"id": "4", "text": "  ", "status": "pending"},
        {"id": "5", "text": "bad status", "status": "blocked"},
        {"id": "6", "text": "no status"},
        "not an object",
    ]

    assert TodoList.load(stored).items() == [
        TodoItem("1", "keep", "done"),
        TodoItem("2", "no id gets its number", "pending"),
    ]


def test_the_items_of_a_todo_list_are_a_copy() -> None:
    todos = TodoList()
    todos.replace([TodoItem("1", "a", "done")])

    todos.items().clear()

    assert len(todos.items()) == 1
