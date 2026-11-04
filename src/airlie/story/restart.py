"""A story started over: from scratch on today's trunk (`FRESH`), or from a later stage with the stages before
it kept, moved onto the trunk as it is now. What the old run published and built is set aside first: its open
PRs are closed with a word and their branches deleted on the remote, its kept worktrees and the local branches
of the layers that go are removed, and its record is archived under archive/stories/ in the state directory
(moved whole for a fresh start, copied for a later one, whose record stays in place with the dropped stages
cleared and the trunk fetched now as its base). Then the card gets a note and goes back to approved, as the
person who asked moved it, and the line's next beat takes it.

A story running now, one with a merged PR (what is merged stays merged), and one whose kept layers do not
replay on the trunk (a conflict, or the story's gate red there, as `task story:restack` holds them) are
refused before anything is changed. Each step skips what an earlier, interrupted run of it already did (a
closed PR, a branch or a worktree that is gone), so running it again finishes the job."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from airlie.obs import EventSink
from airlie.story.board import Board, Card, note
from airlie.story.cleanup import prune_stale_stacks
from airlie.story.local_board import LocalBoard
from airlie.story.model import PLANNED, StoryRunning, StorySetupError
from airlie.story.publish import Publisher, PublishError
from airlie.story.restack import RestackError
from airlie.story.state import Lane, StoryState, lane_lock, lock_held
from airlie.teams.worktree import delete_branch, move_branches, remove_worktree, rev_parse

FRESH = "refine"
"""Starting over from the refine is starting from scratch: nothing of the old run is kept, so the next run
resolves its base from the trunk as it is then and refines the card as it reads then."""

POINTS = (FRESH, "layers", "amigos", "packages")
"""Where a story can start over from, in the order it meets them (the workflow screen's steps), and
`layer-NN` (one built layer: the layers below it are kept). Each keeps every stage before it."""

_LAYER = re.compile(r"layer-(\d\d)")

_KEEPS = {
    "layers": "the story as refine wrote it, before the meeting, is kept",
    "amigos": "the story and its layer plan as they stood before the meeting are kept",
    "packages": "the agreed plan is kept",
}
"""What a note says each later point keeps (a layer's is worded where it is made)."""

Replay = Callable[[Card, str, Mapping[str, str]], Awaitable[dict[str, str]]]
"""The kept layers ({"01": commit, ...}) replayed on a commit of the trunk, each held to the story's gate
there, with nothing moved: their new commits, or a RestackError naming the layer
(`StoryPipeline.replay_layers`)."""


class RestartError(Exception):
    """The story cannot be started over from there; nothing was changed."""


class RestartIncomplete(RuntimeError):
    """A step failed after the first one that changes anything (closing the PRs): the restart is part-way,
    the card waits out of the line, and running the restart again finishes it. The cause is chained."""


@dataclass(frozen=True)
class Restarted:
    """What starting a story over did: the PRs it closed, the local branches and worktrees it removed, where
    the old record is archived (None when the story had none) and the note on the card (None for none)."""

    key: str
    point: str
    closed: list[int]
    branches: list[str]
    worktrees: list[str]
    archive: str | None
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _running_now(key: str, stories_dir: Path, running: Callable[[str], bool] | None) -> str | None:
    """Why the story of `key` is running now, or None: its serve job runs (`running`, by job id), or a lane
    of the line holds its lock and names it (`line/lanes/<lane>.json`, believed only while the lock is held,
    as the line's row believes it)."""
    if running is not None and running(f"story-{key}"):
        return f"its job story-{key} is running"
    lanes: tuple[Lane, ...] = ("build", "plan")
    for lane in lanes:
        record = stories_dir.parent / "line" / "lanes" / f"{lane}.json"
        try:
            named = json.loads(record.read_text()).get("key")
        except (OSError, ValueError, AttributeError):
            continue
        if named == key and lock_held(stories_dir, lane):
            return f"the {lane} lane is running it"
    return None


def _saved(path: Path) -> dict[str, Any] | None:
    """The story's state.json as written, or None when there is none; an empty record for one that cannot
    be read (a fresh start sets it aside all the same)."""
    f = path / "state.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _checked(key: str, point: str, saved: dict[str, Any] | None) -> int | None:
    """Refuse a point that is none, or that the story has not reached; the layer number of a `layer-NN`."""
    nn = _LAYER.fullmatch(point)
    if point not in POINTS and nn is None:
        raise RestartError(
            f"start over from one of {', '.join(POINTS)} or layer-NN (layer-02), not {point!r}"
        )
    if point == FRESH:
        return None
    if not saved:
        raise RestartError(f"story {key} has not started (it has no record to keep): start it fresh instead")
    if saved.get("story") is None:
        raise RestartError(f"story {key} has not been refined yet: start it fresh instead")
    if point == "amigos" and saved.get("layers") is None:
        raise RestartError(f"story {key} has no layer plan yet: start it over from layers instead")
    if point in ("layers", "amigos") and saved.get("agreement") is not None:
        wanted = ("refined_story",) if point == "layers" else ("refined_story", "refined_layers")
        if any(saved.get(k) is None for k in wanted):
            raise RestartError(
                f"story {key} was recorded before airlie kept its story and layer plan as they stood "
                "before the meeting, so they cannot be restored: start it over from refine instead"
            )
    if (point == "packages" or nn is not None) and saved.get("agreement") is None:
        raise RestartError(f"story {key} has not been planned yet: start it over from an earlier stage")
    if nn is None:
        return None
    n = int(nn.group(1))
    built = saved.get("built") or {}
    if not 1 <= n <= len(saved.get("layers") or []):
        raise RestartError(f"story {key} has no layer {n:02d}")
    missing = [f"{i:02d}" for i in range(1, n) if f"{i:02d}" not in built]
    if missing:
        raise RestartError(f"story {key} has not built layer {missing[0]} yet: start it over from that one")
    return n


def _dropped(saved: dict[str, Any], point: str, layer: int | None) -> list[str]:
    """The local branches of the layers that go: every built layer's from `layer` up (all of them, but for a
    `layer-NN`), and the stopped attempt's."""
    built = saved.get("built") or {}
    out = [str(b["branch"]) for nn, b in sorted(built.items()) if layer is None or int(nn) >= layer]
    attempt = saved.get("attempt") or {}
    if attempt.get("branch"):
        out.append(str(attempt["branch"]))
    if point == FRESH:  # a fresh start drops the PRs' branches too, whatever the record says was built
        out += [str(p["branch"]) for p in saved.get("prs") or [] if p.get("branch")]
    return list(dict.fromkeys(out))


def _kept(saved: dict[str, Any], layer: int | None) -> dict[str, str]:
    """The built layers a `layer-NN` keeps, by number, at their recorded commits (none for other points)."""
    built = saved.get("built") or {}
    return {nn: str(b["sha"]) for nn, b in sorted(built.items()) if layer is not None and int(nn) < layer}


def _worktrees(repo: Path, branches: list[str], inside: Path, attempt: str | None) -> list[str]:
    """The worktrees of `repo` the story leaves: on one of `branches`, under its own directory (an
    interrupted publish's, restack's or meeting's), or its stopped attempt's."""
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True, text=True
    ).stdout
    found: list[str] = []
    for block in out.split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path = fields.get("worktree")
        if path is None or Path(path).resolve() == repo.resolve():
            continue
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        under = Path(path).resolve().is_relative_to(inside.resolve())
        if branch in branches or under or path == attempt:
            found.append(path)
    return found


def _archive(stories_dir: Path, key: str, point: str) -> Path:
    """A new directory name under archive/stories/ in the state directory, beside stories/."""
    root = stories_dir.parent / "archive" / "stories"
    root.mkdir(parents=True, exist_ok=True)
    tag = "fresh" if point == FRESH else f"from-{point}"
    stem = f"{key}-{tag}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = root / stem, 1
    while dest.exists():
        n += 1
        dest = root / f"{stem}-{n}"
    return dest


def _cleared(
    path: Path,
    card: Card,
    saved: dict[str, Any],
    point: str,
    layer: int | None,
    base: dict[str, str] | None,
    moved: Mapping[str, str],
) -> None:
    """The record kept in place, with the stages from `point` on cleared (the next run does them again), on
    `base`: the kept layers at their `moved` commits. With no layer kept the test gate is read again at the
    new base; a kept layer passed the story's gate, which the story keeps, as a restack does."""
    st = StoryState.open(path, card, Path(saved["repo"]), github_repo=saved.get("github_repo"))
    st.base = base
    if layer is None or layer == 1:
        st.gate = None
    if point in ("layers", "amigos"):
        st.stage = FRESH  # refined; the layer split and the meeting run again
        # the story (and for the meeting the plan) as they stood before the meeting: the agreement and a
        # reworded criterion had replaced them. A story that never met has them as they are.
        if st.refined_story is not None:
            st.story = st.refined_story
        if point == "layers":
            st.layers = st.refined_layers = None
        elif st.refined_layers is not None:
            st.layers = st.refined_layers
        st.agreement = None
        st.meeting = None
    else:
        st.stage = PLANNED
    if layer is None:
        st.built, st.drafts, st.drafted = {}, {}, False
    else:
        st.built = {nn: b for nn, b in st.built.items() if int(nn) < layer}
        for nn, sha in moved.items():
            st.built[nn]["sha"] = sha
        st.drafts = {nn: d for nn, d in st.drafts.items() if int(nn) < layer}
    st.attempt = None
    st.prs = []
    st.blocked = None
    st.save()


async def _nothing() -> None:
    return None


async def _hold(board: Board, card: Card, by: str) -> Callable[[], Awaitable[None]]:
    """Take the card out of the line while the story is set aside, so no beat picks it between the running
    check and the end (the replay can hold the gate for minutes): todo on airlie's own board (as `by` moved
    it), blocked on a board that says where a card is (`column_of`: Jira's) when it is in intake, the status
    the tick takes. Returns what puts it back where it was, for a refusal."""
    if isinstance(board, LocalBoard):
        before = board.status_of(card)
        if before == "todo":
            return _nothing
        board.store.update(card.key, status="todo", by=by)

        async def local_back() -> None:
            board.store.update(card.key, status=before, by=by)  # type: ignore[arg-type]

        return local_back
    column_of = getattr(board, "column_of", None)
    if column_of is None or await column_of(card) != "intake":
        return _nothing
    await board.move(card, "blocked")

    async def back() -> None:
        await board.move(card, "intake")

    return back


async def _approve(board: Board, card: Card, by: str) -> None:
    """Back to approved on airlie's own board, moved by `by`; to intake (To Do) on any other, which the tick
    takes from."""
    if isinstance(board, LocalBoard):
        board.store.update(card.key, status="approved", by=by)
    else:
        await board.move(card, "intake")


async def _trunk_sha(publisher: Publisher, repo: Path, trunk: str) -> str | None:
    """The remote's trunk, fetched now, for a fresh start's note; as it was last fetched when the fetch fails
    (the next run fetches it again either way), and None when there is none."""
    with contextlib.suppress(PublishError, StorySetupError):
        await publisher.fetch(repo, trunk)
    return rev_parse(repo, f"refs/remotes/{publisher.remote}/{trunk}")


async def _fetched(publisher: Publisher, repo: Path, trunk: str) -> str:
    """The remote's trunk, fetched now: what a later point moves the story onto. A fetch that fails is a
    RestartError, and nothing has changed."""
    try:
        await publisher.fetch(repo, trunk)
    except (PublishError, StorySetupError) as e:
        failure = str(e)
    else:
        sha = rev_parse(repo, f"refs/remotes/{publisher.remote}/{trunk}")
        if sha is not None:
            return sha
        failure = "it is not there after the fetch"
    raise RestartError(f"the trunk {trunk} could not be fetched from {publisher.remote}: {failure}")


def _replayed_already(repo: Path, branches: Mapping[str, str], sha: str) -> dict[str, str] | None:
    """The kept layers' commits when an earlier run of this restart moved their branches onto `sha` and then
    stopped before it recorded them: each branch one commit on the one below it (the bottom one on `sha`).
    None when they are not."""
    found: dict[str, str] = {}
    below = sha
    for nn, branch in branches.items():
        tip = rev_parse(repo, f"refs/heads/{branch}")
        if tip is None or rev_parse(repo, f"{tip}^") != below:
            return None
        found[nn] = below = tip
    return found


async def _onto(
    card: Card,
    saved: dict[str, Any],
    kept: dict[str, str],
    sha: str,
    replay: Replay | None,
    stories_dir: Path,
) -> dict[str, str]:
    """The kept layers' new commits on `sha` (the trunk fetched now), found before anything changes: each kept
    branch must be where the record says (or where an earlier, interrupted run of this restart moved it), and
    the layers must replay there and pass the story's gate, in the build lane (as a restack replays: another
    story building is a refusal, not a wait). Nothing to replay when no layer is kept or the story is on `sha`
    already."""
    if not kept or (saved.get("base") or {}).get("sha") == sha:
        return {}
    repo, built = Path(saved["repo"]), saved.get("built") or {}
    branches = {nn: str(built[nn]["branch"]) for nn in kept}
    moved = [nn for nn, at in kept.items() if rev_parse(repo, f"refs/heads/{branches[nn]}") != at]
    if moved:
        again = _replayed_already(repo, branches, sha)
        if again is not None:
            return again
        nn = moved[0]
        raise RestartError(
            f"layer {nn}'s branch {branches[nn]} is not where the story left it ({kept[nn][:12]}), so "
            "nothing was changed: put it back, or start the story over from an earlier step"
        )
    if replay is None:
        raise RestartError("the kept layers cannot be replayed here: no replay was given")
    with lane_lock(stories_dir, "build") as held:
        if not held:
            raise RestartError(
                "the build lane is busy with another story, and the kept layers are replayed in it: start "
                "this one over when it ends"
            )
        try:
            return await replay(card, sha, kept)
        except RestackError as e:
            raise RestartError(
                f"{e}; nothing was changed. Start the story over from an earlier step (layer-{min(kept)} or "
                "packages) instead"
            ) from None


async def restart(
    card: Card,
    point: str = FRESH,
    *,
    board: Board,
    publisher: Publisher,
    stories_dir: Path,
    trunk: str,
    by: str = "person",
    running: Callable[[str], bool] | None = None,
    replay: Replay | None = None,
) -> Restarted:
    """Start the story of `card` over from `point` (`FRESH`, a later one of `POINTS`, or `layer-NN`), as `by`
    asked. In order: refuse it, changing nothing, when it is running now (StoryRunning: its serve job, by
    `running`, or a lane of the line) or a PR of it is merged, or `point` is none or not reached
    (RestartError); for a later point, fetch the trunk and replay the layers a `layer-NN` keeps on it with
    `replay` (a conflict or a red gate is a RestartError, and nothing has changed); close its open PRs (as the
    story published them: as the bot or as you) with a word and delete their remote branches; remove its kept
    worktrees, then the local branches of the layers that go, and gh stack's record of their stack; move the
    kept layers' branches onto their replayed commits; archive its record (moved whole for a fresh start;
    copied for a later one, whose record is kept with the stages from `point` on cleared and the fetched
    trunk as its base); a note on the card; the card to approved. A story with no record is only moved to
    approved.

    From the running check to the end the card waits out of the line (`_hold`), and a refusal puts it back.
    A step that fails once the first PR is closed is a RestartIncomplete (its cause chained): the card stays
    out of the line, and running the restart again finishes the job."""
    key = card.key
    path = stories_dir / key
    saved = _saved(path)
    layer = _checked(key, point, saved)
    why = _running_now(key, stories_dir, running)
    if why is not None:
        raise StoryRunning(f"story {key} is running now ({why}): start it over once it has stopped")
    if saved is None and not path.exists():
        await _approve(board, card, by)
        return Restarted(key, point, [], [], [], None, None)
    saved = saved or {}
    repo = Path(saved["repo"]) if isinstance(saved.get("repo"), str) else None
    prs = [int(p["number"]) for p in saved.get("prs") or []]
    states: dict[int, str] = {}
    kept = _kept(saved, layer)
    moved: dict[str, str] = {}
    back = await _hold(board, card, by)
    try:
        if repo is not None and prs:
            publisher.prepare(repo, expected=saved.get("github_repo"))
            for n in prs:
                states[n] = await publisher.pr_state(repo, n)
            merged = [n for n, s in states.items() if s == "MERGED"]
            if merged:
                raise RestartError(
                    f"story {key}'s PR #{merged[0]} is merged, and what is merged stays: make a new story "
                    "for what is left instead"
                )
        if point == FRESH:
            sha = await _trunk_sha(publisher, repo, trunk) if repo is not None else None
            said = f"started fresh from {trunk}"
        else:
            if repo is None:
                raise RestartError(f"story {key}'s record names no repository: start it fresh instead")
            sha = await _fetched(publisher, repo, trunk)
            moved = await _onto(card, saved, kept, sha, replay, stories_dir)
            said = f"started over from {point}"
    except BaseException:
        with contextlib.suppress(Exception):  # the refusal is what the person needs to read
            await back()
        raise
    try:
        return await _set_aside(
            card, point, layer, saved, repo, prs, states, kept, moved, sha, said,
            board=board, publisher=publisher, stories_dir=stories_dir, trunk=trunk, by=by,
        )  # fmt: skip
    except Exception as e:
        raise RestartIncomplete(
            f"{e}; the restart stopped part-way and the card waits out of the line: run it again to finish"
        ) from e


async def _set_aside(
    card: Card,
    point: str,
    layer: int | None,
    saved: dict[str, Any],
    repo: Path | None,
    prs: list[int],
    states: Mapping[int, str],
    kept: Mapping[str, str],
    moved: Mapping[str, str],
    sha: str | None,
    said: str,
    *,
    board: Board,
    publisher: Publisher,
    stories_dir: Path,
    trunk: str,
    by: str,
) -> Restarted:
    """restart's steps that change things, once nothing refused it: close the open PRs, remove the worktrees
    and the branches of what goes, move the kept layers' branches onto their replayed commits, archive the
    record (and clear it in place for a later point), the note, the card to approved."""
    key, path = card.key, stories_dir / card.key
    closed: list[int] = []
    for n in prs:
        if states.get(n) == "OPEN" and repo is not None:
            await publisher.close(repo, n, f"{said}; this PR is superseded")
            closed.append(n)
    dropped = _dropped(saved, point, layer)
    removed: list[str] = []
    deleted: list[str] = []
    if repo is not None and (repo / ".git").exists():
        attempt = (saved.get("attempt") or {}).get("worktree")
        for wt in _worktrees(repo, dropped, path, attempt):
            remove_worktree(repo, Path(wt))
            removed.append(wt)
        if attempt and Path(attempt).exists() and attempt not in removed:
            shutil.rmtree(attempt, ignore_errors=True)  # a worktree git no longer knows: its files only
        for branch in dropped:
            if rev_parse(repo, f"refs/heads/{branch}") is not None:
                delete_branch(repo, branch)
                deleted.append(branch)
        # the remote's branches of the closed PRs are gone: a stale tracking ref would refuse the next push
        for branch in dict.fromkeys([*dropped, *(str(p.get("branch")) for p in saved.get("prs") or [])]):
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "-d", f"refs/remotes/{publisher.remote}/{branch}"],
                capture_output=True,
            )
        prune_stale_stacks(repo)
        built = saved.get("built") or {}
        # from where they are, together; one an interrupted run of this restart already moved stays put
        moves: dict[str, tuple[str, str]] = {}
        for nn, new in moved.items():
            branch = str(built[nn]["branch"])
            now = rev_parse(repo, f"refs/heads/{branch}")
            if now != new:
                moves[branch] = (str(now), new)
        move_branches(repo, moves)
    dest = _archive(stories_dir, key, point)
    done = {"from": point, "closed": closed, "branches": deleted, "worktrees": removed, "archive": str(dest)}
    at = f" @ {sha[:12]}" if sha else ""
    if point == FRESH:
        EventSink(path / "events.jsonl", key).emit("restarted", by=by, **done)
        path.rename(dest)
        text = f"{said}{at}; the old run is archived at {dest}"
    else:
        shutil.copytree(path, dest, symlinks=True)
        # a story stacked on another's branch, with no layer kept, finds its base again at its next run
        stacked = bool((saved.get("base") or {}).get("story")) and not kept
        base = None if stacked else {"ref": f"{publisher.remote}/{trunk}", "sha": str(sha)}
        _cleared(path, card, saved, point, layer, base, moved)
        EventSink(path / "events.jsonl", key).emit("restarted", by=by, base=base, moved=moved, **done)
        if layer is None or layer == 1:
            keeps = _KEEPS["packages" if layer == 1 else point]
        else:
            keeps = f"the agreed plan and layers 01 to {layer - 1:02d}, replayed on it, are kept"
        text = f"{said} on {publisher.remote}/{trunk}{at}: {keeps}; the old run is archived at {dest}"
    await board.comment(card, note(text))
    await _approve(board, card, by)
    return Restarted(key, point, closed, deleted, removed, str(dest), text)
