"""A brief from a recipe: its entries checked, its parts joined, their sizes told."""

import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from airlie.story.brief_recipe import (
    SECTIONS,
    BriefInputs,
    RecipeEntry,
    RecipeError,
    assemble,
    drop_headings,
    load_recipe,
    parse_recipe,
    resolve,
    span,
)
from airlie.story.context import CHARS_PER_TOKEN
from airlie.story.model import Criterion, Layer, Story
from airlie.story.package import Interface, Package, PackageFile
from tests.unit.git_helpers import git
from tests.unit.test_story_briefs import LAYERS, PACKAGE, STORY

BIG = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(120))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for name, text in {
        "src/a.py": BIG,
        "src/b.md": "# B\n" + "text\n" * 600,
        "tests/test_a.py": "base\n",
    }.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def inputs(
    repo: Path | None = None,
    *,
    brief: str | None = None,
    survey: str = "",
    steps: tuple[str, ...] = (),
    scope: str = "src/a.py, tests/test_a.py",
    files: tuple[PackageFile, ...] = (PackageFile("src/a.py", "modify", "change"),),
) -> BriefInputs:
    """Layer 1 of the shared story; with a `repo`, its scope, brief and package name the repo's files.
    `brief`, `survey`, `steps`, `scope` and `files` override the layer brief, the survey notes, the
    package's steps, the layer's scope and the package's files."""
    if repo is None:
        return BriefInputs(STORY, LAYERS, 1, PACKAGE, Path("."), "HEAD", 196_608, survey=survey)
    layers = (LAYERS[0], replace(LAYERS[1], scope=scope, brief=brief or "Update `src/b.md`."))
    package = Package(
        "profile",
        {"tests/test_a.py": "def test_locked():\n    pass\n"},
        files,
        (),
        steps,
        {},
        "done",
    )
    return BriefInputs(
        STORY, layers, 1, package, repo, git(repo, "rev-parse", "HEAD"), 196_608, survey=survey
    )


def test_a_recipe_is_its_sections_and_rules_in_order() -> None:
    r = parse_recipe("- section: layer_context\n- rule: brief-layer-plan-agents-md\n", "t")
    assert r == (RecipeEntry(section="layer_context"), RecipeEntry(rule="brief-layer-plan-agents-md"))


@pytest.mark.parametrize(
    "text, phrase",
    [
        ("- section: nope\n", "unknown section 'nope'"),
        ("- rule: no-such-rule\n", "unknown rule 'no-such-rule'"),
        ("- section: package\n  shares: 0.2\n", "unknown key 'shares'"),
        ("- section: package\n  rule: package-contract\n", "both section and rule"),
        ("- section: package\n  over: outline\n", "package does not take over"),
        ("- section: package\n  share: 1.5\n", "share must be between 0 and 1"),
    ],
)
def test_a_wrong_recipe_is_refused_naming_the_entry(text: str, phrase: str) -> None:
    with pytest.raises(RecipeError, match=re.escape(phrase)) as e:
        parse_recipe(text, "t")
    assert "recipe t, entry 0" in str(e.value)


def test_a_missing_recipe_is_refused_by_name() -> None:
    with pytest.raises(RecipeError, match="no-such-recipe"):
        load_recipe("no-such-recipe")


def test_the_assembled_brief_is_its_parts_and_their_sizes_add_up() -> None:
    recipe = parse_recipe("- section: layer_context\n- section: package\n- rule: package-contract\n", "t")
    brief = assemble("t", recipe, inputs())
    assert brief.text == "\n\n".join(p.text for p in brief.parts if p.text)
    joins = 2 * (len([p for p in brief.parts if p.text]) - 1)
    assert sum(s["chars"] for s in brief.sizes()) + joins == len(brief.text)
    assert brief.text.startswith(f"Story: {STORY.title}.")
    assert "## The tests you make pass (in the worktree, locked)" in brief.text


