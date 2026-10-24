import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, ContentSegment
from app.intelligence.pipeline import Pipeline
from app.intelligence.provider_failures import classify_provider_status
from app.intelligence.queue import JobQueue
from app.intelligence.semantics import SemanticOutputError, decode_semantic
from app.intelligence.worker import execute
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit


def payload(attributes):
    return {
        "entities": [
            {
                "name": "云衡",
                "kind": "brand",
                "quote": "云衡",
                "start": 0,
                "end": 2,
                "sentiment": "unknown",
                "attributes": attributes,
            }
        ],
        "claims": [],
        "topics": [],
        "brand_signals": [],
        "information_units": [],
    }


def test_string_attribute_repair_preserves_information_and_is_auditable():
    result = decode_semantic("云衡", json.dumps(payload("门店权限仍需核实")))
    assert result["entities"][0]["attributes"] == ["门店权限仍需核实"]
    assert result["schema_repairs"] == [
        {"path": ["entities", 0, "attributes"], "code": "string_to_singleton"}
    ]


@pytest.mark.parametrize("attributes", [None, {"权限": "三类"}, ["有效", 9], [""], ""])
def test_invalid_attributes_are_never_replaced_by_an_empty_success(attributes):
    with pytest.raises(SemanticOutputError) as raised:
        decode_semantic("云衡", json.dumps(payload(attributes)))
    assert "attributes" in str(raised.value.fields)
    assert "三类" not in str(raised.value.fields)


@pytest.mark.parametrize(
    "value", [{}, {"entities": []}, [], {"entities": [], "claims": [], "topics": []}]
)
def test_missing_extraction_arrays_are_not_a_checked_negative_sample(value):
    with pytest.raises(SemanticOutputError):
        decode_semantic("有实际文本", json.dumps(value))


@pytest.mark.parametrize("field", ["entities", "claims", "topics", "brand_signals", "information_units"])
def test_explicit_null_does_not_count_as_a_checked_empty_array(field):
    value = payload([])
    value[field] = None
    with pytest.raises(SemanticOutputError) as raised:
        decode_semantic("云衡", json.dumps(value))
    assert raised.value.fields == [{"path": [field], "type": "list_type"}]


@pytest.mark.parametrize("offset", [False, "0", 0.0])
def test_semantic_offsets_do_not_coerce_booleans_strings_or_floats(offset):
    value = payload([])
    value["entities"][0]["start"] = offset
    with pytest.raises(SemanticOutputError):
        decode_semantic("云衡", json.dumps(value))


@pytest.mark.parametrize(
    "status,retry",
    [
        (400, False),
        (401, False),
        (402, False),
        (403, False),
        (404, False),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_provider_errors_distinguish_intervention_from_retry(status, retry):
    assert classify_provider_status(status).retryable is retry


@pytest.mark.asyncio
async def test_insufficient_balance_stops_retry_and_keeps_raw_inputs(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    text = "甲" * 3500 + "后续未登记品牌"
    audit = await fixture_audit(db_session, text=text)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    calls = []

    async def unavailable(value, *_):
        calls.append(value)
        response = httpx.Response(
            402,
            request=httpx.Request(
                "POST", "https://provider.invalid/private?token=secret"
            ),
            json={"error": "private-provider-body"},
        )
        response.raise_for_status()

    queue = JobQueue(TestSessionLocal, "test-balance")
    lease = await queue.claim()
    await execute(queue, Pipeline(queue, None, analyze=unavailable), lease)
    await db_session.refresh(run)
    job = await db_session.get(AnalysisJob, lease.id)
    assert calls == [text[:900]]
    assert job.status == "failed" and job.attempt == 1
    assert job.error_code == "analysis_provider_insufficient_balance"
    assert run.status == "failed"
    assert run.manifest["responses"][0]["text"] == text
    segments = (
        await db_session.scalars(
            select(ContentSegment).order_by(ContentSegment.ordinal)
        )
    ).all()
    assert "".join(segment.text for segment in segments) == text
    assert "private-provider-body" not in json.dumps(job.payload)
    assert "secret" not in json.dumps(job.payload)
    assert await queue.claim() is None
