"""Advisors for tests: one that answers with fixed advice and records what it was asked and told, and one that
fails at every call, as a broken implementation of the studio's `Advisor` protocol could."""

from __future__ import annotations

from collections.abc import Sequence

from airlie.engine.types import Message


class RecordingAdvisor:
    """Answers every question with `reply` and records each call."""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.stuck_calls: list[tuple[str, str, str, int]] = []  # (subtask, role, told, messages seen)
        self.stuck_settles: list[tuple[str, bool]] = []
        self.advised: list[tuple[str, str, str, str, tuple[str, ...], str]] = []
        self.settled: list[tuple[str, str, str | None]] = []  # (trigger, outcome, subtask)

    async def stuck(self, subtask: str, role: str, told: str, messages: Sequence[Message]) -> str | None:
        self.stuck_calls.append((subtask, role, told, len(messages)))
        return self.reply

    def stuck_settled(self, subtask: str, helped: bool) -> None:
        self.stuck_settles.append((subtask, helped))

    async def advise(
        self,
        trigger: str,
        subtask: str,
        role: str,
        told: str,
        *,
        kinds: Sequence[str] = (),
        evidence: str = "",
    ) -> str | None:
        self.advised.append((trigger, subtask, role, told, tuple(kinds), evidence))
        return self.reply

    def settle(self, trigger: str, outcome: str, detail: str = "", *, subtask: str | None = None) -> None:
        self.settled.append((trigger, outcome, subtask))


class RaisingAdvisor:
    """Fails at every call."""

    async def stuck(self, subtask: str, role: str, told: str, messages: Sequence[Message]) -> str | None:
        raise RuntimeError("advisor down")

    def stuck_settled(self, subtask: str, helped: bool) -> None:
        raise RuntimeError("advisor down")

    async def advise(
        self,
        trigger: str,
        subtask: str,
        role: str,
        told: str,
        *,
        kinds: Sequence[str] = (),
        evidence: str = "",
    ) -> str | None:
        raise RuntimeError("advisor down")

    def settle(self, trigger: str, outcome: str, detail: str = "", *, subtask: str | None = None) -> None:
        raise RuntimeError("advisor down")


class SettleRaisingAdvisor(RecordingAdvisor):
    """Advises, then fails when told what came of it."""

    def stuck_settled(self, subtask: str, helped: bool) -> None:
        raise RuntimeError("ledger down")

    def settle(self, trigger: str, outcome: str, detail: str = "", *, subtask: str | None = None) -> None:
        raise RuntimeError("ledger down")
