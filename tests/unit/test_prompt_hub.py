"""The prompt hub loader (spec 2026-10-22-prompt-hub-design, "File format" and "Loader").

`parse` is tested on raw strings; only the last tests read files, through `_load_from` on a tmp_path and
`load()` on the installed package. A broken prompt file must fail here, in the gate, not in a run."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

import airlie.prompts as prompts
from airlie.prompts import hub
from airlie.prompts.hub import PromptError, PromptText, _load_from, entry, load, parse, render, text


def _raw(body: str = "hello\n", **front: Any) -> str:
    """A prompt file: defaults for a valid rule, each keyword overriding a key, `None` dropping it."""
    meta: dict[str, Any] = {"kind": "rule", "roles": ["coder"], "why": "because"}
    meta.update(front)
    meta = {k: v for k, v in meta.items() if v is not None}
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{body}"


def _steering(body: str = "hello\n", **front: Any) -> str:
    return _raw(body, **{"kind": "steering", "when": "the model stalls", **front})


def test_parse_reads_front_matter_and_body() -> None:
    raw = (
        "---\n"
        "kind: steering\n"
        "roles: [coder, tester]\n"
        "fields: [n, total]\n"
        "when: two-fifths of the steps used and nothing written\n"
        "governs:\n"
        "  reading: stop\n"
        "  tests.location: new-file\n"
        "yields_to: [brief-names-test]\n"
        "why: |\n"
        "  AIR-31, 2026-10-19: the coder read for 12 steps.\n"
        "  Second line.\n"
        "---\n"
        "Step {n} of {total}.\n"
        "Write now.\n"
    )
    p = parse("write-now.md", "steering", raw)

    assert p == PromptText(
        id="write-now",
        kind="steering",
        roles=("coder", "tester"),
        fields=("n", "total"),
        when="two-fifths of the steps used and nothing written",
        governs={"reading": "stop", "tests.location": "new-file"},
        yields_to=("brief-names-test",),
        why="AIR-31, 2026-10-19: the coder read for 12 steps.\nSecond line.\n",
        body="Step {n} of {total}.\nWrite now.",
        path="steering/write-now.md",
    )


def test_governs_is_a_read_only_copy() -> None:
    governs = {"reading": "stop"}
    p = parse("a.md", "rule", _raw(governs=governs))
    again = PromptText(**{**p.__dict__, "governs": governs})
    governs["reading"] = "free"

    assert dict(again.governs) == {"reading": "stop"}
    with pytest.raises(TypeError):
        p.governs["reading"] = "free"  # type: ignore[index]


def test_a_rule_with_only_the_required_keys_gets_the_defaults() -> None:
    p = parse("test-rules.md", "rule", _raw())

    assert (p.id, p.kind, p.path) == ("test-rules", "rule", "rules/test-rules.md")
    assert p.fields == ()
    assert p.when is None
    assert dict(p.governs) == {}
    assert p.yields_to == ()


def test_body_keeps_inner_whitespace_and_drops_one_final_newline() -> None:
    assert parse("a.md", "rule", _raw("a\n\nb\n\n")).body == "a\n\nb\n"
    assert parse("a.md", "rule", _raw("  a\n\n  b\n")).body == "  a\n\n  b"
    assert parse("a.md", "rule", _raw("a")).body == "a"


def test_a_body_may_contain_a_line_of_dashes_after_the_front_matter() -> None:
    p = parse("a.md", "rule", _raw("above\n---\nbelow\n"))

    assert p.body == "above\n---\nbelow"


def test_doubled_braces_render_single() -> None:
    p = parse("json-shape.md", "rule", _raw('{{"x": {n}}}\n', fields=["n"]))

    assert render(p, n=3) == '{"x": 3}'


def test_render_fills_every_field_and_only_those() -> None:
    p = parse("a.md", "rule", _raw("{a} then {b} then {a}\n", fields=["a", "b"]))

    assert render(p, a="x", b=2) == "x then 2 then x"


def test_a_field_may_be_named_id() -> None:
    p = parse("a.md", "rule", _raw("ticket {id}\n", fields=["id"]))

    assert render(p, id="AIR-7") == "ticket AIR-7"


def test_placeholders_must_match_fields() -> None:
    with pytest.raises(PromptError, match=r"steering/x\.md.*total.*never used"):
        parse("x.md", "steering", _steering("only {n}\n", fields=["n", "total"]))
    with pytest.raises(PromptError, match=r"rules/x\.md.*\bk\b.*not declared"):
        parse("x.md", "rule", _raw("{n} and {k}\n", fields=["n"]))


@pytest.mark.parametrize("body", ["a {} b\n", "a {0} b\n", "a {n.real} b\n", "a {n[0]} b\n", "a { n} b\n"])
def test_a_placeholder_must_be_a_plain_name(body: str) -> None:
    with pytest.raises(PromptError, match=r"rules/x\.md.*placeholder"):
        parse("x.md", "rule", _raw(body, fields=["n"]))


@pytest.mark.parametrize("body", ["one } brace\n", "one { brace\n", "{n!z}\n"])
def test_a_body_that_is_not_a_format_string_is_named(body: str) -> None:
    with pytest.raises(PromptError, match=r"rules/x\.md.*format string"):
        parse("x.md", "rule", _raw(body, fields=["n"]))


def test_a_placeholder_nested_in_a_format_spec_counts() -> None:
    p = parse("x.md", "rule", _raw("{n:>{width}}\n", fields=["n", "width"]))

    assert render(p, n="ab", width=4) == "  ab"
    with pytest.raises(PromptError, match=r"\bwidth\b.*not declared"):
        parse("x.md", "rule", _raw("{n:>{width}}\n", fields=["n"]))


def test_kind_must_match_folder() -> None:
    with pytest.raises(PromptError, match=r"rules/x\.md.*kind.*steering.*rule"):
        parse("x.md", "rule", _steering())
    with pytest.raises(PromptError, match=r"steering/x\.md.*kind.*rule.*steering"):
        parse("x.md", "steering", _raw())


def test_an_unknown_kind_is_named() -> None:
    with pytest.raises(PromptError, match=r"rules/x\.md.*kind.*'nudge'"):
        parse("x.md", "rule", _raw(kind="nudge"))


def test_steering_needs_when_and_rules_have_none() -> None:
    with pytest.raises(PromptError, match=r"steering/x\.md.*when"):
        parse("x.md", "steering", _steering(when=None))
    with pytest.raises(PromptError, match=r"steering/x\.md.*when"):
        parse("x.md", "steering", _steering(when="  "))
    with pytest.raises(PromptError, match=r"rules/x\.md.*when"):
        parse("x.md", "rule", _raw(when="the model stalls"))


def test_text_rejects_missing_and_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse("nudge.md", "rule", _raw("{n} of {total}\n", fields=["n", "total"]))
    monkeypatch.setattr(hub, "load", lambda: {p.id: p})

    assert text("nudge", n=1, total=2) == "1 of 2"
    with pytest.raises(PromptError, match=r"nudge.*missing.*total"):
        text("nudge", n=1)
    with pytest.raises(PromptError, match=r"nudge.*unexpected.*extra"):
        text("nudge", n=1, total=2, extra=3)
    with pytest.raises(PromptError, match=r"nudge.*missing.*n.*unexpected.*k"):
        text("nudge", total=2, k=1)


def test_text_takes_the_id_positionally_so_a_field_can_be_named_id(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse("ticket.md", "rule", _raw("ticket {id}\n", fields=["id"]))
    monkeypatch.setattr(hub, "load", lambda: {p.id: p})

    assert text("ticket", id="AIR-7") == "ticket AIR-7"


def test_entry_returns_the_parsed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    p = parse("nudge.md", "rule", _raw("hi\n"))
    monkeypatch.setattr(hub, "load", lambda: {p.id: p})

    assert entry("nudge") is p


def test_unknown_id_raises() -> None:
    with pytest.raises(PromptError, match="no-such-id"):
        text("no-such-id")
    with pytest.raises(PromptError, match="no-such-id"):
        entry("no-such-id")


@pytest.mark.parametrize(
    "name", ["Write-Now.md", "write_now.md", "write--now.md", "-now.md", "now-.md", ".md"]
)
def test_the_id_is_kebab_case(name: str) -> None:
    with pytest.raises(PromptError, match=r"kebab-case"):
        parse(name, "rule", _raw())


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("no front matter\n", "start with a line of ---"),
        ("---\nkind: rule\nroles: [coder]\nwhy: x\n", "closing ---"),
        ("---\n---\nbody\n", "mapping"),
        ("---\n- a\n- b\n---\nbody\n", "mapping"),
        ("---\nkind: [unclosed\n---\nbody\n", "not valid YAML"),
        (_raw(roles=None), "roles"),
        (_raw(roles=[]), "roles"),
        (_raw(roles="coder"), "roles"),
        (_raw(roles=["coder", 3]), "roles"),
        (_raw(why=None), "why"),
        (_raw(why="  "), "why"),
        (_raw(why=["a"]), "why"),
        (_raw(fields="n"), "fields"),
        (_raw(yields_to="other"), "yields_to"),
        (_raw(governs=["a"]), "governs"),
        (_raw(governs={"a": 1}), "governs"),
        (_raw(governs={"a": False}), "governs"),
        (_raw(yeilds_to=["x"]), "unknown key"),
    ],
)
def test_a_malformed_file_is_named_with_the_problem(raw: str, message: str) -> None:
    with pytest.raises(PromptError, match=rf"rules/x\.md.*{message}"):
        parse("x.md", "rule", raw)


def test_a_governs_value_that_yaml_reads_as_a_boolean_says_to_quote_it() -> None:
    raw = "---\nkind: rule\nroles: [coder]\ngoverns:\n  reading: off\nwhy: x\n---\nbody\n"

    with pytest.raises(PromptError, match=r"quote"):
        parse("x.md", "rule", raw)


def test_the_hub_loads_from_the_installed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    root = files("airlie.prompts")
    assert (root / "rules").is_dir()
    assert (root / "steering").is_dir()

    seen: list[Any] = []
    real = hub._load_from

    def spy(r: Any) -> Mapping[str, PromptText]:
        seen.append(r)
        return real(r)

    monkeypatch.setattr(hub, "_load_from", spy)
    loaded = load.__wrapped__()  # not load(): that would cache the spy's result for the other tests

    assert seen == [root]
    assert isinstance(loaded, Mapping)
    assert dict(loaded) == dict(load())
    assert "last-step" in loaded  # a file the package ships, not only the folders


def test_load_is_cached_and_read_only() -> None:
    assert load() is load()
    with pytest.raises(TypeError):
        load()["new"] = None  # type: ignore[index]


def test_the_package_re_exports_the_loader_api() -> None:
    assert prompts.text is hub.text
    assert prompts.entry is hub.entry
    assert prompts.load is hub.load
    assert prompts.parse is hub.parse
    assert prompts.PromptText is hub.PromptText
    assert prompts.PromptError is hub.PromptError
    assert prompts.Kind is hub.Kind


def _write(root: Path, folder: str, name: str, raw: str) -> None:
    (root / folder).mkdir(exist_ok=True)
    (root / folder / name).write_text(raw, encoding="utf-8")


def test_load_from_reads_both_folders_by_file_stem(tmp_path: Path) -> None:
    _write(tmp_path, "rules", "test-rules.md", _raw("rule body\n"))
    _write(tmp_path, "steering", "write-now.md", _steering("steer body\n"))

    loaded = _load_from(tmp_path)

    assert sorted(loaded) == ["test-rules", "write-now"]
    assert loaded["test-rules"].body == "rule body"
    assert loaded["test-rules"].path == "rules/test-rules.md"
    assert loaded["write-now"].kind == "steering"
    assert loaded["write-now"].path == "steering/write-now.md"


def test_load_from_lists_rules_then_steering_each_in_file_name_order(tmp_path: Path) -> None:
    _write(tmp_path, "rules", "b-rule.md", _raw())
    _write(tmp_path, "rules", "a-rule.md", _raw())
    _write(tmp_path, "steering", "c-steer.md", _steering())
    _write(tmp_path, "steering", "a-steer.md", _steering())

    assert list(_load_from(tmp_path)) == ["a-rule", "b-rule", "a-steer", "c-steer"]


def test_load_from_reads_utf8(tmp_path: Path) -> None:
    _write(tmp_path, "rules", "dash.md", _raw("a — b\n"))
    (tmp_path / "steering").mkdir()

    assert _load_from(tmp_path)["dash"].body == "a — b"


def test_load_from_ignores_dotfiles_and_names_any_other_file(tmp_path: Path) -> None:
    _write(tmp_path, "rules", ".gitkeep", "")
    _write(tmp_path, "rules", ".DS_Store", "\x00")
    _write(tmp_path, "steering", ".gitkeep", "")
    assert _load_from(tmp_path) == {}

    _write(tmp_path, "rules", "notes.txt", "scratch")
    with pytest.raises(PromptError, match=r"rules/notes\.txt.*not a prompt file"):
        _load_from(tmp_path)


def test_load_from_names_a_missing_folder(tmp_path: Path) -> None:
    (tmp_path / "rules").mkdir()

    with pytest.raises(PromptError, match=r"steering"):
        _load_from(tmp_path)


def test_load_from_names_the_file_that_fails(tmp_path: Path) -> None:
    _write(tmp_path, "rules", "fine.md", _raw())
    _write(tmp_path, "steering", "broken.md", _steering("{n}\n"))

    with pytest.raises(PromptError, match=r"steering/broken\.md"):
        _load_from(tmp_path)


def test_ids_are_unique_across_folders(tmp_path: Path) -> None:
    _write(tmp_path, "rules", "same.md", _raw())
    _write(tmp_path, "steering", "same.md", _steering())

    with pytest.raises(PromptError, match=r"same.*rules/same\.md.*steering/same\.md"):
        _load_from(tmp_path)
