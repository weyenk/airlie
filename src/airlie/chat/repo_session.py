"""Repository chat sessions: one VM per chat (the thread's id is the session), registered with the resource
manager so it is held for a turn, stopped when idle and deleted with the chat. `open` makes the session ready
(host mirror, base commit, recipe, the VM's resource); `turn` holds the VM for one turn and seeds the code on
the first; `delete` and `adopt` are the thread's end and serve's restart. Nothing here runs on the host but
git in the mirror (airlie.boxes): the code runs in the VM."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from airlie.boxes.images import Images
from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import BackendMissing, Box, BoxUnavailable, ConfirmationRequired, Network
from airlie.boxes.recipe import load_recipe
from airlie.boxes.transit import seed
from airlie.chat.confirm import Confirmations, NeedsApproval
from airlie.chat.repo_tools import RepoChat
from airlie.chat.threads import Thread, ThreadStore
from airlie.config.schema import AirlieConfig
from airlie.engine.types import EngineError
from airlie.manager.core import Manager
from airlie.manager.vms import VMResource
from airlie.story.publish import Publisher, Runner
from airlie.tools.protocol import ToolContext

log = logging.getLogger("airlie.chat.repo_session")

DEFAULT_BASE = "main"
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/(?!-)[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_session: contextvars.ContextVar[str] = contextvars.ContextVar("airlie_repo_session", default="")
"""The session whose VM is being prepared: the image build's confirmation is that session's."""


def valid_repo(repo: str) -> bool:
    return REPO.fullmatch(repo) is not None and ".." not in repo and not repo.endswith("/.")


def slug_of(thread_id: str) -> str:
    """A thread id (any text the client chose) as a name safe for a VM and a branch."""
    return hashlib.sha256(thread_id.encode()).hexdigest()[:12]


def qualified(ref: str | None) -> str:
    """`ref` as the mirror knows it: a branch name is its remote-tracking branch; a commit or ref is as is."""
    ref = ref or DEFAULT_BASE
    if ref.startswith(("origin/", "refs/")) or _SHA.fullmatch(ref):
        return ref
    return f"origin/{ref}"


def build_confirm(confirmations: Confirmations) -> Callable[[str], bool]:
    """Images' `confirm`: the current session has a person's yes for building this recipe's image."""

    def confirm(key: str) -> bool:
        return confirmations.check(_session.get(), "build_image", {"key": key}) == "allowed"

    return confirm


