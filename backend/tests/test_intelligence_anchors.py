"""Anchor provenance, unchecked coverage, copy families and literal dimension checks."""

import gzip
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.capture_contract import Source
from app.intelligence.citation_context import association, freeze_anchor
from app.intelligence.evidence import checked_verdict
from app.intelligence.evidence_alignment import source_span
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    ClaimEvidence,
    SourceDocument,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue
from app.intelligence.reviews import evidence_material
from app.intelligence.semantics import SemanticOutputError, checked_result, stable_hash
from app.intelligence.source_observations import (
    content_families,
    project_source_observations,
)
from app.main import app
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit


def test_anchor_uses_code_points_and_fails_closed_on_tampered_frozen_text():
    text = "🧭云衡质保三年。锐川仅限企业版。"
    anchor = freeze_anchor([1, 8], text)
    assert (
        anchor["quote"] == text[1:8] and anchor["offset_unit"] == "unicode_code_point"
    )
    source = {"answer_anchor": anchor}
    assert association(source, {"text": text}, 1, 8)["eligible"]
    assert not association(source, {"text": text}, 9, len(text))["eligible"]
    assert not association(source, {"text": text + "改写"}, 1, 8)["eligible"]
    assert association({}, {"text": text}, 1, 8)["method"] == "same_answer_fallback"


@pytest.mark.parametrize(
    "raw", [[True, 3], [1.0, 3], [-1, 3], [1, 100], [3, 3], {"start": 1, "end": 3}]
)
def test_invalid_anchor_is_explicit_not_a_claim_match(raw):
    assert freeze_anchor(raw, "云衡质保三年")["status"] == "invalid"


@pytest.mark.parametrize("raw", [[True, 3], [1.0, 3]])
def test_capture_anchor_rejects_boolean_or_float_coercion(raw):
    with pytest.raises(ValidationError):
        Source.model_validate(
            {
                "ordinal": 1,
                "kind": "answer_reference",
                "urlResolution": "unknown",
                "answerAnchor": raw,
            }
        )


def test_dimension_conflict_cannot_hide_behind_overall_supports():
    result = checked_verdict(
        "云衡质保两年，仅限专业版。",
        {
            "relation": "supports",
            "source_quote": "云衡",
            "checks": [
                {
                    "dimension": "number",
                    "relation": "contradicts",
                    "answer_quote": "三年",
                    "source_quote": "两年",
                },
                {
                    "dimension": "condition",
                    "relation": "partial",
                    "answer_quote": "全部版本",
                    "source_quote": "仅限专业版",
                },
            ],
        },
        "云衡全部版本质保三年。",
    )
    assert result["relation"] == "contradicts" and result["source_quote"] == "两年"
    with pytest.raises(SemanticOutputError):
        checked_verdict(
            "两年",
            {
                "relation": "unverified",
                "source_quote": "",
                "checks": [
                    {
                        "dimension": "time",
                        "relation": "contradicts",
                        "answer_quote": "去年",
                        "source_quote": "两年",
                    }
                ],
            },
            "质保三年",
        )
    with pytest.raises(SemanticOutputError):
        source_span(SimpleNamespace(text="两年和两年", start_offset=10), "两年")


async def seed_sources(db, blobs, audit_id, source_texts, project="p1"):
    for index, text in enumerate(source_texts):
        url = f"https://example.com/article-{index}"
        document = SourceDocument(project_id=project, url=url, url_hash=stable_hash(url))
        db.add(document)
        await db.flush()
        blob = await blobs.put_immutable(
            f"projects/{project}/sources/{document.id}/fixture.txt.gz",
            gzip.compress(text.encode(), mtime=0),
        )
        snapshot = SourceSnapshot(
            project_id=project,
            document_id=document.id,
            body_hash=digest(text.encode()),
            representation_hash=stable_hash([index, text]),
            text_key=blob.key,
            text_sha256=blob.sha256,
            extractor_version="fixture",
            completeness="unknown",
        )
        db.add(snapshot)
        await db.flush()
        db.add(
            SourceFetch(
                project_id=project,
                audit_id=audit_id,
                document_id=document.id,
                snapshot_id=snapshot.id,
                status="captured",
            )
        )
    await db.commit()


async def analyze_fixture(
    db,
    monkeypatch,
    tmp_path,
    *,
    text,
    quotes,
    source_texts,
    anchors=None,
    request_limit=1000,
    verifier=None,
):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-anchor-test-key")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", request_limit)
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 10000000)
    sources = [
        {
            "title": f"材料{index}",
            "url": f"https://example.com/article-{index}",
            "kind": "answer_reference",
            "url_resolution": "exact",
            "answerAnchor": (anchors or [None] * len(source_texts))[index],
        }
        for index in range(len(source_texts))
    ]
    audit = await fixture_audit(db, text=text, sources=sources)
    blobs = LocalBlobStore(tmp_path)
    await seed_sources(db, blobs, audit.id, source_texts)
    run = await prepare_run(db, "p1", audit.id, enrich=False)
    frozen = deepcopy(run.manifest)
    await db.commit()
    calls = []

    async def analyze(value, *_):
        claims = [
            {
                "quote": quote,
                "start": value.index(quote),
                "end": value.index(quote) + len(quote),
                "subject": "云衡",
                "topic": "规格",
            }
            for quote in quotes
            if value == text
        ]
        return checked_result(
            value, {"entities": [], "topics": [], "claims": claims}
        ), {"prompt_tokens": 1, "completion_tokens": 1}

    async def verify(answer, source, *_):
        calls.append((answer, source))
        return (
            verifier(answer, source)
            if verifier
            else {"relation": "partial", "source_quote": source}
        ), {"prompt_tokens": 1, "completion_tokens": 1}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, blobs, analyze=analyze, verify=verify).analyze_run(
        await queue.claim()
    )
    await db.refresh(run)
    assert run.manifest == frozen
    return run, calls, blobs


