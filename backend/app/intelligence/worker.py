"""Run independently: python -m app.intelligence.worker."""

import asyncio
import logging
import os
import signal
import socket
from uuid import uuid4

from app.config import settings
from app.database import async_session
from app.intelligence.blob import configured_store
from app.intelligence.monitor import WorkerMonitor
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, LeaseLost

logger = logging.getLogger("intelligence.worker")


async def execute(queue, pipeline, lease):
    if lease.kind not in ("fetch_document", "analyze_run", "refresh_run"):
        await queue.fail(lease, "unsupported_job_kind", retry=False)
        return

    async def maintain():
        while True:
            await asyncio.sleep(30)
            await queue.heartbeat(lease)

    heart = asyncio.create_task(maintain())
    work = asyncio.create_task(getattr(pipeline, lease.kind)(lease))
    try:
        done, _ = await asyncio.wait({heart, work}, return_when=asyncio.FIRST_COMPLETED)
        if heart in done:
            await heart
            raise LeaseLost("Heartbeat stopped")
        await work
    except LeaseLost:
        logger.warning("lease_lost job=%s", lease.id)
    except Exception as error:
        # Never log URLs, model response bodies, auth headers, or source text.
        logger.error("job_error job=%s class=%s", lease.id, type(error).__name__)
        try:
            await queue.fail(lease, type(error).__name__)
        except LeaseLost:
            pass
    finally:
        heart.cancel()
        work.cancel()
        await asyncio.gather(heart, work, return_exceptions=True)


async def main():
    if not settings.intelligence_enabled:
        raise RuntimeError(
            "Enable intelligence after migrations and storage verification"
        )
    from app.main import _check_schema_revision

    await asyncio.to_thread(_check_schema_revision)
    try:
        blobs = configured_store()
        if hasattr(blobs, "verify_configuration"):
            await blobs.verify_configuration()
    except Exception as error:
        logger.error("storage_startup_error class=%s", type(error).__name__)
        raise RuntimeError("Storage configuration verification failed") from None
    queue = JobQueue(async_session, f"{socket.gethostname()}-{uuid4().hex[:8]}")
    pipeline = Pipeline(queue, blobs)
    from app.intelligence.refresh import recover_settled_refreshes

    async with async_session() as db, db.begin():
        await recover_settled_refreshes(db)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
    monitor = WorkerMonitor(
        async_session, blobs, os.getenv("AISCOPE_INTELLIGENCE_WORKER_MONITOR_ID")
    )
    await monitor.beat()
    tasks = [
        asyncio.create_task(monitor.heartbeat_loop()),
        asyncio.create_task(monitor.probe_loop()),
    ]
    try:
        while not stop.is_set():
            await queue.reap()
            from app.intelligence.sampling import reconcile_capture_failures

            async with async_session() as db, db.begin():
                await reconcile_capture_failures(db)
            lease = await queue.claim()
            if lease:
                monitor.state = "working"
                try:
                    await execute(queue, pipeline, lease)
                finally:
                    monitor.state = "idle"
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await monitor.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
