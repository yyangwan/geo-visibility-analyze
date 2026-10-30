import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.intelligence.blob import digest
from app.intelligence.evaluation import EvaluationInvalid, render_report
from app.intelligence.evaluation_export import (
    export_recent,
    publish_dataset,
    score_dataset,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    ClaimEvidence,
    ContentSegment,
    IntelligenceRun,
    SegmentAnalysis,
    SemanticCache,
)
from app.intelligence.semantics import SEGMENT_VERSION, chunks
from tests.test_intelligence_foundation import fixture_audit


async def evaluation_fixture(db, project="p1"):
    audit = await fixture_audit(
        db, project=project, text="青禾可靠。" + "字" * 900 + "尾部远岚"
    )
    run = await prepare_run(db, project, audit.id, enrich=False)
    response = run.manifest["responses"][0]
    source = {
        "id": "c" * 32,
        "response_id": response["id"],
        "platform": response["platform"],
        "kind": "answer_reference",
        "snapshot_id": "d" * 32,
        "fetch_status": "captured",
        "url_resolution": "exact",
        "title": "",
        "snippet": "",
    }
    candidate = {**source, "id": "e" * 32, "kind": "search_candidate"}
    duplicate_card = {**source, "id": "f" * 32}
    run.manifest = {**run.manifest, "sources": [source, candidate, duplicate_card]}
    run.status = "partial"
    segments = []
    owners = [
        ("answer", response["id"], response["text"]),
        ("snapshot", "d" * 32, "远岚仅支持一年的质保。"),
    ]
    for kind, owner, text in owners:
        for chunk in chunks(text):
            segment = ContentSegment(
                project_id=project,
                owner_kind=kind,
                owner_id=owner,
                ordinal=chunk["ordinal"],
                start_offset=chunk["start"],
                end_offset=chunk["end"],
                text=chunk["text"],
                segment_hash=digest(chunk["text"].encode()),
                extractor_version=SEGMENT_VERSION,
            )
            db.add(segment)
            await db.flush()
            entities = []
            for name in ("青禾", "远岚"):
                if name in segment.text:
                    offset = segment.text.index(name)
                    entities.append(
                        {
                            "name": name,
                            "kind": "brand",
                            "quote": name,
                            "start": offset,
                            "end": offset + len(name),
                            "sentiment": "neutral",
                            "attributes": [],
                        }
                    )
            cache = SemanticCache(
                project_id=project,
                segment_hash=segment.segment_hash,
                signature=run.signature,
                result={"entities": entities, "claims": [], "topics": []},
            )
            db.add(cache)
            await db.flush()
            db.add(
                SegmentAnalysis(
                    project_id=project,
                    run_id=run.id,
                    segment_id=segment.id,
                    cache_id=cache.id,
                    status="completed",
                )
            )
            segments.append(segment)
    answer = next(segment for segment in segments if segment.owner_kind == "answer")
    snapshot = next(segment for segment in segments if segment.owner_kind == "snapshot")
    for index, citation in enumerate((source, candidate)):
        db.add(
            ClaimEvidence(
                project_id=project,
                run_id=run.id,
                claim_hash=str(index) * 64,
                answer_segment_id=answer.id,
                citation_id=citation["id"],
                source_segment_id=snapshot.id,
                answer_quote="青禾可靠。",
                source_quote="仅支持一年的质保",
                relation="partial",
                method="model_verification",
            )
        )
    await db.commit()
    return audit, run


@pytest.mark.asyncio
async def test_recent_export_is_read_only_full_tail_all_sources_and_blinded(
    db_session, tmp_path
):
    audit, run = await evaluation_fixture(db_session)
    jobs_before = (await db_session.scalars(select(AnalysisJob.id))).all()
    original = deepcopy(run.manifest)
    dataset = await export_recent(db_session, "p1")
    assert dataset["selection"][0]["audit_id"] == str(audit.id)
    assert dataset["selection"][0]["run_id"] == run.id
    assert len(dataset["cases"]) == len({row["case_id"] for row in dataset["cases"]})
    assert any(
        row["global_offset"] > 0 and "远岚" in row["text"] for row in dataset["cases"]
    )
    assert {row["source_kind"] for row in dataset["cases"]} == {
        "answer",
        "answer_reference",
        "search_candidate",
    }
    relations = [row for row in dataset["cases"] if row["task"] == "relation"]
    assert {row["citation_eligible"] for row in relations} == {True, False}
    assert any(
        entity["name"] == "远岚"
        for prediction in dataset["predictions"]
        for entity in prediction["entities"]
    )
    assert run.manifest == original and not db_session.new and not db_session.dirty
    assert jobs_before == (await db_session.scalars(select(AnalysisJob.id))).all()
    target = tmp_path / "private-evaluation"
    publish_dataset(dataset, target)
    template = (target / "annotations-a.template.jsonl").read_text(encoding="utf-8")
    assert "远岚" not in template and '"status":"pending"' in template
    assert all(
        row["entities"] is None and row["relation"] is None
        for row in map(json.loads, template.splitlines())
    )
    report = score_dataset(
        target,
        [
            target / "annotations-a.template.jsonl",
            target / "annotations-b.template.jsonl",
        ],
    )
    assert not report["release_eligible"]
    assert sum(row["citation_chain_violations"] for row in report["strata"]) == 1
    assert "还没有一致的对照答案" in render_report(report)
    assert (await export_recent(db_session, "other-project"))["cases"] == []


