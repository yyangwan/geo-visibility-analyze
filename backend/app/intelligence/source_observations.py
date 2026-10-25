"""Article grouping preserves every platform/answer observation and frozen version."""

import re
from collections import Counter, defaultdict

from sqlalchemy import select

from app.intelligence.blob import digest
from app.intelligence.citation_context import snapshot_matches
from app.intelligence.models import ContentSegment, SegmentAnalysis, SourceSnapshot
from app.intelligence.semantics import stable_hash

VERSION = "source-observations-v2"
SIMILARITY_PAIR_LIMIT = 20000


def content_families(texts):
    """Conservative exact/near-copy clustering; never a claim of publisher independence."""
    parents = {key: key for key in texts}
    normalized = {
        key: re.sub(r"\s+", "", text).casefold() for key, text in texts.items()
    }
    shingles = {
        key: Counter(text[index : index + 5] for index in range(len(text) - 4))
        for key, text in normalized.items()
        if len(text) >= 200
    }

    def root(key):
        while parents[key] != key:
            key = parents[key]
        return key

    hashes, checked, limited = {}, 0, False
    for key in sorted(texts):
        body_hash = digest(texts[key].encode())
        if body_hash in hashes:
            parents[root(key)] = root(hashes[body_hash])
        hashes[body_hash] = key
    keys = sorted(shingles)
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            if root(left) == root(right):
                continue
            if checked >= SIMILARITY_PAIR_LIMIT:
                limited = True
                break
            checked += 1
            if min(len(normalized[left]), len(normalized[right])) < 0.8 * max(
                len(normalized[left]), len(normalized[right])
            ):
                continue
            a, b = shingles[left], shingles[right]
            if sum((a & b).values()) / max(1, sum((a | b).values())) >= 0.9:
                parents[root(right)] = root(left)
        if limited:
            break
    groups = defaultdict(list)
    for key in texts:
        groups[root(key)].append(key)
    families = {}
    for members in groups.values():
        family = stable_hash(
            [VERSION, sorted({digest(texts[key].encode()) for key in members})]
        )
        exact = len({digest(texts[key].encode()) for key in members}) == 1
        for key in members:
            families[key] = {
                "id": family,
                "method": "exact_body" if exact else "near_copy_candidate",
                "snapshot_ids": sorted(members),
            }
    return families, {
        "compared_pairs": checked,
        "pair_limit": SIMILARITY_PAIR_LIMIT,
        "status": "partial" if limited else "processed",
        "provenance_independence": "unknown",
    }


