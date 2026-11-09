"""The nightly mutation sweep's survivors become fix cards on airlie's own board: one per function, `todo`,
asked of GitHub at most once an hour (mutation-nightly Task 4)."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.story import fixes, mutation_cards
from airlie.story.mutation_cards import Survivor
from tests.unit.test_studio_orchestrator import cfg_for

HOME = "weyenk/airlie"
RUN_URL = "https://github.com/weyenk/airlie/actions/runs/7"
F = "airlie.a.x_f__mutmut_*"
SIG = f"mutation:{F}"


@pytest.fixture(autouse=True)
def _no_mutation_harvest() -> None:
    """The conftest keeps every other unit test from asking GitHub for the nightly; these tests ask a fake."""


def survivor(n: int, function: str = F) -> Survivor:
    mutant = function.removesuffix("*") + str(n)
    return Survivor(mutant, "survived", function, f"-    return a + {n}\n+    return a - {n}")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeGh:
    """`gh run list` answers `runs`; `gh run download` writes `files` (or raises `download_error`)."""

    def __init__(self, runs: list[dict[str, object]], files: dict[str, object] | None = None) -> None:
        self.runs = runs
        self.files = files if files is not None else {"status": "ran", "survivors": [_as_dict(survivor(1))]}
        self.download_error: Exception | None = None
        self.asked: list[list[str]] = []

    async def __call__(self, args: list[str]) -> str:
        self.asked.append(args)
        if args[1:3] == ["run", "list"]:
            return json.dumps(self.runs)
        assert args[1:3] == ["run", "download"]
        if self.download_error is not None:
            raise self.download_error
        out = Path(args[args.index("--dir") + 1])
        (out / "survivors.json").write_text(json.dumps(self.files))
        return ""

    def kinds(self) -> list[str]:
        return [a[2] for a in self.asked]


def _as_dict(s: Survivor) -> dict[str, str]:
    return {"mutant": s.mutant, "status": s.status, "function": s.function, "diff": s.diff}


def run(n: int, conclusion: str = "failure") -> dict[str, object]:
    return {
        "databaseId": n,
        "url": f"https://github.com/weyenk/airlie/actions/runs/{n}",
        "conclusion": conclusion,
    }


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(fixes, "home_checkout", lambda: tmp_path)
    monkeypatch.setattr(fixes, "home_repo", lambda cfg, checkout: HOME)
    return tmp_path


def mutation_fixes(store: BoardStore) -> list[str]:
    return [i.key for i in store.list(level="story") if mutation_cards.MUTATION in i.labels]


async def test_survivors_of_one_function_file_one_todo_card_with_its_labels(
    tmp_path: Path, home: Path
) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    gh = FakeGh([run(7)], {"status": "ran", "survivors": [_as_dict(survivor(1)), _as_dict(survivor(2))]})
    lines = await mutation_cards.harvest(cfg_for(tmp_path), store, gh=gh, now=Clock())
    [key] = mutation_fixes(store)
    card = store.get(key)
    assert list(card.labels) == ["fix", f"repo:{HOME}", f"signature:{SIG}", "mutation"]
    assert card.status == "todo" and card.created_by == "line"
    assert card.title == "Mutation: tests miss 2 mutant(s) of airlie.a.x_f"
    assert lines == [f"{key}: mutation card filed for airlie.a.x_f (2 mutants, nightly run 7)"]
    assert gh.asked[0] == [
        "gh", "run", "list", "--workflow", "mutation-nightly.yml", "--branch", "main",
        "--status", "completed", "--limit", "1", "--json", "databaseId,url,conclusion",
    ]  # fmt: skip
    assert gh.asked[1][:6] == ["gh", "run", "download", "7", "--name", "mutation-survivors"]
    state = json.loads((tmp_path / "state" / "mutation-harvest.json").read_text())
    assert state["run_id"] == 7 and state["reported"] == {SIG: [survivor(1).mutant, survivor(2).mutant]}
    events = (tmp_path / "state" / "line" / "events.jsonl").read_text().splitlines()
    [filed] = [json.loads(e) for e in events if '"mutation_cards_filed"' in e]
    assert (filed["run"], filed["created"], filed["commented"]) == (7, [key], [])


def test_survivors_of_two_functions_are_two_cards(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    other = "airlie.b.xǁCǁm__mutmut_*"
    reported: dict[str, list[str]] = {}
    lines = mutation_cards.file_mutation_cards(
        store, [survivor(1), survivor(1, other)], home=None, run_url=RUN_URL, reported=reported
    )
    assert len(lines) == 2 and len(mutation_fixes(store)) == 2
    titles = sorted(store.get(k).title for k in mutation_fixes(store))
    assert titles == [
        "Mutation: tests miss 1 mutant(s) of airlie.a.x_f",
        "Mutation: tests miss 1 mutant(s) of airlie.b.xǁCǁm",
    ]
    assert all(f"repo:{HOME}" not in store.get(k).labels for k in mutation_fixes(store))  # no home: no label


async def test_an_open_card_is_commented_only_when_its_mutants_change(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    reported: dict[str, list[str]] = {}
    mutation_cards.file_mutation_cards(store, [survivor(1)], home=HOME, run_url=RUN_URL, reported=reported)
    [key] = mutation_fixes(store)
    store.update(key, status="approved")
    # the same mutant the next night: nothing to say
    same = mutation_cards.file_mutation_cards(
        store, [survivor(1)], home=HOME, run_url=RUN_URL, reported=reported
    )
    assert same == [] and not store.get(key).comments
    # a second mutant of the function survives too: one comment on the open card, no second card
    more = mutation_cards.file_mutation_cards(
        store, [survivor(2), survivor(1)], home=HOME, run_url=RUN_URL, reported=reported
    )
    assert more == [f"{key}: mutation card updated for airlie.a.x_f (2 mutants)"]
    assert mutation_fixes(store) == [key]
    [comment] = store.get(key).comments
    assert comment.author == fixes.FIX_AUTHOR
    assert survivor(2).mutant in comment.text and survivor(2).diff in comment.text and RUN_URL in comment.text
    assert reported[SIG] == [survivor(1).mutant, survivor(2).mutant]


async def test_a_done_card_does_not_stop_a_new_one(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    reported: dict[str, list[str]] = {}
    mutation_cards.file_mutation_cards(store, [survivor(1)], home=HOME, run_url=RUN_URL, reported=reported)
    [first] = mutation_fixes(store)
    store.update(first, status="done")
    lines = mutation_cards.file_mutation_cards(
        store, [survivor(1)], home=HOME, run_url=RUN_URL, reported=reported
    )
    assert len(lines) == 1 and len(mutation_fixes(store)) == 2


async def test_harvest_asks_github_at_most_once_an_hour(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    cfg, clock, gh = cfg_for(tmp_path), Clock(), FakeGh([run(7)])
    await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    clock.t += 59 * 60
    assert await mutation_cards.harvest(cfg, store, gh=gh, now=clock) == []
    assert gh.kinds() == ["list", "download"]  # the second call asked nothing
    clock.t += 2 * 60
    await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    assert gh.kinds() == ["list", "download", "list"]


async def test_a_harvest_under_way_elsewhere_is_left_to_it(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    gh = FakeGh([run(7)])
    (tmp_path / "state").mkdir()
    with (tmp_path / "state" / mutation_cards.LOCK_FILE).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another beat is harvesting
        assert await mutation_cards.harvest(cfg_for(tmp_path), store, gh=gh, now=Clock()) == []
    assert gh.asked == [] and mutation_fixes(store) == []


async def test_the_look_is_claimed_on_disk_before_github_is_asked(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    clock, gh = Clock(), FakeGh([run(7)])
    seen: list[object] = []
    answer = gh.__call__

    async def claimed_first(args: list[str]) -> str:
        if not seen:
            state = json.loads((tmp_path / "state" / "mutation-harvest.json").read_text())
            seen.append(state["checked_at"])
        return await answer(args)

    await mutation_cards.harvest(cfg_for(tmp_path), store, gh=claimed_first, now=clock)
    assert seen == [clock.t] and gh.kinds() == ["list", "download"]


async def test_a_run_already_harvested_is_not_downloaded_again(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    cfg, clock, gh = cfg_for(tmp_path), Clock(), FakeGh([run(7)])
    await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    clock.t += mutation_cards.CHECK_EVERY_S
    assert await mutation_cards.harvest(cfg, store, gh=gh, now=clock) == []
    assert gh.kinds() == ["list", "download", "list"]
    assert len(mutation_fixes(store)) == 1


async def test_a_download_that_fails_is_retried_next_hour(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    cfg, clock, gh = cfg_for(tmp_path), Clock(), FakeGh([run(7)])
    gh.download_error = RuntimeError("gh: no network")
    lines = await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    assert lines == ["nightly mutation run 7: survivors not read (RuntimeError: gh: no network)"]
    state = json.loads((tmp_path / "state" / "mutation-harvest.json").read_text())
    assert state["run_id"] is None and state["checked_at"] == clock.t
    clock.t += 30 * 60
    assert await mutation_cards.harvest(cfg, store, gh=gh, now=clock) == []  # the hour holds after a failure
    gh.download_error = None
    clock.t += 31 * 60
    await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    assert gh.kinds() == ["list", "download", "list", "download"] and len(mutation_fixes(store)) == 1


@pytest.mark.parametrize("absent", [False, True], ids=["could_not_run", "no_artifact"])
async def test_a_nightly_that_could_not_run_files_nothing_and_says_so_once(
    tmp_path: Path, home: Path, absent: bool
) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    cfg, clock = cfg_for(tmp_path), Clock()
    gh = FakeGh([run(8, "failure")], {"status": "could_not_run", "survivors": []})
    if absent:
        gh.download_error = RuntimeError("`gh run download` failed: no valid artifacts found to download")
    lines = await mutation_cards.harvest(cfg, store, gh=gh, now=clock)
    url = "https://github.com/weyenk/airlie/actions/runs/8"
    assert lines == [f"nightly mutation run 8 could not run: no cards ({url})"]
    assert mutation_fixes(store) == []
    clock.t += mutation_cards.CHECK_EVERY_S
    assert await mutation_cards.harvest(cfg, store, gh=gh, now=clock) == []  # said once
    state = json.loads((tmp_path / "state" / "mutation-harvest.json").read_text())
    assert state["run_id"] == 8 and state["said"] == [8]


async def test_mutation_cards_off_asks_nothing(tmp_path: Path, home: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    cfg = cfg_for(tmp_path, extra={"line": {"mutation_cards": False}})
    gh = FakeGh([run(7)])
    assert await mutation_cards.harvest(cfg, store, gh=gh, now=Clock()) == []
    assert gh.asked == [] and cfg_for(tmp_path).line.mutation_cards is True


async def test_no_home_checkout_asks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fixes, "home_checkout", lambda: None)
    gh = FakeGh([run(7)])
    assert (
        await mutation_cards.harvest(cfg_for(tmp_path), BoardStore(tmp_path / "b.sqlite"), gh=gh, now=Clock())
        == []
    )
    assert gh.asked == []


async def test_bookkeep_goes_on_when_the_harvest_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fixes, "home_checkout", lambda: None)
    store = BoardStore(tmp_path / "board.sqlite")
    story = store.create(level="story", title="A story")
    store.update(story.key, status="blocked")
    monkeypatch.setattr(fixes, "transient_retry", lambda *a, **k: True)
    seen: dict[str, object] = {}

    async def broken(cfg: object, s: BoardStore, **kw: object) -> list[str]:
        seen.update(kw)
        raise OSError("disk full")

    monkeypatch.setattr(mutation_cards, "harvest", broken)

    async def gh(args: list[str]) -> str:
        return ""

    lines = await fixes.bookkeep(cfg_for(tmp_path), store, serve_commit=None, gh=gh, now=lambda: 5.0)
    assert lines == [f"{story.key}: transient block, retried", "mutation cards failed (OSError: disk full)"]
    assert seen["gh"] is gh and callable(seen["now"])


def test_the_card_body_carries_each_diff_and_the_equivalent_way_out() -> None:
    title, body = mutation_cards.render_mutation_card(F, [survivor(1), survivor(2)], RUN_URL)
    assert title == "Mutation: tests miss 2 mutant(s) of airlie.a.x_f"
    for section in ("## Why", "## Evidence", "## Reproduce", "## Acceptance criteria", "## Out of scope"):
        assert section in body
    for s in (survivor(1), survivor(2)):
        assert f"```diff\n{s.diff}\n```" in body and s.mutant in body
    assert "uv run python scripts/mutate_changed.py --scope enrolled --list" in body
    assert f"uv run mutmut run '{F}'" in body
    assert "a unit test fails" in body and "not the mutated literal" in body
    assert "changes to the code under test" in body
    # the form pyproject.toml accepts: an inline table in `equivalent = [...]`, not an array-of-tables header
    assert '`{ mutant = "...", line = "...", reason = "..." }`' in body and "`equivalent = [...]`" in body
    assert "[[tool.airlie.mutation.equivalent]]" not in body
    assert RUN_URL in body


def test_read_survivors_gives_the_status_and_the_survivors(tmp_path: Path) -> None:
    path = tmp_path / "survivors.json"
    path.write_text(json.dumps({"status": "ran", "survivors": [_as_dict(survivor(1))]}))
    assert mutation_cards.read_survivors(path) == ("ran", [survivor(1)])
    path.write_text(json.dumps({"status": "maybe", "survivors": []}))
    with pytest.raises(ValueError):
        mutation_cards.read_survivors(path)
