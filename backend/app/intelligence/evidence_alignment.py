"""Bounded, anchor-aware evidence selection with explicit unchecked ranges."""

import json
from collections import defaultdict

from pydantic import ValidationError
from sqlalchemy import select

from app.intelligence.blob import digest
from app.intelligence.citation_context import (
    EVIDENCE_POLICY,
    PAIRS_PER_CLAIM,
    association,
    snapshot_matches,
)
from app.intelligence.evidence import SYSTEM, checked_verdict, relevance
from app.intelligence.model_policy import structured_options
from app.intelligence.models import (
    ClaimEvidence,
    ContentSegment,
    SegmentAnalysis,
    SemanticCache,
    SourceSnapshot,
)
from app.intelligence.semantics import SemanticOutputError, stable_hash
from app.intelligence.source_quality import usable_snapshot


def ranges(segments):
    return merge_ranges(
        [[segment.start_offset, segment.end_offset] for segment in segments]
    )


def merge_ranges(values):
    result = []
    for start, end in sorted(values):
        if result and result[-1][1] >= start:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def verified_segments(rows, snapshot):
    ordered = sorted(rows, key=lambda row: row.start_offset)
    position = 0
    for row in ordered:
        if row.start_offset != position or row.end_offset - row.start_offset != len(
            row.text
        ):
            return []
        position = row.end_offset
    if digest("".join(row.text for row in ordered).encode()) != snapshot.body_hash:
        return []
    return ordered


def source_span(segment, quote):
    if not quote:
        return None
    start = segment.text.find(quote)
    if start < 0 or segment.text.find(quote, start + 1) >= 0:
        raise SemanticOutputError("Ambiguous source quotation")
    return {
        "start": segment.start_offset + start,
        "end": segment.start_offset + start + len(quote),
        "quote": quote,
    }