@pytest.mark.asyncio
async def test_anchor_prevents_cross_claim_reference_and_keeps_fallback(
    db_session, monkeypatch, tmp_path
):
    first, second = "云衡质保三年。", "锐川仅限企业版。"
    text = "🧭" + first + second
    run, calls, _ = await analyze_fixture(
        db_session,
        monkeypatch,
        tmp_path,
        text=text,
        quotes=[first, second],
        source_texts=["云衡质保两年。", "锐川仅限专业版。", "公开规格需要人工核对。"],
        anchors=[[1, 1 + len(first)], [1 + len(first), len(text)], None],
    )
    assert len(calls) == 4
    assert (second, "云衡质保两年。") not in calls and (
        first,
        "锐川仅限专业版。",
    ) not in calls
    alignment = run.coverage["evidence_alignment"]
    assert (
        sum(
            row["association"]["method"] == "anchor_outside_claim"
            for row in alignment["observations"]
        )
        == 2
    )
    assert (
        sum(
            row["association"]["method"] == "same_answer_fallback"
            for row in alignment["observations"]
        )
        == 2
    )
    rows = (await db_session.scalars(select(ClaimEvidence))).all()
    assert all(
        row.verification["source_span"]["quote"] == row.source_quote for row in rows
    )
    anchored = next(
        row
        for row in rows
        if row.verification["association"]["method"] == "answer_anchor"
    )
    # A malicious row cannot be reviewed against a different anchored claim.
    anchored.verification = {
        **anchored.verification,
        "answer_span": {"start": 1 + len(first), "end": len(text), "quote": second},
    }
    anchored.answer_quote = second
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await evidence_material(db_session, run, anchored)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_more_than_five_passages_has_explicit_unchecked_ranges(
    db_session, monkeypatch, tmp_path
):
    source = "".join(
        f"第{index}段" + "云衡规格资料" * 149 + "\n" for index in range(18)
    )
    run, calls, _ = await analyze_fixture(
        db_session,
        monkeypatch,
        tmp_path,
        text="云衡质保三年。",
        quotes=["云衡质保三年。"],
        source_texts=[source],
    )
    alignment = run.coverage["evidence_alignment"]
    assert len(calls) == alignment["candidate_pairs"] == 12
    assert alignment["available_pairs"] > 12 and alignment["unchecked_pairs"] > 0
    plan = alignment["observations"][0]
    assert plan["unchecked_ranges"] and run.status == "partial"
    checked_ranges = [(row["start"], row["end"]) for row in plan["selected"]]
    assert all(
        not (a < end and start < b)
        for a, b in checked_ranges
        for start, end in plan["unchecked_ranges"]
    )


@pytest.mark.asyncio
async def test_duplicate_articles_preserve_observations_without_extra_calls_or_votes(
    db_session, monkeypatch, tmp_path, client
):
    text = "云衡质保三年。"
    run, calls, _ = await analyze_fixture(
        db_session,
        monkeypatch,
        tmp_path,
        text=text,
        quotes=[text],
        source_texts=["云衡质保两年。", "云衡质保两年。"],
    )
    assert len(calls) == 1
    evidence = run.summary["claim_evidence"]
    assert (
        len(evidence) == 2
        and sum(row["deduplicated_content_weight"] for row in evidence) == 1
    )
    assert len({row["content_family_id"] for row in evidence}) == 1
    projection = run.summary["source_observations"]
    assert (
        len(projection["observations"]) == 2
        and projection["actual_reference_content_families"] == 1
    )
    assert projection["independent_publishers"] is None
    initial = deepcopy(run.manifest)
    # Projection preserves type/platform per observation even for the same article.
    manifest = deepcopy(run.manifest)
    source = deepcopy(manifest["sources"][0])
    source.update(
        id="candidate-id",
        platform="kimi",
        kind="search_candidate",
        response_id="candidate-response",
    )
    manifest["sources"].append(source)
    manifest["responses"].append(
        {**manifest["responses"][0], "id": "candidate-response", "platform": "kimi"}
    )
    projected = await project_source_observations(
        db_session,
        SimpleNamespace(project_id=run.project_id, id=run.id, manifest=manifest),
    )
    assert projected["observations"][-1]["actual_reference"] is False
    assert projected["observations"][-1]["platform"] == "kimi"
    assert len(projected["articles"][0]["observation_ids"]) == 2
    assert run.manifest == initial

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "reader",
        "pid": "p1",
        "role": "viewer",
        "scope": "project",
    }
    count_before = len((await db_session.scalars(select(AnalysisJob))).all())
    response = await client.get(
        f"/api/intelligence/runs/{run.id}/source-observations?limit=1"
    )
    assert response.status_code == 200 and response.json()["next_offset"] == 1
    assert len((await db_session.scalars(select(AnalysisJob))).all()) == count_before
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "reader",
        "pid": "other",
        "role": "viewer",
        "scope": "project",
    }
    assert (
        await client.get(f"/api/intelligence/runs/{run.id}/source-observations")
    ).status_code == 404


def test_near_copy_family_is_explicitly_a_candidate_and_not_independence():
    text = "云衡门店管理规格说明，服务范围及实施限制需核对合同。" * 30
    families, coverage = content_families(
        {"a": text, "b": text + "转载声明", "c": "另一独立材料描述不同的服务。" * 50}
    )
    assert families["a"]["id"] == families["b"]["id"] != families["c"]["id"]
    assert families["a"]["method"] == "near_copy_candidate"
    assert coverage["provenance_independence"] == "unknown"
