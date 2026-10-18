"""The studio prompts and the QA skill library carry the frontmatter the router needs."""

from pathlib import Path

from airlie.config import load_config
from airlie.skills import load_skills, routable_roles, split_frontmatter

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml
EIGHT = {"architect", "coder", "product", "research", "docs", "security", "design", "ux"}


def test_studio_prompts_have_summaries_and_the_right_routability() -> None:
    for name in EIGHT:
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("summary"), name
    for name in ("chief", "reviewer"):
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("routable") == "false", name


def test_qa_skill_library_has_nine_guides_with_summaries() -> None:
    skills = load_skills(ROOT / "skills/qa")
    assert {s.name for s in skills} == {
        "unit", "functional", "integration", "component", "contract", "e2e", "visual-regression",
        "mutation", "ai-skill",
    }  # fmt: skip


def test_studio_profiles_load_and_route_the_eight_roles() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    assert cfg.topology.kind == "studio" and cfg.engines["main"].max_kv_size == 131072
    assert cfg.engines["main"].server_parsers is True  # see the profile comment in airlie.yaml
    assert set(routable_roles(cfg, exclude={cfg.studio.chief_role, cfg.studio.review.role})) == EIGHT
    assert cfg.roles["coder"].model == "generalist"  # no Qwen3-Coder; the coder does not think (2026-10-18)
    assert "base" not in cfg.models and "qa" not in cfg.roles