class RepoSessions:
    def __init__(
        self,
        cfg: AirlieConfig,
        manager: Manager,
        *,
        images: Images,
        confirmations: Confirmations,
        mirror_for: Callable[[str], Mirror],
        box_for: Callable[[str], Box],
        threads: ThreadStore,
        network_check: Callable[[Network], None] = lambda network: None,
    ) -> None:
        """`mirror_for` gives each repository its own Mirror with its own Publisher (repo_mirrors).
        `network_check` raises BoxUnavailable, with how to fix it, when the backend cannot give the VM the
        configured network (softnet missing)."""
        self._cfg = cfg
        self._manager = manager
        self._network_check = network_check
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._images = images
        self._confirmations = confirmations
        self._mirror_for = mirror_for
        self._box_for = box_for
        self._threads = threads
        self._chats: dict[str, RepoChat] = {}
        self._resources: dict[str, VMResource] = {}

    # --- the VM's resource --------------------------------------------------------------------------------

    def _resource(self, thread: Thread) -> VMResource:
        """The thread's VM resource, registered with the manager once (a restart's `adopt` made it first)."""
        found = self._resources.get(thread.id)
        if found is not None:
            return found
        assert thread.repo is not None
        box = self._box_for(f"airlie-chat-{slug_of(thread.id)}")
        resource = VMResource(
            thread.id,
            thread.repo,
            box,
            self._cfg.boxes,
            image=lambda: self._image(thread.id),
            network=self._network,
        )
        self._manager.add(resource)
        self._resources[thread.id] = resource
        return resource

    async def _image(self, thread_id: str) -> str:
        chat = self._chats.get(thread_id)
        if chat is None:
            raise EngineError("this chat's environment is not known yet: send a message to open it")
        token = _session.set(thread_id)
        try:
            return await self._images.ensure(chat.recipe, chat.repo)
        finally:
            _session.reset(token)

    async def _network(self) -> Network:
        return self._configured_network()

    def _configured_network(self) -> Network:
        spec = self._cfg.boxes.network
        return Network(spec.after_setup, tuple(spec.allow_hosts) if spec.after_setup == "allow_list" else ())

    # --- a session ------------------------------------------------------------------------------------------

    async def open(self, thread: Thread) -> RepoChat:
        """The session of a repository chat, made ready: its VM registered, the base commit fixed (the first
        time) and the recipe read at it. Raises BoxUnavailable when Tart is missing (nothing has run yet), and
        MirrorError or RecipeError (EngineErrors naming the problem) for a repository that cannot be read."""
        assert thread.kind == "repository" and thread.repo is not None
        found = self._chats.get(thread.id)
        if found is not None:
            return found
        resource = self._resource(thread)
        await resource.box.exists()  # BoxUnavailable, with the install hint, when tart is not there
        self._network_check(self._configured_network())  # and when softnet is not, for allow_list or off
        mirror = self._mirror_for(thread.repo)
        lock = self._repo_locks.setdefault(thread.repo, asyncio.Lock())
        async with lock:  # one repository's mirror is made and fetched by one chat at a time
            base_sha = thread.base_sha
            if base_sha is None:
                await mirror.refresh()
                base_sha = await mirror.resolve(qualified(thread.base_ref))
                self._threads.set_base(thread.id, base_sha)
            recipe = load_recipe(await mirror.recipe_text(base_sha), self._cfg.boxes)
        chat = RepoChat(
            session_id=thread.id,
            slug=slug_of(thread.id),
            repo=thread.repo,
            branch=f"airlie/chat-{slug_of(thread.id)}",
            base_ref=qualified(thread.base_ref),
            base_sha=base_sha,
            recipe=recipe,
            box=resource.box,
            mirror=mirror,
            max_bytes=self._cfg.boxes.bundle_max_mb * 1024 * 1024,
        )
        self._chats[thread.id] = chat
        return chat

    def tool_context(self, chat: RepoChat) -> ToolContext:
        tools = self._cfg.tools
        return ToolContext(
            Path(chat.recipe.workdir),
            tools.shell_timeout_s,
            tools.output_cap_chars,
            test_steps=chat.recipe.test,
            role="chat",
            box=chat.box,
            box_env=chat.recipe.env,
        )

    @asynccontextmanager
    async def turn(self, chat: RepoChat, owner: str) -> AsyncIterator[ToolContext]:
        """Hold the VM for one turn (the manager starts it, and makes room or waits, naming the holders); the
        first turn puts the code in. `owner` is the holder's name; the hold ends with the turn."""
        try:
            async with self._manager.hold(chat.vm_name, owner):
                await seed(
                    chat.box,
                    chat.mirror,
                    chat.recipe,
                    ref=chat.base_sha,
                    branch=chat.branch,
                    session_id=chat.slug,
                )
                yield self.tool_context(chat)
        except ConfirmationRequired as e:  # the image build needs the person's yes: ask, as a tool call would
            args = {"key": e.what}
            approval_id = self._confirmations.request(chat.session_id, "", "build_image", args)
            raise NeedsApproval(approval_id, "build_image", args, e.what) from e

    async def delete(self, thread: Thread) -> None:
        """The chat is going: stop and forget its VM, delete the disk and drop the session branch from the
        mirror. A VM held by a turn is not deleted (EngineError "in use": the route refuses a busy thread
        first)."""
        if thread.kind != "repository":
            return
        name = f"vm:{thread.id}"
        chat = self._chats.pop(thread.id, None)  # first: nothing can open this session while its disk goes
        resource = self._resources.pop(thread.id, None)
        try:
            if name in {s.name for s in self._manager.status()}:
                await self._manager.discard(name)
        except EngineError:  # held or would not stop: the chat stays as it was
            if chat is not None:
                self._chats[thread.id] = chat
            if resource is not None:
                self._resources[thread.id] = resource
            raise
        box = resource.box if resource is not None else self._box_for(f"airlie-chat-{slug_of(thread.id)}")
        try:
            if await box.exists():
                await box.delete()
        except BackendMissing:  # no tart at all: no VM disk can be there
            pass
        except BoxUnavailable as e:  # tart failed: the disk may be there, so the chat that names it stays
            if chat is not None:
                self._chats[thread.id] = chat
            if resource is not None:
                self._resources[thread.id] = resource
                self._manager.add(resource)  # discarded above: registered again, stopped
            raise EngineError(f"could not delete the VM {box.name} (the chat is kept; try again): {e}") from e
        self._confirmations.forget(thread.id)
        if thread.repo is not None:
            try:  # the host's copy of the session branch (pushed or not)
                mirror = self._mirror_for(thread.repo)
                await mirror.git("update-ref", "-d", f"refs/heads/airlie/chat-{slug_of(thread.id)}")
            except Exception:
                log.warning(
                    "could not drop the session branch of %s from the mirror", thread.id, exc_info=True
                )

    async def stop_all(self) -> None:
        """At serve's shutdown: stop every chat VM that runs (the disks stay; the next start adopts or boots
        them). A VM that will not stop is logged and left: shutdown goes on."""
        for thread_id, resource in list(self._resources.items()):
            try:
                if await resource.running():
                    await self._manager.stop(resource.name)
            except Exception:
                log.warning("could not stop the VM of chat %s at shutdown", thread_id, exc_info=True)

    async def adopt(self) -> None:
        """At serve's start: register the VM of every stored repository chat whose disk exists, and have the
        manager mark the running ones ready."""
        for thread in self._threads.repository_threads():
            try:
                if not await self._box_for(f"airlie-chat-{slug_of(thread.id)}").exists():
                    continue
                self._resource(thread)
            except Exception:
                log.warning("could not adopt the VM of chat %s", thread.id, exc_info=True)
        await self._manager.adopt()


