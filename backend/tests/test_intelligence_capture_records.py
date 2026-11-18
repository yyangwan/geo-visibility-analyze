import gzip

import pytest
from sqlalchemy import select

from app.api.auth import get_current_user
from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AuditSample, SourceFetch, SourceSnapshot
from app.main import app
from app.services.device_gateway_service import claim_task, complete_task, fail_task
from tests.test_intelligence_sampling import new_audit, result_for


@pytest.fixture
def project_user(client):
    user = {"scope": "project", "pid": "p1", "role": "owner"}
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.mark.asyncio
async def test_capture_records_page_filters_scope_and_attempts(
    client, db_session, project_user
):
    audit, prompt, _ = await new_audit(db_session)
    failed_task, failed_token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(
        db_session,
        "CHAO",
        failed_task.id,
        failed_token,
        "timeout",
        "Device diagnostics must stay private",
        False,
        0,
    )
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    result = result_for(task, "实际获得的回答")
    result["answer"]["status"] = "partial"
    result["sources"] = [
        {
            "ordinal": 1,
            "urlResolution": "exact",
            "kind": "answer_reference",
            "title": "已取得链接",
            "url": "https://example.com/article",
        },
        {
            "ordinal": 2,
            "urlResolution": "unavailable",
            "kind": "answer_reference",
            "title": "缺失链接",
        },
    ]
    result["sourcePanel"] = {
        "status": "partial",
        "expectedCount": 3,
        "capturedCount": 2,
    }
    await complete_task(db_session, "CHAO", task.id, token, result)
    prompt.text = "之后修改的提示词"
    await db_session.commit()

    path = f"/api/intelligence/audits/{audit.id}/capture"
    first = (await client.get(f"{path}/samples?limit=1")).json()
    second = (
        await client.get(
            f"{path}/samples", params={"limit": 1, "cursor": first["next_cursor"]}
        )
    ).json()
    assert second["next_cursor"] is None
    rows = {row["status"]: row for row in first["items"] + second["items"]}
    assert set(rows) == {"received", "failed"}
    received = rows["received"]
    filtered = await client.get(
        f"{path}/samples",
        params={"status": "received", "prompt_id": prompt.id, "platform": "qwen"},
    )
    assert filtered.json()["items"] == [received]
    assert received["prompt_excerpt"] == "完整提示词"
    assert received["answer_status"] == "partial"
    assert received["source_count"] == 2
    assert received["archived_count"] == 0
    assert "answer" not in received

    summary = (await client.get(f"{path}?summary_only=true")).json()
    assert summary["samples"] == []
    assert summary["platforms"] == [
        {"platform": "qwen", "planned": 2, "received": 1, "failed": 1, "pending": 0}
    ]
    assert all(
        row["attempt_count"] == 1 for row in (await client.get(path)).json()["samples"]
    )

    detail = (await client.get(f"{path}/samples/{received['id']}")).json()
    assert detail["prompt"]["text"] == "完整提示词"
    assert detail["answer"]["text"] == "实际获得的回答"
    assert detail["source_panel"]["expectedCount"] == 3
    assert [source["archive"]["status"] for source in detail["sources"]] == [
        "not_started",
        "unavailable",
    ]
    failed = await client.get(f"{path}/samples/{rows['failed']['id']}")
    assert failed.json()["answer"] is None
    assert failed.json()["attempts"][0]["reason"] == "capture_timeout"
    assert "Device diagnostics" not in failed.text
    assert "surface" not in detail

    project_user["pid"] = "p2"
    assert (await client.get(f"{path}/samples")).status_code == 403
    assert (await client.get(f"{path}/samples/{received['id']}")).status_code == 403
    project_user["pid"] = "p1"
    assert (await client.get(f"{path}/samples/{'a' * 32}")).status_code == 404
    assert (await client.get(f"{path}/samples?cursor={'a' * 32}")).status_code == 400


@pytest.mark.asyncio
async def test_capture_citation_text_is_versioned_scoped_and_verified(
    client, db_session, project_user, monkeypatch, tmp_path
):
    audit, _, _ = await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    result = result_for(task)
    result["sources"] = [
        {
            "ordinal": 1,
            "urlResolution": "exact",
            "kind": "answer_reference",
            "title": "文章",
            "url": "https://example.com/article",
        },
        {
            "ordinal": 2,
            "urlResolution": "exact",
            "kind": "answer_reference",
            "title": "不支持的链接",
            "url": "javascript:void(0)",
        },
    ]
    result["sourcePanel"]["capturedCount"] = 2
    await complete_task(db_session, "CHAO", task.id, token, result)
    run = await prepare_run(db_session, "p1", audit.id)
    document_id = run.manifest["sources"][0]["document_id"]
    sample_id = await db_session.scalar(select(AuditSample.id))
    blobs = LocalBlobStore(tmp_path)
    monkeypatch.setattr(
        "app.intelligence.capture_records.configured_store", lambda: blobs
    )
    raw_text = "版本固定的文章正文".encode()
    blob = await blobs.put_immutable(
        "projects/p1/sources/article.txt.gz", gzip.compress(raw_text)
    )
    snapshot = SourceSnapshot(
        project_id="p1",
        document_id=document_id,
        representation_hash="a" * 64,
        body_hash=digest(raw_text),
        text_key=blob.key,
        text_sha256=blob.sha256,
        completeness="partial",
        extractor_version="test-v1",
    )
    db_session.add(snapshot)
    await db_session.flush()
    db_session.add(
        SourceFetch(
            project_id="p1",
            document_id=document_id,
            snapshot_id=snapshot.id,
            audit_id=audit.id,
            status="captured",
        )
    )
    await db_session.commit()

    path = f"/api/intelligence/audits/{audit.id}/capture/samples/{sample_id}"
    detail = (await client.get(path)).json()
    listing = (await client.get(path.rsplit("/", 1)[0])).json()
    assert listing["items"][0]["archived_count"] == 1
    assert detail["sources"][0]["archive"]["snapshot_id"] == snapshot.id
    assert detail["sources"][1]["url"] is None
    assert detail["sources"][1]["archive"]["reason"] == "link_unsupported"
    assert blob.key not in str(detail)

    params = {"snapshot_id": snapshot.id}
    text_path = f"{path}/sources/1/text"
    saved = await client.get(text_path, params=params)
    assert saved.status_code == 200
    assert saved.json()["text"] == raw_text.decode()
    assert (
        await client.get(f"{path}/sources/2/text", params=params)
    ).status_code == 404
    project_user["pid"] = "p2"
    assert (await client.get(text_path, params=params)).status_code == 403
    project_user["pid"] = "p1"
    blobs.path(blob.key).write_bytes(b"changed")
    assert (await client.get(text_path, params=params)).status_code == 409
