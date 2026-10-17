"""Observed entity presence in captured source cards and verified stored text only."""

from collections import Counter, defaultdict

from sqlalchemy import select

from app.intelligence.aliases import resolved_entity
from app.intelligence.blob import digest
from app.intelligence.mention_bindings import attributed_entity
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    SegmentAnalysis,
    SourceSnapshot,
)

METRICS = {
    "cited_card_presence": ("answer_reference", "citation"),
    "cited_archive_presence": ("answer_reference", "snapshot"),
    "candidate_card_presence": ("search_candidate", "citation"),
}


def complete_text(rows):
    position, parts = 0, []
    if not rows:
        return None
    for status, segment in sorted(rows, key=lambda row: row[1].start_offset):
        if (
            status != "completed"
            or segment.start_offset != position
            or segment.end_offset - segment.start_offset != len(segment.text)
        ):
            return None
        position = segment.end_offset
        parts.append(segment.text)
    return "".join(parts)


async def source_snapshot(db, run):
    rows = (
        await db.execute(
            select(SegmentAnalysis.status, ContentSegment)
            .join(
                ContentSegment,
                (SegmentAnalysis.segment_id == ContentSegment.id)
                & (SegmentAnalysis.project_id == ContentSegment.project_id),
            )
            .where(
                SegmentAnalysis.project_id == run.project_id,
                SegmentAnalysis.run_id == run.id,
                ContentSegment.owner_kind.in_(["citation", "snapshot"]),
            )
        )
    ).all()
    owners, segments = defaultdict(list), {}
    for status, segment in rows:
        owners[(segment.owner_kind, segment.owner_id)].append((status, segment))
        segments[segment.id] = segment
    snapshots = (
        (
            await db.scalars(
                select(SourceSnapshot)
                .join(
                    ContentSegment,
                    (ContentSegment.owner_id == SourceSnapshot.id)
                    & (ContentSegment.project_id == SourceSnapshot.project_id),
                )
                .join(
                    SegmentAnalysis,
                    (SegmentAnalysis.segment_id == ContentSegment.id)
                    & (SegmentAnalysis.project_id == ContentSegment.project_id),
                )
                .where(
                    SourceSnapshot.project_id == run.project_id,
                    ContentSegment.owner_kind == "snapshot",
                    SegmentAnalysis.run_id == run.id,
                )
            )
        )
        .unique()
        .all()
    )
    by_snapshot = {row.id: row for row in snapshots}
    texts = {key: complete_text(value) for key, value in owners.items()}
    archived = {
        row.id: bool(texts.get(("snapshot", row.id)))
        and row.integrity_status == "verified"
        and digest(texts[("snapshot", row.id)].encode("utf-8")) == row.text_sha256
        for row in snapshots
    }
    mentions = (
        await db.scalars(
            select(EntityMention).where(
                EntityMention.project_id == run.project_id,
                EntityMention.run_id == run.id,
            )
        )
    ).all()
    hits, ambiguous = defaultdict(dict), set()
    split_sources = {
        item["source_entity_id"]
        for item in run.manifest.get("entity_splits", {}).values()
    }
    from app.intelligence.mention_bindings import binding_key

    for mention in mentions:
        segment = segments.get(mention.segment_id)
        if segment is None:
            continue
        start, end = (
            mention.start_offset - segment.start_offset,
            mention.end_offset - segment.start_offset,
        )
        if (
            not (0 <= start < end <= len(segment.text))
            or segment.text[start:end] != mention.quote
        ):
            continue
        eid = attributed_entity(
            mention,
            segment,
            run.manifest,
            resolved_entity(mention.entity_id, run.manifest),
        )
        binding = run.manifest.get("mention_bindings", {}).get(
            binding_key(mention, segment), {}
        )
        if mention.entity_id in split_sources and not (
            binding.get("decision") in ("assign", "split")
            and run.manifest.get("entity_resolutions", {}).get(eid, {}).get("decision")
            == "confirmed"
        ):
            ambiguous.add((segment.owner_kind, segment.owner_id))
        hits[(segment.owner_kind, segment.owner_id)].setdefault(
            eid,
            {
                "entity_id": eid,
                "mention_id": mention.id,
                "segment_id": segment.id,
                "start": mention.start_offset,
                "end": mention.end_offset,
            },
        )
    sources = run.manifest.get("sources", [])
    source_ids = Counter(row["id"] for row in sources)
    by_response = defaultdict(list)
    for source in sources:
        by_response[source["response_id"]].append(source)
    output = {}
    for metric, (source_kind, owner_kind) in METRICS.items():
        eligible, entity_hits, reasons, traces = (
            set(),
            defaultdict(set),
            {},
            defaultdict(dict),
        )
        for response in run.manifest.get("responses", []):
            rid, panel = response["id"], response.get("source_panel") or {}
            observed = by_response[rid]
            reason = None
            if (
                panel.get("status") != "complete"
                or panel.get("capturedCount") != len(observed)
                or panel.get("expectedCount") not in (None, len(observed))
            ):
                reason = "source_panel_incomplete"
            elif any(
                source_ids[item["id"]] != 1
                or item.get("platform") != response["platform"]
                for item in observed
            ):
                reason = "source_response_mismatch"
            elif any(
                item.get("kind") in ("unknown", "named_inference") for item in observed
            ):
                reason = "source_classification_unknown"
            local_hits = []
            for source in [
                item for item in observed if item.get("kind") == source_kind
            ]:
                owner_id = (
                    source["id"]
                    if owner_kind == "citation"
                    else source.get("snapshot_id")
                )
                owner = (owner_kind, owner_id)
                text = texts.get(owner)
                snapshot = (
                    by_snapshot.get(owner_id) if owner_kind == "snapshot" else None
                )
                expected = "\n".join(
                    part
                    for part in (source.get("title", ""), source.get("snippet", ""))
                    if part
                )
                valid = text is not None and bool(text)
                if owner_kind == "citation":
                    valid = valid and text == expected
                else:
                    valid = (
                        valid
                        and source.get("url_resolution") == "exact"
                        and source.get("fetch_status") == "captured"
                        and snapshot is not None
                        and snapshot.document_id == source.get("document_id")
                        and archived.get(owner_id, False)
                    )
                if not valid:
                    reason = reason or (
                        "cited_archive_incomplete"
                        if owner_kind == "snapshot"
                        else "source_card_incomplete"
                    )
                    continue
                if owner in ambiguous:
                    reason = reason or "ambiguous_source_identity"
                for hit in hits.get(owner, {}).values():
                    local_hits.append(hit["entity_id"])
                    traces[rid].setdefault(
                        hit["entity_id"],
                        {
                            **hit,
                            "citation_id": source["id"],
                            "snapshot_id": source.get("snapshot_id"),
                            "owner_kind": owner_kind,
                            "owner_id": owner_id,
                            "article_completeness": snapshot.completeness
                            if snapshot
                            else None,
                        },
                    )
            # Verified positives are kept as observed evidence, but missing sources never become zeros.
            for eid in local_hits:
                entity_hits[eid].add(rid)
            if reason:
                reasons[rid] = reason
            else:
                eligible.add(rid)
        output[metric] = {
            "completed": eligible,
            "hits": entity_hits,
            "unusable_reasons": reasons,
            "traces": {rid: list(value.values()) for rid, value in traces.items()},
        }
    return output