def repo_mirrors(
    root: Path,
    publisher: Callable[[], Publisher],
    *,
    run: Runner,
    local_checkout: Callable[[str], Path | None] = lambda repo: None,
) -> Callable[[str], Mirror]:
    """`mirror_for`: one Mirror per repository ("owner/name"), made on its first use and kept, each with a
    Publisher of its own. A Publisher holds the one repository its steps may fetch from and push to (what
    `prepare` read), so one shared by two repositories would send a push of one to the other's check (and
    refuse it), or mint a token for the wrong repository."""
    made: dict[str, Mirror] = {}

    def mirror_for(repo: str) -> Mirror:
        found = made.get(repo)
        if found is None:
            found = made[repo] = Mirror(root, repo, publisher(), run=run, local_checkout=local_checkout(repo))
        return found

    return mirror_for


def default_repo_sessions(
    cfg: AirlieConfig, manager: Manager, threads: ThreadStore, confirmations: Confirmations
) -> RepoSessions:
    """The serve wiring: Tart boxes (softnet checked for a filtered network), and a host mirror per
    repository under `<state_dir>/chat/mirrors` with its own publisher, as the GitHub App's bot when one is
    configured (else read from `story.repos_root`, and nothing is pushed)."""
    from airlie.boxes.images import SecretStore
    from airlie.boxes.mirror import MirrorError
    from airlie.boxes.tart import SOFTNET_HINT, TartBox, softnet_installed
    from airlie.story.checkout import find_checkout
    from airlie.story.factory import github_app
    from airlie.story.model import RepositoryError
    from airlie.story.publish import run_cmd

    state = cfg.observability.state_dir.expanduser()
    app = github_app(cfg)
    log_dir = state / "boxes" / "logs"

    def box_for(name: str) -> Box:
        log_dir.mkdir(parents=True, exist_ok=True)
        return TartBox(name, log_dir=log_dir)

    def local_checkout(repo: str) -> Path | None:
        try:
            return find_checkout(cfg.story.repos_root.expanduser(), repo)
        except RepositoryError as e:
            if app is None:  # nothing else to read the code from
                raise MirrorError(str(e)) from e
            return None

    def network_check(network: Network) -> None:
        if network.mode != "open" and not softnet_installed():
            raise BoxUnavailable(SOFTNET_HINT)

    images = Images(
        box_for,
        cfg.boxes,
        secrets=SecretStore(state / "boxes" / "secrets"),
        confirm=build_confirm(confirmations),
    )
    return RepoSessions(
        cfg,
        manager,
        images=images,
        confirmations=confirmations,
        mirror_for=repo_mirrors(
            state / "chat" / "mirrors",
            lambda: Publisher(remote=cfg.story.remote, app=app, publish="single"),
            run=run_cmd,
            local_checkout=local_checkout,
        ),
        box_for=box_for,
        threads=threads,
        network_check=network_check,
    )
