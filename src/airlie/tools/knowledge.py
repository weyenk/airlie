"""search_knowledge: a role's knowledge directory, searched with BM25; hits are plain text with their path."""

from __future__ import annotations

from typing import Any

from airlie.skills.knowledge import KnowledgeIndex
from airlie.tools.protocol import ToolContext, ToolResult


class SearchKnowledge:
    name = "search_knowledge"
    description = (
        "Search this role's knowledge base (guides, past decisions, notes). "
        "Returns the best matching passages with their source file."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what you want to know, in a few words"},
            "k": {"type": "integer", "description": "passages to return (default 3)"},
        },
        "required": ["query"],
    }

    def __init__(self, index: KnowledgeIndex) -> None:
        self.index = index

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        hits = self.index.search(str(args["query"]), k=int(args.get("k") or 3))
        if not hits:
            return ToolResult(True, "no passages match; try other words or proceed without")
        return ToolResult(True, "\n\n".join(f"[{h.path}] (score {h.score:.1f})\n{h.text}" for h in hits))
