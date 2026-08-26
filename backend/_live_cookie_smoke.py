import asyncio
import json
import logging

from app.integrations.cookie_manager import CookieManager, CookieRefreshError

logging.disable(logging.CRITICAL)
events = []


def sink(event):
    events.append(event)
    print(json.dumps(event, separators=(",", ":")))


async def main():
    manager = CookieManager(telemetry_sink=sink)
    try:
        await manager.refresh_cookies(max_wait=60, poll_interval=5, force=True)
        result = {
            "stage": "smailpro_cookie",
            "status": "ok",
            "reason_code": "refresh_complete",
            "elapsed_ms": sum(event["elapsed_ms"] for event in events),
        }
    except CookieRefreshError as exc:
        result = {
            "stage": exc.stage.value,
            "status": "failed",
            "reason_code": exc.reason_code.value,
            "elapsed_ms": sum(event["elapsed_ms"] for event in events),
        }
    print(json.dumps(result, separators=(",", ":")))


asyncio.run(main())
