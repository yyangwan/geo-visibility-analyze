import pytest
from fastapi import HTTPException

from app.intelligence.ingestion import prepare_run
from app.intelligence.models import EntityMention, EntityResolutionEvent
from app.intelligence.pipeline import build_summary
from app.intelligence.project_context import (
    ContextRequest,
    binding_proof,
    bound_roles,
    receive_context,
)
from tests.test_intelligence_aliases import alias_fixture


@pytest.mark.asyncio
async def test_bound_roles_use_confirmed_identity_not_name_matching(db_session):
    run, target, source, segment, _ = await alias_fixture(db_session)
    proof = await binding_proof(db_session, "p1", target.id)
    assert proof["version"] == 1
    data = ContextRequest(
        version=1,
        brands=[
            {
                "id": "registered",
                "name": "云衡",
                "aliases": ["Yunheng"],
                "entity_id": target.id,
                "identity_version": proof["version"],
            }
        ],
    )
    await receive_context(db_session, "p1", data, "reviewer")
    await db_session.commit()
    refreshed = await prepare_run(db_session, "p1", run.audit_id)
    for entity, start, end in ((target, 0, 2), (source, 3, 10)):
        db_session.add(
            EntityMention(
                project_id="p1",
                run_id=refreshed.id,
                entity_id=entity.id,
                segment_id=segment.id,
                start_offset=start,
                end_offset=end,
                quote=segment.text[start:end],
            )
        )
    await db_session.flush()
    _, summary = await build_summary(db_session, refreshed)
    roles = {row["id"]: row["role"] for row in summary["entities"]}
    assert roles[target.id] == "own" and roles[source.id] == "unregistered"
    assert bound_roles(refreshed.manifest, source.id) == set()
    data.brands[0].analysis_role = "comparison"
    data.version = 2
    await receive_context(db_session, "p1", data, "reviewer")
    await db_session.commit()
    compared = await prepare_run(db_session, "p1", run.audit_id)
    assert bound_roles(compared.manifest, target.id) == {"comparison"}
    assert bound_roles(refreshed.manifest, target.id) == {"own"}


@pytest.mark.asyncio
async def test_revised_identity_invalidates_binding_without_poisoning_old_context_delivery(
    db_session,
):
    run, target, _, _, _ = await alias_fixture(db_session)
    data = ContextRequest(
        version=1,
        brands=[
            {
                "id": "registered",
                "name": "云衡",
                "entity_id": target.id,
                "identity_version": 1,
            }
        ],
    )
    await receive_context(db_session, "p1", data, "reviewer")
    db_session.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=target.id,
            run_id=run.id,
            version=2,
            decision="excluded",
            canonical_name="云衡",
            reason="进一步核实后暂排除候选身份",
            actor_id="reviewer",
            idempotency_key="exclude-binding-123",
            request_hash="a" * 64,
        )
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await binding_proof(db_session, "p1", target.id)
    assert error.value.status_code == 422
    data.version = 2
    await receive_context(db_session, "p1", data, "reviewer")
    await db_session.commit()
    new = await prepare_run(db_session, "p1", run.audit_id)
    assert bound_roles(new.manifest, target.id) == set()
    with pytest.raises(HTTPException) as error:
        await receive_context(
            db_session, "p2", data.model_copy(update={"version": 1}), "reviewer"
        )
    assert error.value.status_code == 422


def test_unbound_versions_and_alias_redirects_cannot_assign_brand_roles():
    entity = "a" * 32
    manifest = {
        "brand_context": {
            "brands": [{"id": "brand", "entity_id": entity, "identity_version": 1}]
        },
        "entity_resolutions": {entity: {"version": 2, "decision": "confirmed"}},
    }
    assert bound_roles(manifest, entity) == set()
    manifest["entity_resolutions"][entity]["version"] = 1
    assert bound_roles(manifest, entity) == {"own"}
    manifest["entity_aliases"] = {
        entity: {"decision": "merge", "target_entity_id": "b" * 32}
    }
    assert bound_roles(manifest, entity) == set()
