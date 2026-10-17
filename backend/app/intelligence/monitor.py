"""Independent heartbeat. Probes describe configuration, not object integrity."""

import asyncio
import logging
import re
import shutil
import tempfile
from uuid import uuid4

from app.intelligence.models import WorkerHeartbeat
from app.intelligence.queue import db_now

logger = logging.getLogger("intelligence.worker")
HEARTBEAT_SECONDS = 15
RECENT_SECONDS = 90
PROBE_SECONDS = 60
MIN_TEMP_FREE_BYTES = 256 * 1024 * 1024


class WorkerMonitor:
    def __init__(self, session_factory, blobs, monitor_id=None):
        if monitor_id is not None and not re.fullmatch(r"[a-f0-9]{32}", monitor_id):
            raise ValueError("Worker monitor identity must be 32 lowercase hex characters")
        self.sessions = session_factory
        self.blobs = blobs
        self.id = uuid4().hex if monitor_id is None else monitor_id
        self.state = "idle"
        self.storage_status = "unknown"
        self.disk_status = "unknown"

    async def beat(self, *, probed=False):
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            row = await db.get(WorkerHeartbeat, self.id)
            if row is None:
                row = WorkerHeartbeat(id=self.id, started_at=now)
                db.add(row)
            row.last_seen_at = now
            row.state = self.state
            row.storage_status = self.storage_status
            row.disk_status = self.disk_status
            if probed:
                row.storage_checked_at = now
                row.disk_checked_at = now

    async def probe(self):
        try:
            if hasattr(self.blobs, "verify_configuration"):
                await asyncio.wait_for(self.blobs.verify_configuration(), timeout=10)
                self.storage_status = "configuration_verified"
            else:
                self.storage_status = "development_local"
        except Exception as error:
            self.storage_status = "failed"
            logger.warning("storage_probe_error class=%s", type(error).__name__)
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, tempfile.gettempdir())
            self.disk_status = "ok" if usage.free >= MIN_TEMP_FREE_BYTES else "low"
        except Exception as error:
            self.disk_status = "unknown"
            logger.warning("disk_probe_error class=%s", type(error).__name__)
        await self.beat(probed=True)

    async def heartbeat_loop(self):
        while True:
            try:
                await self.beat()
            except Exception as error:
                logger.warning("worker_heartbeat_error class=%s", type(error).__name__)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def probe_loop(self):
        while True:
            try:
                await self.probe()
            except Exception as error:
                logger.warning(
                    "worker_probe_record_error class=%s", type(error).__name__
                )
            await asyncio.sleep(PROBE_SECONDS)

    async def close(self):
        self.state = "stopped"
        try:
            await self.beat()
        except Exception as error:
            logger.warning("worker_stop_record_error class=%s", type(error).__name__)
