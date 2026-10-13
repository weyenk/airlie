"""`always` (the spec 04 §3 blueprint's `return True`) and `never` (memory-free baseline)."""

from __future__ import annotations

from airlie.lts.controller.protocol import AdmissionContext, Decision


class AlwaysAdmit:
    kind = "always"

    async def decide(self, ctx: AdmissionContext) -> Decision:
        return Decision(True, 1.0, "always")


class NeverAdmit:
    kind = "never"

    async def decide(self, ctx: AdmissionContext) -> Decision:
        return Decision(False, 0.0, "never")
