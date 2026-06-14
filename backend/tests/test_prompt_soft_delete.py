"""Test prompt soft delete functionality.

Tests cover:
1. Soft delete sets deleted_at timestamp
2. Deleted prompts are filtered from list queries
3. Historical audit results still show deleted prompts (referenced by ID)
4. References are checked but don't block deletion
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.models.models import Audit, AuditPlatformRun, PlatformResponseRecord, Prompt, QueryResult
from app.api.prompts import delete_prompt
from app.utils.timezone import utcnow


@pytest.mark.asyncio
class TestPromptSoftDeleteModel:
    """Test soft delete at the model level."""

    async def test_soft_delete_sets_deleted_at(self, db_session: AsyncSession):
        """Setting deleted_at should mark prompt as deleted without removing the row."""
        # Create a prompt
        prompt = Prompt(
            project_id="test-project",
            text="Test prompt",
            category="recommend",
        )
        db_session.add(prompt)
        await db_session.commit()
        await db_session.refresh(prompt)

        prompt_id = prompt.id
        assert prompt.deleted_at is None

        # Soft delete
        prompt.deleted_at = utcnow()
        await db_session.commit()

        # Verify row still exists with deleted_at set
        result = await db_session.execute(select(Prompt).where(Prompt.id == prompt_id))
        deleted_prompt = result.scalar_one()
        assert deleted_prompt is not None
        assert deleted_prompt.deleted_at is not None
        assert deleted_prompt.text == "Test prompt"

    async def test_filter_deleted_prompts(self, db_session: AsyncSession):
        """Queries with deleted_at IS NULL should exclude deleted prompts."""
        # Create two prompts
        p1 = Prompt(project_id="test-project", text="Active 1", category="recommend")
        p2 = Prompt(project_id="test-project", text="To be deleted", category="recommend")
        db_session.add_all([p1, p2])
        await db_session.commit()

        # Both should be visible
        result = await db_session.execute(
            select(Prompt).where(
                Prompt.project_id == "test-project",
                Prompt.deleted_at.is_(None)
            )
        )
        assert len(result.scalars().all()) == 2

        # Soft delete one
        p2.deleted_at = utcnow()
        await db_session.commit()

        # Only one should be visible
        result = await db_session.execute(
            select(Prompt).where(
                Prompt.project_id == "test-project",
                Prompt.deleted_at.is_(None)
            )
        )
        prompts = result.scalars().all()
        assert len(prompts) == 1
        assert prompts[0].text == "Active 1"

    async def test_deleted_prompt_still_accessible_by_id(self, db_session: AsyncSession):
        """Deleted prompts should still be queryable by ID for historical views."""
        # Create and delete a prompt
        prompt = Prompt(project_id="test-project", text="Historical prompt", category="recommend")
        db_session.add(prompt)
        await db_session.commit()

        prompt_id = prompt.id
        prompt.deleted_at = utcnow()
        await db_session.commit()

        # Should still be accessible by direct ID lookup
        result = await db_session.execute(select(Prompt).where(Prompt.id == prompt_id))
        historical_prompt = result.scalar_one()
        assert historical_prompt is not None
        assert historical_prompt.text == "Historical prompt"
        assert historical_prompt.deleted_at is not None

    async def test_soft_delete_with_audit_references(self, db_session: AsyncSession):
        """Soft delete should work even when prompt has audit history references."""
        # Create a prompt
        prompt = Prompt(project_id="test-project", text="Referenced prompt", category="recommend")
        db_session.add(prompt)
        await db_session.commit()
        await db_session.flush()

        # Create an audit with references to the prompt
        audit = Audit(
            project_id="test-project",
            platforms_json=["deepseek"],
            brands_json=[{"id": "test_brand", "name": "Test Brand"}],
            status="completed",
        )
        db_session.add(audit)
        await db_session.flush()

        # Create reference records
        platform_run = AuditPlatformRun(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            status="completed",
        )
        db_session.add(platform_run)

        response_record = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="Test response",
        )
        db_session.add(response_record)

        query_result = QueryResult(
            audit_id=audit.id,
            prompt_id=prompt.id,
            brand_id="test_brand",
            platform="deepseek",
            response_text="Test response",
        )
        db_session.add(query_result)
        await db_session.commit()

        # Soft delete the prompt (should not raise foreign key errors)
        prompt.deleted_at = utcnow()
        await db_session.commit()

        # Verify references still exist
        refs_count = await db_session.execute(
            select(AuditPlatformRun).where(AuditPlatformRun.prompt_id == prompt.id)
        )
        assert refs_count.scalar_one_or_none() is not None

        # Verify prompt is marked as deleted
        await db_session.refresh(prompt)
        assert prompt.deleted_at is not None

    async def test_soft_delete_with_multiple_audit_references(self, db_session: AsyncSession):
        """Soft delete should not fail when a prompt has many historical references."""
        prompt = Prompt(project_id="test-project", text="Multi referenced prompt", category="recommend")
        db_session.add(prompt)
        await db_session.commit()
        await db_session.refresh(prompt)

        audit = Audit(
            project_id="test-project",
            platforms_json=["deepseek"],
            brands_json=[{"id": "test_brand", "name": "Test Brand"}],
            status="completed",
        )
        db_session.add(audit)
        await db_session.flush()

        db_session.add(
            AuditPlatformRun(
                audit_id=audit.id,
                prompt_id=prompt.id,
                platform="deepseek",
                status="completed",
                attempt_no=1,
            )
        )
        db_session.add(
            AuditPlatformRun(
                audit_id=audit.id,
                prompt_id=prompt.id,
                platform="deepseek",
                status="completed",
                attempt_no=2,
            )
        )
        db_session.add(
            PlatformResponseRecord(
                audit_id=audit.id,
                prompt_id=prompt.id,
                platform="deepseek",
                response_text="Response 1",
            )
        )
        db_session.add(
            QueryResult(
                audit_id=audit.id,
                prompt_id=prompt.id,
                brand_id="test_brand",
                platform="deepseek",
                response_text="Response 1",
            )
        )
        await db_session.commit()

        await delete_prompt(
            prompt_id=prompt.id,
            project_id=prompt.project_id,
            current_user={"scope": "project", "pid": prompt.project_id},
            db=db_session,
        )

        await db_session.refresh(prompt)
        assert prompt.deleted_at is not None
