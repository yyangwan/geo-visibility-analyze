"""In-memory event bus for audit progress streaming.

Each audit gets a list of asyncio.Queue instances (one per SSE subscriber).
When a platform completes, the audit service publishes an event.
SSE endpoints subscribe and forward events to the client.
"""

import asyncio
from dataclasses import dataclass
from typing import Literal

from app.logging_config import get_logger

logger = get_logger("audit_events")


@dataclass
class PlatformEvent:
    """A single platform completion event."""
    type: Literal["platform_start", "platform_done", "platform_error", "audit_done", "audit_failed"]
    platform: str | None = None
    error: str | None = None


# audit_id -> list of subscriber queues
_subscribers: dict[int, list[asyncio.Queue]] = {}


def subscribe(audit_id: int) -> asyncio.Queue:
    """Create a subscriber queue for an audit's events."""
    q: asyncio.Queue = asyncio.Queue()
    if audit_id not in _subscribers:
        _subscribers[audit_id] = []
    _subscribers[audit_id].append(q)
    return q


def unsubscribe(audit_id: int, queue: asyncio.Queue) -> None:
    """Remove a subscriber queue."""
    if audit_id in _subscribers:
        try:
            _subscribers[audit_id].remove(queue)
        except ValueError:
            pass
        if not _subscribers[audit_id]:
            del _subscribers[audit_id]


def publish(audit_id: int, event: PlatformEvent) -> None:
    """Publish an event to all subscribers of an audit."""
    queues = _subscribers.get(audit_id, [])
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("event_queue_full", audit_id=audit_id)
