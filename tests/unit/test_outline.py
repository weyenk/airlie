"""The code outline: defs and classes with line numbers, and the head fallback."""

from __future__ import annotations

from airlie.outline import Entry, outline, outline_entries

PY = (
    "import x\n\nclass A:\n    def m(self, k: int) -> str:\n        return ''\n\n"
    "async def f() -> None:\n    pass\n"
)


def test_outline_lists_classes_methods_and_functions_with_lines() -> None:
    assert outline("src/a.py", PY, head_chars=1000).splitlines() == [
        "src/a.py: 8 lines",
        "3: class A",
        "4:     def m(self, k: int) -> str",
        "7: async def f() -> None",
    ]


def test_entries_carry_line_depth_and_text() -> None:
    assert outline_entries(PY) == [
        Entry(3, 0, "class A"),
        Entry(4, 1, "def m(self, k: int) -> str"),
        Entry(7, 0, "async def f() -> None"),
    ]


def test_entries_are_none_when_the_source_does_not_parse() -> None:
    assert outline_entries("def oops(:\n") is None


def test_a_file_that_does_not_parse_falls_back_to_its_head() -> None:
    header = "src/b.py: 5001 lines"
    text = outline("src/b.py", "def oops(:\n" + "x\n" * 5000, head_chars=100)
    assert text.startswith(header)
    assert len(text) <= 100 + len(header) + 1


def test_non_python_gets_line_count_and_head() -> None:
    source = "".join(f"key{i}: value{i}\n" for i in range(100))
    text = outline("ci.yml", source, head_chars=50)
    lines = text.splitlines()
    assert lines[0] == "ci.yml: 100 lines"
    assert lines[1] == "key0: value0"
    assert len("\n".join(lines[1:])) <= 50
