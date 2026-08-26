import asyncio
from typing import Any, Awaitable, Callable


class SingleFlight:
    """Coalesces concurrent calls for the same key into a single execution.

    N concurrent callers for the same key result in exactly one ``factory()``
    execution; all callers share the same result (or exception). The in-flight
    entry is cleaned up after completion (success or error) so subsequent calls
    re-execute.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            future = self._inflight.get(key)
            if future is not None:
                leader = False
            else:
                loop = asyncio.get_event_loop()
                future = loop.create_future()
                self._inflight[key] = future
                leader = True

        if not leader:
            # Followers await the leader's result; exceptions propagate to all.
            return await future

        try:
            result = await factory()
        except Exception as e:
            future.set_exception(e)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            async with self._lock:
                # Remove the key so future calls re-execute.
                if self._inflight.get(key) is future:
                    del self._inflight[key]


# Module-level singleton.
single_flight = SingleFlight()
