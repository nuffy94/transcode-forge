"""WebSocket endpoint — streams real-time updates from Redis pub/sub to the UI."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket) -> Any:
    """Stream real-time job progress and worker updates to the UI.

    Subscribes to Redis pub/sub channels and forwards events as
    HTML fragments that HTMX can swap into the DOM.
    """
    # Origin validation — only accept connections from same host
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    if origin and host:
        # Strip protocol from origin for comparison
        origin_host = origin.split("://", 1)[-1].rstrip("/")
        if origin_host != host:
            logger.warning("WebSocket rejected: origin=%s, host=%s", origin, host)
            await websocket.close(code=1008)
            return

    await websocket.accept()
    redis: Redis | None = websocket.app.state.redis
    if redis is None:
        # No Redis — close gracefully (HTMX polling is the fallback)
        await websocket.close(code=1001, reason="Redis not available")
        return
    prefix = websocket.app.state.settings.redis_prefix

    pubsub = redis.pubsub()
    channel = f"{prefix}:pub:progress"

    try:
        await pubsub.subscribe(channel)
        logger.info("WebSocket client connected, subscribed to %s", channel)

        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = json.loads(message["data"])
                # Send as JSON — the client JS can update the DOM
                await websocket.send_json(data)

            # Small sleep to prevent tight loop when no messages
            await asyncio.sleep(0.1)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
