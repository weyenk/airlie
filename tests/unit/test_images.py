import asyncio
import itertools
import os
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.images import Images, SecretStore
from airlie.boxes.protocol import BoxUnavailable, ConfirmationRequired, ExecResult, Network
from airlie.boxes.recipe import Recipe, RecipeError
from airlie.config.schema import BoxesSpec
from tests.support.box import Call, FakeBox, Handler

SPEC = BoxesSpec(cpus=2, memory_gb=4, disk_gb=20, boot_timeout_s=30, setup_timeout_s=99)
REPO = "acme/widgets"


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def sh_ok(box: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
    return ExecResult(0, "")


class Rig:
    """Boxes by their current name: a rename moves a box to its new name, as the VM's disk does."""

    def __init__(self, tmp_path: Path, *, secrets: str | None = None, allow: bool = True) -> None:
        self.all: list[FakeBox] = []
        self._tokens = itertools.count()
        self.sh: Handler = sh_ok
        self.fail_boot: str | None = None
        self.delete_fails: str | None = None
        self.asked: list[str] = []
        self.allow = allow
        sdir = tmp_path / "secrets"
        sdir.mkdir()
        if secrets is not None:
            path = sdir / "acme__widgets.env"
            path.write_text(secrets)
            path.chmod(0o600)
        self.images = Images(
            self.box_for, SPEC, secrets=SecretStore(sdir), confirm=self.confirm, token=self.token
        )

    def box_for(self, name: str) -> FakeBox:
        for box in self.all:
            if box.name == name and box.state != "absent":
                return box
        box = FakeBox(name, handlers={"sh": lambda b, argv, stdin: self.sh(b, argv, stdin)})
        box.fail_boot = self.fail_boot
        if self.delete_fails:
            box.delete = self._failing_delete  # type: ignore[method-assign]
        self.all.append(box)
        return box

    async def _failing_delete(self) -> None:
        raise BoxUnavailable(self.delete_fails or "")

    def token(self) -> str:
        return f"t{next(self._tokens)}k"

    def building(self) -> list[FakeBox]:
        """The boxes an image build used (all of them, whatever became of them)."""
        return [b for b in self.all if b.created]

    def built(self) -> list[FakeBox]:
        """The boxes that exist."""
        return [b for b in self.all if b.state != "absent"]

    def confirm(self, what: str) -> bool:
        self.asked.append(what)
        return self.allow


def test_no_setup_uses_the_base_image_without_building(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert run(rig.images.ensure(Recipe(base="img:1"), REPO)) == "img:1"
    assert run(rig.images.ensure(Recipe(), REPO)) == SPEC.image
    assert rig.all == []


def test_image_name_is_airlie_env_and_the_key() -> None:
    recipe = Recipe(base="img:1", setup=("a",))
    images = Images(FakeBox, SPEC, secrets=SecretStore(Path("/nowhere")), confirm=lambda _: True)
    assert images.image_name(recipe) == f"airlie-env-{recipe.key('img:1')}"
    assert images.image_name(Recipe(setup=("a",))) == f"airlie-env-{Recipe(setup=('a',)).key(SPEC.image)}"


def test_the_repository_is_part_of_the_name_of_an_image_built_with_secrets() -> None:
    recipe = Recipe(base="img:1", setup=("a",))
    images = Images(FakeBox, SPEC, secrets=SecretStore(Path("/nowhere")), confirm=lambda _: True)
    a, b = images.image_name(recipe, "acme/widgets"), images.image_name(recipe, "acme/other")
    assert a != b and a != images.image_name(recipe)
    assert a.startswith(f"airlie-env-{recipe.key('img:1')}-")


def test_setup_builds_once_and_the_second_ensure_reuses_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="img:1", setup=("apt-get update", "uv sync"), env={"CI": "1"})
    name = run(rig.images.ensure(recipe, REPO))
    (box,) = rig.built()
    assert name == rig.images.image_name(recipe) == box.name
    assert box.created == [("img:1", 2, 4, 20)]
    assert box.boots == [Network("open")]
    assert box.state == "stopped" and not box.deleted
    assert [c.argv for c in box.calls] == [("sh", "-s"), ("sh", "-s"), ("sync",)]
    assert [c.stdin for c in box.calls[:2]] == [b"apt-get update\n", b"uv sync\n"]
    assert box.calls[0].env["CI"] == "1"  # the recipe's variables, on top of the box environment
    assert "PATH" in box.calls[0].env
    assert run(rig.images.ensure(recipe, REPO)) == name
    assert len(box.calls) == 3 and len(box.created) == 1


class StopRecordingBox(FakeBox):
    """Records `stop` among the execs, so the order of the guest's sync and the stop shows."""

    async def stop(self) -> None:
        self.calls.append(Call(("<stop>",), {}, None, None))
        await super().stop()


def test_the_build_syncs_the_guest_before_it_stops_and_saves_the_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    boxes: list[FakeBox] = []

    def box_for(name: str) -> FakeBox:
        found = [b for b in boxes if b.name == name and b.state != "absent"]
        if found:
            return found[0]
        boxes.append(StopRecordingBox(name, handlers={"sh": sh_ok}))
        return boxes[-1]

    images = Images(box_for, SPEC, secrets=SecretStore(tmp_path / "secrets"), confirm=rig.confirm)

    run(images.ensure(Recipe(base="i", setup=("make",)), REPO))

    assert [c.argv for c in boxes[-1].calls][-2:] == [("sync",), ("<stop>",)]


def test_a_build_whose_sync_fails_is_not_saved(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.sh = sh_ok
    original = rig.box_for

    def box_for(name: str) -> FakeBox:
        box = original(name)
        box.responses[("sync",)] = ExecResult(None, "error: command timed out after 20s")
        return box

    images = Images(box_for, SPEC, secrets=SecretStore(tmp_path / "secrets"), confirm=rig.confirm)

    with pytest.raises(RecipeError, match="sync"):
        run(images.ensure(Recipe(base="i", setup=("make",)), REPO))
    assert rig.built() == []


def test_an_image_is_built_under_a_temporary_name_and_renamed_when_done(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    name = run(rig.images.ensure(Recipe(base="i", setup=("x",)), REPO))
    (box,) = rig.built()
    assert box.renamed == [(f"{name}-building-t0k", name)]
    assert box.state == "stopped"  # stopped before the rename: the disk is whole when it gets its name


def test_a_half_built_image_is_never_served_as_cached(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="i", setup=("ok", "bad"))
    final = rig.images.image_name(recipe)
    rig.sh = lambda b, argv, stdin: ExecResult(2, "boom") if b"bad" in (stdin or b"") else ExecResult(0, "")
    rig.delete_fails = "disk busy"  # the cleanup fails: the partial disk stays
    with pytest.raises(RecipeError):
        run(rig.images.ensure(recipe, REPO))
    (partial,) = rig.built()
    assert partial.name == f"{final}-building-t0k"  # under its temporary name, never as `final`
    rig.delete_fails = None
    rig.sh = sh_ok
    assert run(rig.images.ensure(recipe, REPO)) == final  # the next ensure builds it properly
    assert sorted(b.name for b in rig.built()) == sorted([final, partial.name])
    assert len([b for b in rig.built() if b.name == final]) == 1


def test_setup_secrets_reach_the_guest_on_stdin_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="# token\nNPM_TOKEN=s3cr3t value\n\nOTHER=a'b\n")
    run(rig.images.ensure(Recipe(base="i", setup=("npm ci",), env={"CI": "1"}), REPO))
    (box,) = rig.built()
    call, _sync = box.calls
    assert call.stdin == b"export NPM_TOKEN='s3cr3t value'\nexport OTHER='a'\"'\"'b'\nnpm ci\n"
    seen = [*call.argv, *call.env.values(), *call.env, call.cwd or ""]
    assert not any("s3cr3t" in part or "a'b" in part for part in seen)
    assert "NPM_TOKEN" not in call.env
    assert not any("s3cr3t" in str(v) for v in (box.created, box.boots, box.files, box.renamed, box.name))


def test_an_image_built_with_secrets_is_not_shared_with_another_repository(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n")
    recipe = Recipe(base="i", setup=("x",))
    mine = run(rig.images.ensure(recipe, REPO))
    other = run(rig.images.ensure(recipe, "acme/other"))  # no secrets of its own: a build of its own
    assert mine != other and other == rig.images.image_name(recipe)
    assert len(rig.built()) == 2
    assert run(rig.images.ensure(recipe, REPO)) == mine  # the same repository reuses its own


def test_an_image_built_without_secrets_is_shared_by_repositories(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="i", setup=("x",))
    assert run(rig.images.ensure(recipe, REPO)) == run(rig.images.ensure(recipe, "acme/other"))
    assert len(rig.built()) == 1


def test_a_failed_setup_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="TOKEN=hunter2\n")

    def sh(b: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        if b"bad" in (stdin or b""):
            return ExecResult(2, "x" * 3000 + "boom with hunter2 inside")
        return ExecResult(0, "")

    rig.sh = sh
    with pytest.raises(RecipeError) as err:
        run(rig.images.ensure(Recipe(base="i", setup=("ok", "bad")), REPO))
    message = str(err.value)
    assert "setup failed (exit 2) at: bad" in message
    assert message.endswith("boom with *** inside")
    assert "hunter2" not in message
    assert len(message) < 2200  # the tail, not all the output
    (box,) = rig.building()
    assert box.deleted and box.state == "absent"


def test_short_secrets_are_not_redacted_so_the_tail_stays_readable(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="A=1\nB=ab\nLONGER=hunter2\n")
    rig.sh = lambda b, argv, stdin: ExecResult(1, "step 1 of 12 failed: ab hunter2")
    with pytest.raises(RecipeError) as err:
        run(rig.images.ensure(Recipe(base="i", setup=("x",)), REPO))
    assert str(err.value).endswith("step 1 of 12 failed: ab ***")


def test_a_setup_that_times_out_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.sh = lambda b, a, s: ExecResult(None, "error: command timed out after 99s")
    with pytest.raises(RecipeError, match=r"setup failed \(timed out\) at: sleep 9999"):
        run(rig.images.ensure(Recipe(base="i", setup=("sleep 9999",)), REPO))
    assert rig.building()[0].deleted


def test_a_boot_that_fails_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.fail_boot = "no answer"
    with pytest.raises(BoxUnavailable, match="no answer"):
        run(rig.images.ensure(Recipe(base="i", setup=("x",)), REPO))
    assert rig.building()[0].deleted


def test_a_failed_cleanup_does_not_mask_the_setup_error(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.sh = lambda b, a, s: ExecResult(1, "the real failure")
    rig.delete_fails = "tart delete failed: disk busy"
    with pytest.raises(RecipeError, match="the real failure") as err:
        run(rig.images.ensure(Recipe(base="i", setup=("x",)), REPO))
    assert any("disk busy" in note for note in err.value.__notes__)


def test_concurrent_ensures_of_one_image_build_it_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="i", setup=("x",))

    async def both() -> list[str]:
        return list(await asyncio.gather(rig.images.ensure(recipe, REPO), rig.images.ensure(recipe, REPO)))

    first, second = run(both())
    assert first == second
    assert len(rig.built()) == 1 and len(rig.built()[0].created) == 1


def test_a_new_recipe_with_secrets_needs_confirmation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n", allow=False)
    recipe = Recipe(base="i", setup=("x",))
    with pytest.raises(ConfirmationRequired, match="build_image:acme/widgets:"):
        run(rig.images.ensure(recipe, REPO))
    assert rig.asked == [f"build_image:{REPO}:{recipe.key('i')}"]
    assert rig.built() == []  # nothing was built


def test_a_confirmed_build_with_secrets_goes_ahead(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n", allow=True)
    recipe = Recipe(base="i", setup=("x",))
    run(rig.images.ensure(recipe, REPO))
    assert rig.built()[0].created and rig.asked == [f"build_image:{REPO}:{recipe.key('i')}"]


def test_no_secrets_means_no_question(tmp_path: Path) -> None:
    rig = Rig(tmp_path, allow=False)
    run(rig.images.ensure(Recipe(base="i", setup=("x",)), REPO))
    assert rig.asked == []


def test_a_cached_image_is_not_asked_about_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n")
    recipe = Recipe(base="i", setup=("x",))
    run(rig.images.ensure(recipe, REPO))
    run(rig.images.ensure(recipe, REPO))
    assert len(rig.asked) == 1


def test_secrets_of_a_repository_without_a_file_are_empty(tmp_path: Path) -> None:
    assert SecretStore(tmp_path).for_repo(REPO) == {}


def test_a_secrets_file_others_can_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "acme__widgets.env"
    path.write_text("K=v\n")
    path.chmod(0o640)
    with pytest.raises(RecipeError, match=r"mode 640.*chmod 600"):
        SecretStore(tmp_path).for_repo(REPO)


def test_a_malformed_secrets_line_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "acme__widgets.env"
    path.write_text("A=1\nnot a pair\n")
    path.chmod(0o600)
    with pytest.raises(RecipeError, match="line 2: expected KEY=VALUE"):
        SecretStore(tmp_path).for_repo(REPO)


def test_a_secret_name_that_is_not_an_identifier_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "acme__widgets.env"
    path.write_text("A B=1\n")
    path.chmod(0o600)
    with pytest.raises(RecipeError, match="line 1"):
        SecretStore(tmp_path).for_repo(REPO)


def test_secret_values_keep_equals_signs_and_spaces(tmp_path: Path) -> None:
    path = tmp_path / "acme__widgets.env"
    path.write_text("A= x=y \n")
    path.chmod(0o600)
    assert SecretStore(tmp_path).for_repo(REPO) == {"A": " x=y "}


@pytest.mark.parametrize("repo", ["../etc/passwd", "a/b/c", "nowhere", "a/..", "../x"])
def test_a_repository_that_is_not_owner_slash_name_is_refused(tmp_path: Path, repo: str) -> None:
    with pytest.raises(RecipeError, match="not an owner/name repository"):
        SecretStore(tmp_path).for_repo(repo)


def test_the_store_reads_only_its_own_directory(tmp_path: Path) -> None:
    (tmp_path / "acme__other.env").write_text("X=1\n")
    os.chmod(tmp_path / "acme__other.env", 0o600)
    assert SecretStore(tmp_path).for_repo(REPO) == {}


def test_a_build_is_live_while_it_runs_and_not_after_success_or_failure(tmp_path: Path) -> None:
    from airlie.boxes.images import LIVE_BUILDS

    rig = Rig(tmp_path)
    seen: list[set[str]] = []

    def sh(box: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        seen.append(set(LIVE_BUILDS))
        return ExecResult(0, "")

    rig.sh = sh
    run(rig.images.ensure(Recipe(base="img:1", setup=("a",)), REPO))
    assert seen[0] and all("-building-" in n for n in seen[0]) and not LIVE_BUILDS

    rig.sh = lambda b, argv, stdin: ExecResult(1, "boom")
    with pytest.raises(RecipeError):
        run(rig.images.ensure(Recipe(base="img:1", setup=("b",)), REPO))
    assert not LIVE_BUILDS
