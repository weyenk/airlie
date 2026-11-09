"""`airlie workflows catalog`: every check a node may name, printed as a table."""

from __future__ import annotations

from typer.testing import CliRunner

from airlie import cli
from airlie.story.flow import STORY_CATALOG

runner = CliRunner()


def test_workflows_catalog_prints_every_catalog_entry() -> None:
    res = runner.invoke(cli.app, ["workflows", "catalog"], env={"COLUMNS": "300"})
    assert res.exit_code == 0, res.stdout
    for col in ("name", "title", "inputs", "outputs", "args", "transient", "every_run"):
        assert col in res.stdout
    described = STORY_CATALOG.describe()
    assert described
    for c in described:
        assert c["name"] in res.stdout
    assert "stages" in res.stdout  # story.costed's args
