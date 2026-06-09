import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audits import router as audits_router
from app.api.analysis import router as analysis_router
from app.api.auth import router as auth_router
from app.api.platforms import router as platforms_router
from app.api.prompts import router as prompts_router
from app.api.reports import router as reports_router
from app.api.schedules import router as schedules_router
from app.api.integration import router as integration_router
from app.api.strategic import router as strategic_router
from app.api.suggestions import router as suggestions_router
from app.api.trends import router as trends_router
from app.config import settings
from app.logging_config import get_logger, setup_logging
from app.middleware import RequestLoggingMiddleware

# Initialize structured logging
debug = os.getenv("AISCOPE_DEBUG", "0") == "1"
setup_logging(debug=debug)
logger = get_logger("main")

app = FastAPI(
    title="智见",
    description="AI搜索可见性分析平台",
    version="0.1.0",
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(platforms_router, prefix="/api/platforms", tags=["platforms"])
app.include_router(prompts_router, prefix="/api/prompts", tags=["prompts"])
app.include_router(audits_router, prefix="/api/audits", tags=["audits"])
app.include_router(analysis_router, prefix="/api/analysis", tags=["analysis"])
app.include_router(schedules_router, prefix="/api/schedules", tags=["schedules"])
app.include_router(trends_router, prefix="/api/trends", tags=["trends"])
app.include_router(reports_router, prefix="/api/reports", tags=["reports"])
app.include_router(suggestions_router, prefix="/api/suggestions", tags=["suggestions"])
app.include_router(strategic_router, prefix="/api/strategic", tags=["strategic"])
app.include_router(integration_router, prefix="/api/integration", tags=["integration"])


@app.on_event("startup")
async def startup():
    logger.info("starting_app")
    _run_upgrade_sync()
    logger.info("upgrade_done")
    await _recover_orphan_audits()
    from app.services.scheduler import start_scheduler
    logger.info("scheduler_imported")
    start_scheduler()
    logger.info("scheduler_started")


@app.on_event("shutdown")
async def shutdown():
    logger.info("stopping_app")
    from app.services.scheduler import stop_scheduler
    stop_scheduler()


def _run_upgrade_sync():
    """Run Alembic upgrade synchronously."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    db_url = settings.database_url.replace("+aiomysql", "+pymysql")
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("script_location", "alembic")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Check if already at head — skip upgrade if so
    script = ScriptDirectory.from_config(alembic_cfg)
    head = script.get_current_head()
    engine = create_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        current = ctx.get_current_revision()
    engine.dispose()

    if current != head:
        command.upgrade(alembic_cfg, "head")
    else:
        logger.info("db_already_at_head", current=current)


async def _recover_orphan_audits():
    """Recover audits left behind by a crashed worker on startup.

    Audits created before the worker dies can be left in 'pending' or
    'running' forever because the in-process background task disappears.
    Pending audits are rescheduled; running audits are marked failed and
    closed out so the UI doesn't keep showing them as active.
    """
    import asyncio

    from app.database import async_session
    from sqlalchemy import select
    from app.models.models import Audit, QueryStatus
    from app.services.audit_service import run_audit

    async with async_session() as db:
        running_result = await db.execute(
            select(Audit).where(Audit.status == QueryStatus.RUNNING)
        )
        running_audits = running_result.scalars().all()
        for audit in running_audits:
            audit.status = QueryStatus.FAILED
            audit.error_message = 'Server restarted — audit cancelled'
            audit.completed_at = datetime.now(timezone.utc)
        if running_audits:
            await db.commit()
            logger.warning("running_audits_recovered", count=len(running_audits))

        pending_result = await db.execute(
            select(Audit).where(Audit.status == QueryStatus.PENDING)
        )
        pending_audits = pending_result.scalars().all()

        if pending_audits:
            logger.warning("pending_audits_rescheduled", count=len(pending_audits))

            for audit in pending_audits:
                asyncio.create_task(run_audit(audit.id))
        if not running_audits and not pending_audits:
            logger.info("no_orphan_audits")


@app.get("/api/health")
async def health():
    from fastapi.responses import JSONResponse
    from sqlalchemy import text

    from app.database import async_session

    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception:
        return JSONResponse(
            {"status": "degraded", "db": "disconnected"},
            status_code=503,
        )
