import asyncio
import threading

import pytest

from app.intelligence import report_pdf


@pytest.mark.asyncio
async def test_cancelled_requests_keep_render_permits_until_threads_complete(monkeypatch):
    loop = asyncio.get_running_loop()
    both_running = asyncio.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = peak = started = 0

    def render(report, detail):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            if started == 2:
                loop.call_soon_threadsafe(both_running.set)
        try:
            assert release.wait(5), "test did not release blocked renders"
            return b"pdf"
        finally:
            with lock:
                active -= 1

    slots = asyncio.Semaphore(2)
    monkeypatch.setattr(report_pdf, "RENDER_SLOTS", slots)
    monkeypatch.setattr(report_pdf, "render_pdf", render)
    requests = [asyncio.create_task(report_pdf.render_report({}, "full")) for _ in range(2)]
    queued = []
    try:
        await asyncio.wait_for(both_running.wait(), 3)
        for request in requests:
            request.cancel()
        results = await asyncio.gather(*requests, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

        async def next_request(entered):
            entered.set()
            return await report_pdf.render_report({}, "full")

        for _ in range(2):
            entered = asyncio.Event()
            queued.append(asyncio.create_task(next_request(entered)))
            await entered.wait()
        with lock:
            assert active == started == peak == 2
        queued[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued[1]
        release.set()
        assert await asyncio.wait_for(queued[0], 3) == b"pdf"
        assert await report_pdf.render_report({}, "basic") == b"pdf"
        assert peak == 2
        assert slots._value == 2
    finally:
        release.set()
        for request in [*requests, *queued]:
            request.cancel()
        await asyncio.gather(*requests, *queued, return_exceptions=True)
        await asyncio.gather(*report_pdf._RENDER_TASKS, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_request_consumes_background_render_failure(monkeypatch):
    loop = asyncio.get_running_loop()
    running = asyncio.Event()
    release = threading.Event()

    def render(report, detail):
        loop.call_soon_threadsafe(running.set)
        assert release.wait(5), "test did not release failed render"
        raise ValueError("synthetic render failure")

    slots = asyncio.Semaphore(2)
    monkeypatch.setattr(report_pdf, "RENDER_SLOTS", slots)
    monkeypatch.setattr(report_pdf, "render_pdf", render)
    request = asyncio.create_task(report_pdf.render_report({}, "full"))
    try:
        await asyncio.wait_for(running.wait(), 3)
        rendering = next(iter(report_pdf._RENDER_TASKS))
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        # asyncio.wait observes completion without retrieving the exception.
        done, pending = await asyncio.wait([rendering], timeout=3)
        assert done == {rendering} and not pending
        assert not rendering._log_traceback
        assert rendering not in report_pdf._RENDER_TASKS
        assert slots._value == 2
        with pytest.raises(ValueError, match="synthetic render failure"):
            await report_pdf.render_report({}, "full")
        assert slots._value == 2
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(*report_pdf._RENDER_TASKS, return_exceptions=True)
