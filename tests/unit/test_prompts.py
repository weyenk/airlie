"""The studio prompts and the QA skill library carry the frontmatter the router needs."""

from pathlib import Path

from airlie.config import load_config
from airlie.skills import load_skills, routable_roles, split_frontmatter

ROOT = Path(__file__).resolve().parents[2]
NINE = {"architect", "coder", "product", "research", "docs", "qa", "security", "design", "ux"}


def test_studio_prompts_have_summaries_and_the_right_routability() -> None:
    for name in NINE:
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


def test_studio_profiles_load_and_route_the_nine_roles() -> None:
    cfg = load_config(ROOT / "airlie.yaml", topology="studio")
    assert cfg.topology.kind == "studio" and cfg.engines["main"].max_kv_size == 131072
    assert cfg.engines["main"].server_parsers is True  # see the profile comment in airlie.yaml
    assert set(routable_roles(cfg, exclude={cfg.studio.chief_role, cfg.studio.review.role})) == NINE
    q = load_config(ROOT / "airlie.yaml", topology="studio-qwen38")
    assert q.topology.kind == "studio" and q.roles["coder"].model == "generalist"
    assert q.roles["coder"].thinking is True
