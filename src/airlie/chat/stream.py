"""The AI SDK UI message stream (v1) as a pure encoder: each call returns the SSE strings to send.

One part (text or reasoning) is open at a time; a tool call, a step boundary and the finish close it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

STREAM_HEADERS: dict[str, str] = {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    "x-vercel-ai-ui-message-stream": "v1",
    "x-accel-buffering": "no",
}
DONE = "data: [DONE]\n\n"


def sse(chunk: Mapping[str, Any]) -> str:
    """One event: compact JSON never holds a raw newline, so it stays on one `data:` line."""
    return "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"


class StreamWriter:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id
        self._open: tuple[str, str] | None = None  # (kind, part id)
        self._parts = 0

    def start(self) -> list[str]:
        return [sse({"type": "start", "messageId": self._message_id}), sse({"type": "start-step"})]

    def text(self, delta: str) -> list[str]:
        return self._delta("text", delta)

    def reasoning(self, delta: str) -> list[str]:
        return self._delta("reasoning", delta)

    def tool_call(self, call_id: str, name: str, arguments: str) -> list[str]:
        try:
            parsed: Any = json.loads(arguments)
        except ValueError:
            parsed = {"_raw": arguments}
        return [
            *self._close(),
            sse({"type": "tool-input-start", "toolCallId": call_id, "toolName": name}),
            sse({"type": "tool-input-available", "toolCallId": call_id, "toolName": name, "input": parsed}),
        ]

    def tool_input_error(self, call_id: str, name: str, raw: str, error: str) -> list[str]:
        """A call whose arguments did not parse: the tool never runs, and the SDK shows the error on it."""
        return [
            *self._close(),
            sse({"type": "tool-input-start", "toolCallId": call_id, "toolName": name}),
            sse(
                {
                    "type": "tool-input-error",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": raw,
                    "errorText": error,
                }
            ),
        ]

    def tool_result(self, call_id: str, output: str, ok: bool) -> list[str]:
        if ok:
            return [sse({"type": "tool-output-available", "toolCallId": call_id, "output": output})]
        return [sse({"type": "tool-output-error", "toolCallId": call_id, "errorText": output})]

    def tool_approval_request(self, approval_id: str, call_id: str) -> list[str]:
        """The tool call at `call_id` waits for a person's yes: the client shows the request and answers it in
        its next turn's `approvals`. Sent after the call's own result so the part ends as requested."""
        return [sse({"type": "tool-approval-request", "approvalId": approval_id, "toolCallId": call_id})]

    def data(
        self, name: str, payload: Any, *, transient: bool = False, part_id: str | None = None
    ) -> list[str]:
        """A data part; one with `part_id` replaces the client's part of the same type and id."""
        chunk: dict[str, Any] = {"type": f"data-{name}"}
        if part_id is not None:
            chunk["id"] = part_id
        chunk["data"] = payload
        if transient:
            chunk["transient"] = True
        return [sse(chunk)]

    def next_step(self) -> list[str]:
        return [*self._close(), sse({"type": "finish-step"}), sse({"type": "start-step"})]

    def error(self, text: str) -> list[str]:
        return [sse({"type": "error", "errorText": text})]

    def finish(self, usage: Mapping[str, Any]) -> list[str]:
        return [
            *self._close(),
            sse({"type": "finish-step"}),
            sse({"type": "finish", "messageMetadata": {"usage": dict(usage)}}),
            DONE,
        ]

    def _delta(self, kind: str, delta: str) -> list[str]:
        out: list[str] = []
        if self._open is None or self._open[0] != kind:
            out.extend(self._close())
            self._parts += 1
            self._open = (kind, f"{self._message_id}-{kind}-{self._parts}")
            out.append(sse({"type": f"{kind}-start", "id": self._open[1]}))
        out.append(sse({"type": f"{kind}-delta", "id": self._open[1], "delta": delta}))
        return out

    def _close(self) -> list[str]:
        if self._open is None:
            return []
        kind, part_id = self._open
        self._open = None
        return [sse({"type": f"{kind}-end", "id": part_id})]
