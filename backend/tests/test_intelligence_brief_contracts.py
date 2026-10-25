"""Cross-language golden contract and real local citation-byte verification."""

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.brief_contracts import EvidenceBriefV3
from app.intelligence.fact_qualification import verify_citation_bytes
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import FactSource


def test_golden_v3_contract_keeps_all_questions_and_rejects_truncated_or_changed_material():
    value = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts/fixtures/evidence-brief-v3-valid.json"
        ).read_text(encoding="utf-8")
    )
    pack = value["taskPack"]
    target = pack["ai_targets"][0]
    original = value["aiTargets"][0]
    pack["delivery"]["questions"] = [
        {"prompt_hash": stable_hash([index]), "text": f"第{index}个原始问题"}
        for index in range(8)
    ]
    pack["ai_targets"] = [
        {**target, "prompt_hash": question["prompt_hash"]}
        for question in pack["delivery"]["questions"]
    ]
    value["aiTargets"] = [
        {**original, "promptHash": question["prompt_hash"], "prompt": question["text"]}
        for question in pack["delivery"]["questions"]
    ]
    value["actionRevision"]["taskSignature"] = stable_hash(pack)
    brief = EvidenceBriefV3.model_validate(value)
    assert len(brief.aiTargets) == 8 and brief.aiTargets[-1].prompt == "第7个原始问题"
    value["aiTargets"].pop()
    with pytest.raises(ValidationError):
        EvidenceBriefV3.model_validate(value)


@pytest.mark.asyncio
async def test_citation_content_authority_checks_actual_blob_hash_and_unicode_span(
    tmp_path,
):
    text = "🧭云衡支持门店权限分离。"
    store = LocalBlobStore(tmp_path)
    blob = await store.put_immutable(
        "projects/p1/sources/doc/proof/text.gz", gzip.compress(text.encode())
    )
    snapshot = SimpleNamespace(text_key=blob.key, text_sha256=blob.sha256)
    source = TypeAdapter(FactSource).validate_python(
        {
            "kind": "citation_review",
            "review_id": "review",
            "evidence_id": "evidence",
            "citation_id": "citation",
            "snapshot_id": "snapshot",
            "evidence": {
                "project_id": "p1",
                "run_id": "run",
                "owner_kind": "snapshot",
                "owner_id": "snapshot",
                "segment_id": "segment",
                "text_hash": digest(text.encode()),
                "quote": text[1:],
                "start": 1,
                "end": len(text),
            },
        }
    )
    await verify_citation_bytes(snapshot, source, store)
    changed = source.model_copy(
        update={"evidence": source.evidence.model_copy(update={"text_hash": "0" * 64})}
    )
    with pytest.raises(HTTPException) as failure:
        await verify_citation_bytes(snapshot, changed, store)
    assert failure.value.status_code == 409
    store.path(blob.key).write_bytes(b"corrupt")
    with pytest.raises(HTTPException) as failure:
        await verify_citation_bytes(snapshot, source, store)
    assert failure.value.status_code == 409