def test_locked_tests_are_the_package_text_whole() -> None:
    part = SECTIONS["locked_tests"](inputs(), RecipeEntry(section="locked_tests"), None, frozenset())
    assert PACKAGE.tests["tests/unit/test_profile.py"] in part.text
    assert part.shown == {"tests/unit/test_profile.py": "whole"}


def test_scope_files_leave_out_the_locked_tests_and_show_the_layers_files_then_the_brief_named_ones(
    repo: Path,
) -> None:
    recipe = parse_recipe("- section: locked_tests\n- section: scope_files\n", "t")
    brief = assemble("t", recipe, inputs(repo))
    assert brief.text.count("### tests/test_a.py") == 1
    assert "def test_locked" in brief.text and "base\n" not in brief.text
    assert brief.text.index("### src/a.py") < brief.text.index("### src/b.md")


def test_scope_files_skip_what_an_earlier_part_shows_whole(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files")
    part = SECTIONS["scope_files"](inputs(repo), entry, None, frozenset({"src/a.py"}))
    assert "src/a.py" not in part.shown and "src/b.md" in part.shown


def test_a_file_past_the_cap_is_outlined_then_listed(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files", over="outline")
    part = SECTIONS["scope_files"](inputs(repo), entry, 2_000, frozenset())
    assert "(outline: read a span with read_file offset and limit)" in part.text
    assert "### Not shown (read with read_file)\n- " in part.text
    assert set(part.shown.values()) == {"outline", "listed"}


def test_a_small_context_lists_what_does_not_fit(repo: Path) -> None:
    part = SECTIONS["scope_files"](inputs(repo), RecipeEntry(section="scope_files"), 50, frozenset())
    assert "### Not shown (read with read_file)" in part.text
    assert "whole" not in part.shown.values()
    assert "- src/a.py (360 lines)" in part.text


def test_no_files_is_an_empty_part(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files")
    part = SECTIONS["scope_files"](inputs(repo), entry, None, frozenset({"src/a.py", "src/b.md"}))
    assert part.text == "" and part.shown == {}


def _numbered(total: int, special: dict[int, str]) -> str:
    return "".join(special.get(n, f"V{n} = {n}\n") for n in range(1, total + 1))


PROTOCOL = _numbered(
    60,
    {40: "def cut_off_text(x):\n", **{n: "    y = x\n" for n in range(41, 48)}, 48: "    return x\n"},
)
BIG_CLASS = _numbered(
    220,
    {
        20: "class Big:\n",
        **{n: "    x = 1\n" for n in range(21, 221)},
        120: "    def method(self):\n",
        **{n: "        pass\n" for n in range(121, 125)},
        125: "        return 1\n",
    },
)
MD = "".join(f"line {n}\n" for n in range(1, 11))
TREE = [
    "docs/x.md",
    "src/a/util.py",
    "src/b/util.py",
    "src/big.py",
    "src/router/protocol.py",
    "src/teams/protocol.py",
]


@pytest.fixture
def span_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for name, text in {
        "src/teams/protocol.py": PROTOCOL,
        "src/router/protocol.py": "R = 1\n",
        "src/big.py": BIG_CLASS,
        "src/a/util.py": "A = 1\n" * 5,
        "src/b/util.py": "B = 1\n" * 5,
        "docs/x.md": MD,
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_a_bare_name_resolves_through_a_full_path_the_story_names() -> None:
    assert resolve("protocol.py", ["src/teams/protocol.py"], TREE) == ["src/teams/protocol.py"]


def test_a_bare_name_with_two_files_and_no_full_path_is_ambiguous() -> None:
    assert sorted(resolve("protocol.py", [], TREE)) == ["src/router/protocol.py", "src/teams/protocol.py"]


def test_a_span_widens_to_its_innermost_def_of_at_most_80_lines() -> None:
    assert span(PROTOCOL, "src/teams/protocol.py", 42, 45) == (40, 48)
    assert span(BIG_CLASS, "src/big.py", 121, 122) == (120, 125)  # the method, not the 200-line class
    assert span(BIG_CLASS, "src/big.py", 10, 10) == (1, 20)  # outside any def: N+-10, clamped at 1
    assert span(MD, "docs/x.md", 3, 5) == (3, 5)  # not Python: as cited


def test_cited_spans_show_the_span_and_list_the_ambiguous(span_repo: Path) -> None:
    part = SECTIONS["cited_spans"](
        inputs(
            span_repo,
            brief="protocol.py:42-48",
            survey="see src/teams/protocol.py:18-48",
            steps=("util.py:3",),
        ),
        RecipeEntry(section="cited_spans"),
        None,
        frozenset(),
    )
    assert "### src/teams/protocol.py (lines 40-48)" in part.text
    assert part.shown["src/teams/protocol.py"] == "span"
    assert "- util.py:3 (more than one file has this name)" in part.text


def test_a_citation_past_the_end_or_to_a_missing_file_is_listed(span_repo: Path) -> None:
    part = SECTIONS["cited_spans"](
        inputs(span_repo, brief="gone.py:10 and src/teams/protocol.py:900"),
        RecipeEntry(section="cited_spans"),
        None,
        frozenset(),
    )
    assert "- gone.py:10 (no such file at this layer's base)" in part.text
    assert "### src/teams/protocol.py (lines 50-60)" in part.text


def test_spans_past_the_cap_are_listed_and_a_shown_file_is_skipped(span_repo: Path) -> None:
    entry = RecipeEntry(section="cited_spans")
    brief = "src/teams/protocol.py:42-48 and src/big.py:121"
    part = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, 190, frozenset())
    assert "### src/teams/protocol.py (lines 40-48)" in part.text
    assert "- src/big.py:120-125" in part.text and "Not shown" in part.text
    skipped = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, None, frozenset({"src/big.py"}))
    assert "src/big.py" not in skipped.text


def test_overlapping_spans_in_one_file_merge(span_repo: Path) -> None:
    brief = "src/teams/protocol.py:3-5 src/teams/protocol.py:5-8 src/teams/protocol.py:55-56"
    entry = RecipeEntry(section="cited_spans")
    part = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, None, frozenset())
    assert "(lines 3-8)" in part.text and "(lines 55-56)" in part.text


def test_no_citations_is_an_empty_part(span_repo: Path) -> None:
    entry = RecipeEntry(section="cited_spans")
    assert SECTIONS["cited_spans"](inputs(span_repo), entry, None, frozenset()).text == ""


def test_agents_md_leaves_out_the_dropped_headings(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    agents = "# x\n\n## Configuration\nc\n\n## Test gate\nuv run pytest\n\n\n## After\na\n"
    (tmp_path / "AGENTS.md").write_text(agents)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    entry = RecipeEntry(section="agents_md", drop=("Test gate",))
    part = SECTIONS["agents_md"](inputs(tmp_path), entry, None, frozenset())
    assert "## Test gate" not in part.text and "uv run pytest" not in part.text
    assert "## Configuration\nc\n\n## After\na" in part.text
    assert part.text.startswith("### AGENTS.md\n") and part.shown == {"AGENTS.md": "whole"}
    clipped = SECTIONS["agents_md"](inputs(tmp_path), entry, 40, frozenset())
    assert clipped.text.endswith("\n[clipped]") and len(clipped.text) <= 40 and not clipped.shown


def test_agents_md_is_empty_without_an_agents_file(repo: Path) -> None:
    part = SECTIONS["agents_md"](inputs(repo), RecipeEntry(section="agents_md"), None, frozenset())
    assert part.text == "" and not part.shown


def test_every_shipped_recipe_loads() -> None:
    found = [f.name for f in (files("airlie.prompts") / "briefs").iterdir() if f.name.endswith(".yaml")]
    assert "layer-build.yaml" in found
    for name in found:
        assert load_recipe(name.removesuffix(".yaml"))


def test_the_layer_build_recipe_puts_the_locked_tests_before_the_scope_files() -> None:
    names = [e.section or e.rule for e in load_recipe("layer-build")]
    assert names.index("locked_tests") < names.index("scope_files") < names.index("cited_spans")
    assert "coder-gate-autofix" in names and "brief-files-given" in names


def test_a_layer_whose_scope_names_no_file_still_has_a_brief(repo: Path) -> None:
    recipe = load_recipe("layer-build")
    brief = assemble("layer-build", recipe, inputs(repo, scope="the cut_off rule", files=()))
    assert "## Steps" in brief.text and "Scope of this layer: the cut_off rule" in brief.text


def layer_brief(repo: Path, i: int = 1, *, built: str = "") -> str:
    """The shared story's layer `i` brief from the layer-build recipe, its files read at `repo`'s HEAD."""
    commit = git(repo, "rev-parse", "HEAD")
    got = BriefInputs(STORY, LAYERS, i, PACKAGE, repo, commit, 196_608, built=built)
    return assemble("layer-build", load_recipe("layer-build"), got).text


def test_the_layer_brief_lists_the_tests_per_criterion_the_files_the_interfaces_and_the_steps(
    repo: Path,
) -> None:
    text = layer_brief(repo)
    assert "layer 2 of 3" in text and "03 Docs" in text and "Show the avatar." not in text
    tests = text.split("## The tests you make pass")[1].split("## Files")[0]
    assert "**AC2** Given a stored avatar" in tests and "  - tests/unit/test_profile.py::test_shows" in tests
    assert "- src/profile.py (create): the profile view" in text
    assert "- src/profile.py: def show() -> int\n  returns 1, the avatar id" in text
    assert "1. create src/profile.py with show() returning 1\n2. export show from src/__init__.py" in text
    assert "## Done when\nrun_tests passes on tests/unit/test_profile.py" in text
    assert "you cannot change them" in text and "once they pass, you are done" in text
    assert "Write the tests first" not in text and "Read for you:" not in text
    assert text.index("## Steps") < text.index("you cannot change them")


def test_the_layer_brief_tells_the_coder_in_one_line_to_follow_agents_md(repo: Path) -> None:
    for i in (1, 2):  # a layer that owns criteria, and an enabling one
        [line] = [x for x in layer_brief(repo, i).splitlines() if "AGENTS.md" in x]
        assert line == "Follow AGENTS.md at the repository root, if it has one."


def test_the_brief_of_a_later_layer_puts_what_the_earlier_layers_added_before_its_tests(repo: Path) -> None:
    digest = "## What the earlier layers of this story added (read nothing to find this)\n- src/a.py: f0"
    brief = layer_brief(repo, built=digest)
    assert digest in brief and brief.index(digest) < brief.index("## The tests you make pass")
    assert "What the earlier layers" not in layer_brief(repo, 0)


# ---- AIR-48 layer 01: trimmed copies of its files at the base, its layer, package and survey ----

AIR48_DIAGNOSIS = '''\
"""A block's evidence pack and class, from disk alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNFETCHED = re.compile(r"could not be fetched|failed to fetch", re.IGNORECASE)


@dataclass(frozen=True)
class Classified:
    class_: str
    kind: str
    where: str
    rule: str
    evidence: tuple[str, ...]


def _made(class_: str, kind: str, where: str, rule: str, *evidence: str) -> Classified:
    return Classified(class_, kind, where, rule, evidence)


def classify(pack, *, muse_category=None):
    if _UNFETCHED.search(pack.reason):
        return _made("transient", "fetch_failed", pack.stage, "fetch_failed", pack.reason)
    who = pack.role or pack.stage
    if pack.stop_reason == "cut_off":
        return _made("config", "cut_off", who, "cut_off", f"{who} stopped: cut_off")
    return _made("process", "unknown", who, "unknown")
'''
CUT_OFF_TEXT = '''\
def cut_off_text(role_name: str, role: RoleSpec, *, thinking: bool = False) -> str:
    """What a person reads when a reply of `role_name` ended at max_tokens: what happened, and what to raise.
    `thinking`: the reply was all thinking (the server split it off, so there is no visible text)."""
    still = ", still thinking" if thinking else ""
    fix = f"raise roles.{role_name}.max_tokens"
    if role.thinking and role.thinking_budget is not None and role.max_tokens <= role.thinking_budget:
        fix += f" above its thinking_budget ({role.thinking_budget})"
    return f"the reply was cut off at max_tokens ({role.max_tokens}) before it ended{still}; {fix}"
'''
AIR48_PROTOCOL = (
    "".join(f"V{n} = {n}\n" for n in range(1, 41))
    + "\n"
    + CUT_OFF_TEXT  # lines 42-49
    + "\n\nclass StuckAdvisor:\n    pass\n"
)
AIR48_TEST_BASE = '''\
"""A block's evidence pack and class, from disk alone."""

from airlie.story.diagnosis import classify


def test_a_fetch_failure_is_transient(pack) -> None:
    assert classify(pack).class_ == "transient"
'''
AIR48_TEST_LOCKED = (
    AIR48_TEST_BASE
    + """

def test_classify_cut_off_names_config_cut_off_and_role_signature() -> None:
    got = classify(blocked_pack(stop_reason="cut_off", role="architect", reason=cut_off_text("architect", R)))
    assert got.signature == "config:cut_off:architect"
    assert "16384" in " ".join(got.evidence)
"""
)
AIR48_AGENTS = """\
# airlie

Python 3.12 with uv; every action is a Taskfile task (`task --list`).

## Configuration

Committed config is split for readability.

## Test gate

Every change must pass these commands:

```sh
uv run pytest -q -n auto --maxprocesses 8
```
"""
AIR48_STORY = Story(
    "Surface a cut_off stop with an actionable reason",
    "A cut_off stop names the role, its cap and a concrete next step, never a silent truncation.",
    (
        Criterion(
            "AC1", "a role whose reply is cut off at its token cap", "the block is diagnosed", "it is config"
        ),
        Criterion(
            "AC2", "a config / cut_off diagnosis", "it is rendered", "it names max_tokens and the role"
        ),
        Criterion("AC3", "a cut_off block", "the fix card is rendered", "it is the generic cut_off template"),
    ),
)
AIR48_LAYERS = (
    Layer(
        "cut-off-diagnosis",
        "The cut_off rule names the cap and a next step",
        "src/airlie/story/diagnosis.py, tests/unit/test_story_diagnosis.py",
        "In classify, keep the cut_off rule's class config, kind cut_off and where = pack.role or pack.stage "
        '(so the signature stays config:cut_off:architect), but replace the bare evidence f"{who} stopped: '
        'cut_off" with evidence that names the role, the token cap its reply hit, and a concrete next step '
        "naming max_tokens and the role. The cap is NOT a Pack field: parse it out of pack.reason, which on "
        "the real path already carries cut_off_text's sentence 'the reply was cut off at max_tokens (N) "
        "before it ended; raise roles.<role>.max_tokens' (protocol.py:42-49; appended to the stop's reason "
        "by pipeline._cut_off, pipeline.py:243-250; gathered into pack.reason by gather).",
        ("AC1", "AC2"),
    ),
    Layer(
        "fix-card",
        "Guard the cut_off fix card's generic criteria and out-of-scope",
        "src/airlie/story/fixes.py",
        "Guard the fix card.",
        ("AC3",),
    ),
)
AIR48_PACKAGE = Package(
    "cut-off-diagnosis",
    {"tests/unit/test_story_diagnosis.py": AIR48_TEST_LOCKED},
    (
        PackageFile(
            "src/airlie/story/diagnosis.py",
            "modify",
            "the cut_off rule's evidence names the role, the cap parsed from pack.reason, and a max_tokens "
            "next step (with a fallback)",
        ),
        PackageFile("tests/unit/test_story_diagnosis.py", "modify", "three tests: AC1 classify, AC2 render"),
    ),
    (
        Interface(
            "src/airlie/story/diagnosis.py",
            "def classify(pack: Pack, *, muse_category: str | None = None) -> Classified",
            "cut_off rule (stop_reason == 'cut_off') keeps class_ 'config', kind 'cut_off', where = "
            "pack.role or pack.stage; its evidence names the cap when "
            "re.search(r'max_tokens \\((\\d+)\\)', pack.reason) matches, else a sentence naming max_tokens "
            "and the role",
        ),
    ),
    (
        "In src/airlie/story/diagnosis.py, beside the other module-level regexes (near _UNFETCHED), add "
        '_CUT_OFF_CAP = re.compile(r"max_tokens \\((\\d+)\\)") with a docstring: the cap cut_off_text put in '
        "the stop's reason.",
        "In classify, replace the cut_off rule's single bare evidence: keep who = pack.role or pack.stage "
        "and the _made('config', 'cut_off', who, 'cut_off', ...) call, but build the one evidence string "
        "from _CUT_OFF_CAP.search(pack.reason), with the cap when it matches, else the fallback sentence.",
        "Leave every other rule, the Diagnosis/render_diagnosis/next_for functions untouched.",
    ),
    {
        "AC1": (
            "tests/unit/test_story_diagnosis.py::test_classify_cut_off_names_config_cut_off_and_role_signature",
        )
    },
    "classify of a cut_off block returns config/cut_off/<role> with evidence naming the role, the cap parsed "
    "from pack.reason (16384) and a max_tokens next step.",
)
AIR48_SURVEY = (
    "## Notes for the engineer planning `config:cut_off:architect`\n\n"
    "- **`src/airlie/teams/protocol.py:18-49`** - `StopReason` includes `cut_off`; `cut_off_text(role_name, "
    "role, *, thinking=False)` builds the human-facing message.\n"
    "- **`src/airlie/story/diagnosis.py:514-515`** - the cut_off rule, the `config:cut_off:architect` "
    "signature.\n"
    "- **`src/airlie/story/pipeline.py:243-249`** - `_cut_off(orch)` names the subtasks whose reply was cut "
    "off.\n"
)


@pytest.fixture
def air48_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for name, text in {
        "src/airlie/story/diagnosis.py": AIR48_DIAGNOSIS,
        "src/airlie/teams/protocol.py": AIR48_PROTOCOL,
        "src/airlie/router/protocol.py": "class Router:\n    pass\n",
        "tests/unit/test_story_diagnosis.py": AIR48_TEST_BASE,
        "AGENTS.md": AIR48_AGENTS,
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def air48_inputs(repo: Path) -> BriefInputs:
    """AIR-48's layer 01 at `repo`'s HEAD, with the coder's context (131,072 tokens)."""
    commit = git(repo, "rev-parse", "HEAD")
    chars = 131_072 * CHARS_PER_TOKEN
    return BriefInputs(AIR48_STORY, AIR48_LAYERS, 0, AIR48_PACKAGE, repo, commit, chars, survey=AIR48_SURVEY)


def test_the_air48_layer_one_brief(air48_repo: Path) -> None:
    """AIR-48 layer 01 (2026-11-01): the coder's brief carried the story dossier, a repository map and the
    whole AGENTS.md, and clipped the very file it changed. From the recipe it shows diagnosis.py once and
    whole, the locked test as the package wrote it, cut_off_text from the bare `protocol.py:42-49` (the story
    names the full path; another protocol.py exists), and AGENTS.md without its Test gate."""
    brief = assemble("layer-build", load_recipe("layer-build"), air48_inputs(air48_repo))
    t = brief.text
    assert "[clipped: read it with read_file]" not in t and "## Test gate" not in t
    assert t.count("### src/airlie/story/diagnosis.py") == 1
    assert "### src/airlie/teams/protocol.py (lines 42-49)" in t and "def cut_off_text" in t
    assert "src/airlie/router/protocol.py" not in t
    assert AIR48_PACKAGE.tests["tests/unit/test_story_diagnosis.py"] in t
    assert "## Configuration" in t
    assert sum(s["chars"] for s in brief.sizes()) + 2 * (len([p for p in brief.parts if p.text]) - 1) == len(
        t
    )


def _cited(repo: Path, brief: str, package_tests: dict[str, str]) -> str:
    got = inputs(repo, brief=brief)
    got = replace(got, package=replace(got.package, tests=package_tests))
    return SECTIONS["cited_spans"](got, RecipeEntry(section="cited_spans"), None, frozenset()).text


def _commit(path: Path, files: dict[str, str]) -> None:
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")


def test_a_citation_into_a_locked_test_shows_the_packages_lines_not_the_bases(repo: Path) -> None:
    text = _cited(repo, "see tests/test_a.py:2", {"tests/test_a.py": "base\ndef test_new():\n    pass\n"})
    assert "### tests/test_a.py (lines 2-3)" in text and "def test_new" in text


def test_a_citation_into_a_new_locked_test_is_shown_not_listed(repo: Path) -> None:
    text = _cited(repo, "see tests/test_new.py:1", {"tests/test_new.py": "def test_x():\n    pass\n"})
    assert "def test_x" in text and "no such file" not in text
    bare = _cited(repo, "see test_new.py:1", {"tests/test_new.py": "def test_x():\n    pass\n"})
    assert "### tests/test_new.py" in bare


def test_a_citation_of_line_zero_or_into_an_empty_file_is_never_an_empty_card(tmp_path: Path) -> None:
    assert span("a\nb\nc\n", "x.md", 0, 0) == (1, 3)
    assert span("", "x.md", 1, 1) is None and span("", "x.py", 0, 0) is None
    _commit(tmp_path, {"docs/e.md": ""})
    text = _cited(tmp_path, "docs/e.md:1 and docs/e.md:0", {})
    assert "(lines 1-0)" not in text and "- docs/e.md:1 (no such line)" in text


def test_drop_headings_stops_at_a_level_one_heading_and_ignores_fenced_headings() -> None:
    assert drop_headings("# T\n## Test gate\nx\n# Other\nkeep", ["Test gate"]) == "# T\n# Other\nkeep"
    text = "## Gate\n```sh\n## Gate\n# x\n```\nstill\n## Next\nkeep\n"
    assert drop_headings(text, ["Gate"]) == "## Next\nkeep\n"
    kept = "## Keep\n~~~\n## Gate\n~~~\nmore\n"
    assert drop_headings(kept, ["Gate"]) == kept


def test_agents_md_cited_in_the_layer_brief_appears_once(tmp_path: Path) -> None:
    _commit(tmp_path, {"AGENTS.md": "# x\n\n## Rules\nr\n"})
    got = inputs(tmp_path, brief="Follow AGENTS.md:3 here.", files=())
    text = assemble("layer-build", load_recipe("layer-build"), got).text
    assert text.count("### AGENTS.md") == 1 and "(lines" not in text.split("### AGENTS.md")[1].split("\n")[0]


def test_the_files_given_rule_is_left_out_when_no_file_section_has_text(tmp_path: Path) -> None:
    _commit(tmp_path, {"x": "x"})
    got = inputs(tmp_path, brief="Do it.", scope="nothing", files=())
    got = replace(got, package=replace(got.package, tests={}))
    brief = assemble("layer-build", load_recipe("layer-build"), got)
    assert all(p.name != "rule:brief-files-given" for p in brief.parts)
    assert sum(s["chars"] for s in brief.sizes()) + 2 * (len([p for p in brief.parts if p.text]) - 1) == len(
        brief.text
    )
