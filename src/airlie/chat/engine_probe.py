"""The chat turn's pre-flight: a first answer from the engine, asked before the turn takes anything.

Once the turn's SSE stream is open, an engine that never answers has no way left to say so: the person keeps a
spinner and the server log gets nothing pointing at the engine. So a turn asks for the engine's model list --
the cheapest request it makes -- and is refused with a status when nothing answers.

It lives apart from `chat.api` because serve imports it at module level and `chat.api` imports serve's own
dependencies; the cycle has to break somewhere, and this is the smallest place it can.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("airlie.chat.engine_probe")

ENGINE_PROBE_TIMEOUT_S = 5.0


class EngineOffline(Exception):
    """Nothing answers the engine, so no turn of this request can be answered either."""


class EngineProber:
    """One small GET of the engine's model list, on a client this owns, its pool opened once and reused. A
    server that answers 401 or 404 counts as reachable, because it answered.

    It costs a deployment one round trip per turn, against a 600 s read timeout: a turn that was going to
    hang now says "not reachable" in seconds. Pass `create_app(chat_engine_probe=...)` to drive it elsewhere.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __call__(self, base_url: str) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport, timeout=httpx.Timeout(ENGINE_PROBE_TIMEOUT_S)
            )
        url = f"{base_url.rstrip('/')}/models"
        try:
            response = await self._client.get(url)
            await response.aread()  # a streamed body cannot be closed sync; reading it returns the pool
        except httpx.HTTPStatusError as e:  # it refused, which is still an answer
            log.debug("engine probe %s answered %s", url, e.response.status_code)
        except httpx.HTTPError as e:
            raise EngineOffline(f"the model engine at {url} is not reachable: {e}") from e
        except Exception as e:  # a probe that cannot be asked is not ok, whatever else it turns out to be
            raise EngineOffline(f"the model engine at {url} could not be asked: {e}") from e

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