@pytest.mark.asyncio
async def test_latest_failed_or_queued_run_never_falls_back_and_one_two_audit_scope(
    db_session,
):
    audit, run = await evaluation_fixture(db_session)
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=audit.id,
            revision=run.revision + 1,
            input_hash="b" * 64,
            signature=run.signature,
            manifest=run.manifest,
            status="queued",
        )
    )
    await db_session.commit()
    dataset = await export_recent(db_session, "p1", audit_limit=1)
    assert dataset["selection"][0]["status"] == "queued" and dataset["cases"] == []
    with pytest.raises(EvaluationInvalid):
        await export_recent(db_session, "p1", audit_limit=3)


@pytest.mark.asyncio
async def test_missing_cache_is_not_scored_as_an_empty_model_response(db_session):
    _, run = await evaluation_fixture(db_session)
    application = await db_session.scalar(
        select(SegmentAnalysis).where(SegmentAnalysis.run_id == run.id)
    )
    application.cache_id = None
    application.status = "deferred"
    await db_session.commit()
    dataset = await export_recent(db_session, "p1")
    assert any(
        row["status"] == "deferred" and row["entities"] == []
        for row in dataset["predictions"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "cross_project_pair"])
async def test_changed_segments_and_cross_project_evidence_fail_closed(
    db_session, damage
):
    await evaluation_fixture(db_session)
    if damage == "hash":
        segment = await db_session.scalar(select(ContentSegment))
        segment.text += "被改写"
    else:
        evidence = await db_session.scalar(select(ClaimEvidence))
        evidence.source_segment_id = (
            "a" * 32
        )  # Never follow an arbitrary owner ID outside the run.
    await db_session.commit()
    with pytest.raises(EvaluationInvalid):
        await export_recent(db_session, "p1")


@pytest.mark.asyncio
async def test_export_and_score_protect_frozen_files_and_do_not_overwrite(
    db_session, tmp_path
):
    await evaluation_fixture(db_session)
    dataset = await export_recent(db_session, "p1")
    target = tmp_path / "evaluation"
    publish_dataset(dataset, target)
    with pytest.raises(FileExistsError):
        publish_dataset(dataset, target)
    with (target / "predictions.jsonl").open("a", encoding="utf-8") as file:
        file.write("{}\n")
    with pytest.raises(EvaluationInvalid, match="changed"):
        score_dataset(target, [])


@pytest.mark.asyncio
async def test_cli_sanitizes_private_annotation_errors(monkeypatch, capsys):
    from app.intelligence import evaluation_export

    def broken(*_):
        raise ValueError("secret full source and database password")

    monkeypatch.setattr(evaluation_export, "score_dataset", broken)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluation_export",
            "score",
            "--directory",
            "unused",
            "--annotations",
            "unused",
            "--output-report",
            "unused",
        ],
    )
    assert await evaluation_export.cli() == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed" and "secret" not in output


@pytest.mark.asyncio
async def test_missing_source_archives_remain_visible_in_coverage(db_session):
    _, run = await evaluation_fixture(db_session)
    run.manifest = {
        **run.manifest,
        "sources": [
            *run.manifest["sources"],
            {
                **run.manifest["sources"][0],
                "id": "9" * 32,
                "snapshot_id": None,
                "fetch_status": "blocked",
            },
        ],
    }
    await db_session.commit()
    dataset = await export_recent(db_session, "p1")
    assert dataset["selection"][0]["source_status_counts"]["blocked"] == 1
    assert dataset["selection"][0]["source_cards_without_archives"] == 1


@pytest.mark.asyncio
async def test_oversized_export_is_rejected_without_silent_sampling(
    db_session, monkeypatch
):
    from app.intelligence import evaluation_export

    await evaluation_fixture(db_session)
    monkeypatch.setattr(evaluation_export, "MAX_CASES", 2)
    with pytest.raises(EvaluationInvalid, match="limit"):
        await export_recent(db_session, "p1")
