"""Skills: frontmatter summaries drive routing (stage A over roles, stage B over a role's skill library)."""

from pathlib import Path

import yaml

from airlie.config import RoleSpec, load_config
from airlie.skills import (
    load_skills,
    prompt_text,
    role_summary,
    routable_roles,
    skill_criteria,
    split_frontmatter,
)


def test_split_frontmatter_returns_fields_and_body() -> None:
    meta, body = split_frontmatter("---\nsummary: Test strategy\nrole: qa\n---\n\n# qa\n\nText.\n")
    assert meta == {"summary": "Test strategy", "role": "qa"} and body == "# qa\n\nText.\n"
    assert split_frontmatter("# plain\n") == ({}, "# plain\n")


def test_prompt_text_strips_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "qa.md"
    p.write_text("---\nsummary: s\n---\n# qa\n")
    assert prompt_text(p) == "# qa\n"


def test_role_summary_prefers_frontmatter_over_config(tmp_path: Path) -> None:
    p = tmp_path / "qa.md"
    p.write_text("---\nsummary: from file\n---\n# qa\n")
    assert role_summary(RoleSpec(prompt=p, model="m", summary="from config")) == "from file"
    q = tmp_path / "docs.md"
    q.write_text("# docs\n")
    assert role_summary(RoleSpec(prompt=q, model="m", summary="from config")) == "from config"
    assert role_summary(RoleSpec(prompt=q, model="m")) is None


def test_routable_roles_need_a_summary(tmp_path: Path) -> None:
    for name, text in {
        "coder": "---\nsummary: Implement code\n---\n# coder\n",
        "qa": "---\nsummary: Tests\n---\n# qa\n",
        "chief": "---\nsummary: The chief\n---\n# chief\n",
        "summarizer": "# summarizer\n",
        "judge": "# judge\n",
        "aggregator": "# aggregator\n",
    }.items():
        (tmp_path / f"{name}.md").write_text(text)
    names = ("coder", "qa", "chief", "summarizer", "judge", "aggregator")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["m"]}},
        "models": {"m": {"repo": "o/m", "revision": "r"}},
        "roles": {n: {"prompt": f"{n}.md", "model": "m"} for n in names},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_path / "airlie.yaml")
    assert routable_roles(cfg, exclude={"chief"}) == {"coder": "Implement code", "qa": "Tests"}


def test_load_skills_reads_summaries_and_skips_files_without(tmp_path: Path) -> None:
    (tmp_path / "unit.md").write_text("---\nsummary: Pure functions in isolation\n---\n# Unit\nBody\n")
    (tmp_path / "e2e.md").write_text("---\nsummary: Full journeys\n---\n# E2E\n")
    (tmp_path / "README.md").write_text("# index, no summary\n")
    skills = load_skills(tmp_path)
    assert [s.name for s in skills] == ["e2e", "unit"]
    assert skills[1].summary == "Pure functions in isolation" and skills[1].text.startswith("# Unit")
    assert skill_criteria(skills) == {"e2e": "Full journeys", "unit": "Pure functions in isolation"}


def test_routable_false_in_frontmatter_excludes_the_role(tmp_path: Path) -> None:
    (tmp_path / "chief.md").write_text("---\nsummary: the chief\nroutable: false\n---\n# chief\n")
    (tmp_path / "coder.md").write_text("---\nsummary: Implement code\n---\n# coder\n")
    for n in ("summarizer", "judge", "aggregator"):
        (tmp_path / f"{n}.md").write_text(f"# {n}\n")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["m"]}},
        "models": {"m": {"repo": "o/m", "revision": "r"}},
        "roles": {
            n: {"prompt": f"{n}.md", "model": "m"}
            for n in ("chief", "coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    assert routable_roles(load_config(tmp_path / "airlie.yaml")) == {"coder": "Implement code"}
