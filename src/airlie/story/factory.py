"""Wiring for the story front doors (`task story`, `task story:tick`, POST /v1/airlie/stories): where a story
is built (an issue's repo: label, a written request's repository), the board, the studio over the HTTP engine,
gh, the pipeline and the one-story lock."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, NamedTuple

from airlie.board import BoardStore
from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.story.board import Board, BoardError, Card, Column, NullBoard, note, text_card
from airlie.story.checkout import check_checkout, find_checkout, held_label
from airlie.story.gate import missing_gate, read_gate
from airlie.story.github_app import GitHubApp, GitHubAppError, masked, ping
from airlie.story.jira import JiraBoard, jira_board
from airlie.story.local_board import LocalBoard
from airlie.story.model import NoTestGate, RepositoryError, StoryRunning, StorySetupError
from airlie.story.pipeline import StoryPipeline, _cap, _redact_urls
from airlie.story.publish import Checked, Publisher
from airlie.story.state import STAGES, StoryState, story_lock
from airlie.teams.worktree import remote_url, rev_parse
from airlie.topology.studio import StudioOrchestrator, specialists


class StoryBusy(RuntimeError):
    """Another story holds the lock: stories are built one at a time."""


PUBLISHED = STAGES[STAGES.index("publish") :]
"""The stages of a story whose PRs are open: nothing is built or published again."""


def stories_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir / "stories"


def story_config(cfg: AirlieConfig) -> AirlieConfig:
    """The config with story.topology's profile applied: story runs need the studio's roles and engines."""
    if cfg.source_path is None:
        return cfg
    return load_config(cfg.source_path, topology=cfg.story.topology)


def _unusable_ref(name: str) -> bool:
    """A trunk or remote name that is empty or that git and gh would read as an option: gh-stack passes
    `--base` on to git as a positional argument, and the pipeline passes both to `git fetch`."""
    return not name.strip() or name.startswith("-")


def story_problems(cfg: AirlieConfig) -> list[str]:
    """What a story needs that this config (after story_config) lacks."""
    s = cfg.story
    problems: list[str] = []
    if cfg.topology.kind != "studio":
        problems.append(
            f"story runs need the studio topology; story.topology '{s.topology}' loads '{cfg.topology.kind}'"
        )
    roles = specialists(cfg)
    for label, role in (
        ("story.product_role", s.product_role),
        ("story.architect_role", s.architect_role),
        ("story.build_role", s.build_role),
    ):
        if role not in roles:
            problems.append(f"{label} names '{role}', which is not a specialist (no summary: in its prompt)")
    for label, name in (("story.trunk", s.trunk), ("story.remote", s.remote)):
        if _unusable_ref(name):
            problems.append(f'{label} must not be empty or start with "-" (git would read it as an option)')
    return problems


class Place(NamedTuple):
    """Where a story is built: its checkout, in one spelling, and the "owner/name" its issue's repo: label
    names (None for a written request, which has no label: its remote as written is its pin)."""

    repo: Path
    github_repo: str | None


def git_repo(raw: Path) -> Path:
    """`raw`, ~ expanded and resolved (StoryState.open compares the spelling verbatim, so every caller gets
    this one); a StorySetupError when it is not a git repository."""
    path = raw.expanduser().resolve()
    if not (path / ".git").exists():
        raise StorySetupError(f"{path} is not a git repository")
    return path


def written_repo(cfg: AirlieConfig, repo: Path | None) -> Path:
    """The repository a written request is built in: `repo` (--repo), else story.repo."""
    raw = repo or cfg.story.repo
    if raw is None:
        raise StorySetupError(
            "no repository: pass --repo or set story.repo (a written request names none; an issue's "
            "repository is its repo:<owner>/<name> label)"
        )
    return git_repo(raw)


def _recorded(cfg: AirlieConfig, card: Card) -> dict[str, str]:
    """The text fields of the story of `card` as its state.json says (stage, repo, github_repo, …); none when
    there is no state or it cannot be read (the run then starts the story, or says what is wrong with the
    file)."""
    try:
        saved = json.loads((stories_dir(cfg) / card.key / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in saved.items() if isinstance(v, str)} if isinstance(saved, dict) else {}


def place(cfg: AirlieConfig, card: Card, repo: Path | None = None) -> Place:
    """Where the story of `card` is built. A written request: in `repo` (a person's --repo), else story.repo.
    A Jira issue: in the repository its one repo:<owner>/<name> label names (the one its story started with,
    if it did), which is the checkout of that name in story.repos_root, or `repo` in its place; either way its
    remote, as written, must name that repository. A RepositoryError is for the issue (`placed` tells it); a
    `repo` that is not a git repository is a StorySetupError, for the person who named it.

    Once a story has published, nothing is built or published again: it gets the checkout and the repository
    its state recorded, and neither its label nor that checkout is looked at, so a label tidied or a clone
    moved since still lets it post its PR links and reach review."""
    if not card.ref:  # a written request has no labels
        return Place(written_repo(cfg, repo), None)
    saved = _recorded(cfg, card)
    if saved.get("stage") in PUBLISHED and "repo" in saved:
        return Place(Path(saved["repo"]), saved.get("github_repo"))
    state = stories_dir(cfg) / card.key / "state.json"
    label = held_label(card.labels, saved.get("github_repo"), state)
    path = git_repo(repo) if repo is not None else find_checkout(cfg.story.repos_root, label)
    check_checkout(path, cfg.story.remote, label)
    return Place(path.resolve(), label)


async def placed(cfg: AirlieConfig, board: Board, card: Card, repo: Path | None = None) -> Place:
    """place; when the issue's repository cannot be used (RepositoryError) the issue is told why and moved out
    of intake, as tick sets aside an issue whose story cannot be opened, and the error is raised again."""
    try:
        return place(cfg, card, repo)
    except RepositoryError as e:
        await _set_aside(cfg, board, card, e)
        raise


def make_board(cfg: AirlieConfig) -> Board:
    """story.board: local → a LocalBoard over the board in observability.state_dir; jira (the default) → a
    JiraBoard once the Jira site and project resolve (jira_board: the process environment, then the .env
    beside airlie.yaml, then story.jira), else a NullBoard. BoardError names what a half-configured Jira
    lacks."""
    if cfg.story.board == "local":
        state_dir = cfg.observability.state_dir.expanduser()
        return LocalBoard(BoardStore(state_dir / "board.sqlite", prefix=cfg.board.prefix))
    board = jira_board(cfg)
    return board if board is not None else NullBoard(stories_dir(cfg))


async def card_for(cfg: AirlieConfig, ref: str | None, text: str | None) -> tuple[Board, Card]:
    """The board and the card for an issue (fetched now: a bad reference fails here, not in the job) or for
    a written request (a NullBoard)."""
    if ref is not None:  # "" is a reference too, and a bad one: it is not turned into a request
        board = make_board(cfg)
        return board, await board.card(ref)
    if not (text or "").strip():
        raise StorySetupError("the request is empty: say what to build")
    return NullBoard(stories_dir(cfg)), text_card(text or "")


def github_app(cfg: AirlieConfig) -> GitHubApp | None:
    """The app of story.github_app once its app_id is set (the publish steps then act as its bot), else None
    (they act as you, with your gh login and SSH key)."""
    spec = cfg.story.github_app
    return GitHubApp(spec) if spec.app_id is not None else None


async def github_down(cfg: AirlieConfig) -> str | None:
    """Why a story could not reach GitHub now, or None when GitHub answers: as the bot, the app as GitHub sees
    it (GitHubApp.slug: the key, the app's id, GitHub's reply); as you, GitHub's API at all (ping). A story
    started while GitHub is down stops at its first publish step (the bot's identity, or the trunk fetch), and
    its issue goes to blocked."""
    app = github_app(cfg)
    try:
        if app is not None:
            await app.slug()
        else:
            await ping(cfg.story.github_app.api_url)
    except GitHubAppError as e:
        return str(e)
    return None


def _engine(cfg: AirlieConfig) -> Any:
    from airlie.cli import _http_engine  # the CLI owns engine construction

    return _http_engine(cfg, cfg.engine_for_role(cfg.story.build_role))


def make_pipeline(
    cfg: AirlieConfig,
    *,
    board: Board,
    repo: Path,
    test_cmd: str | None = None,
    github_repo: str | None = None,
) -> StoryPipeline:
    engine = _engine(cfg)
    return StoryPipeline(
        cfg,
        board=board,
        studio=lambda: StudioOrchestrator(cfg, engine),
        publisher=Publisher(remote=cfg.story.remote, app=github_app(cfg)),
        repo=repo,
        test_cmd=test_cmd,
        github_repo=github_repo,
    )


async def run_one(
    cfg: AirlieConfig, *, board: Board, card: Card, repo: Path | None = None, test_cmd: str | None = None
) -> StoryState:
    """Build, or resume, the story of `card`, one story at a time: where `placed` says (`repo` is a person's
    --repo), gated by `test_cmd` (a person's --test-cmd), else by the test gate of the repository's
    AGENTS.md."""
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            raise StoryBusy("another story is in flight (one at a time); try again when it finishes")
        where = await placed(cfg, board, card, repo)
        pipe = make_pipeline(
            cfg, board=board, repo=where.repo, test_cmd=test_cmd, github_repo=where.github_repo
        )
        return await pipe.run(card)


def _why(e: Exception) -> str:
    """What a board said when it refused a call, masked and cut short, for the line a tick logs."""
    return _cap(_redact_urls(f"{type(e).__name__}: {e}"), 200)


async def _note_and_move(board: Board, card: Card, text: str, column: Column) -> str:
    """A note on the issue and then a move to `column`, as two calls, as the pipeline's stops make them: a
    note the board refuses does not keep the issue where it is, and the other way round. Neither raises (a
    board that fails must not make a tick fail twice); what happened is returned, for the line a tick logs."""
    told: list[str] = []
    try:
        await board.comment(card, note(text))
        told.append("noted on the issue")
    except Exception as err:  # noqa: BLE001
        told.append(f"the note failed ({_why(err)})")
    try:
        await board.move(card, column)
        told.append(f"moved to {column}")
    except Exception as err:  # noqa: BLE001
        told.append(f"the move to {column} failed ({_why(err)})")
    return ", ".join(told)


async def _set_aside(cfg: AirlieConfig, board: Board, card: Card, e: StorySetupError) -> str:
    """The story of `card` cannot be opened (its state is damaged, or was started in another repository) or
    placed (its repo: label, or the checkout it names, cannot be used: RepositoryError), and no later tick
    will change that. The issue would stay at the top of the intake query and every tick would stop on it, so
    nothing behind it would ever be built: it is told why and moved out of intake, to blocked, or to building
    when story.jira.statuses.blocked is off (a board leaves an issue where it is for a column that is off).
    Best effort: a board that fails is named in the line, and the tick still ends."""
    reason = _cap(_redact_urls(str(e)))  # masked, then cut: as every stop reason is
    statuses = cfg.story.jira.statuses
    column: Column = "blocked" if statuses.blocked is not None else "building"
    text = f"cannot be picked up: {reason}\nNeeds a person: fix that, then resume with {card.resume}."
    if column == "building":
        text += (
            f" story.jira.statuses.blocked is off, so the issue was moved to '{statuses.building}' "
            "to keep story:tick from taking it again."
        )
    return (
        f"{card.ref} {card.title}: cannot start ({reason}); {await _note_and_move(board, card, text, column)}"
    )


async def _already_in_review(board: Board, card: Card, st: StoryState) -> str:
    """The story of `card` reached review earlier, and its issue is in intake again (it was put back in To
    Do): a run has nothing to do and nothing moves the issue, so every tick would take it again and nothing
    behind it would ever be built. It is told so and moved to review. Best effort, as _set_aside is."""
    links = " ".join(str(p["url"]) for p in st.prs)
    text = (
        f"this story is already in review{': ' + links if links else ''}; airlie has nothing more to do.\n"
        f"A finished story is not built again; its state is in {st.path}."
    )
    return (
        f"{card.ref} {card.title}: already in review (nothing to do); "
        f"{await _note_and_move(board, card, text, 'review')}"
    )


async def tick(cfg: AirlieConfig) -> str:
    """One scheduler beat: build the top issue of the intake query, unless a story is in flight, the engine
    is down or GitHub does not answer (github_down), in the repository its label names, gated by its
    AGENTS.md. Returns one line for the log. An issue that a run leaves in intake would hold up the ones
    behind it, so it is moved out: one whose story cannot be placed or opened (_set_aside), or whose story has
    finished (_already_in_review)."""
    board = make_board(cfg)
    if isinstance(board, NullBoard):
        return "no board configured (ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY): nothing to pick up"
    with story_lock(stories_dir(cfg)) as held:
        if not held:
            return "busy: a story is in flight"
        try:
            healthy = bool((await _engine(cfg).health()).ok)
        except Exception:  # noqa: BLE001 - any failure to reach the engine means it is down
            healthy = False
        if not healthy:
            return "engine down: start it with `task restart -- --topology studio`"
        down = await github_down(cfg)
        if down is not None:  # an issue taken now would stop at its first publish step: the queue waits
            return " ".join(f"github down: {down}".split())  # GitHub's own message may hold a line break
        card = await board.next_card()
        if card is None:
            return "idle: no issue matches the intake query"
        finished = _recorded(cfg, card).get("stage") == "review"  # then the run below has nothing to do
        try:
            where = place(cfg, card)
            pipe = make_pipeline(
                cfg, board=board, repo=where.repo, test_cmd=None, github_repo=where.github_repo
            )
            st = await pipe.run(card)
        except StorySetupError as e:
            line = await _set_aside(cfg, board, card, e)
        else:
            if finished and st.stage == "review":
                line = await _already_in_review(board, card, st)
            else:
                outcome = f"blocked ({st.blocked})" if st.blocked else st.stage
                line = f"{card.ref} {card.title}: {outcome}"
        # a title and a stop reason are issue and git text: their whitespace is folded, so it stays one line
        return " ".join(line.split())


async def story_job(
    cfg: AirlieConfig,
    *,
    card: str | None,
    text: str | None,
    repo: str | None,
    running: Callable[[str], bool] | None = None,
) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]:
    """For POST /v1/airlie/stories: the issue is fetched, and where its story is built found, now (a bad
    reference, or a story that cannot start, is the caller's 400; an issue whose repository cannot be used is
    told so too); the story is built in the job. Its gate is the repository's AGENTS.md: a request names
    none. `running` says whether a job (by its id) runs: one for the same story is StoryRunning, before the
    issue can be told or moved."""
    try:
        scfg = story_config(cfg)
    except ConfigError as e:  # story.topology names no profile, or the file changed under a running serve
        raise StorySetupError(str(e)) from None
    problems = story_problems(scfg)
    if problems:
        raise StorySetupError("; ".join(problems))
    given = Path(repo) if repo else None
    board, c = await card_for(scfg, card, text)
    job_id = f"story-{c.key}"
    if running is not None and running(job_id):
        raise StoryRunning(f"job '{job_id}' is already running")
    await placed(scfg, board, c, given)

    async def work() -> dict[str, Any]:
        st = await run_one(scfg, board=board, card=c, repo=given)
        return {"stage": st.stage, "blocked": st.blocked, "prs": st.prs, "dir": str(st.path)}

    return job_id, stories_dir(scfg) / c.key / "events.jsonl", work()


def _check_trunk_and_gate(
    cfg: AirlieConfig, path: Path, who: str, test_cmd: str | None, blocked: str, out: Checked
) -> None:
    """The trunk a story in `path` would build on, the remote's as last fetched (a story fetches it first,
    and never builds on the local one), read by its full name as a story reads it (StoryPipeline._base), and
    the test gate it would have: a person's `test_cmd`, else the one AGENTS.md lists there (`who` names the
    repository, `blocked` the story, in a problem)."""
    ref = f"{cfg.story.remote}/{cfg.story.trunk}"
    sha = rev_parse(path, f"refs/remotes/{ref}")
    if sha is None:
        out.notes.append(
            f"trunk: {ref} has not been fetched into {path}: a story fetches it first, and reads its test "
            "gate there"
        )
    else:
        out.notes.append(f"trunk: {ref} at {sha[:12]}, as last fetched (a story fetches it first)")
    cmd = (test_cmd or "").strip()
    if cmd:
        out.notes.append(f"test gate: {cmd} (--test-cmd)")
        return
    if sha is None:
        return
    try:
        gate = read_gate(path, sha)
    except NoTestGate as e:
        out.problems.append(f"{blocked} would be blocked: {missing_gate(who, ref, sha, str(e))}")
        return
    out.notes.append(f"test gate (AGENTS.md at {ref}): {gate.shown}")


async def _check_issue(
    cfg: AirlieConfig, board: Board | None, ref: str, repo: Path | None, out: Checked
) -> tuple[Card, Place] | None:
    """The issue `ref` (read, never told anything) and where its story would be built, or None and the problem
    in `out`."""
    if board is None:  # a board that cannot be made is a problem of its own
        return None
    try:
        card = await board.card(ref)
    except BoardError as e:
        out.problems.append(str(e))
        return None
    saved = _recorded(cfg, card)
    if saved.get("stage") in PUBLISHED and "repo" in saved:  # place would not look at its repository either
        out.notes.append(
            f"{card.ref}'s story has published (stage {saved['stage']}, in {saved['repo']}): nothing is "
            "built or published again, so its repository and test gate are not checked"
        )
        return None
    try:
        where = place(cfg, card, repo)
    except StorySetupError as e:
        out.problems.append(f"{card.ref} would be blocked: {e}")
        return None
    url = masked(remote_url(where.repo, cfg.story.remote) or "")
    out.notes.append(
        f"{card.ref} is for {where.github_repo} (its repo: label): {where.repo}, whose remote "
        f"'{cfg.story.remote}' is {url}"
    )
    return card, where


async def check(
    cfg: AirlieConfig, repo: Path | None, test_cmd: str | None, ref: str | None = None
) -> Checked:
    """Everything `task story:check` reports: roles; for the issue `ref`, its repo: label, checkout and remote
    (`repo` replaces the lookup), else the repository of a written request (`repo`, story.repo); that
    repository's trunk and the test gate a story there would pass (a person's `test_cmd`, else AGENTS.md's),
    or why the story would be blocked; gh and gh-stack and who publishes (your gh login, or
    story.github_app's bot); Jira. No model runs and nothing is written: no fetch, no state, no note or
    move."""
    out = Checked(story_problems(cfg), [])
    board: Board | None = None
    no_board = ""
    try:
        board = make_board(cfg)
    except BoardError as e:
        no_board = str(e)
    path: Path | None = None
    who, blocked = "", "a written request"
    if ref is not None:
        found = await _check_issue(cfg, board, ref, repo, out)
        if found is not None:
            path, who, blocked = found[1].repo, str(found[1].github_repo), found[0].ref
    elif repo is None and cfg.story.repo is None:
        out.problems.append(
            "no repository to check: name an issue (task story:check -- AIR-7), or pass --repo or set "
            "story.repo (the repository of a written request)"
        )
    else:
        try:
            path = written_repo(cfg, repo)
        except StorySetupError as e:
            out.problems.append(str(e))
        else:
            who = str(path)
    remote_ok = not _unusable_ref(cfg.story.remote)  # reported above; git is not handed it
    if path is not None and remote_ok and not _unusable_ref(cfg.story.trunk):
        _check_trunk_and_gate(cfg, path, who, test_cmd, blocked, out)
    if path is not None and remote_ok:
        found_publishing = await Publisher(remote=cfg.story.remote, app=github_app(cfg)).check(path)
        out.problems.extend(found_publishing.problems)
        out.notes.extend(found_publishing.notes)
    if no_board:
        out.problems.append(no_board)
    elif isinstance(board, JiraBoard):
        out.problems.extend(await board.check())
    return out