async def align_claims(pipeline, lease, manifest, api_key, base_url, model):
    async with pipeline.queue.sessions() as db:
        applications = (
            await db.execute(
                select(ContentSegment, SemanticCache)
                .join(
                    SegmentAnalysis,
                    (SegmentAnalysis.segment_id == ContentSegment.id)
                    & (SegmentAnalysis.project_id == ContentSegment.project_id),
                )
                .join(SemanticCache, SegmentAnalysis.cache_id == SemanticCache.id)
                .where(
                    SegmentAnalysis.project_id == lease.project_id,
                    SegmentAnalysis.run_id == lease.run_id,
                    ContentSegment.owner_kind == "answer",
                )
                .order_by(ContentSegment.owner_id, ContentSegment.ordinal)
            )
        ).all()
        snapshots = {
            source["snapshot_id"]
            for source in manifest["sources"]
            if usable_snapshot(source)
        }
        snapshot_rows = (
            (
                await db.scalars(
                    select(SourceSnapshot).where(
                        SourceSnapshot.project_id == lease.project_id,
                        SourceSnapshot.id.in_(snapshots),
                    )
                )
            ).all()
            if snapshots
            else []
        )
        segment_rows = (
            (
                await db.scalars(
                    select(ContentSegment)
                    .join(
                        SegmentAnalysis,
                        (SegmentAnalysis.segment_id == ContentSegment.id)
                        & (SegmentAnalysis.project_id == ContentSegment.project_id),
                    )
                    .where(
                        SegmentAnalysis.project_id == lease.project_id,
                        SegmentAnalysis.run_id == lease.run_id,
                        ContentSegment.owner_kind == "snapshot",
                        ContentSegment.owner_id.in_(snapshots),
                    )
                )
            ).all()
            if snapshots
            else []
        )
        existing = (
            await db.scalars(
                select(ClaimEvidence).where(
                    ClaimEvidence.project_id == lease.project_id,
                    ClaimEvidence.run_id == lease.run_id,
                )
            )
        ).all()
    by_snapshot = defaultdict(list)
    for segment in segment_rows:
        by_snapshot[segment.owner_id].append(segment)
    snapshot_map = {row.id: row for row in snapshot_rows}
    source_segments = {
        row.id: verified_segments(by_snapshot[row.id], row)
        for row in snapshot_rows
        if row.integrity_status == "verified"
        and row.quality.get("evidence_usable") is not False
    }
    by_pair = {
        (row.claim_hash, row.citation_id, row.source_segment_id): row
        for row in existing
    }
    reusable = {
        row.verification.get("input_hash"): row.verification["result"]
        for row in existing
        if row.method != "model_output_invalid" and row.verification.get("result")
    }
    responses = {row["id"]: row for row in manifest["responses"]}
    plans, processed, deferred, invalid, selected_count, total = [], 0, 0, 0, 0, 0
    for answer_segment, cache in applications:
        response = responses.get(answer_segment.owner_id)
        if (
            not response
            or response.get("error")
            or response["text"][answer_segment.start_offset : answer_segment.end_offset]
            != answer_segment.text
        ):
            continue
        references = [
            source
            for source in manifest["sources"]
            if source["response_id"] == response["id"]
            and source["platform"] == response["platform"]
            and source["kind"] == "answer_reference"
            and source.get("url_resolution") == "exact"
        ]
        for claim in cache.result.get("claims", []):
            start, end = (
                answer_segment.start_offset + claim["start"],
                answer_segment.start_offset + claim["end"],
            )
            if not (start < end and response["text"][start:end] == claim["quote"]):
                continue
            claim_hash = stable_hash(
                [answer_segment.id, claim["start"], claim["end"], claim["quote"]]
            )
            claim_plans, ranked = [], []
            for source in references:
                link = association(source, response, start, end)
                snapshot = snapshot_map.get(source.get("snapshot_id"))
                candidates = (
                    source_segments.get(source.get("snapshot_id"), [])
                    if link["eligible"]
                    and usable_snapshot(source)
                    and snapshot_matches(source, snapshot)
                    and snapshot.document_id == source.get("document_id")
                    else []
                )
                candidates = sorted(
                    candidates,
                    key=lambda row: (-relevance(claim, row.text), row.ordinal),
                )
                plan = {
                    "claim_hash": claim_hash,
                    "answer_segment_id": answer_segment.id,
                    "answer_span": {
                        "start": start,
                        "end": end,
                        "quote": claim["quote"],
                    },
                    "citation_id": source["id"],
                    "response_id": response["id"],
                    "platform": response["platform"],
                    "snapshot_id": source.get("snapshot_id"),
                    "association": link,
                    "available_segments": len(candidates),
                    "selected": [],
                    "unchecked_ranges": [],
                    "unavailable_reason": None
                    if candidates
                    else link["method"]
                    if not link["eligible"]
                    else "saved_text_unavailable_or_invalid",
                }
                claim_plans.append((plan, source, candidates))
                # One best candidate from each observation before second passages.
                ranked.extend(
                    (
                        depth,
                        link["priority"],
                        -relevance(claim, segment.text),
                        source.get("ordinal", 0),
                        source["id"],
                        segment.ordinal,
                        plan,
                        source,
                        segment,
                    )
                    for depth, segment in enumerate(candidates)
                )
            chosen = sorted(ranked, key=lambda item: item[:6])[:PAIRS_PER_CLAIM]
            total += len(ranked)
            selected_count += len(chosen)
            selected_ids = {(item[7]["id"], item[8].id) for item in chosen}
            for plan, source, candidates in claim_plans:
                plan["unchecked_ranges"] = ranges(
                    [
                        row
                        for row in candidates
                        if (source["id"], row.id) not in selected_ids
                    ]
                )
                plans.append(plan)
            for _, _, _, _, _, _, plan, source, segment in chosen:
                operation_id = stable_hash([claim_hash, source["id"], segment.id])
                input_hash = stable_hash(
                    [
                        EVIDENCE_POLICY,
                        SYSTEM,
                        model,
                        base_url,
                        claim["quote"],
                        segment.text,
                    ]
                )
                trace = {
                    "segment_id": segment.id,
                    "start": segment.start_offset,
                    "end": segment.end_offset,
                    "status": "deferred",
                }
                plan["selected"].append(trace)
                prior = by_pair.get((claim_hash, source["id"], segment.id))
                if prior:
                    trace.update(
                        status="invalid"
                        if prior.method == "model_output_invalid"
                        else "checked",
                        evidence_id=prior.id,
                    )
                    invalid += prior.method == "model_output_invalid"
                    processed += 1
                    continue
                receipt, usage = None, {}
                verdict = reusable.get(input_hash)
                method = (
                    "same_text_reuse" if verdict is not None else "model_verification"
                )
                if verdict is None:
                    if api_key:
                        receipt = await pipeline.reserve_model_request(
                            lease,
                            phase="verification",
                            messages=[
                                SYSTEM,
                                json.dumps(
                                    {
                                        "answer_claim": claim["quote"],
                                        "source_passage": segment.text,
                                    },
                                    ensure_ascii=False,
                                ),
                            ],
                            max_output=structured_options(
                                base_url, model, verification=True
                            )["max_tokens"],
                            subject_id=operation_id,
                        )
                    else:

                        async def defer(db, operation_id=operation_id):
                            await pipeline.record_model_deferral(
                                db,
                                lease,
                                "verification",
                                operation_id,
                                "analysis_provider_unconfigured",
                            )

                        await pipeline.queue.checkpoint(lease, defer)
                    if not receipt:
                        deferred += 1
                        continue
                    try:
                        raw, usage = await pipeline.verify(
                            claim["quote"], segment.text, api_key, base_url, model
                        )
                        verdict = checked_verdict(segment.text, raw, claim["quote"])
                        if (
                            raw.get("relation") != "unverified"
                            and verdict["relation"] == "unverified"
                        ):
                            raise SemanticOutputError(
                                "Ungrounded verification quotation"
                            )
                        source_span(segment, verdict["source_quote"])
                    except (
                        ValidationError,
                        json.JSONDecodeError,
                        SemanticOutputError,
                    ) as error:
                        usage = getattr(error, "usage", usage)
                        verdict, method = (
                            {"relation": "unverified", "source_quote": ""},
                            "model_output_invalid",
                        )
                    except Exception:
                        await pipeline.settle_model_request(lease, receipt, None)
                        raise
                proof = {
                    "policy": EVIDENCE_POLICY,
                    "input_hash": input_hash,
                    "association": plan["association"],
                    "answer_span": plan["answer_span"],
                    "source_span": source_span(segment, verdict["source_quote"]),
                    "result": verdict,
                    "checked_dimensions": [
                        item["dimension"] for item in verdict.get("checks", [])
                    ],
                }
                saved = ClaimEvidence(
                    project_id=lease.project_id,
                    run_id=lease.run_id,
                    claim_hash=claim_hash,
                    answer_segment_id=answer_segment.id,
                    citation_id=source["id"],
                    source_segment_id=segment.id,
                    answer_quote=claim["quote"],
                    source_quote=verdict["source_quote"],
                    relation=verdict["relation"],
                    method=method,
                    verification=proof,
                )

                async def save(db, saved=saved, receipt=receipt, usage=usage):
                    if receipt:
                        await pipeline.record_model_usage(db, lease, receipt, usage)
                    db.add(saved)

                await pipeline.queue.checkpoint(lease, save)
                trace.update(
                    status="invalid" if method == "model_output_invalid" else "checked",
                    evidence_id=saved.id,
                )
                by_pair[(claim_hash, source["id"], segment.id)] = saved
                if method != "model_output_invalid":
                    reusable[input_hash] = verdict
                invalid += method == "model_output_invalid"
                processed += 1
    for plan in plans:
        plan["candidate_excluded_ranges"] = plan["unchecked_ranges"]
        plan["unchecked_ranges"] = merge_ranges(
            [
                *plan["candidate_excluded_ranges"],
                *[
                    [row["start"], row["end"]]
                    for row in plan["selected"]
                    if row["status"] != "checked"
                ],
            ]
        )
    return {
        "policy": EVIDENCE_POLICY,
        "candidate_pairs": selected_count,
        "available_pairs": total,
        "processed_pairs": processed,
        "deferred_pairs": deferred,
        "invalid_pairs": invalid,
        "checked_pairs": processed - invalid,
        "unchecked_pairs": total - processed + invalid,
        "candidate_limit_per_claim": PAIRS_PER_CLAIM,
        "observations": plans,
        "scope": "same_answer_actual_references",
        "review_state": "needs_review",
        "unverified_meaning": "no_conclusion_on_unchecked_text",
    }
