"""The studio prompts and the QA skill library carry the frontmatter the router needs."""

from pathlib import Path

from airlie.skills import load_skills, split_frontmatter

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml
ROUTABLE_STUDIO = {
    "architect",
    "product",
    "research",
    "docs",
    "security",
    "design",
    "ux",
}


def test_studio_prompts_have_summaries_and_the_right_routability() -> None:
    for name in ROUTABLE_STUDIO:
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("summary"), name
    for name in ("chief", "reviewer"):
        meta, _ = split_frontmatter((ROOT / "prompts/roles" / f"{name}.md").read_text())
        assert meta.get("routable") == "false", name


def test_qa_skill_library_has_nine_guides_with_summaries() -> None:
    skills = load_skills(ROOT / "docs/wiki/qa")
    assert {s.name for s in skills} == {
        "unit", "functional", "integration", "component", "contract", "e2e", "visual-regression",
        "mutation", "ai-skill",
    }  # fmt: skip


def test_the_chat_prompt_is_not_routable_and_says_replies_are_prose_never_json() -> None:
    meta, body = split_frontmatter((ROOT / "prompts/roles/chat.md").read_text())
    assert meta.get("summary") and meta.get("routable") == "false"
    assert "prose" in body and "Never reply with JSON" in body
