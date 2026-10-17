"""DB-backed jobs. All result commits are fenced in the same transaction."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.intelligence.models import AnalysisJob


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    id: str
    project_id: str
    owner: str
    token: str
    version: int
    kind: str
    subject_id: str
    payload: dict
    run_id: str | None


async def db_now(db: AsyncSession) -> datetime:
    value = await db.scalar(select(func.current_timestamp()))
    return value.replace(tzinfo=None)


def fence(lease: Lease, now: datetime):
    return (
        AnalysisJob.id == lease.id,
        AnalysisJob.project_id == lease.project_id,
        AnalysisJob.status == "leased",
        AnalysisJob.lease_owner == lease.owner,
        AnalysisJob.lease_token == lease.token,
        AnalysisJob.lease_version == lease.version,
        AnalysisJob.lease_until > now,
    )


class JobQueue:
    def __init__(
        self, sessions: async_sessionmaker, owner: str, lease_seconds: int = 180
    ):
        self.sessions, self.owner, self.lease_seconds = sessions, owner, lease_seconds

    async def claim(self) -> Lease | None:
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            candidates = (
                await db.scalars(
                    select(AnalysisJob.id)
                    .where(
                        AnalysisJob.status.in_(["queued", "retry_wait"]),
                        AnalysisJob.available_at <= now,
                        AnalysisJob.attempt < AnalysisJob.max_attempts,
                    )
                    .order_by(
                        AnalysisJob.available_at, AnalysisJob.created_at, AnalysisJob.id
                    )
                    .limit(20)
                )
            ).all()
            for candidate in candidates:
                token = uuid4().hex
                result = await db.execute(
                    update(AnalysisJob)
                    .where(
                        AnalysisJob.id == candidate,
                        AnalysisJob.status.in_(["queued", "retry_wait"]),
                        AnalysisJob.available_at <= now,
                        AnalysisJob.attempt < AnalysisJob.max_attempts,
                    )
                    .values(
                        status="leased",
                        lease_owner=self.owner,
                        lease_token=token,
                        lease_version=AnalysisJob.lease_version + 1,
                        lease_until=now + timedelta(seconds=self.lease_seconds),
                        attempt=AnalysisJob.attempt + 1,
                    )
                )
                if result.rowcount == 1:
                    job = await db.get(AnalysisJob, candidate, populate_existing=True)
                    return Lease(
                        job.id,
                        job.project_id,
                        self.owner,
                        token,
                        job.lease_version,
                        job.kind,
                        job.subject_id,
                        job.payload,
                        job.run_id,
                    )
        return None

    async def heartbeat(self, lease: Lease) -> None:
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            job = await db.scalar(
                select(AnalysisJob).where(*fence(lease, now)).with_for_update()
            )
            current = await db_now(db)
            if job is None or job.lease_until <= current:
                raise LeaseLost("Lease no longer belongs to worker")
            job.lease_until = current + timedelta(seconds=self.lease_seconds)

    async def commit(
        self, lease: Lease, write: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            job = await db.scalar(
                select(AnalysisJob).where(*fence(lease, now)).with_for_update()
            )
            if job is None or job.lease_until <= await db_now(db):
                raise LeaseLost("Stale worker cannot commit results")
            await write(db)
            if job.lease_until <= await db_now(db):
                raise LeaseLost("Lease expired during result transaction")
            job.status, job.lease_until, job.error_code = "succeeded", None, None

    async def checkpoint(
        self, lease: Lease, write: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        async with self.sessions() as db, db.begin():
            job = await db.scalar(
                select(AnalysisJob)
                .where(*fence(lease, await db_now(db)))
                .with_for_update()
            )
            if job is None or job.lease_until <= await db_now(db):
                raise LeaseLost("Stale worker cannot write checkpoint")
            await write(db)
            if job.lease_until <= await db_now(db):
                raise LeaseLost("Lease expired during checkpoint transaction")

    async def fail(self, lease: Lease, code: str, *, retry: bool = True) -> None:
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            job = await db.scalar(
                select(AnalysisJob).where(*fence(lease, now)).with_for_update()
            )
            if job is None or job.lease_until <= await db_now(db):
                raise LeaseLost("Stale worker cannot fail a job")
            job.status = (
                "retry_wait" if retry and job.attempt < job.max_attempts else "failed"
            )
            job.available_at = now + timedelta(seconds=min(300, 5 * 2**job.attempt))
            job.lease_until, job.error_code = None, code[:50]
            if job.status == "failed":
                from app.intelligence.terminal import terminal_job

                await terminal_job(db, job)
            if job.status == "failed" and job.run_id:
                from app.intelligence.models import IntelligenceRun

                run = await db.get(IntelligenceRun, job.run_id)
                if (
                    run
                    and run.project_id == lease.project_id
                    and run.status not in ("ready", "partial")
                ):
                    run.status = "failed"
                    run.finished_at = now

    async def reap(self) -> int:
        async with self.sessions() as db, db.begin():
            now = await db_now(db)
            jobs = (
                await db.scalars(
                    select(AnalysisJob)
                    .where(
                        AnalysisJob.status == "leased",
                        AnalysisJob.lease_until <= now,
                    )
                    .with_for_update()
                )
            ).all()
            for job in jobs:
                job.status = (
                    "retry_wait" if job.attempt < job.max_attempts else "failed"
                )
                job.error_code, job.lease_until, job.available_at = (
                    "lease_expired",
                    None,
                    now,
                )
                if job.status == "failed":
                    from app.intelligence.terminal import terminal_job

                    await terminal_job(db, job)
                if job.status == "failed" and job.run_id:
                    from app.intelligence.models import IntelligenceRun

                    run = await db.get(IntelligenceRun, job.run_id)
                    if (
                        run
                        and run.project_id == job.project_id
                        and run.status not in ("ready", "partial")
                    ):
                        run.status, run.finished_at = "failed", now
            return len(jobs)
