"""Vitest tests found in a TS/JS test file with no TypeScript toolchain (the web-tests design, 2026-10-27)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.story.js_tests import JsParseError, js_tests
from airlie.tools.scope import is_js_test, is_test_file

SPEC = """import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
// it("a comment is no test", () => {})
const note = "it('a string is no test either')";

describe("Wiki screen", () => {
  it("lists the topics", async () => {
    const w = mount(Wiki, { props: { pages: [{ slug: "a)b" }] } });
    expect(w.text()).toContain("build-spec");
  });

  describe('a page', () => {
    test(`renders its title`, () => {
      expect(1).toBe(1);
    });
    it.skip("waits for search", () => {
      console.log("{ not a brace }");
    });
  });
});

test("top level", () => { expect.assertions(0); });
"""


def test_a_test_file_is_a_pytest_module_or_a_js_test_file() -> None:
    for path in ("web/src/Wiki.test.ts", "web/src/a.spec.ts", "web/x.test.js", "web/c.spec.tsx"):
        assert is_js_test(path) and is_test_file(path), path
    assert is_test_file("tests/unit/test_a.py") and not is_js_test("tests/unit/test_a.py")
    for path in (
        "web/src/Wiki.vue",
        "web/src/test.ts",
        "web/src/a.test.ts.map",
        "web/node_modules/x/a.test.ts",
    ):
        assert not is_test_file(path), path


def test_vitest_tests_are_found_with_their_describe_names_and_whether_they_expect() -> None:
    found = js_tests(SPEC)
    assert [t.name for t in found] == [
        "Wiki screen > lists the topics",
        "Wiki screen > a page > renders its title",
        "Wiki screen > a page > waits for search",
        "top level",
    ]
    assert [t.asserts for t in found] == [True, True, False, True]
    assert found[0].source.startswith('it("lists the topics"') and found[0].source.endswith("})")


def test_a_title_that_is_not_a_literal_or_a_file_that_does_not_close_is_a_parse_error() -> None:
    with pytest.raises(JsParseError, match="line 1: the title of it\\(\\) is not a literal string"):
        js_tests("it(name, () => { expect(1).toBe(1) })")
    with pytest.raises(JsParseError, match="line 1: the title of test\\(\\) is not a literal string"):
        js_tests("test(`a ${x}`, () => {})")
    with pytest.raises(JsParseError, match="unbalanced"):
        js_tests('it("open", () => { expect(1)')


def test_vitest_report_and_red_findings(tmp_path: Path) -> None:
    from airlie.story.js_tests import vitest_findings, vitest_outcomes
    from airlie.story.layer_checks import ChangedTest

    root = tmp_path
    report = {
        "testResults": [
            {
                "name": str(root / "web/src/Wiki.test.ts"),
                "status": "failed",
                "assertionResults": [
                    {"ancestorTitles": ["Wiki screen"], "title": "lists the topics", "status": "failed"},
                    {"ancestorTitles": ["Wiki screen"], "title": "shows a page", "status": "passed"},
                ],
            },
            {"name": str(root / "web/src/Search.test.ts"), "status": "failed", "assertionResults": []},
            {
                "name": str(root / "web/src/router.test.ts"),
                "status": "failed",
                "assertionResults": [{"ancestorTitles": [], "title": "routes home", "status": "failed"}],
            },
        ]
    }
    outcomes = vitest_outcomes(report, root)
    assert outcomes == {
        "web/src/Wiki.test.ts::Wiki screen > lists the topics": "failed",
        "web/src/Wiki.test.ts::Wiki screen > shows a page": "passed",
        "web/src/Search.test.ts": "failed",  # could not be loaded: the layer's component is not there yet
        "web/src/router.test.ts::routes home": "failed",
    }
    changed = [
        ChangedTest("web/src/Wiki.test.ts", "Wiki screen > lists the topics", "src", True),
        ChangedTest("web/src/Wiki.test.ts", "Wiki screen > shows a page", "src", True),
        ChangedTest("web/src/Search.test.ts", "search > filters", "src", True),
    ]
    found = vitest_findings(outcomes, changed)
    assert [(f.kind, f.test) for f in found] == [
        ("passes_before_code", "web/src/Wiki.test.ts::Wiki screen > shows a page"),
        ("existing_test_fails", "web/src/router.test.ts::routes home"),
    ]


def test_the_changed_vitest_tests_of_a_layer_are_its_new_and_edited_ones(tmp_path: Path) -> None:
    from airlie.story.js_tests import changed_js_tests, unparseable_js_tests
    from tests.unit.test_story_layer_checks import git, repo

    old = 'it("kept", () => { expect(1).toBe(1) })\nit("edited", () => { expect(1).toBe(1) })\n'
    root, base = repo(tmp_path, {"web/src/a.test.ts": old})
    new = (
        old.replace('toBe(1) })\nit("edited"', 'toBe(1) })\nit("edited"').replace(
            'it("edited", () => { expect(1).toBe(1) })', 'it("edited", () => { expect(2).toBe(2) })'
        )
        + 'it("added", () => { doIt() })\n'
    )
    (root / "web/src/a.test.ts").write_text(new)
    (root / "web/src/b.test.ts").write_text("it(title, () => {})\n")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "layer")
    got = changed_js_tests(root, base)
    assert [(t.name, t.asserts, bool(t.before)) for t in got] == [
        ("edited", True, True),
        ("added", False, False),
    ]
    assert [f.test for f in unparseable_js_tests(root, base)] == ["web/src/b.test.ts"]


def test_only_type_errors_no_layer_code_can_fix_in_the_package_tests_are_findings() -> None:
    """vue-tsc's plain output (wiki run B, 2026-10-27): an unused import and a name never imported are the
    test's own; a module the layer will add is the expected red, and another file's error is not the
    package's."""
    from airlie.story.js_tests import type_findings

    out = (
        "src/zz.test.ts(2,1): error TS6133: 'useSessionStore' is declared but its value is never read.\n"
        "src/zz.test.ts(3,27): error TS2307: Cannot find module '@/wiki' or its type declarations.\n"
        "src/zz.test.ts(7,12): error TS2304: Cannot find name 'createAppRouter'.\n"
        "src/zz.test.ts(9,3): error TS1005: ')' expected.\n"
        "src/other.ts(1,1): error TS2304: Cannot find name 'x'.\n"
        "Found 5 errors.\n"
    )
    found = type_findings(out, "web", {"web/src/zz.test.ts"})
    assert [(f.kind, f.test) for f in found] == [("test_type_error", "web/src/zz.test.ts")] * 3
    assert [f.why.split(":")[0] for f in found] == ["line 2", "line 7", "line 9"]
    assert found[1].why.startswith("line 7: TS2304 Cannot find name 'createAppRouter': the gate's typecheck")
    assert type_findings(out, "web", {"web/src/other.test.ts"}) == []
