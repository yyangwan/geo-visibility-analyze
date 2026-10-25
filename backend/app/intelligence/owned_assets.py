"""Project-owned immutable material revisions, with fenced public acquisition."""

import gzip
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import func, select

from app.config import settings
from app.intelligence.blob import configured_store, digest
from app.intelligence.models import (
    AnalysisJob,
    IntelligenceRun,
    OwnedContentAsset,
    ProjectContextEvent,
    ProjectContextHead,
    SourceDocument,
    SourceSnapshot,
)
from app.intelligence.project_context import context_snapshot
from app.intelligence.refresh import request_refresh
from app.intelligence.semantics import stable_hash
from app.intelligence.source_quality import assess_text
from app.intelligence.sources import SourceBlocked, SourceUnavailable, normalized_url
from app.utils.timezone import utc_isoformat, utcnow


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


class RegisterAsset(Request):
    registry_brand_id: str = Field(alias="registryBrandId", min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    url: str | None = Field(default=None, max_length=4096)
    text: str | None = Field(default=None, min_length=1, max_length=200000)
    asset_key: str | None = Field(
        default=None, alias="assetKey", pattern=r"^[a-f0-9]{32}$"
    )
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)

    @model_validator(mode="after")
    def source(self):
        if not self.title.strip() or not (self.url or self.text):
            raise ValueError("Supply URL or plain text source")
        if self.text is not None and not self.text.strip():
            raise ValueError("Material text cannot be blank")
        if bool(self.asset_key) != bool(self.expected_version):
            raise ValueError("Revision requires existing asset key and version")
        return self


class VerifyAsset(Request):
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)
    material_hash: str = Field(alias="materialHash", pattern=r"^[a-f0-9]{64}$")
    decision: str = Field(pattern=r"^(verified|rejected)$")
    scope: str = Field(min_length=5, max_length=1000)
    reason: str = Field(min_length=5, max_length=2000)


class RetryAsset(Request):
    reason: str = Field(min_length=5, max_length=2000)


async def lock_project(db, project_id):
    head = await db.scalar(
        select(ProjectContextHead)
        .where(ProjectContextHead.project_id == project_id)
        .with_for_update()
    )
    if head is None:
        raise HTTPException(409, "Synchronize registered brand context first")


async def own_brand(db, project_id, brand_id, *, lock=False):
    if lock:
        event = await db.scalar(
            select(ProjectContextEvent)
            .where(ProjectContextEvent.project_id == project_id)
            .order_by(ProjectContextEvent.version.desc())
            .limit(1)
            .with_for_update()
        )
        context = {"brands": event.brands} if event else None
    else:
        context = await context_snapshot(db, project_id)
    brand = next(
        (
            row
            for row in (context or {}).get("brands", [])
            if str(row["id"]) == brand_id
        ),
        None,
    )
    if (
        not brand
        or brand.get("is_competitor")
        or brand.get("analysis_role") == "comparison"
    ):
        raise HTTPException(422, "Material requires a current own registry brand")
    return brand