async def project_source_observations(db, run):
    sources = run.manifest.get("sources", [])
    ids = {row["snapshot_id"] for row in sources if row.get("snapshot_id")}
    snapshots = (
        (
            await db.scalars(
                select(SourceSnapshot).where(
                    SourceSnapshot.project_id == run.project_id,
                    SourceSnapshot.id.in_(ids),
                )
            )
        ).all()
        if ids
        else []
    )
    rows = (
        (
            await db.execute(
                select(ContentSegment, SegmentAnalysis.status)
                .join(
                    SegmentAnalysis,
                    (SegmentAnalysis.segment_id == ContentSegment.id)
                    & (SegmentAnalysis.project_id == ContentSegment.project_id),
                )
                .where(
                    SegmentAnalysis.project_id == run.project_id,
                    SegmentAnalysis.run_id == run.id,
                    ContentSegment.owner_kind == "snapshot",
                    ContentSegment.owner_id.in_(ids),
                )
            )
        ).all()
        if ids
        else []
    )
    segments, texts, coverage = defaultdict(list), {}, {}
    for segment, status in rows:
        segments[segment.owner_id].append((segment, status))
    for snapshot in snapshots:
        ordered = sorted(segments[snapshot.id], key=lambda row: row[0].start_offset)
        position, parts, processed, valid = 0, [], 0, bool(ordered)
        for segment, status in ordered:
            valid = (
                valid
                and segment.start_offset == position
                and segment.end_offset - segment.start_offset == len(segment.text)
            )
            position = segment.end_offset
            parts.append(segment.text)
            processed += len(segment.text) if status in ("completed", "partial") else 0
        text = "".join(parts)
        valid = (
            valid
            and snapshot.integrity_status == "verified"
            and digest(text.encode()) == snapshot.body_hash
        )
        coverage[snapshot.id] = {
            "saved_text_characters": len(text) if valid else None,
            "processed_characters": processed if valid else None,
            "stored_text_integrity": "verified" if valid else "unknown",
            "article_completeness": snapshot.completeness,
        }
        if valid:
            texts[snapshot.id] = text
    families, family_coverage = content_families(texts)
    responses = {row["id"]: row for row in run.manifest.get("responses", [])}
    by_snapshot = {row.id: row for row in snapshots}
    observations, articles = [], {}
    for source in sources:
        response = responses.get(source["response_id"], {})
        family = families.get(source.get("snapshot_id"))
        version_valid = snapshot_matches(
            source, by_snapshot.get(source.get("snapshot_id"))
        )
        if not version_valid:
            family = None
        observation = {
            **{
                key: source.get(key)
                for key in (
                    "id",
                    "ordinal",
                    "response_id",
                    "document_id",
                    "platform",
                    "kind",
                    "url",
                    "title",
                    "url_resolution",
                    "snapshot_id",
                    "snapshot_version",
                    "answer_anchor",
                    "fetch_status",
                )
            },
            "prompt_hash": response.get("prompt_hash"),
            "observed_at": response.get("observed_at"),
            "actual_reference": source.get("kind") == "answer_reference",
            "content_family": family,
            "frozen_version_valid": version_valid,
            "content_coverage": coverage.get(source.get("snapshot_id")),
            "publisher_independence": "unknown",
            "platform_preference_inference": "not_established",
        }
        observations.append(observation)
        key = source.get("document_id") or "observation:" + source["id"]
        article = articles.setdefault(
            key,
            {
                "id": key,
                "document_id": source.get("document_id"),
                "observation_ids": [],
                "platforms": [],
                "snapshot_ids": [],
                "content_family_ids": [],
            },
        )
        article["observation_ids"].append(source["id"])
        for field, value in (
            ("platforms", source["platform"]),
            ("snapshot_ids", source.get("snapshot_id")),
            ("content_family_ids", family["id"] if family else None),
        ):
            if value and value not in article[field]:
                article[field].append(value)
    actual = {
        row["content_family"]["id"]
        for row in observations
        if row["actual_reference"] and row["content_family"]
    }
    return {
        "version": VERSION,
        "observations": observations,
        "articles": list(articles.values()),
        "content_family_coverage": family_coverage,
        "actual_reference_content_families": len(actual),
        "independent_publishers": None,
        "independent_evidence_note": "Duplicate bodies/near-copy candidates are one content family; editorial independence and original authorship remain unknown.",
    }


def annotate_evidence(evidence, projection):
    sources = {row["id"]: row for row in projection["observations"]}
    groups = defaultdict(list)
    for row in evidence:
        source = sources.get(row["citation_id"], {})
        family = source.get("content_family")
        row["content_family_id"] = family["id"] if family else None
        row["publisher_independence"] = "unknown"
        row["deduplicated_content_weight"] = 0
        if (
            family
            and row["relation"] != "unverified"
            and row.get("method") != "model_output_invalid"
        ):
            groups[(row["claim_hash"], family["id"])].append(row)
    for rows in groups.values():
        relations = sorted({row["relation"] for row in rows})
        for row in rows:
            row["content_family_relations"] = relations
            row["conflicting_interpretations"] = len(relations) > 1
        if len(relations) == 1:
            min(rows, key=lambda row: row["id"])["deduplicated_content_weight"] = 1
