"""Resumable source enrichment and complete segment analysis."""

import gzip
from collections import Counter, defaultdict

from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.intelligence.analysis_coverage import segment_processed
from app.intelligence.blob import digest
from app.intelligence.brand_evaluation import summarize_answer_scores
from app.intelligence.brand_signals import summarize_brand_signals
from app.intelligence.evidence import align_claims, verify_pair
from app.intelligence.model_budget import (
    budget_status,
    estimate_tokens,
    recover_reservations,
    reserve,
    settle,
)
from app.intelligence.model_policy import structured_options
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    IntelligenceEntity,
    IntelligenceRun,
    SegmentAnalysis,
    SemanticCache,
    SourceDocument,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.queue import JobQueue, Lease
from app.intelligence.semantics import (
    SEGMENT_VERSION,
    SYSTEM,
    SemanticOutputError,
    analysis_signature,
    analyze_text,
    chunks,
    safe_validation_fields,
    stable_hash,
)
from app.intelligence.source_quality import assess_text, usable_snapshot
from app.intelligence.sources import SourceBlocked, SourceUnavailable, capture_source
from app.utils.timezone import utcnow


class Pipeline:
    def __init__(
        self,
        queue: JobQueue,
        blobs,
        *,
        capture=capture_source,
        analyze=analyze_text,
        verify=verify_pair,
    ):
        self.queue, self.blobs, self.capture, self.analyze = (
            queue,
            blobs,
            capture,
            analyze,
        )
        self.verify = verify

    async def reserve_model_request(
        self, lease, *, phase, messages, max_output, subject_id
    ):
        receipt = None

        async def record(db):
            nonlocal receipt
            from app.intelligence.models import AnalysisJob

            job = await db.get(AnalysisJob, lease.id)
            run = await db.get(IntelligenceRun, lease.run_id)
            if not run or run.project_id != lease.project_id:
                raise LookupError("Run not found")
            ledger, receipt, reason = reserve(
                job.payload.get("model_budget"),
                run.manifest["analysis_budget"],
                phase,
                estimate_tokens(messages, max_output),
                subject_id,
            )
            deferred = {**job.payload.get("model_deferred", {})}
            key = stable_hash([phase, subject_id])
            if receipt:
                ledger["receipts"][receipt]["lease_version"] = lease.version
                deferred.pop(key, None)
            else:
                deferred[key] = {
                    "phase": phase,
                    "subject_id": subject_id,
                    "reason": reason,
                }
            job.payload = {
                **job.payload,
                "model_budget": ledger,
                "model_requests": len(ledger["receipts"]),
                "model_deferred": deferred,
            }

        await self.queue.checkpoint(lease, record)
        return receipt

    async def settle_model_request(self, lease, receipt, usage):
        async def record(db):
            await self.record_model_usage(db, lease, receipt, usage)

        await self.queue.checkpoint(lease, record)

    async def record_model_usage(self, db, lease, receipt, usage):
        from app.intelligence.models import AnalysisJob

        job = await db.get(AnalysisJob, lease.id)
        job.payload = {
            **job.payload,
            "model_budget": settle(job.payload["model_budget"], receipt, usage),
        }

    async def record_model_deferral(self, db, lease, phase, subject_id, reason):
        from app.intelligence.models import AnalysisJob

        job = await db.get(AnalysisJob, lease.id)
        job.payload = {
            **job.payload,
            "model_deferred": {
                **job.payload.get("model_deferred", {}),
                stable_hash([phase, subject_id]): {
                    "phase": phase,
                    "subject_id": subject_id,
                    "reason": reason,
                },
            },
        }

    async def fetch_document(self, lease: Lease):
        audit_id = int(lease.payload["audit_id"])
        for document_id in [lease.subject_id]:
            async with self.queue.sessions() as db:
                document = await db.scalar(
                    select(SourceDocument).where(
                        SourceDocument.project_id == lease.project_id,
                        SourceDocument.id == document_id,
                    )
                )
                if document is None:
                    continue
                url = document.url
                from app.intelligence.models import AnalysisJob

                job = await db.get(AnalysisJob, lease.id)
                existing = await db.scalar(
                    select(SourceFetch.id)
                    .where(
                        SourceFetch.project_id == lease.project_id,
                        SourceFetch.document_id == document_id,
                        SourceFetch.status == "captured",
                        SourceFetch.job_id == job.id,
                    )
                    .limit(1)
                )
                if existing:
                    continue
            try:
                captured = await self.capture(
                    url, settings.intelligence_source_byte_limit
                )
            except (SourceBlocked, SourceUnavailable) as error:
                code = (
                    error.code if isinstance(error, SourceUnavailable) else str(error)
                )

                async def failure(db, code=code, document_id=document_id, error=error):
                    db.add(
                        SourceFetch(
                            project_id=lease.project_id,
                            document_id=document_id,
                            status="blocked"
                            if isinstance(error, SourceBlocked) or code == "blocked"
                            else code,
                            error_code=code[:50],
                            http_status=getattr(error, "status", None),
                            audit_id=audit_id,
                            job_id=lease.id,
                        )
                    )

                await self.queue.checkpoint(lease, failure)
                if isinstance(error, SourceUnavailable) and (
                    error.status == 429 or (error.status or 0) >= 500
                ):
                    raise
                continue
            body_hash = digest(captured.text.encode())
            quality = assess_text(captured.text)
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
            prefix = (
                f"projects/{lease.project_id}/sources/{document_id}/{representation}"
            )
            text_blob = await self.blobs.put_immutable(
                prefix + "/text.txt.gz", gzip.compress(captured.text.encode(), mtime=0)
            )
            raw_blob = await self.blobs.put_immutable(
                prefix + "/raw.html.gz", gzip.compress(captured.raw, mtime=0)
            )

            async def success(
                db,
                document_id=document_id,
                captured=captured,
                representation=representation,
                body_hash=body_hash,
                text_blob=text_blob,
                raw_blob=raw_blob,
                quality=quality,
            ):
                # The document lock serializes shared-snapshot insertion across audits.
                await db.scalar(
                    select(SourceDocument)
                    .where(
                        SourceDocument.project_id == lease.project_id,
                        SourceDocument.id == document_id,
                    )
                    .with_for_update()
                )
                snapshot = await db.scalar(
                    select(SourceSnapshot).where(
                        SourceSnapshot.project_id == lease.project_id,
                        SourceSnapshot.document_id == document_id,
                        SourceSnapshot.representation_hash == representation,
                    )
                )
                if snapshot is None:
                    snapshot = SourceSnapshot(
                        project_id=lease.project_id,
                        document_id=document_id,
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
                db.add(
                    SourceFetch(
                        project_id=lease.project_id,
                        document_id=document_id,
                        snapshot_id=snapshot.id,
                        final_url=captured.final_url,
                        status="captured",
                        http_status=captured.status,
                        audit_id=audit_id,
                        job_id=lease.id,
                    )
                )

            await self.queue.checkpoint(lease, success)

        async def refresh(db):
            from app.intelligence.refresh import request_refresh

            await request_refresh(
                db, lease.project_id, audit_id, "source_terminal", lease.id
            )

        await self.queue.commit(lease, refresh)

    async def refresh_run(self, lease):
        from app.intelligence.refresh import perform_refresh

        async def refresh(db):
            await perform_refresh(db, lease.project_id, int(lease.subject_id), lease.id)

        await self.queue.commit(lease, refresh)

    async def analyze_run(self, lease: Lease):
        async with self.queue.sessions() as db:
            run = await db.scalar(
                select(IntelligenceRun).where(
                    IntelligenceRun.project_id == lease.project_id,
                    IntelligenceRun.id == lease.run_id,
                )
            )
            if run is None:
                raise LookupError("Run not found")
            manifest, signature = run.manifest, run.signature
        api_key, base_url, model = settings.get_analysis_llm_config()
        if analysis_signature(model, base_url) != signature:
            raise ValueError("Analysis configuration changed; create a new run")
        owners = [
            ("answer", item["id"], item["text"])
            for item in manifest["responses"]
            if not item["error"]
        ]
        owners += [
            (
                "citation",
                item["id"],
                "\n".join(part for part in (item["title"], item["snippet"]) if part),
            )
            for item in manifest["sources"]
            if item["title"] or item["snippet"]
        ]
        for snapshot_id in sorted(
            {
                item["snapshot_id"]
                for item in manifest["sources"]
                if usable_snapshot(item)
            }
        ):
            async with self.queue.sessions() as db:
                snapshot = await db.scalar(
                    select(SourceSnapshot).where(
                        SourceSnapshot.project_id == lease.project_id,
                        SourceSnapshot.id == snapshot_id,
                        SourceSnapshot.integrity_status == "verified",
                    )
                )
                if snapshot is None:
                    raise ValueError("Snapshot integrity unavailable")
                text_key, checksum = snapshot.text_key, snapshot.text_sha256
            text = gzip.decompress(await self.blobs.get(text_key, checksum)).decode(
                "utf-8"
            )
            owners.append(("snapshot", snapshot_id, text))

        async def segment_all(db):
            from app.intelligence.models import AnalysisJob

            job = await db.get(AnalysisJob, lease.id)
            ledger = job.payload.get("model_budget") or {
                "policy": manifest["analysis_budget"],
                "receipts": {},
            }
            job.payload = {
                **job.payload,
                "model_budget": recover_reservations(ledger, lease.version),
            }
            current = await db.get(IntelligenceRun, lease.run_id)
            current.status = "running"
            for kind, owner_id, text in owners:
                for chunk in chunks(text):
                    existing = await db.scalar(
                        select(ContentSegment.id).where(
                            ContentSegment.project_id == lease.project_id,
                            ContentSegment.owner_kind == kind,
                            ContentSegment.owner_id == owner_id,
                            ContentSegment.ordinal == chunk["ordinal"],
                            ContentSegment.extractor_version == SEGMENT_VERSION,
                        )
                    )
                    if not existing:
                        db.add(
                            ContentSegment(
                                project_id=lease.project_id,
                                owner_kind=kind,
                                owner_id=owner_id,
                                ordinal=chunk["ordinal"],
                                start_offset=chunk["start"],
                                end_offset=chunk["end"],
                                text=chunk["text"],
                                segment_hash=digest(chunk["text"].encode()),
                                extractor_version=SEGMENT_VERSION,
                            )
                        )
                        await db.flush()

        await self.queue.checkpoint(lease, segment_all)
        for kind, owner_id, _ in owners:
            async with self.queue.sessions() as db:
                segments = (
                    await db.scalars(
                        select(ContentSegment)
                        .where(
                            ContentSegment.project_id == lease.project_id,
                            ContentSegment.owner_kind == kind,
                            ContentSegment.owner_id == owner_id,
                            ContentSegment.extractor_version == SEGMENT_VERSION,
                        )
                        .order_by(ContentSegment.ordinal)
                    )
                ).all()
            for segment in segments:
                async with self.queue.sessions() as db:
                    application = await db.scalar(
                        select(SegmentAnalysis).where(
                            SegmentAnalysis.project_id == lease.project_id,
                            SegmentAnalysis.run_id == lease.run_id,
                            SegmentAnalysis.segment_id == segment.id,
                        )
                    )
                    if application and application.status in (
                        "completed",
                        "partial",
                        "failed",
                    ):
                        continue
                    cache = await db.scalar(
                        select(SemanticCache).where(
                            SemanticCache.project_id == lease.project_id,
                            SemanticCache.segment_hash == segment.segment_hash,
                            SemanticCache.signature == signature,
                        )
                    )
                result, usage, status, diagnostic = None, {}, "deferred", None
                receipt = None
                if cache:
                    result, usage, status = cache.result, cache.usage, "completed"
                    status = "partial" if result.get("rejected_spans") else "completed"
                elif api_key:
                    receipt = await self.reserve_model_request(
                        lease,
                        phase="answer_extraction"
                        if kind == "answer"
                        else "source_extraction",
                        messages=[SYSTEM, segment.text],
                        max_output=structured_options(base_url, model)["max_tokens"],
                        subject_id=segment.id,
                    )
                    if receipt:
                        try:
                            result, usage = await self.analyze(
                                segment.text, api_key, base_url, model
                            )
                            status = (
                                "partial"
                                if result.get("rejected_spans")
                                else "completed"
                            )
                        except (SemanticOutputError, ValidationError) as error:
                            usage = getattr(error, "usage", {})
                            status = "failed"
                            diagnostic = {
                                "segment_id": segment.id,
                                "code": "semantic_output_invalid",
                                "fields": error.fields
                                if isinstance(error, SemanticOutputError)
                                else safe_validation_fields(error),
                            }
                        except Exception:
                            await self.settle_model_request(lease, receipt, None)
                            raise

                async def apply(
                    db,
                    segment=segment,
                    result=result,
                    usage=usage,
                    status=status,
                    diagnostic=diagnostic,
                    receipt=receipt,
                    kind=kind,
                ):
                    if receipt:
                        await self.record_model_usage(db, lease, receipt, usage)
                    if status == "deferred" and not api_key:
                        await self.record_model_deferral(
                            db,
                            lease,
                            "answer_extraction"
                            if kind == "answer"
                            else "source_extraction",
                            segment.id,
                            "analysis_provider_unconfigured",
                        )
                    elif status != "deferred":
                        from app.intelligence.models import AnalysisJob

                        job = await db.get(AnalysisJob, lease.id)
                        deferred = {**job.payload.get("model_deferred", {})}
                        deferred.pop(
                            stable_hash(
                                [
                                    "answer_extraction"
                                    if kind == "answer"
                                    else "source_extraction",
                                    segment.id,
                                ]
                            ),
                            None,
                        )
                        job.payload = {**job.payload, "model_deferred": deferred}
                    if diagnostic:
                        from app.intelligence.models import AnalysisJob

                        job = await db.get(AnalysisJob, lease.id)
                        previous = job.payload.get("semantic_errors", [])
                        job.payload = {
                            **job.payload,
                            "semantic_error_count": job.payload.get(
                                "semantic_error_count", 0
                            )
                            + 1,
                            "semantic_errors": [*previous, diagnostic][:20],
                        }
                    cache = None
                    if result is not None:
                        cache = await db.scalar(
                            select(SemanticCache).where(
                                SemanticCache.project_id == lease.project_id,
                                SemanticCache.segment_hash == segment.segment_hash,
                                SemanticCache.signature == signature,
                            )
                        )
                        if cache is None:
                            cache = SemanticCache(
                                project_id=lease.project_id,
                                segment_hash=segment.segment_hash,
                                signature=signature,
                                result=result,
                                usage=usage,
                            )
                            db.add(cache)
                            await db.flush()
                    application = await db.scalar(
                        select(SegmentAnalysis).where(
                            SegmentAnalysis.project_id == lease.project_id,
                            SegmentAnalysis.run_id == lease.run_id,
                            SegmentAnalysis.segment_id == segment.id,
                        )
                    )
                    if application is None:
                        application = SegmentAnalysis(
                            project_id=lease.project_id,
                            run_id=lease.run_id,
                            segment_id=segment.id,
                            status=status,
                            cache_id=cache.id if cache else None,
                        )
                        db.add(application)
                    else:
                        application.status, application.cache_id = (
                            status,
                            cache.id if cache else None,
                        )
                    for candidate in (result or {}).get("entities", []):
                        identity = stable_hash([candidate["kind"], candidate["name"]])
                        entity = await db.scalar(
                            select(IntelligenceEntity).where(
                                IntelligenceEntity.project_id == lease.project_id,
                                IntelligenceEntity.identity_hash == identity,
                            )
                        )
                        if entity is None:
                            entity = IntelligenceEntity(
                                project_id=lease.project_id,
                                name=candidate["name"],
                                kind=candidate["kind"],
                                identity_hash=identity,
                            )
                            db.add(entity)
                            await db.flush()
                        start, end = (
                            candidate["start"] + segment.start_offset,
                            candidate["end"] + segment.start_offset,
                        )
                        exists = await db.scalar(
                            select(EntityMention.id).where(
                                EntityMention.project_id == lease.project_id,
                                EntityMention.run_id == lease.run_id,
                                EntityMention.segment_id == segment.id,
                                EntityMention.entity_id == entity.id,
                                EntityMention.start_offset == start,
                                EntityMention.end_offset == end,
                            )
                        )
                        if not exists:
                            db.add(
                                EntityMention(
                                    project_id=lease.project_id,
                                    run_id=lease.run_id,
                                    segment_id=segment.id,
                                    entity_id=entity.id,
                                    start_offset=start,
                                    end_offset=end,
                                    quote=candidate["quote"],
                                    sentiment=candidate["sentiment"],
                                    attributes=candidate["attributes"],
                                )
                            )

                await self.queue.checkpoint(lease, apply)

        alignment = await align_claims(self, lease, manifest, api_key, base_url, model)

        async def publish(db):
            current = await db.get(IntelligenceRun, lease.run_id)
            coverage, summary = await build_summary(db, current)
            from app.intelligence.actions import persist_diagnoses

            await persist_diagnoses(db, current, summary)
            from app.intelligence.models import AnalysisJob

            job = await db.get(AnalysisJob, lease.id)
            coverage["semantic_errors"] = job.payload.get("semantic_errors", [])
            coverage["semantic_error_count"] = job.payload.get(
                "semantic_error_count", 0
            )
            ledger = job.payload.get("model_budget") or {
                "policy": manifest["analysis_budget"],
                "receipts": {},
            }
            coverage["model_budget"] = budget_status(ledger)
            coverage["deferred_operations"] = list(
                job.payload.get("model_deferred", {}).values()
            )
            coverage["evidence_alignment"] = alignment
            coverage["complete"] = (
                coverage["complete"]
                and not alignment["deferred_pairs"]
                and not alignment["invalid_pairs"]
            )
            current.coverage, current.summary = coverage, summary
            current.status = "ready" if coverage["complete"] else "partial"
            current.finished_at = utcnow()
            from app.intelligence.refresh import publish_pointer

            await publish_pointer(db, current)

        await self.queue.commit(lease, publish)


async def build_summary(db, run):
    manifest = run.manifest
    applications = (
        await db.execute(
            select(SegmentAnalysis, ContentSegment, SemanticCache)
            .join(ContentSegment, SegmentAnalysis.segment_id == ContentSegment.id)
            .outerjoin(SemanticCache, SegmentAnalysis.cache_id == SemanticCache.id)
            .where(
                SegmentAnalysis.project_id == run.project_id,
                SegmentAnalysis.run_id == run.id,
            )
        )
    ).all()
    mentions = (
        await db.execute(
            select(EntityMention, IntelligenceEntity, ContentSegment)
            .join(IntelligenceEntity, EntityMention.entity_id == IntelligenceEntity.id)
            .join(ContentSegment, EntityMention.segment_id == ContentSegment.id)
            .where(
                EntityMention.project_id == run.project_id,
                EntityMention.run_id == run.id,
            )
        )
    ).all()
    states = Counter(application.status for application, _, _ in applications)
    response_map = {item["id"]: item for item in manifest["responses"]}
    roles = defaultdict(set)
    for brand in manifest.get("brands", []):
        for name in [brand.get("name", ""), *(brand.get("aliases") or [])]:
            roles[name.casefold()].add(
                "competitor" if brand.get("is_competitor") else "own"
            )
    entity_rows = {}
    platforms = defaultdict(
        lambda: {
            "responses": 0,
            "analyzed_responses": 0,
            "source_observations": 0,
            "snapshots": set(),
        }
    )
    answer_states = defaultdict(list)
    claims, topics = [], Counter()
    for application, segment, cache in applications:
        if segment.owner_kind == "answer":
            answer_states[segment.owner_id].append(application.status)
        if cache:
            topics.update(cache.result.get("topics", []))
            for claim in cache.result.get("claims", []):
                claims.append(
                    {
                        "segment_id": segment.id,
                        "owner_kind": segment.owner_kind,
                        "owner_id": segment.owner_id,
                        "quote": claim["quote"],
                        "start": segment.start_offset + claim["start"],
                        "end": segment.start_offset + claim["end"],
                        "subject": claim["subject"],
                        "topic": claim["topic"],
                        "evidence_relation": "unverified",
                    }
                )
    for item in manifest["responses"]:
        if item["error"]:
            continue
        platform = platforms[item["platform"]]
        platform["responses"] += 1
        complete = bool(answer_states[item["id"]]) and all(
            s == "completed" for s in answer_states[item["id"]]
        )
        platform["analyzed_responses"] += int(complete)
    for source in manifest["sources"]:
        platforms[source["platform"]]["source_observations"] += 1
        if source["snapshot_id"]:
            platforms[source["platform"]]["snapshots"].add(source["snapshot_id"])
    evidence = []
    from app.intelligence.aliases import resolved_entity
    from app.intelligence.mention_bindings import attributed_entity, binding_key

    split_sources = {
        item["source_entity_id"] for item in manifest.get("entity_splits", {}).values()
    }
    for mention, entity, segment in mentions:
        resolved_id = attributed_entity(
            mention, segment, manifest, resolved_entity(entity.id, manifest)
        )
        identity = manifest.get("entity_resolutions", {}).get(resolved_id, {})
        resolved_name = (
            identity.get("canonical_name", entity.name)
            if resolved_id != entity.id
            else entity.name
        )
        row = entity_rows.setdefault(
            resolved_id,
            {
                "id": resolved_id,
                "name": resolved_name,
                "kind": entity.kind,
                "role": next(iter(roles[entity.name.casefold()]))
                if len(roles[entity.name.casefold()]) == 1
                else "unregistered",
                "identity_status": manifest.get("entity_resolutions", {})
                .get(resolved_id, {})
                .get("decision", "provisional"),
                "canonical_name": manifest.get("entity_resolutions", {})
                .get(resolved_id, {})
                .get("canonical_name", entity.name),
                "responses": set(),
                "source_mentions": 0,
                "platform_responses": defaultdict(set),
                "member_ids": set(),
                "observed_names": set(),
                "registered_roles": set(),
                "members": {},
            },
        )
        row["member_ids"].add(entity.id)
        row["members"][entity.id] = {"id": entity.id, "name": entity.name}
        row["observed_names"].add(entity.name)
        if manifest.get("mention_bindings", {}).get(
            binding_key(mention, segment), {}
        ).get("decision") not in ("assign", "split"):
            row["registered_roles"].update(roles[entity.name.casefold()])
        row["registered_roles"].update(roles[resolved_name.casefold()])
        if manifest.get("brand_context") is not None:
            from app.intelligence.project_context import bound_roles

            row["registered_roles"] = bound_roles(manifest, resolved_id)
        row["role"] = (
            next(iter(row["registered_roles"]))
            if len(row["registered_roles"]) == 1
            else "unregistered"
        )
        if segment.owner_kind == "answer" and segment.owner_id in response_map:
            response = response_map[segment.owner_id]
            row["responses"].add(segment.owner_id)
            row["platform_responses"][response["platform"]].add(segment.owner_id)
        else:
            row["source_mentions"] += 1
        evidence.append(
            {
                "id": mention.id,
                "binding_key": binding_key(mention, segment),
                "attribution": manifest.get("mention_bindings", {}).get(
                    binding_key(mention, segment)
                ),
                "entity_id": resolved_id,
                "entity_name": resolved_name,
                "identity_ambiguity": entity.id in split_sources
                and not (
                    manifest.get("mention_bindings", {})
                    .get(binding_key(mention, segment), {})
                    .get("decision")
                    in ("assign", "split")
                    and identity.get("decision") == "confirmed"
                ),
                "observed_entity_id": entity.id,
                "observed_entity_name": entity.name,
                "segment_id": segment.id,
                "owner_kind": segment.owner_kind,
                "owner_id": segment.owner_id,
                "start": mention.start_offset,
                "end": mention.end_offset,
                "quote": mention.quote,
                "sentiment": mention.sentiment,
                "attributes": mention.attributes,
            }
        )
    entities = [
        {
            **{
                key: value
                for key, value in row.items()
                if key
                not in (
                    "responses",
                    "platform_responses",
                    "member_ids",
                    "observed_names",
                    "registered_roles",
                    "members",
                )
            },
            "response_count": len(row["responses"]),
            "member_ids": sorted(row["member_ids"]),
            "observed_entities": sorted(
                row["members"].values(), key=lambda item: item["id"]
            ),
            "observed_names": sorted(row["observed_names"]),
            "role_conflict": len(row["registered_roles"]) > 1,
            "platform_response_counts": {
                key: len(value) for key, value in row["platform_responses"].items()
            },
        }
        for row in entity_rows.values()
    ]
    total_chars = sum(len(segment.text) for _, segment, _ in applications)
    analyzed_chars = sum(
        len(segment.text)
        for application, segment, _ in applications
        if application.status == "completed"
    )
    coverage = {
        "total_segments": len(applications),
        "segment_states": dict(states),
        "total_characters": total_chars,
        "analyzed_characters": analyzed_chars,
        "processed_segments": sum(
            segment_processed(application, cache)
            for application, _, cache in applications
        ),
        "processed_characters": sum(
            len(segment.text)
            for application, segment, cache in applications
            if segment_processed(application, cache)
        ),
        "rejected_spans": sum(
            (cache.result or {}).get("rejected_spans", 0)
            for _, _, cache in applications
            if cache
        ),
        "responses": len(response_map),
        "expected_samples": manifest.get("expected_samples", len(response_map)),
        "pending_samples": sum(
            status == "pending"
            for status in manifest.get("sample_statuses", {}).values()
        ),
        "failed_samples": sum(
            status == "failed"
            for status in manifest.get("sample_statuses", {}).values()
        ),
        "partial_answers": sum(
            item.get("answer_status", "unknown") != "complete"
            for item in response_map.values()
        ),
        "failed_responses": sum(bool(item["error"]) for item in response_map.values()),
        "sources": len(manifest["sources"]),
        "captured_sources": sum(bool(s["snapshot_id"]) for s in manifest["sources"]),
        "unusable_source_observations": sum(
            bool(s["snapshot_id"]) and not usable_snapshot(s)
            for s in manifest["sources"]
        ),
        "complete": bool(applications)
        and states.get("completed", 0) == len(applications)
        and not any(s["fetch_status"] != "captured" for s in manifest["sources"])
        and not any(item["error"] for item in response_map.values()),
    }
    # Retrieval/semantic completeness are independent; HTML article coverage is unknown.
    coverage["article_completeness"] = (
        "unknown" if coverage["captured_sources"] else "unavailable"
    )
    coverage["complete"] = (
        coverage["complete"]
        and not manifest["sources"]
        and not coverage["pending_samples"]
        and not coverage["failed_samples"]
        and not coverage["partial_answers"]
    )
    platform_rows = [
        {
            "platform": name,
            **{key: value for key, value in data.items() if key != "snapshots"},
            "snapshot_count": len(data["snapshots"]),
        }
        for name, data in platforms.items()
    ]
    diagnoses = [
        {
            "platform": p["platform"],
            "kind": "coverage_gap",
            "finding": "部分回答或来源正文尚未完成分析，未发现实体不能判定品牌缺席。",
            "source_ids": [
                s["id"]
                for s in manifest["sources"]
                if s["platform"] == p["platform"] and not s["snapshot_id"]
            ],
            "review_state": "needs_review",
        }
        for p in platform_rows
        if p["analyzed_responses"] < p["responses"]
        or any(
            s["platform"] == p["platform"] and not s["snapshot_id"]
            for s in manifest["sources"]
        )
    ]
    from app.intelligence.models import ClaimEvidence

    evidence_rows = (
        await db.scalars(
            select(ClaimEvidence).where(
                ClaimEvidence.project_id == run.project_id,
                ClaimEvidence.run_id == run.id,
            )
        )
    ).all()
    sources_by_id = {source["id"]: source for source in manifest["sources"]}
    claim_evidence = [
        {
            "id": row.id,
            "claim_hash": row.claim_hash,
            "answer_quote": row.answer_quote,
            "source_quote": row.source_quote,
            "relation": row.relation,
            "review_state": row.review_state,
            "citation_id": row.citation_id,
            "source_segment_id": row.source_segment_id,
            "snapshot_id": sources_by_id[row.citation_id]["snapshot_id"],
            "source_title": sources_by_id[row.citation_id]["title"],
            "source_url": sources_by_id[row.citation_id]["url"],
            "platform": sources_by_id[row.citation_id]["platform"],
        }
        for row in evidence_rows
        if row.citation_id in sources_by_id
    ]
    groups = {}
    for response in response_map.values():
        key = stable_hash(
            [
                response["prompt_hash"],
                response["platform"],
                response["conditions"],
                response.get("surface"),
            ]
        )
        group = groups.setdefault(
            key,
            {
                "prompt_hash": response["prompt_hash"],
                "prompt": response["prompt"],
                "platform": response["platform"],
                "conditions": response["conditions"],
                "sample_count": 0,
                "comparable": response["conditions"].get("searchMode")
                in ("enabled", "disabled")
                and bool(response["conditions"].get("modelVersion"))
                and bool(response["conditions"].get("accountClass")),
            },
        )
        group["sample_count"] += 1
    summary = {
        "brand_signals": summarize_brand_signals(applications, response_map, evidence),
        "entities": entities,
        "platforms": platform_rows,
        "mentions": evidence,
        "claims": claims,
        "claim_evidence": claim_evidence,
        "prompt_platforms": list(groups.values()),
        "topics": dict(topics),
        "diagnoses": diagnoses,
        "identity_note": "同名实体仅作候选分组，未经身份确认，不自动登记为竞品。",
    }
    from app.intelligence.diagnosis import platform_diagnoses

    summary["brand_evaluation"] = summarize_answer_scores(
        manifest, applications, entities, evidence, summary["brand_signals"]
    )
    from app.intelligence.brand_presence import build_brand_presence

    summary["brand_presence"] = build_brand_presence(
        manifest,
        applications,
        entities,
        evidence,
        summary["brand_signals"],
        summary["brand_evaluation"],
    )
    summary["diagnoses"] = diagnoses + platform_diagnoses(run, summary)
    from app.intelligence.competitive_actions import competitive_diagnoses

    summary["diagnoses"].extend(competitive_diagnoses(run, summary))
    return coverage, summary
