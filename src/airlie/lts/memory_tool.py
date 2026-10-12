"""`retrieve_value(entry_id)`: how a team fetches another team's full output (spec 01 §4 step 7)."""

from __future__ import annotations

from typing import Any

from airlie.lts.bank import SharedMemoryBank
from airlie.tools.protocol import ToolContext, ToolResult


class RetrieveValue:
    name = "retrieve_value"
    description = (
        "Fetch the full stored output behind a shared-memory key listed under 'Shared Memory Keys Available'."
        " "
        "Use it when a key is relevant to your current step instead of re-deriving the information."
    )
    parameters = {"type": "object", "properties": {"entry_id": {"type": "string"}}, "required": ["entry_id"]}

    def __init__(self, bank: SharedMemoryBank, team_id: str, on_retrieve: Any = None) -> None:
        self.bank = bank
        self.team_id = team_id
        self.on_retrieve = on_retrieve

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        entry_id = str(args.get("entry_id", ""))
        value = self.bank.retrieve_value(entry_id, self.team_id)
        if self.on_retrieve:
            self.on_retrieve(self.team_id, entry_id, value is not None)
        if value is None:
            known = ", ".join(i for i, _ in self.bank.get_summaries()[-10:]) or "(none)"
            return ToolResult(False, f"error: no memory entry '{entry_id}'; recent ids: {known}")
        return ToolResult(True, value[: ctx.output_cap_chars])
