from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audits import router as audits_router
from app.api.projects import router as projects_router
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="AI Scope",
    description="AI搜索可见性分析平台",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(audits_router, prefix="/api/audits", tags=["audits"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
