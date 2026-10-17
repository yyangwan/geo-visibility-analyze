import pytest

from app.intelligence.blob import digest
from app.intelligence.measurement import compare_snapshots
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    SegmentAnalysis,
    SourceDocument,
    SourceSnapshot,
)
from app.intelligence.source_metrics import source_snapshot
from tests.test_intelligence_aliases import alias_fixture
from tests.test_intelligence_measurement import ENTITY, snapshots


async def corpus(db, kind="answer_reference"):
    run, target, _, _, _ = await alias_fixture(db)
    response = run.manifest["responses"][0]
    doc = SourceDocument(
        project_id="p1",
        url="https://example.com/report",
        url_hash="b" * 64,
        title="报告",
    )
    db.add(doc)
    await db.flush()
    body = "正文包含云衡"
    snapshot = SourceSnapshot(
        project_id="p1",
        document_id=doc.id,
        body_hash=digest(body.encode()),
        representation_hash="c" * 64,
        text_key="projects/p1/text.gz",
        text_sha256=digest(body.encode()),
        extractor_version="test",
        completeness="unknown",
        integrity_status="verified",
    )
    db.add(snapshot)
    await db.flush()
    citation = {
        "id": "d" * 32,
        "response_id": response["id"],
        "platform": response["platform"],
        "kind": kind,
        "title": "云衡研究报告",
        "snippet": "",
        "snapshot_id": snapshot.id,
        "document_id": doc.id,
        "fetch_status": "captured",
        "url_resolution": "exact",
    }
    run.manifest = {
        **run.manifest,
        "sources": [citation],
        "responses": [
            {
                **response,
                "source_panel": {
                    "status": "complete",
                    "expectedCount": 1,
                    "capturedCount": 1,
                },
            }
        ],
    }
    for owner_kind, owner_id, text, start in (
        ("citation", citation["id"], citation["title"], 0),
        ("snapshot", snapshot.id, body, 4),
    ):
        segment = ContentSegment(
            project_id="p1",
            owner_kind=owner_kind,
            owner_id=owner_id,
            ordinal=0,
            start_offset=0,
            end_offset=len(text),
            text=text,
            segment_hash=digest(text.encode()),
            extractor_version="test",
        )
        db.add(segment)
        await db.flush()
        db.add_all(
            [
                SegmentAnalysis(
                    project_id="p1",
                    run_id=run.id,
                    segment_id=segment.id,
                    status="completed",
                ),
                EntityMention(
                    project_id="p1",
                    run_id=run.id,
                    segment_id=segment.id,
                    entity_id=target.id,
                    start_offset=start,
                    end_offset=start + 2,
                    quote="云衡",
                ),
            ]
        )
    await db.flush()
    return run, target, snapshot


@pytest.mark.asyncio
async def test_cited_cards_and_stored_articles_have_independent_traceable_presence(
    db_session,
):
    run, target, snapshot = await corpus(db_session)
    metrics = await source_snapshot(db_session, run)
    rid = run.manifest["responses"][0]["id"]
    for name in ("cited_card_presence", "cited_archive_presence"):
        assert metrics[name]["completed"] == {rid}
        assert metrics[name]["hits"][target.id] == {rid}
        assert metrics[name]["traces"][rid][0]["citation_id"] == "d" * 32
    assert (
        metrics["cited_archive_presence"]["traces"][rid][0]["article_completeness"]
        == "unknown"
    )
    assert metrics["candidate_card_presence"]["hits"] == {}
    snapshot.integrity_status = "unverified"
    await db_session.flush()
    invalid = await source_snapshot(db_session, run)
    assert not invalid["cited_archive_presence"]["completed"]
    assert invalid["cited_card_presence"]["completed"] == {rid}


@pytest.mark.asyncio
async def test_search_candidates_do_not_become_actual_citations(db_session):
    run, target, _ = await corpus(db_session, "search_candidate")
    metrics = await source_snapshot(db_session, run)
    rid = run.manifest["responses"][0]["id"]
    assert metrics["candidate_card_presence"]["hits"][target.id] == {rid}
    for name in ("cited_card_presence", "cited_archive_presence"):
        assert metrics[name]["completed"] == {rid} and not metrics[name]["hits"]


@pytest.mark.asyncio
async def test_partial_panels_and_archive_hash_mismatches_are_unknown_not_zero(
    db_session,
):
    run, _, snapshot = await corpus(db_session)
    rid = run.manifest["responses"][0]["id"]
    run.manifest = {
        **run.manifest,
        "responses": [
            {
                **run.manifest["responses"][0],
                "source_panel": {
                    "status": "partial",
                    "capturedCount": 1,
                    "expectedCount": 2,
                },
            }
        ],
    }
    metrics = await source_snapshot(db_session, run)
    assert all(not row["completed"] for row in metrics.values())
    assert (
        metrics["cited_card_presence"]["unusable_reasons"][rid]
        == "source_panel_incomplete"
    )
    run.manifest = {
        **run.manifest,
        "responses": [
            {
                **run.manifest["responses"][0],
                "source_panel": {
                    "status": "complete",
                    "capturedCount": 1,
                    "expectedCount": 1,
                },
            }
        ],
    }
    snapshot.text_sha256 = "f" * 64
    await db_session.flush()
    metrics = await source_snapshot(db_session, run)
    assert not metrics["cited_archive_presence"]["completed"]
    assert (
        metrics["cited_archive_presence"]["unusable_reasons"][rid]
        == "cited_archive_incomplete"
    )


def test_each_metric_uses_its_own_matched_population_and_uncertainty():
    spec, before, after = snapshots()
    for snapshot in (before, after):
        allowed = {
            row["id"]
            for row in snapshot["responses"]
            if row["prompt_version_id"] != "0"
        }
        snapshot["source_metrics"] = {
            "cited_card_presence": {
                "completed": allowed,
                "hits": {ENTITY: allowed},
                "traces": {},
                "unusable_reasons": {
                    row["id"]: "source_panel_incomplete"
                    for row in snapshot["responses"]
                    if row["id"] not in allowed
                },
            }
        }
    result = compare_snapshots(spec, before, after)
    metrics = result["platforms"][0]["metrics"]
    assert metrics[0]["difference"] == 1
    source = metrics[1]
    assert (
        source["metric"] == "cited_card_presence" and source["status"] == "insufficient"
    )
    assert source["baseline"]["denominator"] == 57
    assert source["difference"] is None and source["interval95"] is None
    assert (
        len(
            result["platforms"][0]["source_populations"]["cited_card_presence"][
                "excluded"
            ]
        )
        == 3
    )


def test_source_witnesses_are_bounded_without_truncating_paired_positive_counts():
    spec, before, after = snapshots()
    for snapshot in (before, after):
        ids = {row["id"] for row in snapshot["responses"]}
        snapshot["source_metrics"] = {
            "cited_archive_presence": {
                "completed": ids,
                "hits": {ENTITY: ids},
                "unusable_reasons": {},
                "traces": {
                    rid: [
                        {
                            "entity_id": ENTITY,
                            "mention_id": "mention",
                            "citation_id": "citation",
                        }
                    ]
                    for rid in ids
                },
            }
        }
    result = compare_snapshots(spec, before, after)
    platform = result["platforms"][0]
    metric = platform["metrics"][1]
    assert metric["baseline"] == {"numerator": 60, "denominator": 60}
    assert len(metric["paired_positive_response_ids"]["baseline"]) == 60
    assert len(metric["traces"]["baseline"]) == 20
    assert "included" not in metric
    assert (
        len(platform["source_populations"]["cited_archive_presence"]["included"]) == 60
    )
