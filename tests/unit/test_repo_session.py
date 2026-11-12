"""Repository chat sessions: a VM per chat held for a turn through the manager, seeded once, adopted after a
restart and deleted with the chat. A real Manager and ThreadStore, FakeBox, a Mirror over a recording runner;
no host process runs (the runner and the box record every command)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from airlie.boxes.mirror import MirrorError
from airlie.boxes.protocol import BoxUnavailable
from airlie.boxes.tart import INSTALL_HINT
from airlie.chat.confirm import Approval, Confirmations, NeedsApproval
from airlie.chat.kinds import registry_for
from airlie.chat.repo_session import qualified, slug_of, valid_repo
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.threads import Thread, ThreadStore
from airlie.chat.tools import TodoList
from airlie.config import AirlieConfig, load_config
from airlie.config.schema import ChatKindSpec, ChatKindsSpec
from airlie.engine.types import EngineError
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock
from tests.support.box import FakeBox
from tests.support.repo_chat import RECIPE, FakeMirror, RepoFakes
from tests.unit.test_manager import FakeResource, settle
from tests.unit.test_transit import TIP

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {
    "read_file", "write_file", "edit_file", "list_dir", "grep", "shell", "run_tests", "git", "task",
    "push_branch", "open_pr",
}  # fmt: skip


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")


class World(RepoFakes):
    """Everything a session needs, over fakes, with a real Manager and ThreadStore."""

    def __init__(
        self, tmp_path: Path, cfg: AirlieConfig, *, recipe: str | None = RECIPE, budget: float = 100.0
    ) -> None:
        super().__init__(tmp_path, recipe=recipe)
        self.clock = FakeClock()
        self.manager = Manager([], budget_gb=budget, clock=self.clock)
        self.threads = ThreadStore(tmp_path / "threads.db")
        self.confirmations = Confirmations(cfg)
        self.sessions = self.build(cfg, self.manager, self.threads, self.confirmations)
        self.images = self.sessions._images

    def thread(self, thread_id: str = "t1") -> Thread:
        return self.threads.open(thread_id, "noah", "repository", repo="o/r", base_ref="main")

    def holders(self, thread_id: str = "t1") -> tuple[str, ...]:
        return next(s.holders for s in self.manager.status() if s.name == f"vm:{thread_id}")


@pytest.fixture
def world(tmp_path: Path, cfg: AirlieConfig) -> World:
    return World(tmp_path, cfg)


async def test_opening_a_repository_chat_refreshes_the_mirror_fixes_the_base_and_reads_the_recipe(
    world: World,
) -> None:
    thread = world.thread()

    chat = await world.sessions.open(thread)

    assert world.mirror.refreshes == 1
    assert (chat.repo, chat.base_ref, chat.base_sha) == ("o/r", "origin/main", TIP)
    assert chat.branch == f"airlie/chat-{slug_of('t1')}"
    assert chat.recipe.workdir == "/w" and chat.recipe.test == ("pytest -q",)
    assert world.threads.get("t1", "noah").base_sha == TIP  # kept, so a restart finds the same base
    assert await world.sessions.open(thread) is chat  # idempotent per session
    assert world.mirror.refreshes == 1


async def test_a_session_already_based_does_not_refresh_the_mirror(world: World) -> None:
    world.thread()
    world.threads.set_base("t1", "c" * 40)

    chat = await world.sessions.open(world.threads.get("t1", "noah"))

    assert chat.base_sha == "c" * 40
    assert world.mirror.refreshes == 0


async def test_a_repository_without_a_recipe_gets_the_default_one(tmp_path: Path, cfg: AirlieConfig) -> None:
    w = World(tmp_path, cfg, recipe=None)

    chat = await w.sessions.open(w.thread())

    assert chat.recipe.workdir == "/home/admin/work" and chat.recipe.test == ()


async def test_opening_a_repository_chat_without_tart_is_refused_with_the_install_hint(world: World) -> None:
    class NoTart(FakeBox):
        async def exists(self) -> bool:
            raise BoxUnavailable(INSTALL_HINT)

    world.boxes[f"airlie-chat-{slug_of('t1')}"] = NoTart("x")

    with pytest.raises(BoxUnavailable, match="tart"):
        await world.sessions.open(world.thread())

    assert world.runner.calls == [] and world.mirror.refreshes == 0  # nothing ran, host or VM


async def test_a_boot_that_fails_ends_the_turn_with_its_reason_and_holds_nothing(world: World) -> None:
    chat = await world.sessions.open(world.thread())
    world.vm().fail_boot = "the VM did not answer in 120s"

    with pytest.raises(EngineError, match="did not answer"):
        async with world.sessions.turn(chat, "chat:t1"):
            pytest.fail("a VM that did not boot is not a turn")

    assert world.holders() == ()


async def test_the_first_turn_seeds_the_clone_and_the_second_does_not(world: World) -> None:
    chat = await world.sessions.open(world.thread())

    async with world.sessions.turn(chat, "chat:t1"):
        pass
    async with world.sessions.turn(chat, "chat:t1"):
        pass

    clones = [c.argv for c in world.vm().calls if c.argv[:2] == ("git", "clone")]
    assert clones == [("git", "clone", "-q", "--no-checkout", "/tmp/in.bundle", "/w.seed-tmp")]
    assert ("git", "checkout", "-q", "-b", chat.branch, TIP) in [c.argv for c in world.vm().calls]


async def test_a_turn_holds_the_vm_and_releases_it_at_the_end(world: World) -> None:
    chat = await world.sessions.open(world.thread())

    async with world.sessions.turn(chat, "chat:t1"):
        during = world.holders()
    after = world.holders()

    assert during == ("chat:t1",) and after == ()
    assert world.vm().state == "running"  # idle stop is the manager's reap, not the turn's end


async def test_the_tool_context_carries_the_box_and_the_recipes_variables_and_tests(world: World) -> None:
    chat = await world.sessions.open(world.thread())

    async with world.sessions.turn(chat, "chat:t1") as ctx:
        assert ctx == world.sessions.tool_context(chat)

    assert ctx.box is world.vm()
    assert (ctx.worktree, dict(ctx.box_env), ctx.test_steps) == (Path("/w"), {"A": "1"}, ("pytest -q",))
    assert ctx.role == "chat" and ctx.shell_timeout_s > 0


async def test_a_vm_waits_for_memory_with_the_holders_named(tmp_path: Path, cfg: AirlieConfig) -> None:
    w = World(tmp_path, cfg, budget=20.0)
    w.manager.add(FakeResource("big", 14.0))
    chat = await w.sessions.open(w.thread())

    async with w.manager.hold("big", "chat:other"):
        waiting = asyncio.create_task(w.sessions.turn(chat, "chat:t1").__aenter__())
        await settle()
        status = next(s for s in w.manager.status() if s.name == "vm:t1")
        assert (status.state, status.detail) == ("waiting", "waiting for memory: chat:other")
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting


async def test_an_image_build_that_needs_a_yes_asks_and_runs_after_it(
    tmp_path: Path, cfg: AirlieConfig
) -> None:
    w = World(tmp_path, cfg, recipe='{"setup": ["make deps"], "workdir": "/w"}')
    secrets = w.secrets / "o__r.env"
    secrets.write_text("TOKEN=hunter2hunter2\n")
    secrets.chmod(0o600)
    chat = await w.sessions.open(w.thread())

    with pytest.raises(NeedsApproval) as asked:
        async with w.sessions.turn(chat, "chat:t1"):
            pytest.fail("the VM may not start before the build is approved")

    key = f"build_image:o/r:{chat.recipe.key(w.images.base_of(chat.recipe))}"
    assert asked.value.tool == "build_image" and asked.value.arguments == {"key": key}
    assert not any("-building-" in name for name in w.boxes)  # nothing was built
    w.confirmations.approve("t1", [Approval(id=asked.value.approval_id, approved=True)])

    async with w.sessions.turn(chat, "chat:t1"):
        pass

    assert any("-building-" in name for name in w.boxes)
    assert w.confirmations.status("t1", "build_image", {"key": key}) == "pending"  # the yes was spent


async def test_serve_restart_adopts_running_vms(world: World) -> None:
    thread = world.thread()
    world.threads.set_base("t1", TIP)
    vm = world.box_for(f"airlie-chat-{slug_of('t1')}")
    vm.state = "running"
    world.thread("t2")  # opened but never based: no session, no VM
    world.threads.open("t3", "noah", "repository", repo="o/r")
    world.threads.set_base("t3", TIP)  # based, but its VM disk is gone

    await world.sessions.adopt()

    names = {s.name: s.state for s in world.manager.status()}
    assert names == {f"vm:{thread.id}": "ready"}


async def test_adopting_registers_a_stopped_vm_without_starting_it(world: World) -> None:
    world.thread()
    world.threads.set_base("t1", TIP)
    vm = world.box_for(f"airlie-chat-{slug_of('t1')}")
    vm.state = "stopped"

    await world.sessions.adopt()

    assert [(s.name, s.state) for s in world.manager.status()] == [("vm:t1", "stopped")]
    assert vm.state == "stopped" and vm.boots == []
    chat = await world.sessions.open(world.threads.get("t1", "noah"))  # the first message opens it as usual
    assert chat.box is vm


async def test_adopting_skips_a_box_that_cannot_be_asked(world: World) -> None:
    world.thread()
    world.threads.set_base("t1", TIP)

    class NoTart(FakeBox):
        async def exists(self) -> bool:
            raise BoxUnavailable(INSTALL_HINT)

    world.boxes[f"airlie-chat-{slug_of('t1')}"] = NoTart("x")

    await world.sessions.adopt()

    assert world.manager.status() == []


async def test_deleting_a_chat_deletes_its_vm(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    async with world.sessions.turn(chat, "chat:t1"):
        pass
    vm = world.vm()
    chat_id = chat.session_id
    world.confirmations.request(chat_id, "c", "push_branch", {})

    await world.sessions.delete(thread)

    assert vm.deleted and vm.state == "absent"  # stopped (the manager), then the disk deleted
    assert world.manager.status() == []
    update = [a for a, _, _ in world.runner.calls if a[1:3] == ["update-ref", "-d"]]
    assert update[-1][3] == f"refs/heads/{chat.branch}"
    assert world.confirmations.check(chat_id, "push_branch", {}) == "pending"
    again = await world.sessions.open(world.thread())  # a new chat under the id starts clean
    assert again is not chat


async def test_deleting_a_chat_whose_turn_holds_the_vm_is_refused(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)

    async with world.sessions.turn(chat, "chat:t1"):
        with pytest.raises(EngineError, match="in use"):
            await world.sessions.delete(thread)

    assert not world.vm().deleted


async def test_deleting_a_chat_that_never_started_a_vm_is_quiet(world: World) -> None:
    thread = world.thread()

    await world.sessions.delete(thread)

    assert world.boxes == {} or not any(b.deleted for b in world.boxes.values())


async def test_deleting_a_plain_chat_touches_nothing(world: World) -> None:
    plain = world.threads.open("p1", "noah", "plain")

    await world.sessions.delete(plain)

    assert world.boxes == {} and world.runner.calls == []


async def test_deleting_survives_a_mirror_that_is_gone(world: World) -> None:
    thread = world.thread()
    world.runner.failures["update-ref"] = "no such ref"

    await world.sessions.delete(thread)  # the branch could not be dropped: logged, not raised


def test_a_plain_kind_registry_has_no_file_shell_git_or_push_tool(cfg: AirlieConfig) -> None:
    from airlie.tools.protocol import ToolContext

    library = SuperpowersLibrary(ROOT / "vendor" / "superpowers")

    registry = registry_for(
        "plain", cfg, library=library, todos=TodoList(), ctx=ToolContext(Path(".")), web={}, repo=None
    )

    assert not set(registry.names()) & FORBIDDEN


@pytest.mark.parametrize("tool", sorted(FORBIDDEN))
def test_config_rejects_a_plain_kind_that_lists_a_file_tool(tool: str) -> None:
    with pytest.raises(ValidationError, match=f"chat.kinds.plain cannot hold {tool}"):
        ChatKindsSpec(plain=ChatKindSpec(tools=["skill", tool]))


async def test_a_repository_registry_runs_in_the_vm_and_carries_the_host_tools(
    world: World, cfg: AirlieConfig
) -> None:
    chat = await world.sessions.open(world.thread())
    ctx = world.sessions.tool_context(chat)

    registry = registry_for("repository", cfg, library=None, todos=TodoList(), ctx=ctx, web={}, repo=chat)

    names = set(registry.names())
    assert {"read_file", "write_file", "shell", "git", "run_tests", "push_branch", "open_pr", "todo"} <= names
    assert registry.ctx.box is chat.box


def test_a_repo_must_be_owner_slash_name() -> None:
    assert valid_repo("acme/app") and valid_repo("a.b/c_d-e")
    for bad in ("", "acme", "acme/app/x", "../x", "a/..", "acme/app;rm", "a b/c"):
        assert not valid_repo(bad), bad


def test_a_base_ref_is_qualified_for_the_mirror() -> None:
    assert qualified(None) == "origin/main" and qualified("dev") == "origin/dev"
    assert qualified("origin/dev") == "origin/dev" and qualified("refs/tags/v1") == "refs/tags/v1"
    assert qualified("a" * 40) == "a" * 40


def test_a_thread_id_becomes_a_safe_name() -> None:
    assert slug_of("../a b") == hashlib.sha256(b"../a b").hexdigest()[:12]
    assert slug_of("a") != slug_of("b")


async def test_a_mirror_that_cannot_be_read_is_an_engine_error(world: World) -> None:
    class Broken(FakeMirror):
        async def refresh(self) -> None:
            raise MirrorError("no checkout of o/r")

    world.sessions._mirror_for = lambda repo: Broken(world.tmp / "m", "o/r", world.runner, None)  # type: ignore[assignment]

    with pytest.raises(MirrorError, match="no checkout"):
        await world.sessions.open(world.thread())


@pytest.mark.parametrize("bad", ["./x", "-o/x", "o/-x", "o/.", "o/..", "é/x", "o/é", "o/x\n"])
def test_a_repo_follows_githubs_name_rule(bad: str) -> None:
    assert not valid_repo(bad)


async def test_deleting_forgets_the_session_before_the_disk_goes(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    async with world.sessions.turn(chat, "chat:t1"):
        pass
    seen: list[bool] = []
    box = world.vm()
    real_delete = box.delete

    async def spying() -> None:
        seen.append("t1" in world.sessions._chats or "t1" in world.sessions._resources)
        await real_delete()

    box.delete = spying  # type: ignore[method-assign]

    await world.sessions.delete(thread)

    assert seen == [False]  # a turn that starts now cannot find the session being deleted


# --- final review fixes ---------------------------------------------------------------------------------


async def test_a_tart_delete_that_fails_keeps_the_chat_and_names_the_vm(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    async with world.sessions.turn(chat, "chat:t1"):
        pass
    box = world.vm()

    async def locked() -> None:
        raise BoxUnavailable("tart delete airlie-chat-x failed: the VM is locked")

    box.delete = locked  # type: ignore[method-assign]

    with pytest.raises(EngineError, match=f"could not delete the VM {box.name}.*locked"):
        await world.sessions.delete(thread)

    assert [s.name for s in world.manager.status()] == ["vm:t1"]  # still registered, to be deleted again
    assert await world.sessions.open(thread) is chat
    dropped = [a[3] for a, _, _ in world.runner.calls if a[1:3] == ["update-ref", "-d"]]
    assert f"refs/heads/{chat.branch}" not in dropped  # the session branch is kept too


async def test_deleting_a_chat_without_tart_at_all_is_quiet(world: World) -> None:
    from airlie.boxes.protocol import BackendMissing

    class NoTart(FakeBox):
        async def exists(self) -> bool:
            raise BackendMissing(INSTALL_HINT)

    world.boxes[f"airlie-chat-{slug_of('t1')}"] = NoTart("x")

    await world.sessions.delete(world.thread())  # no tart: no disk can be there


async def test_two_chats_opening_one_repository_refresh_its_mirror_one_at_a_time(world: World) -> None:
    inside: list[int] = []
    overlapped: list[bool] = []

    async def refresh() -> None:
        inside.append(1)
        overlapped.append(len(inside) > 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        inside.pop()

    world.mirror.refresh = refresh  # type: ignore[method-assign]

    await asyncio.gather(world.sessions.open(world.thread("t1")), world.sessions.open(world.thread("t2")))

    assert overlapped == [False, False]


async def test_a_filtered_network_without_softnet_is_refused_before_anything_runs(
    tmp_path: Path, cfg: AirlieConfig
) -> None:
    from airlie.boxes.protocol import Network
    from airlie.boxes.tart import SOFTNET_HINT

    world = World(tmp_path, cfg)
    asked: list[Network] = []

    def no_softnet(network: Network) -> None:
        asked.append(network)
        raise BoxUnavailable(SOFTNET_HINT)

    world.sessions._network_check = no_softnet

    with pytest.raises(BoxUnavailable, match="softnet"):
        await world.sessions.open(world.thread())

    assert asked[0].mode == cfg.boxes.network.after_setup
    assert world.runner.calls == [] and world.mirror.refreshes == 0


async def test_shutdown_stops_every_running_chat_vm_and_keeps_its_disk(world: World) -> None:
    chat = await world.sessions.open(world.thread())
    async with world.sessions.turn(chat, "chat:t1"):
        pass

    await world.sessions.stop_all()

    vm = world.vm()
    assert vm.state == "stopped" and not vm.deleted
    assert [(s.name, s.state) for s in world.manager.status()] == [("vm:t1", "stopped")]


# --- the serve wiring -----------------------------------------------------------------------------------


class Wired:
    """`default_repo_sessions` over a config that keeps its state and its checkouts under tmp_path."""

    def __init__(self, tmp_path: Path, cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch, *, app: object):
        from airlie.chat.repo_session import default_repo_sessions

        self.cfg = cfg.model_copy(deep=True)
        self.state = tmp_path / "state"
        self.repos = tmp_path / "repos"
        self.cfg.observability.state_dir = self.state
        self.cfg.story.repos_root = self.repos
        self.cfg.story.remote = "upstream"
        monkeypatch.setattr("airlie.story.factory.github_app", lambda c: app if c is self.cfg else None)
        self.manager = Manager([], budget_gb=100.0, clock=FakeClock())
        self.threads = ThreadStore(tmp_path / "threads.db")
        self.confirmations = Confirmations(self.cfg)
        self.sessions = default_repo_sessions(self.cfg, self.manager, self.threads, self.confirmations)


@pytest.fixture
def wired(tmp_path: Path, cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch) -> Wired:
    return Wired(tmp_path, cfg, monkeypatch, app=None)


def test_the_serve_wiring_gives_each_vm_a_tart_box_that_logs_under_the_state_dir(wired: Wired) -> None:
    from airlie.boxes.tart import TartBox

    box = wired.sessions._box_for("airlie-chat-abc")
    again = wired.sessions._box_for("airlie-chat-def")  # the log directory already being there is fine

    assert isinstance(box, TartBox) and box.name == "airlie-chat-abc" and again.name == "airlie-chat-def"
    assert (wired.state / "boxes" / "logs").is_dir()
    assert box._log == wired.state / "boxes" / "logs" / "airlie-chat-abc.log"


def test_the_serve_wiring_hands_the_sessions_what_it_was_given(wired: Wired) -> None:
    s = wired.sessions

    assert s._cfg is wired.cfg and s._manager is wired.manager
    assert s._confirmations is wired.confirmations and s._threads is wired.threads
    assert s._images._spec is wired.cfg.boxes
    assert s._images._box_for("x").name == "x"


def test_the_image_build_reads_its_setup_secrets_from_the_state_dir(wired: Wired) -> None:
    secrets = wired.state / "boxes" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "o__r.env").write_text("TOKEN=hunter2hunter2\n")
    (secrets / "o__r.env").chmod(0o600)

    assert wired.sessions._images._secrets.for_repo("o/r") == {"TOKEN": "hunter2hunter2"}
    assert wired.sessions._images._secrets._dir == secrets  # not another spelling of it


def test_an_image_build_is_confirmed_by_the_yes_of_the_session_it_runs_for(wired: Wired) -> None:
    from airlie.chat.repo_session import _session

    confirm = wired.sessions._images._confirm
    asked = wired.confirmations.request("t1", "", "build_image", {"key": "k"})
    wired.confirmations.approve("t1", [Approval(id=asked, approved=True)])

    assert confirm("k") is False  # no session is preparing a VM
    token = _session.set("t1")
    try:
        assert confirm("other") is False
        assert confirm("k") is True
        assert confirm("k") is False  # spent
    finally:
        _session.reset(token)


def test_a_filtered_network_needs_softnet_and_an_open_one_does_not(
    tmp_path: Path, cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.boxes.protocol import Network
    from airlie.boxes.tart import SOFTNET_HINT

    monkeypatch.setattr("airlie.boxes.tart.softnet_installed", lambda: False)
    missing = Wired(tmp_path / "a", cfg, monkeypatch, app=None).sessions._network_check
    monkeypatch.setattr("airlie.boxes.tart.softnet_installed", lambda: True)
    present = Wired(tmp_path / "b", cfg, monkeypatch, app=None).sessions._network_check

    with pytest.raises(BoxUnavailable) as off:
        missing(Network("off"))
    with pytest.raises(BoxUnavailable, match="softnet"):
        missing(Network("allow_list", ("github.com",)))
    missing(Network("open"))  # nothing to filter: no softnet needed
    present(Network("off"))
    present(Network("allow_list", ("github.com",)))
    assert str(off.value) == SOFTNET_HINT


def test_a_repository_mirror_lives_under_the_state_dir_with_its_own_single_publisher(wired: Wired) -> None:
    from airlie.story.publish import run_cmd

    (wired.repos / "r").mkdir(parents=True)
    (wired.repos / "q").mkdir()

    mirror = wired.sessions._mirror_for("o/r")
    other = wired.sessions._mirror_for("o/q")

    assert wired.sessions._mirror_for("o/r") is mirror
    assert mirror.path == wired.state / "chat" / "mirrors" / "o__r"
    assert mirror.run is run_cmd and mirror.local_checkout == wired.repos / "r"
    assert (mirror.publisher.remote, mirror.publisher.publish) == ("upstream", "single")
    assert mirror.publisher is not other.publisher and mirror.publisher.app is None
    assert not mirror.can_push


def test_a_repository_without_a_checkout_and_without_the_app_cannot_be_mirrored(wired: Wired) -> None:
    with pytest.raises(MirrorError) as e:
        wired.sessions._mirror_for("o/r")

    assert f"no checkout of o/r in {wired.repos}" in str(e.value)
    assert "clone it there" in str(e.value)


def test_with_the_app_a_repository_is_mirrored_without_a_local_checkout(
    tmp_path: Path, cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = object()
    w = Wired(tmp_path, cfg, monkeypatch, app=app)

    mirror = w.sessions._mirror_for("o/r")

    assert mirror.local_checkout is None and mirror.publisher.app is app and mirror.can_push


def test_the_mirror_of_a_repository_is_made_once_and_checked_out_by_its_own_name() -> None:
    from airlie.chat.repo_session import repo_mirrors
    from tests.unit.test_transit import FakePublisher, FakeRunner

    runner = FakeRunner()
    publishers: list[FakePublisher] = []
    asked: list[str] = []

    def publisher() -> FakePublisher:
        publishers.append(FakePublisher())
        return publishers[-1]

    def local_checkout(repo: str) -> Path | None:
        asked.append(repo)
        return Path("/checkouts") / repo

    mirror_for = repo_mirrors(Path("/m"), publisher, run=runner, local_checkout=local_checkout)  # type: ignore[arg-type]

    a, b = mirror_for("o/a"), mirror_for("o/b")

    assert mirror_for("o/a") is a and mirror_for("o/b") is b
    assert (a.repo, a.path, a.run, a.publisher) == ("o/a", Path("/m/o__a"), runner, publishers[0])
    assert b.publisher is publishers[1] and len(publishers) == 2
    assert asked == ["o/a", "o/b"]
    assert (a.local_checkout, b.local_checkout) == (Path("/checkouts/o/a"), Path("/checkouts/o/b"))


def test_a_mirror_has_no_local_checkout_unless_asked_for_one() -> None:
    from airlie.chat.repo_session import repo_mirrors
    from tests.unit.test_transit import FakePublisher, FakeRunner

    mirror_for = repo_mirrors(Path("/m"), FakePublisher, run=FakeRunner())  # type: ignore[arg-type]

    assert mirror_for("o/a").local_checkout is None


# --- the session's parts, one by one -------------------------------------------------------------------


def _cfg_with(cfg: AirlieConfig, **network: object) -> AirlieConfig:
    out = cfg.model_copy(deep=True)
    for key, value in network.items():
        setattr(out.boxes.network, key, value)
    return out


async def test_an_allow_list_network_lets_the_vm_reach_the_configured_hosts_and_no_others(
    tmp_path: Path, cfg: AirlieConfig
) -> None:
    from airlie.boxes.protocol import Network

    w = World(tmp_path, _cfg_with(cfg, after_setup="allow_list", allow_hosts=["pypi.org", "github.com"]))
    chat = await w.sessions.open(w.thread())

    async with w.sessions.turn(chat, "chat:t1"):
        pass

    assert w.sessions._configured_network() == Network("allow_list", ("pypi.org", "github.com"))
    assert w.vm().boots == [Network("allow_list", ("pypi.org", "github.com"))]
    assert type(w.vm().boots[0].allow) is tuple


async def test_a_network_that_is_off_reaches_no_host_even_with_hosts_listed(
    tmp_path: Path, cfg: AirlieConfig
) -> None:
    from airlie.boxes.protocol import Network

    w = World(tmp_path, _cfg_with(cfg, after_setup="off", allow_hosts=["pypi.org"]))
    asked: list[Network] = []
    w.network_check = asked.append
    chat = await w.sessions.open(w.thread())

    async with w.sessions.turn(chat, "chat:t1"):
        pass

    assert asked == [Network("off", ())] and w.vm().boots == [Network("off", ())]


async def test_the_image_of_a_chat_not_opened_yet_is_refused_with_how_to_open_it(world: World) -> None:
    with pytest.raises(EngineError) as e:
        await world.sessions._image("t1")

    assert str(e.value) == "this chat's environment is not known yet: send a message to open it"


async def test_the_image_is_ensured_for_the_chats_recipe_and_repo_as_that_session(world: World) -> None:
    from airlie.chat.repo_session import _session

    chat = await world.sessions.open(world.thread())
    seen: list[tuple[object, str, str]] = []

    async def ensure(recipe: object, repo: str) -> str:
        seen.append((recipe, repo, _session.get()))
        return "airlie-image-x"

    world.images.ensure = ensure  # type: ignore[method-assign]

    assert await world.sessions._image("t1") == "airlie-image-x"
    assert seen == [(chat.recipe, "o/r", "t1")]
    assert _session.get() == ""  # the session is set for the build alone


async def test_a_chats_vm_resource_is_named_for_its_repository(world: World) -> None:
    await world.sessions.open(world.thread())

    resource = world.sessions._resources["t1"]

    assert (resource.name, resource.display) == ("vm:t1", "o/r VM")
    assert resource.box is world.vm() and resource.spec is world.sessions._cfg.boxes


async def test_opening_a_chat_that_is_not_a_repository_chat_is_refused(world: World) -> None:
    plain = Thread("p1", "noah", "plain", "", 0.0, 0.0, repo="o/r")

    with pytest.raises(AssertionError):
        await world.sessions.open(plain)

    assert world.sessions._chats == {} and world.boxes == {}


async def test_an_opened_chat_carries_its_slug_its_mirror_its_vm_and_its_bundle_limit(
    world: World, cfg: AirlieConfig
) -> None:
    chat = await world.sessions.open(world.thread())

    assert (chat.session_id, chat.slug) == ("t1", slug_of("t1"))
    assert chat.mirror is world.mirror and chat.box is world.vm()
    assert chat.max_bytes == cfg.boxes.bundle_max_mb * 1048576 and type(chat.max_bytes) is int


async def test_opening_an_open_chat_again_asks_nothing_of_the_vm_the_network_or_the_mirror(
    world: World,
) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    asked: list[object] = []
    world.network_check = asked.append
    world.sessions._mirror_for = lambda repo: asked.append(repo) or world.mirror  # type: ignore[assignment,func-returns-value]

    assert await world.sessions.open(thread) is chat
    assert asked == []


async def test_a_chat_opens_its_own_repositorys_mirror_at_its_base_ref_and_reads_the_recipe_there(
    world: World,
) -> None:
    asked: list[str] = []
    recipe_at: list[str | None] = []
    real_recipe_text = world.mirror.recipe_text

    def mirror_for(repo: str) -> FakeMirror:
        asked.append(repo)
        return world.mirror

    async def recipe_text(ref: str) -> str | None:
        recipe_at.append(ref)
        return await real_recipe_text(ref)

    world.sessions._mirror_for = mirror_for  # type: ignore[assignment]
    world.mirror.recipe_text = recipe_text  # type: ignore[method-assign]
    thread = world.threads.open("t1", "noah", "repository", repo="o/r", base_ref="dev")

    chat = await world.sessions.open(thread)

    assert asked == ["o/r"] and recipe_at == [TIP]
    assert chat.base_ref == "origin/dev"
    resolved = [a for a, _, _ in world.runner.calls if a[1:2] == ["rev-parse"]]
    assert resolved and resolved[-1][-1] == "origin/dev^{commit}"


async def test_two_opens_of_one_chat_at_once_make_one_session(world: World) -> None:
    thread = world.thread()
    world.mirror.refresh_gate = asyncio.Event()

    first = asyncio.create_task(world.sessions.open(thread))
    await world.mirror.refreshing.wait()
    second = asyncio.create_task(world.sessions.open(thread))
    await settle()
    world.mirror.refresh_gate.set()

    a, b = await asyncio.gather(first, second)

    assert a is b and world.mirror.refreshes == 1


async def test_a_mirror_being_refreshed_for_one_repository_does_not_hold_up_another(world: World) -> None:
    other = FakeMirror(world.tmp / "other", "o/q", world.runner, RECIPE)
    world.sessions._mirror_for = lambda repo: other if repo == "o/q" else world.mirror  # type: ignore[assignment]
    world.mirror.refresh_gate = asyncio.Event()

    slow = asyncio.create_task(world.sessions.open(world.thread("t1")))
    await world.mirror.refreshing.wait()
    quick = asyncio.create_task(
        world.sessions.open(world.threads.open("t2", "noah", "repository", repo="o/q", base_ref="main"))
    )
    await settle()

    assert quick.done() and quick.result().mirror is other and not slow.done()
    world.mirror.refresh_gate.set()
    await slow


async def test_the_tool_context_takes_the_shell_timeout_and_output_cap_from_the_config(
    tmp_path: Path, cfg: AirlieConfig
) -> None:
    custom = cfg.model_copy(deep=True)
    custom.tools.shell_timeout_s = 321.0
    custom.tools.output_cap_chars = 4321
    w = World(tmp_path, custom)
    chat = await w.sessions.open(w.thread())

    ctx = w.sessions.tool_context(chat)

    assert (ctx.shell_timeout_s, ctx.output_cap_chars) == (321.0, 4321)


async def test_a_delete_refused_for_a_busy_vm_keeps_the_session_and_its_resource(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    resource = world.sessions._resources["t1"]

    async with world.sessions.turn(chat, "chat:t1"):
        with pytest.raises(EngineError, match="in use"):
            await world.sessions.delete(thread)

    assert world.sessions._chats == {"t1": chat} and world.sessions._resources == {"t1": resource}
    assert await world.sessions.open(thread) is chat


async def test_a_failed_tart_delete_keeps_the_resource_the_chat_was_using(world: World) -> None:
    thread = world.thread()
    chat = await world.sessions.open(thread)
    resource = world.sessions._resources["t1"]
    async with world.sessions.turn(chat, "chat:t1"):
        pass

    async def locked() -> None:
        raise BoxUnavailable("the VM is locked")

    world.vm().delete = locked  # type: ignore[method-assign]

    with pytest.raises(EngineError, match="could not delete"):
        await world.sessions.delete(thread)

    assert world.sessions._chats == {"t1": chat} and world.sessions._resources == {"t1": resource}


async def test_deleting_an_opened_chat_deletes_the_box_it_was_using_without_making_another(
    world: World,
) -> None:
    thread = world.thread()
    await world.sessions.open(thread)
    made: list[str] = []
    world.sessions._box_for = lambda name: made.append(name) or FakeBox(name)  # type: ignore[assignment,func-returns-value]

    await world.sessions.delete(thread)

    assert made == []


async def test_deleting_a_chat_forgets_its_approvals_and_drops_its_branch_from_its_own_mirror(
    world: World,
) -> None:
    thread = world.thread()
    asked = world.confirmations.request("t1", "c", "push_branch", {})
    world.confirmations.approve("t1", [Approval(id=asked, approved=True)])
    repos: list[str] = []
    world.sessions._mirror_for = lambda repo: repos.append(repo) or world.mirror  # type: ignore[assignment,func-returns-value]

    await world.sessions.delete(thread)

    assert world.confirmations.check("t1", "push_branch", {}) == "pending"  # the yes went with the chat
    assert repos == ["o/r"]


async def test_a_session_branch_that_cannot_be_dropped_is_logged_with_its_chat_and_reason(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    thread = world.thread()
    world.runner.failures["update-ref"] = "no such ref"

    with caplog.at_level("WARNING", logger="airlie.chat.repo_session"):
        await world.sessions.delete(thread)

    [record] = caplog.records
    assert record.getMessage() == "could not drop the session branch of t1 from the mirror"
    assert record.exc_info and record.exc_info[1] is not None and record.levelname == "WARNING"


async def test_a_vm_that_cannot_be_asked_at_shutdown_is_logged_and_the_others_still_stop(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    for thread_id in ("t1", "t2"):
        chat = await world.sessions.open(world.thread(thread_id))
        async with world.sessions.turn(chat, f"chat:{thread_id}"):
            pass

    async def unknown() -> bool:
        raise BoxUnavailable("tart list failed")

    world.vm("t1").running = unknown  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="airlie.chat.repo_session"):
        await world.sessions.stop_all()

    [record] = caplog.records
    assert record.getMessage() == "could not stop the VM of chat t1 at shutdown"
    assert record.exc_info and record.exc_info[1] is not None and record.levelname == "WARNING"
    assert world.vm("t2").state == "stopped"


async def test_adopting_goes_on_past_a_chat_whose_disk_is_gone(world: World) -> None:
    world.thread("t1")
    world.threads.set_base("t1", TIP)  # based, but no VM disk
    world.thread("t2")
    world.threads.set_base("t2", TIP)
    world.box_for(f"airlie-chat-{slug_of('t2')}").state = "stopped"  # type: ignore[attr-defined]

    await world.sessions.adopt()

    assert [s.name for s in world.manager.status()] == ["vm:t2"]


async def test_a_vm_that_cannot_be_adopted_is_logged_with_its_chat_and_reason(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.thread()
    world.threads.set_base("t1", TIP)

    class NoTart(FakeBox):
        async def exists(self) -> bool:
            raise BoxUnavailable(INSTALL_HINT)

    world.boxes[f"airlie-chat-{slug_of('t1')}"] = NoTart("x")

    with caplog.at_level("WARNING", logger="airlie.chat.repo_session"):
        await world.sessions.adopt()

    [record] = caplog.records
    assert record.getMessage() == "could not adopt the VM of chat t1"
    assert record.exc_info and record.exc_info[1] is not None and record.levelname == "WARNING"
