"""Large immutable JSON inputs must not enter MySQL's list-page sort buffer."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.intelligence import list_runs
from app.intelligence.models import IntelligenceRun
from app.models.models import Audit
from app.utils.timezone import utcnow
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_audit", [False, True])
async def test_large_run_list_is_bounded_ordered_and_project_scoped(
    mysql_sessions, filter_audit  # noqa: F811 - imported isolated fixture
):
    sessions, engine = mysql_sessions
    project, other = "ci-run-list-" + uuid4().hex, "ci-run-other-" + uuid4().hex
    projects = [project, other]
    try:
        async with sessions() as writer:
            first, second = Audit(project_id=project), Audit(project_id=project)
            writer.add_all([first, second])
            await writer.flush()
            now = utcnow()
            rows = []
            for pid, audit, revision, offset in [
                (project, first.id, 1, -2),
                (project, first.id, 2, 0),
                (project, second.id, 1, 1),
                (other, second.id, 3, 2),
            ]:
                row = IntelligenceRun(
                    project_id=pid,
                    audit_id=audit,
                    revision=revision,
                    created_at=now + timedelta(seconds=offset),
                    input_hash=uuid4().hex * 2,
                    signature="a" * 64,
                    manifest={"text": "m" * 1048576},
                    summary={"text": "s" * 1048576},
                    coverage={"expected_samples": 5, "responses": 1},
                    status="partial",
                )
                writer.add(row)
                rows.append(row)
            await writer.commit()
            audit_id = first.id
            expected = [rows[1].id, rows[0].id] if filter_audit else [rows[2].id, rows[1].id]

        async with engine.connect() as connection:
            previous = int(await connection.scalar(text("SELECT @@SESSION.sort_buffer_size")))
            await connection.execute(text("SET SESSION sort_buffer_size=32768"))
            await connection.commit()
            try:
                async with AsyncSession(bind=connection) as reader:
                    result = await list_runs(
                        project,
                        audit_id=audit_id if filter_audit else None,
                        limit=2,
                        user={"scope": "project", "pid": project, "role": "viewer"},
                        db=reader,
                    )
                assert [row["id"] for row in result["runs"]] == expected
                assert all(row["coverage"]["responses"] == 1 for row in result["runs"])
                assert all(set(row) == {"id", "audit_id", "revision", "status", "created_at", "coverage"} for row in result["runs"])
            finally:
                await connection.rollback()
                await connection.execute(text(f"SET SESSION sort_buffer_size={previous}"))
                await connection.commit()
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(IntelligenceRun).where(IntelligenceRun.project_id.in_(projects)))
            await cleanup.execute(delete(Audit).where(Audit.project_id.in_(projects)))
            await cleanup.commit()