async def latest_asset(db, project_id, key, *, lock=False):
    query = (
        select(OwnedContentAsset)
        .where(
            OwnedContentAsset.project_id == project_id,
            OwnedContentAsset.asset_key == key,
        )
        .order_by(OwnedContentAsset.version.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def get_asset(db, project_id, asset_id, *, lock=False):
    query = select(OwnedContentAsset).where(
        OwnedContentAsset.project_id == project_id, OwnedContentAsset.id == asset_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(query)
    if row is None:
        raise HTTPException(404, "Material not found")
    return row


async def notify_project(db, project_id, reason, event_id):
    run = await db.scalar(
        select(IntelligenceRun)
        .where(IntelligenceRun.project_id == project_id)
        .order_by(IntelligenceRun.created_at.desc(), IntelligenceRun.revision.desc())
        .limit(1)
    )
    if run:
        await request_refresh(db, project_id, run.audit_id, reason, event_id)


def material_hash(asset):
    return stable_hash(
        [
            asset.id,
            asset.version,
            asset.target_id,
            asset.source_kind,
            asset.snapshot_id,
            asset.text_key,
            asset.text_sha256,
            asset.verification.get("body_hash"),
            asset.verification.get("text_length"),
        ]
    )


async def read_text(asset, blobs=None):
    if asset.status != "captured" or not asset.text_key or not asset.text_sha256:
        raise HTTPException(409, "Material text not captured")
    blobs = blobs or configured_store()
    data = await blobs.get(asset.text_key, asset.text_sha256)
    text = gzip.decompress(data).decode("utf-8")
    if digest(text.encode()) != asset.verification.get("body_hash") or len(
        text
    ) != asset.verification.get("text_length"):
        raise HTTPException(409, "Material text integrity changed")
    return text


async def register_asset(db, project_id, data, actor, blobs=None):
    from app.intelligence.reviews import add_event

    await lock_project(db, project_id)
    request_hash = stable_hash([data.model_dump(), actor])
    old = await db.scalar(
        select(OwnedContentAsset)
        .where(
            OwnedContentAsset.project_id == project_id,
            OwnedContentAsset.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, "Material registration key conflict")
        return old
    await own_brand(db, project_id, data.registry_brand_id, lock=True)
    previous = (
        await latest_asset(db, project_id, data.asset_key, lock=True)
        if data.asset_key
        else None
    )
    if data.asset_key and (
        not previous
        or previous.version != data.expected_version
        or previous.registry_brand_id != data.registry_brand_id
    ):
        raise HTTPException(409, "Material revision changed")
    count = await db.scalar(
        select(func.count(func.distinct(OwnedContentAsset.asset_key))).where(
            OwnedContentAsset.project_id == project_id
        )
    )
    if previous is None and count >= 200:
        raise HTTPException(422, "Project material limit reached")
    try:
        url = normalized_url(data.url) if data.url else None
    except SourceBlocked:
        raise HTTPException(422, "Valid public HTTP(S) material URL required") from None
    asset_id = stable_hash([project_id, "owned-asset", data.idempotency_key])[:32]
    row = OwnedContentAsset(
        id=asset_id,
        project_id=project_id,
        asset_key=previous.asset_key if previous else asset_id,
        version=data.expected_version + 1,
        registry_brand_id=data.registry_brand_id,
        target_id="registry:" + data.registry_brand_id,
        title=data.title.strip(),
        source_kind="text" if data.text is not None else "url",
        original_url=url,
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        verification={"version": 0, "decision": "unreviewed", "purpose": "audit_reference", "history": []},
        status="captured" if data.text is not None else "queued",
    )
    if data.text is not None:
        blobs = blobs or configured_store()
        blob = await blobs.put_immutable(
            f"projects/{project_id}/owned/{asset_id}/text.txt.gz",
            gzip.compress(data.text.encode(), mtime=0),
        )
        row.text_key, row.text_sha256 = blob.key, blob.sha256
        row.verification = {
            **row.verification,
            "body_hash": digest(data.text.encode()),
            "text_length": len(data.text),
            "quality": assess_text(data.text),
            "captured_at": utc_isoformat(utcnow()),
        }
    await add_event(db, row)
    if row.source_kind == "url":
        db.add(
            AnalysisJob(
                project_id=project_id,
                kind="fetch_owned_asset",
                subject_id=row.id,
                payload={"asset_id": row.id},
                idempotency_hash=stable_hash(["fetch-owned", row.id]),
            )
        )
    await notify_project(db, project_id, "owned_material_registered", row.id)
    return row


async def asset_view(db, asset):
    latest = await latest_asset(db, asset.project_id, asset.asset_key)
    job = await db.scalar(
        select(AnalysisJob)
        .where(
            AnalysisJob.project_id == asset.project_id,
            AnalysisJob.kind == "fetch_owned_asset",
            AnalysisJob.subject_id == asset.id,
        )
        .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
        .limit(1)
    )
    status = asset.status
    if status != "captured" and job and job.status == "failed":
        status = "failed"
    return {
        "id": asset.id,
        "asset_key": asset.asset_key,
        "version": asset.version,
        "registry_brand_id": asset.registry_brand_id,
        "target_id": asset.target_id,
        "title": asset.title,
        "source_kind": asset.source_kind,
        "purpose": asset.verification.get("purpose"),
        "url": asset.original_url,
        "status": status,
        "is_latest": latest.id == asset.id,
        "verification": {
            key: value
            for key, value in asset.verification.items()
            if key
            in (
                "version",
                "decision",
                "scope",
                "reason",
                "actor_id",
                "reviewed_at",
                "body_hash",
                "text_length",
                "captured_at",
                "quality",
            )
        },
        "material_hash": material_hash(asset) if asset.status == "captured" else None,
        "job_state": job.status if job else None,
        "error_code": job.error_code
        if job and job.error_code
        else asset.verification.get("error_code"),
        "created_at": utc_isoformat(asset.created_at),
    }


async def list_assets(db, project_id, *, offset=0, limit=50):
    rows = (
        await db.scalars(
            select(OwnedContentAsset)
            .where(OwnedContentAsset.project_id == project_id)
            .order_by(OwnedContentAsset.created_at.desc(), OwnedContentAsset.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [await asset_view(db, row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


async def verify_asset(db, asset, data, actor, blobs=None):
    await lock_project(db, asset.project_id)
    asset = await get_asset(db, asset.project_id, asset.id, lock=True)
    request_hash = stable_hash([data.model_dump(), actor])
    history = asset.verification.get("history", [])
    old = next(
        (
            event
            for event in history
            if event["idempotency_key"] == data.idempotency_key
        ),
        None,
    )
    if old:
        if old["request_hash"] != request_hash:
            raise HTTPException(409, "Material verification key conflict")
        return asset
    await own_brand(db, asset.project_id, asset.registry_brand_id, lock=True)
    if (
        await latest_asset(db, asset.project_id, asset.asset_key, lock=True)
    ).id != asset.id:
        raise HTTPException(409, "Verify the latest material revision")
    if asset.verification.get(
        "version", 0
    ) != data.expected_version or data.material_hash != material_hash(asset):
        raise HTTPException(409, "Material or verification version changed")
    if min(len(data.scope.strip()), len(data.reason.strip())) < 5:
        raise HTTPException(422, "Material verification needs scope and reason")
    await read_text(asset, blobs)
    if asset.verification.get("quality", {}).get("evidence_usable") is False:
        raise HTTPException(409, "Captured text is not usable material")
    event = {
        "id": uuid4().hex,
        "version": data.expected_version + 1,
        "decision": data.decision,
        "scope": data.scope.strip(),
        "reason": data.reason.strip(),
        "actor_id": actor,
        "material_hash": data.material_hash,
        "reviewed_at": utc_isoformat(utcnow()),
        "idempotency_key": data.idempotency_key,
        "request_hash": request_hash,
    }
    asset.verification = {**asset.verification, **event, "history": [*history, event]}
    await notify_project(db, asset.project_id, "owned_material_verified", event["id"])
    return asset


async def retry_asset(db, asset, data, actor):
    await lock_project(db, asset.project_id)
    asset = await get_asset(db, asset.project_id, asset.id, lock=True)
    key = stable_hash(["owned-retry", asset.project_id, data.idempotency_key])
    request_hash = stable_hash([asset.id, data.model_dump(), actor])
    old = await db.scalar(
        select(AnalysisJob)
        .where(
            AnalysisJob.project_id == asset.project_id,
            AnalysisJob.idempotency_hash == key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if old.payload.get("request_hash") != request_hash:
            raise HTTPException(409, "Material retry key conflict")
        return old
    await own_brand(db, asset.project_id, asset.registry_brand_id, lock=True)
    active = await db.scalar(
        select(AnalysisJob.id)
        .where(
            AnalysisJob.project_id == asset.project_id,
            AnalysisJob.kind == "fetch_owned_asset",
            AnalysisJob.subject_id == asset.id,
            AnalysisJob.status.in_(["queued", "leased", "retry_wait"]),
        )
        .limit(1)
        .with_for_update()
    )
    if (
        asset.source_kind != "url"
        or asset.status == "captured"
        or active
        or (await latest_asset(db, asset.project_id, asset.asset_key, lock=True)).id
        != asset.id
    ):
        raise HTTPException(
            409, "Retry requires a terminal uncaptured latest URL revision"
        )
    if len(data.reason.strip()) < 5:
        raise HTTPException(422, "Retry rationale required")
    job = AnalysisJob(
        project_id=asset.project_id,
        kind="fetch_owned_asset",
        subject_id=asset.id,
        idempotency_hash=key,
        payload={
            "asset_id": asset.id,
            "request_hash": request_hash,
            "actor_id": actor,
            "reason": data.reason.strip(),
        },
    )
    db.add(job)
    asset.status = "queued"
    return job


async def material_snapshot(db, project_id):
    rows = (
        await db.scalars(
            select(OwnedContentAsset)
            .where(OwnedContentAsset.project_id == project_id)
            .order_by(OwnedContentAsset.asset_key, OwnedContentAsset.version)
        )
    ).all()
    latest = {row.asset_key: row for row in rows}
    return [
        {
            "id": row.id,
            "asset_key": row.asset_key,
            "version": row.version,
            "target_id": row.target_id,
            "registry_brand_id": row.registry_brand_id,
            "purpose": row.verification.get("purpose"),
            "status": row.status,
            "snapshot_id": row.snapshot_id,
            "material_hash": material_hash(row),
            "body_hash": row.verification.get("body_hash"),
            "verification_id": row.verification.get("id"),
            "verification_version": row.verification.get("version", 0),
            "decision": row.verification.get("decision", "unreviewed"),
        }
        for row in latest.values()
    ]


async def fetch_owned_asset(pipeline, lease):
    async with pipeline.queue.sessions() as db:
        asset = await get_asset(db, lease.project_id, lease.subject_id)
        if asset.status == "captured":
            await pipeline.queue.commit(lease, lambda db: _no_change(db))
            return
        url = asset.original_url
    try:
        captured = await pipeline.capture(url, settings.intelligence_source_byte_limit)
    except (SourceBlocked, SourceUnavailable) as error:
        retryable = isinstance(error, SourceUnavailable) and (
            error.status == 429 or (error.status or 0) >= 500
        )
        code = (
            error.code
            if isinstance(error, SourceUnavailable)
            else "blocked_destination"
        )

        async def failure(db):
            row = await get_asset(db, lease.project_id, lease.subject_id)
            row.status = (
                "retry_wait"
                if retryable
                else "blocked"
                if code in ("blocked", "blocked_destination")
                else "failed"
            )
            row.verification = {**row.verification, "error_code": code}
            if not retryable:
                await notify_project(
                    db, row.project_id, "owned_material_terminal", lease.id
                )

        await pipeline.queue.checkpoint(lease, failure)
        if retryable:
            raise
        await pipeline.queue.commit(lease, _no_change)
        return
    quality, body_hash = assess_text(captured.text), digest(captured.text.encode())
    representation = stable_hash(
        [
            body_hash,
            digest(captured.raw),
            captured.extractor_version,
            captured.completeness,
            "public-http",
            captured.final_url,
            quality,
        ]
    )
    # Acquisition owns private object keys. No model/URL can select a storage path.
    prefix = (
        f"projects/{lease.project_id}/sources/owned-{lease.subject_id}/{representation}"
    )
    text_blob = await pipeline.blobs.put_immutable(
        prefix + "/text.txt.gz", gzip.compress(captured.text.encode(), mtime=0)
    )
    raw_blob = await pipeline.blobs.put_immutable(
        prefix + "/raw.html.gz", gzip.compress(captured.raw, mtime=0)
    )

    async def success(db):
        await lock_project(db, lease.project_id)
        row = await get_asset(db, lease.project_id, lease.subject_id)
        document = await db.scalar(
            select(SourceDocument)
            .where(
                SourceDocument.project_id == lease.project_id,
                SourceDocument.url_hash == stable_hash(url),
            )
            .with_for_update()
        )
        if not document:
            document = SourceDocument(
                project_id=lease.project_id,
                url=url,
                url_hash=stable_hash(url),
                title=row.title,
            )
            db.add(document)
            await db.flush()
        snapshot = await db.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.project_id == lease.project_id,
                SourceSnapshot.document_id == document.id,
                SourceSnapshot.representation_hash == representation,
            )
        )
        if not snapshot:
            snapshot = SourceSnapshot(
                project_id=lease.project_id,
                document_id=document.id,
                body_hash=body_hash,
                representation_hash=representation,
                text_key=text_blob.key,
                text_sha256=text_blob.sha256,
                raw_key=raw_blob.key,
                raw_sha256=raw_blob.sha256,
                completeness=captured.completeness,
                extractor_version=captured.extractor_version,
                quality=quality,
            )
            db.add(snapshot)
            await db.flush()
        row.snapshot_id, row.text_key, row.text_sha256, row.status = (
            snapshot.id,
            snapshot.text_key,
            snapshot.text_sha256,
            "captured",
        )
        row.verification = {
            **row.verification,
            "body_hash": body_hash,
            "text_length": len(captured.text),
            "quality": quality,
            "captured_at": utc_isoformat(utcnow()),
            "error_code": None,
        }
        await notify_project(db, row.project_id, "owned_material_captured", row.id)

    await pipeline.queue.commit(lease, success)


async def _no_change(db):
    pass
