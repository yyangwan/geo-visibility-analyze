"""Offline image acceptance: no database, model, source fetch or cloud calls."""

import asyncio
import json
import tempfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.intelligence.blob import LocalBlobStore
from app.intelligence.report_pdf import render_pdf
from app.intelligence.storage_check import check_storage


async def main():
    # Import both entry points without invoking their startup hooks.
    import oss2
    from alibabacloud_credentials.client import Client

    from app.intelligence.worker import main as worker_main
    from app.main import app

    assert oss2.Bucket and Client and app and callable(worker_main)
    root = Path(__file__).resolve().parents[1]
    assert not list(root.glob(".env*")), "Runtime configuration leaked into image"
    assert not (root / ".keys").exists(), "Signing keys leaked into image"
    config = Config(str(root / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["intelligence_worker_20261016"], "Unexpected migration head"
    with tempfile.TemporaryDirectory(prefix="intelligence-image-") as temporary:
        probe = await check_storage(
            LocalBlobStore(Path(temporary)), backend="local", write_probe=True
        )
        assert probe["status"] == "storage_probe_verified"
    pdf = render_pdf(
        {
            "schema": "citation-report-1",
            "audit_id": "image-smoke",
            "project_id": "synthetic",
            "run_id": "a" * 32,
            "revision": 1,
            "status": "partial",
            "snapshot_current": True,
            "coverage": {},
            "platforms": [],
            "entities": [{"id": "fixture", "name": "镜像验收"}],
            "sources": [],
            "evidence": [],
        }
    )
    assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf
    print(
        json.dumps(
            {
                "status": "image_runtime_verified",
                "schema_head": heads[0],
                "entrypoints": ["api", "worker", "storage_check"],
                "storage": "development_local_only",
                "pdf": "verified",
                "release_eligible": False,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
