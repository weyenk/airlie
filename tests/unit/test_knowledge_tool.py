"""search_knowledge: BM25 over markdown chunks; plain-text hits with their source path."""

import json
from pathlib import Path

from airlie.engine import ToolCall
from airlie.skills.knowledge import KnowledgeIndex
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.knowledge import SearchKnowledge


def corpus(tmp_path: Path) -> Path:
    k = tmp_path / "knowledge"
    (k / "sub").mkdir(parents=True)
    (k / "auth.md").write_text("# Auth\n\nRate limiting on login uses a token bucket per IP.\n")
    (k / "sub" / "billing.md").write_text("# Billing\n\nInvoices are generated nightly by the cron job.\n")
    (k / "notes.txt").write_text("plain text files count too: the cache is redis\n")
    return k


def test_index_ranks_the_matching_chunk_first(tmp_path: Path) -> None:
    idx = KnowledgeIndex.build(corpus(tmp_path))
    assert idx.size == 3
    hits = idx.search("login rate limiting", k=2)
    assert hits[0].path == "auth.md" and "token bucket" in hits[0].text and hits[0].score > 0


async def test_search_knowledge_tool_returns_hits_as_text(tmp_path: Path) -> None:
    reg = ToolRegistry(
        [SearchKnowledge(KnowledgeIndex.build(corpus(tmp_path)))], ToolContext(worktree=tmp_path)
    )
    res = await reg.call(ToolCall("c", "search_knowledge", json.dumps({"query": "invoices cron", "k": 1})))
    assert res.ok and res.output.startswith("[sub/billing.md]") and "nightly" in res.output


def test_knowledge_empty_dir_and_no_overlap(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    idx = KnowledgeIndex.build(empty)
    assert idx.size == 0 and idx.search("anything") == []
    assert KnowledgeIndex.build(corpus(tmp_path)).search("zzqx") == []
