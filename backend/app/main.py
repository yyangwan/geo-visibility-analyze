import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audits import router as audits_router
from app.api.auth import router as auth_router
from app.api.projects import router as projects_router
from app.api.reports import router as reports_router
from app.api.schedules import router as schedules_router
from app.api.suggestions import router as suggestions_router
from app.api.trends import router as trends_router
from app.database import init_db
from app.logging_config import get_logger, setup_logging
from app.middleware import RequestLoggingMiddleware

# Initialize structured logging
debug = os.getenv("AISCOPE_DEBUG", "0") == "1"
setup_logging(debug=debug)
logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting_app")
    await init_db()
    from app.services.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    logger.info("stopping_app")
    stop_scheduler()


app = FastAPI(
    title="AI Scope",
    description="AI搜索可见性分析平台",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(audits_router, prefix="/api/audits", tags=["audits"])
app.include_router(schedules_router, prefix="/api/schedules", tags=["schedules"])
app.include_router(trends_router, prefix="/api/trends", tags=["trends"])
app.include_router(reports_router, prefix="/api/reports", tags=["reports"])
app.include_router(suggestions_router, prefix="/api/suggestions", tags=["suggestions"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
