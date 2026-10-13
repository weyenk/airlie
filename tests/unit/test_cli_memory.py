"""`airlie memory RUN_ID` renders a run's shared memory bank with cross-team access counts."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.lts import MemoryEntry, SharedMemoryBank

runner = CliRunner()


def test_memory_command_lists_entries(tmp_path: Path) -> None:
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    run_dir = tmp_path / "state" / "runs" / "r7"
    run_dir.mkdir(parents=True)
    bank = SharedMemoryBank(persist_path=run_dir / "memory.jsonl")
    bank.add_entry(
        MemoryEntry(
            id="mem-1", team_id="team-1", step_num=3, summary_key="tests fail on edge", raw_value="..."
        )
    )
    bank.retrieve_value("mem-1", "team-2")
    r = runner.invoke(cli.app, ["memory", "r7", "--config", str(tmp_path / "airlie.yaml")])
    assert r.exit_code == 0, r.output
    assert "mem-1" in r.output and "tests fail on edge" in r.output and "team-1" in r.output
    assert "1" in r.output  # access_count
    r = runner.invoke(cli.app, ["memory", "nope", "--config", str(tmp_path / "airlie.yaml")])
    assert r.exit_code == 1 and "no memory log" in r.output
