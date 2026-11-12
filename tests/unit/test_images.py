import asyncio
import os
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.images import ConfirmationRequired, Images, SecretStore
from airlie.boxes.protocol import ExecResult, Network
from airlie.boxes.recipe import Recipe, RecipeError
from airlie.config.schema import BoxesSpec
from tests.support.box import FakeBox

SPEC = BoxesSpec(cpus=2, memory_gb=4, disk_gb=20, boot_timeout_s=30, setup_timeout_s=99)
REPO = "acme/widgets"


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def sh_ok(box: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
    return ExecResult(0, "")


class Rig:
    def __init__(self, tmp_path: Path, *, secrets: str | None = None, allow: bool = True) -> None:
        self.boxes: dict[str, FakeBox] = {}
        self.asked: list[str] = []
        self.allow = allow
        sdir = tmp_path / "secrets"
        sdir.mkdir()
        if secrets is not None:
            path = sdir / "acme__widgets.env"
            path.write_text(secrets)
            path.chmod(0o600)
        self.images = Images(self.box_for, SPEC, secrets=SecretStore(sdir), confirm=self.confirm)

    def box_for(self, name: str) -> FakeBox:
        if name not in self.boxes:
            self.boxes[name] = FakeBox(name, handlers={"sh": sh_ok})
        return self.boxes[name]

    def confirm(self, what: str) -> bool:
        self.asked.append(what)
        return self.allow


def test_no_setup_uses_the_base_image_without_building(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert run(rig.images.ensure(Recipe(base="img:1"), REPO)) == "img:1"
    assert run(rig.images.ensure(Recipe(), REPO)) == SPEC.image
    assert rig.boxes == {}


def test_image_name_is_airlie_env_and_the_key() -> None:
    recipe = Recipe(base="img:1", setup=("a",))
    images = Images(FakeBox, SPEC, secrets=SecretStore(Path("/nowhere")), confirm=lambda _: True)
    assert images.image_name(recipe) == f"airlie-env-{recipe.key('img:1')}"
    assert images.image_name(Recipe(setup=("a",))) == f"airlie-env-{Recipe(setup=('a',)).key(SPEC.image)}"


def test_setup_builds_once_and_the_second_ensure_reuses_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="img:1", setup=("apt-get update", "uv sync"), env={"CI": "1"})
    name = run(rig.images.ensure(recipe, REPO))
    box = rig.boxes[name]
    assert name == rig.images.image_name(recipe)
    assert box.created == [("img:1", 2, 4, 20)]
    assert box.boots == [Network("open")]
    assert box.state == "stopped" and not box.deleted
    assert [c.argv for c in box.calls] == [("sh", "-s")] * 2
    assert [c.stdin for c in box.calls] == [b"apt-get update\n", b"uv sync\n"]
    assert box.calls[0].env["CI"] == "1"  # the recipe's variables, on top of the box environment
    assert "PATH" in box.calls[0].env
    assert run(rig.images.ensure(recipe, REPO)) == name
    assert len(box.calls) == 2 and len(box.created) == 1


def test_setup_secrets_reach_the_guest_on_stdin_only(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="# token\nNPM_TOKEN=s3cr3t value\n\nOTHER=a'b\n")
    name = run(rig.images.ensure(Recipe(base="i", setup=("npm ci",), env={"CI": "1"}), REPO))
    box = rig.boxes[name]
    (call,) = box.calls
    assert call.stdin == b"export NPM_TOKEN='s3cr3t value'\nexport OTHER='a'\"'\"'b'\nnpm ci\n"
    seen = [*call.argv, *call.env.values(), *call.env, call.cwd or ""]
    assert not any("s3cr3t" in part or "a'b" in part for part in seen)
    assert "NPM_TOKEN" not in call.env
    assert not any("s3cr3t" in str(v) for v in (box.created, box.boots, box.files))


def test_secrets_are_not_part_of_the_key(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_secrets = Rig(tmp_path / "a", secrets="K=v\n")
    plain = Rig(tmp_path / "b")
    recipe = Recipe(base="i", setup=("x",))
    assert run(with_secrets.images.ensure(recipe, REPO)) == run(plain.images.ensure(recipe, REPO))


def test_a_failed_setup_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="TOKEN=hunter2\n")
    box = rig.box_for(rig.images.image_name(Recipe(base="i", setup=("ok", "bad"))))

    def sh(b: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        assert stdin is not None
        if b"bad" in stdin:
            return ExecResult(2, "x" * 3000 + "boom with hunter2 inside")
        return ExecResult(0, "")

    box.handlers["sh"] = sh
    with pytest.raises(RecipeError) as err:
        run(rig.images.ensure(Recipe(base="i", setup=("ok", "bad")), REPO))
    message = str(err.value)
    assert "setup failed (exit 2) at: bad" in message
    assert message.endswith("boom with *** inside")
    assert "hunter2" not in message
    assert len(message) < 2200  # the tail, not all the output
    assert box.deleted and box.state == "absent"


def test_a_setup_that_times_out_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="i", setup=("sleep 9999",))
    box = rig.box_for(rig.images.image_name(recipe))
    box.handlers["sh"] = lambda b, a, s: ExecResult(None, "error: command timed out after 99s")
    with pytest.raises(RecipeError, match=r"setup failed \(timed out\) at: sleep 9999"):
        run(rig.images.ensure(recipe, REPO))
    assert box.deleted


def test_a_boot_that_fails_deletes_the_partial_image(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    recipe = Recipe(base="i", setup=("x",))
    box = rig.box_for(rig.images.image_name(recipe))
    box.fail_boot = "no answer"
    with pytest.raises(Exception, match="no answer"):
        run(rig.images.ensure(recipe, REPO))
    assert box.deleted


def test_a_new_recipe_with_secrets_needs_confirmation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n", allow=False)
    recipe = Recipe(base="i", setup=("x",))
    with pytest.raises(ConfirmationRequired, match="build_image:acme/widgets:"):
        run(rig.images.ensure(recipe, REPO))
    assert rig.asked == [f"build_image:{REPO}:{recipe.key('i')}"]
    assert rig.boxes[rig.images.image_name(recipe)].created == []  # nothing was built


def test_a_confirmed_build_with_secrets_goes_ahead(tmp_path: Path) -> None:
    rig = Rig(tmp_path, secrets="K=v\n", allow=True)
    recipe = Recipe(base="i", setup=("x",))
    name = run(rig.images.ensure(recipe, REPO))
    assert rig.boxes[name].created and rig.asked == [f"build_image:{REPO}:{recipe.key('i')}"]


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
