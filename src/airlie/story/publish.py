"""Publishing a story's layers with gh stack: fetch the trunk, adopt the layer branches as a stack, push them
and open draft PRs, describe each PR. It runs in a worktree of its own: `gh stack init` switches the current
checkout to the top branch (gh-stack v0.2.1, seen 2026-10-14). There is no merge here, by design: only a
human merges.

Each publish step (the fetch, the stack, the submit, each `gh pr view` and `gh pr edit`) runs as one of two
identities (docs/integrations/story.md, Publishing as a bot): a GitHub App's bot (story.github_app), with a
token minted for that step, scoped to the story's repository and revoked when the step ends, over HTTPS and
with none of the person's credentials (bot_env); or the person, with their own gh login and SSH key
(person_env). Either way git runs no repository hook and no fsmonitor, uses no credential helper or ssh
command the repository's own configuration names, and nothing prompts; gh is told the story's repository
(GH_REPO) instead of finding one among the remotes, and no other host (GH_HOST is dropped); and a step starts
only once git's own answer to where it would fetch from and push to is the story's repository on github.com
(effective_urls, misdirected). As you, a remote that names no github.com repository has none to be held
to."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from airlie.story.github_app import GitHubApp, GitHubAppError, masked, repo_slug
from airlie.subproc import NO_PROMPTS, agent_env, kill_group, with_git_config, without_hooks
from airlie.teams.worktree import remote_url

Runner = Callable[[list[str], Path, Mapping[str, str]], Awaitable[str]]
"""Runs one publish command (its arguments, its directory, the step's whole environment): its stdout."""
_TIMEOUT_S = 600
_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_HEAD, _TAIL = 200, 600  # how much of a failed command's output a PublishError keeps

QUIET = {"GH_STACK_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1"}
"""gh and gh-stack: no update notice and no prompt."""
NO_GH_HOST = "GH_HOST"
"""Dropped from every publish step's environment: the host gh puts a repository on when GH_REPO names none
(it is "owner/name"). One that named another host would have gh, and the step's credentials with it (the
bot's token, or your login), go there instead of github.com."""
PERSON_TRANSPORTS = "ssh:https"
"""The transports publishing as you allows (GIT_ALLOW_PROTOCOL): SSH and HTTPS remotes, and no remote on this
machine. For a remote that is a path, git runs the receivepack or uploadpack command the repository's
configuration names, with your environment, and takes the first one it reads, so nothing given on the command
line outranks a planted one. Not a setting: only unit tests that publish to a local bare repository widen
it."""
NO_GIT_PROXY = {"GIT_PROXY_COMMAND": ""}
"""No proxy command for git://. git takes the first core.gitProxy it reads, so the repository's would outrank
one given on the command line; the variable, even empty, outranks them all."""
PINNED = (
    ("core.alternateRefsCommand", "true"),
    ("http.proxy", ""),
    ("http.curloptResolve", ""),
    ("http.sslVerify", "true"),
    ("fetch.recurseSubmodules", "false"),
    ("push.recurseSubmodules", "no"),
    ("submodule.recurse", "false"),
)
"""Settings of the repository's that would have a publish step's git run a command or send its credentials
elsewhere, given back their safe values over it: the command git runs to list an alternate's refs while it
fetches (`true` lists none), a proxy (empty: none, whatever HTTPS_PROXY says), a list of addresses for hosts
(empty: none), the TLS check, and going into submodules (each has a configuration and a remote of its own). A
URL-specific http.<url>.* setting outranks these general ones, so as the bot they are given again for each URL
the step uses (bot_env); remote.<name>.proxy outranks http.proxy and is given for the step's remote."""
_HELPER_KEYS = r"^credential\.(.+\.)?helper$"
"""credential.helper and every credential.<url>.helper."""
_NO = ("", "0", "false", "no", "off")
"""What git reads as false in a boolean variable (GIT_CONFIG_NOSYSTEM)."""
BOT_GIT = (
    ("url.https://github.com/.insteadOf", "git@github.com:"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ("credential.helper", ""),
    ("credential.https://github.com.helper", "!gh auth git-credential"),
)
"""The bot's git: GitHub's SSH URLs fetched and pushed over HTTPS (a token cannot use SSH); every credential
helper dropped, the repository's own included (an empty value empties the list); then gh as the one helper,
for https://github.com only: it answers with GH_TOKEN."""


class PublishError(RuntimeError):
    """A git or gh command failed or could not start; the message carries the ends of its output, with the
    values of gh's token variables masked (a stop reason is posted to Jira)."""


class CannotStart(PublishError):
    """The program could not be started: not installed, not executable, or its directory is gone."""


class Checked(NamedTuple):
    """What `task story:check` found: problems (any one of them keeps it from saying ready) and notes (said
    either way: a warning, who publishes)."""

    problems: list[str]
    notes: list[str]


@dataclass(frozen=True)
class PersonGit:
    """What publishing as you keeps of your own git configuration: your core.sshCommand (else ssh) and your
    credential helpers, (key, value) in the order git reads them, from your system and global files and what
    they include. Never from the repository's, which a model can write."""

    ssh_command: str = "ssh"
    helpers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def read(cls, repo: Path) -> PersonGit:
        """Yours, as git in `repo` reads them (an includeIf can depend on the repository): system first, then
        global, as git reads them; no system file when GIT_CONFIG_NOSYSTEM says so, as git skips it then."""
        no_system = os.environ.get("GIT_CONFIG_NOSYSTEM", "").strip().lower() not in _NO
        ssh, helpers = "", []
        for scope in ("--global",) if no_system else ("--system", "--global"):
            for entry in _your_config(repo, scope, "--get-regexp", _HELPER_KEYS).split("\0"):
                key, newline, value = entry.partition("\n")
                if newline:  # a key without a value is no helper (git refuses one)
                    helpers.append((key, value))
            ssh = _your_config(repo, scope, "--get", "core.sshCommand").removesuffix("\0") or ssh
        return cls(ssh or "ssh", tuple(helpers))


def _your_config(repo: Path, scope: str, *query: str) -> str:
    """`git config <scope> --includes -z <query>` in `repo`: what that file of yours (and what it includes)
    says, NUL-terminated; empty when it says nothing, or git cannot answer within 10 s."""
    try:
        done = subprocess.run(
            ["git", "config", scope, "--includes", "-z", *query],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _pins(remote: str, urls: Iterable[str] = ()) -> list[tuple[str, str]]:
    """PINNED; no proxy for `remote`; and the address list and the TLS check again for each of `urls`."""
    exact = [("curloptResolve", ""), ("sslVerify", "true")]
    return [
        *PINNED,
        (f"remote.{remote}.proxy", ""),
        *((f"http.{url}.{k}", v) for url in urls for k, v in exact),
    ]


def person_env(person: PersonGit, remote: str = "origin", *, repository: str | None = None) -> dict[str, str]:
    """A publish step as you: your environment (your gh login, your SSH agent) with no prompt, git running no
    hook or fsmonitor and no transport but PERSON_TRANSPORTS, and your own core.sshCommand and credential
    helpers pinned over whatever the repository's configuration says (command-line configuration outranks it;
    the empty helper resets the list first), then PINNED for `remote`. With `repository` ("owner/name": the
    story's, once the remote names one on github.com), gh is told it as GH_REPO, so neither gh nor gh-stack
    picks one from the remotes, where a model can add one and mark it (remote.<name>.gh-resolved). Your
    GH_HOST is dropped either way (NO_GH_HOST)."""
    env = {**os.environ, **QUIET, **NO_PROMPTS, **NO_GIT_PROXY, "GIT_ALLOW_PROTOCOL": PERSON_TRANSPORTS}
    env.pop(NO_GH_HOST, None)
    if repository is not None:
        env["GH_REPO"] = repository
    pins = [("core.sshCommand", person.ssh_command), ("credential.helper", ""), *person.helpers]
    return with_git_config(without_hooks(env), [*pins, *_pins(remote)])


def bot_env(
    token: str, gh_dir: Path, remote: str = "origin", urls: Iterable[str] = (), *, repository: str
) -> dict[str, str]:
    """A publish step as the bot: the agent environment (none of your credentials: no gh token or login, no
    SSH agent, no git over SSH, every credential helper reset, no prompt), then the step's token as GH_TOKEN,
    `gh_dir` (empty, the step's own) as gh's configuration, `repository` (the one the token covers) as
    GH_REPO and no GH_HOST (NO_GH_HOST), git limited to HTTPS, no hook or fsmonitor, BOT_GIT, and PINNED for
    `remote` and for each of `urls`, the URLs it fetches from and pushes to."""
    env = agent_env()
    env.pop(NO_GH_HOST, None)
    env.update(QUIET)
    env.update(NO_GIT_PROXY)
    env.update(
        {
            "GH_TOKEN": token,
            "GH_CONFIG_DIR": str(gh_dir),
            "GH_REPO": repository,
            "GIT_ALLOW_PROTOCOL": "https",
        }
    )
    return with_git_config(without_hooks(env), [*BOT_GIT, *_pins(remote, urls)])


def effective_urls(cwd: Path, remote: str, env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Where `remote` fetches from and pushes to under `env`, as git works them out (insteadOf,
    pushInsteadOf and pushurl applied, the repository's included); nothing when git cannot say."""

    def ask(*args: str) -> list[str]:
        try:
            done = subprocess.run(
                ["git", *args], cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        return done.stdout.split() if done.returncode == 0 else []

    return ask("ls-remote", "--get-url", remote), ask("remote", "get-url", "--push", "--all", remote)


def misdirected(
    repository: str, remote: str, fetch: list[str], push: list[str], *, as_bot: bool
) -> list[str]:
    """What is wrong with where a publish step's git would fetch from (`fetch`) and push to (`push`), for a
    story that publishes to `repository`. As the bot: any URL but https://github.com/<repository>, with or
    without .git (it fetches and pushes over HTTPS alone). As you: any URL that does not name <repository> on
    github.com in one of GitHub's forms (repo_slug, any case), so a rule of yours that keeps it there, such as
    one that sends GitHub over SSH, still passes."""
    if as_bot:
        wanted = f"https://github.com/{repository}"
        only = "the bot fetches and pushes only over HTTPS, and only to"
    else:
        wanted, only = f"{repository} on github.com", "the story publishes only to"
    problems: list[str] = []
    for what, urls in (("fetch from", fetch), ("push to", push)):
        if not urls:
            problems.append(f"git cannot say where the remote '{remote}' would {what}")
        for url in urls:
            if not _leads_to(url, repository, as_bot=as_bot):
                problems.append(
                    f"the remote '{remote}' would {what} {masked(url)}, not {wanted}: a url.<base>.insteadOf "
                    f"or pushInsteadOf (yours or the repository's) or a remote.{remote}.pushurl sends it "
                    f"there, and {only} {repository}"
                )
    return problems


def _leads_to(url: str, repository: str, *, as_bot: bool) -> bool:
    """Whether `url` is `repository` on github.com: as the bot, as https://github.com/<repository> alone
    (.git optional); as you, in any of GitHub's forms (repo_slug)."""
    if as_bot:
        home = f"https://github.com/{repository}".lower()
        return url.lower() in (home, f"{home}.git")
    try:
        return repo_slug(url).lower() == repository.lower()
    except GitHubAppError:  # not a github.com repository at all
        return False


def _mask(text: str, env: Mapping[str, str]) -> str:
    """`text` with the values of gh's token variables replaced by ***. The longest goes first, so a token that
    contains another is masked whole."""
    for token in sorted({env[name] for name in _TOKENS if env.get(name)}, key=len, reverse=True):
        text = text.replace(token, "***")
    return text


def _ends(text: str) -> str:
    """The first 200 and the last 600 characters of `text`, joined by … when it is longer: gh says what
    it was doing first and why it failed last."""
    return text if len(text) <= _HEAD + _TAIL else f"{text[:_HEAD]}…{text[-_TAIL:]}"


async def run_cmd(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
    """Run one command with `env` (a publish step's: person_env or bot_env) and without a terminal, and return
    its stdout. It runs in a process group of its own (gh starts the gh-stack extension, which starts git): a
    timeout, or the caller being cancelled, kills all of them. A failure's output is masked with `env`'s
    tokens."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group: killpg reaches what it starts
        )
    except OSError as e:  # gh or git is not installed (`check` reports that), or cwd is gone
        raise CannotStart(f"`{' '.join(args)}` could not start: {e}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        await kill_group(proc)
        raise PublishError(f"`{' '.join(args)}` timed out after {_TIMEOUT_S} s") from None
    except BaseException:  # cancelled: nothing of the command may outlive its caller
        await kill_group(proc)
        raise
    if proc.returncode != 0:
        # masked before it is cut: a cut through a token would leave a piece of it that no longer matches
        tail = _ends(_mask((err or out).decode(errors="replace").strip(), env))
        raise PublishError(f"`{' '.join(args)}` exited {proc.returncode}: {tail}")
    return out.decode(errors="replace")


def _refuse_option_like(*names: str) -> None:
    """A branch name is a positional argument to gh: one that starts with "-" would be read as an option
    (--web, --add-reviewer, ...), so it is refused before any command runs."""
    for name in names:
        if name.startswith("-"):
            raise PublishError(f"refusing the branch name {name!r}: gh would read it as an option")


class Publisher:
    """The publish steps, as the person or, with `app`, as that GitHub App's bot."""

    def __init__(
        self, *, remote: str = "origin", run: Runner = run_cmd, app: GitHubApp | None = None
    ) -> None:
        self.remote = remote
        self.run = run
        self.app = app
        self.repository: str | None = None
        """The "owner/name" every publish step must fetch from and push to (prepare): the one the remote names
        as written, which prepare holds to a story's pin when it has one; as the bot, the one every token
        covers too. As you, None while the remote names no github.com repository: then no step is held to
        one."""
        self._person: PersonGit | None = None

    def github_repo_of(self, repo: Path) -> str | None:
        """The "owner/name" the remote's URL names as written (no insteadOf applied); None when `repo` has no
        such remote, its URL is not a github.com repository, or git cannot be run there."""
        try:
            url = remote_url(repo, self.remote)
        except OSError:  # no git, or no such directory: nothing says which repository
            return None
        try:
            return repo_slug(url) if url is not None else None
        except GitHubAppError:
            return None

    def prepare(self, repo: Path, expected: str | None = None) -> None:
        """Read, once per run and before a model works in it, what publishing from `repo` uses: the GitHub
        repository the remote's URL names (as written: no insteadOf applied), which every step must fetch from
        and push to and, as the bot, the one repository each token covers; as you, your own git settings too.
        What a model plants in the repository's configuration later changes none of them. With `expected`
        ("owner/name", any case: the repository a story was started for, or one named elsewhere), the remote
        as written must name that repository."""
        named = self.github_repo_of(repo)
        if expected is not None and (named is None or named.lower() != expected.lower()):
            raise PublishError(
                f"story.remote '{self.remote}' names {named or 'no github.com repository'}, not {expected}: "
                f"the story publishes to {expected}"
            )
        if self.app is None:
            self._person = PersonGit.read(repo)
            self.repository = named
            return
        url = remote_url(repo, self.remote)
        failure = f"{repo} has no remote '{self.remote}' (story.remote)"
        if url is not None:
            try:
                self.repository = repo_slug(url)
                return
            except GitHubAppError as e:
                failure = f"story.remote '{self.remote}': {e}"
        raise PublishError(failure)

    def _as_bot(self, repo: Path) -> tuple[GitHubApp, str]:
        """The app and the repository its tokens cover (prepare reads it from `repo` the first time)."""
        if self.app is None:
            raise PublishError("not publishing as a GitHub App")
        if self.repository is None:
            self.prepare(repo)
        assert self.repository is not None  # prepare set it, or raised
        return self.app, self.repository

    def _misdirected_as_you(self, cwd: Path, env: Mapping[str, str]) -> list[str]:
        """As you: what misdirected finds in where git in `cwd`, under `env`, would fetch from and push to;
        nothing while the remote names no github.com repository (prepare), as there is none to hold it to."""
        if self.repository is None:
            return []
        fetch, push = effective_urls(cwd, self.remote, env)
        return misdirected(self.repository, self.remote, fetch, push, as_bot=False)

    @asynccontextmanager
    async def _step(self, cwd: Path) -> AsyncIterator[dict[str, str]]:
        """The environment of one publish step in `cwd`, once where git would fetch from and push to has been
        found to be the story's repository (anywhere else is refused before any command runs). As the bot: a
        token minted now, for this step alone, and revoked when the step ends however it ends (best effort:
        GitHubApp.revoke), and gh pointed at an empty directory made for it and removed after it. As you:
        person_env."""
        if self.app is None:
            if self._person is None:
                self.prepare(cwd)
            env = person_env(self._person or PersonGit(), self.remote, repository=self.repository)
            wrong = self._misdirected_as_you(cwd, env)
            if wrong:
                raise PublishError(wrong[0])
            yield env
            return
        app, repository = self._as_bot(cwd)
        with tempfile.TemporaryDirectory(prefix="airlie-bot-gh-") as gh_dir:
            # where git would go, before a token exists: anywhere but the repository is refused
            probe = bot_env("", Path(gh_dir), self.remote, repository=repository)
            fetch, push = effective_urls(cwd, self.remote, probe)
            wrong = misdirected(repository, self.remote, fetch, push, as_bot=True)
            if wrong:
                raise PublishError(wrong[0])
            token = await self._token(app, repository)
            urls = dict.fromkeys([*fetch, *push])
            try:
                yield bot_env(token, Path(gh_dir), self.remote, urls=urls, repository=repository)
            finally:  # however the step ended: its token stops working now, not in an hour
                await app.revoke(token)

    @staticmethod
    async def _token(app: GitHubApp, repository: str) -> str:
        try:
            return (await app.token(repository)).token
        except GitHubAppError as e:
            failure = str(e)
        raise PublishError(failure)  # outside the handler: nothing is chained

    async def author(self, repo: Path) -> tuple[str, str] | None:
        """Who commits the layers of a story in `repo`: the bot's name and noreply address when publishing as
        one; None (airlie) otherwise."""
        if self.app is None:
            return None
        app, repository = self._as_bot(repo)
        try:
            return await app.identity(repository)
        except GitHubAppError as e:
            failure = str(e)
        raise PublishError(failure)

    async def fetch(self, repo: Path, trunk: str) -> None:
        """Fetch `trunk` from the remote into `repo`'s remote-tracking branch, refs/remotes/<remote>/<trunk>,
        and update nothing else. The refspec names both ends, and --refmap= (empty) has git use no fetch
        refspec the repository's configuration names: a model can plant one that maps nothing, so the
        remote-tracking branch stays where it was, or one that maps the trunk onto a local branch, which git
        would then move."""
        refspec = f"+refs/heads/{trunk}:refs/remotes/{self.remote}/{trunk}"
        async with self._step(repo) as env:
            await self.run(["git", "fetch", "-q", "--refmap=", self.remote, refspec], repo, env)

    async def _stacked(self, wt: Path, env: Mapping[str, str]) -> tuple[str, list[str]] | None:
        """The trunk and the branches (bottom first) of the stack `wt` is on; None when it is on none."""
        try:
            view = json.loads(await self.run(["gh", "stack", "view", "--json"], wt, env))
            return str(view["trunk"]), [str(b["name"]) for b in view["branches"]]
        except (PublishError, ValueError, KeyError, TypeError):
            return None  # not a stack yet

    async def stack(self, wt: Path, trunk: str, branches: list[str]) -> None:
        """Adopt `branches` (bottom first) as a stack on `trunk`; a stack that already holds exactly them on
        that trunk (an interrupted publish) is left alone. Any other stack is not: init runs, and gh-stack's
        refusal is the error."""
        _refuse_option_like(*branches)
        async with self._step(wt) as env:
            if await self._stacked(wt, env) == (trunk, branches):
                return
            await self.run(["gh", "stack", "init", "--base", trunk, *branches], wt, env)

    async def submit(self, wt: Path) -> None:
        """Push every layer and open the missing PRs as drafts (`--auto`: no editor; never `--open`)."""
        async with self._step(wt) as env:
            await self.run(["gh", "stack", "submit", "--auto", "--remote", self.remote], wt, env)

    async def pr(self, wt: Path, branch: str) -> tuple[int, str]:
        _refuse_option_like(branch)
        async with self._step(wt) as env:
            j = json.loads(await self.run(["gh", "pr", "view", branch, "--json", "number,url"], wt, env))
        return int(j["number"]), str(j["url"])

    async def describe(self, wt: Path, branch: str, title: str, body: Path) -> None:
        _refuse_option_like(branch)
        async with self._step(wt) as env:
            await self.run(["gh", "pr", "edit", branch, "--title", title, "--body-file", str(body)], wt, env)

    async def check(self, repo: Path) -> Checked:
        """What publishing needs, for `task story:check`: gh and gh-stack installed and the remote, and that
        git would fetch from and push to the repository the remote names, as every step checks it; then as
        you, gh logged in; as the bot, a github.com remote and what GitHubApp.check looks at (the key file,
        the installation, the token's permissions; not gh's login: gh uses the token). Without gh there is
        nothing to say about its login or its extension."""
        no_remote = f"{repo} has no remote '{self.remote}' (story.remote)"
        needs = [
            (["gh", "stack", "--help"], "gh-stack is missing: `gh extension install github/gh-stack`"),
            (["git", "remote", "get-url", self.remote], no_remote),
        ]
        if self.app is None:
            needs.insert(0, (["gh", "auth", "status"], "gh is not logged in: run `gh auth login`"))
            self.prepare(repo)
            env = person_env(self._person or PersonGit(), self.remote, repository=self.repository)
        else:  # none of these needs a credential, and none gets one
            env = without_hooks({**agent_env(), **QUIET})
        problems: list[str] = []
        no_gh = False
        for args, why in needs:
            if no_gh and args[0] == "gh":
                continue  # without gh there is no login and no extension to ask about
            try:
                await self.run(list(args), repo, env)
            except PublishError as e:
                if args[0] == "gh" and isinstance(e, CannotStart):
                    no_gh = True
                    problems.append("gh is not installed (https://cli.github.com)")
                else:
                    problems.append(why)
        if no_remote in problems:
            return Checked(problems, [])
        if self.app is None:
            return Checked([*problems, *self._misdirected_as_you(repo, env)], [])
        try:
            app, repository = self._as_bot(repo)
        except PublishError as e:
            return Checked([*problems, str(e)], [])
        with tempfile.TemporaryDirectory(prefix="airlie-bot-gh-") as gh_dir:
            probe = bot_env("", Path(gh_dir), self.remote, repository=repository)
            fetch, push = effective_urls(repo, self.remote, probe)
        found, notes = await app.check(repository)
        wrong = misdirected(repository, self.remote, fetch, push, as_bot=True)
        return Checked([*problems, *wrong, *found], notes)
