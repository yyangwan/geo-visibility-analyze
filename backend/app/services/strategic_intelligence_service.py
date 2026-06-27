"""Strategic Intelligence Service — cross-audit trend analysis.

Aggregates data across multiple audits to reveal strategic patterns:
source authority trends, competitor positioning, answer structure evolution,
and multi-audit comparison.
"""

from collections import Counter, defaultdict

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Audit,
    PlatformResponseRecord,
    QueryResult,
    Report,
    ResponseAnalysis,
    SourceCitation,
)
from app.services.audit_service import BrandData
from app.services.source_quality import clean_cited_sources, is_valid_source_domain, score_source_authority


# ---------------------------------------------------------------------------
# P3.1: Source Authority Trends
# ---------------------------------------------------------------------------

async def get_source_authority_trends(
    db: AsyncSession, project_id: str, limit: int = 10, audit_id: int | None = None
) -> dict:
    """Track which sources/domains AI platforms cite over time."""
    audit_query = (
        select(Audit)
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
    )
    if audit_id is not None:
        audit_query = audit_query.where(Audit.id == audit_id)
    else:
        audit_query = audit_query.order_by(Audit.created_at.desc()).limit(limit)

    audit_result = await db.execute(audit_query)
    audits = audit_result.scalars().all()
    if audit_id is None:
        audits = list(reversed(audits))  # oldest first

    if not audits:
        return {"audits": [], "domain_trends": [], "platform_preferences": [], "authority_trend": {}}

    audit_ids = [a.id for a in audits]
    audit_dates = {a.id: a.created_at.strftime("%Y-%m-%d") for a in audits}

    # Load SourceCitations for these audits
    sc_result = await db.execute(
        select(SourceCitation)
        .where(SourceCitation.project_id == project_id)
        .where(SourceCitation.audit_id.in_(audit_ids))
    )
    citations = sc_result.scalars().all()

    # Load ResponseAnalysis cited_sources for authority scores
    ra_result = await db.execute(
        select(ResponseAnalysis, PlatformResponseRecord.audit_id, PlatformResponseRecord.platform)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id.in_(audit_ids))
        .where(ResponseAnalysis.status == "completed")
    )
    ra_rows = ra_result.all()

    # Build per-audit domain citation counts
    domain_audit_counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    domain_audit_authority: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    platform_domain_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    source_citation_keys: set[tuple[int, str, str]] = set()

    # From SourceCitation
    for sc in citations:
        aid = sc.audit_id
        if aid is None or not is_valid_source_domain(sc.domain):
            continue
        source_citation_keys.add((aid, sc.platform, sc.domain))
        domain_audit_counts[sc.domain][aid] += sc.citation_count
        platform_domain_counts[sc.platform][sc.domain] += sc.citation_count
        authority_score = score_source_authority(sc.domain, sc.urls or [])
        if authority_score:
            domain_audit_authority[sc.domain][aid].extend(
                [authority_score] * (sc.citation_count or 1)
            )

    # From ResponseAnalysis.cited_sources (authority scores + counts)
    for ra, aid, platform in ra_rows:
        for source in clean_cited_sources(ra.cited_sources):
            domain = source["domain"]
            authority = source.get("authority_score")
            if authority is None:
                continue
            domain_audit_authority[domain][aid].append(authority)
            # Count LLM analysis sources only when audit-time structured
            # citations did not already capture this platform/domain.
            if (aid, platform, domain) not in source_citation_keys:
                domain_audit_counts[domain][aid] += 1
                platform_domain_counts[platform][domain] += 1

    # Build domain trends (top domains by total citations)
    domain_totals = Counter()
    for domain, audit_map in domain_audit_counts.items():
        domain_totals[domain] = sum(audit_map.values())

    top_domains = [d for d, _ in domain_totals.most_common(15)]

    domain_trends = []
    for domain in top_domains:
        data = []
        for aid in audit_ids:
            count = domain_audit_counts[domain].get(aid, 0)
            auths = domain_audit_authority[domain].get(aid, [])
            authority_avg = round(sum(auths) / len(auths), 1) if auths else 0
            data.append({"audit_id": aid, "count": count, "authority_avg": authority_avg})
        domain_trends.append({"domain": domain, "data": data})

    # Platform preferences (top 5 domains per platform)
    platform_preferences = []
    for platform, domains in platform_domain_counts.items():
        top = sorted(domains.items(), key=lambda x: x[1], reverse=True)[:5]
        platform_preferences.append({
            "platform": platform,
            "top_domains": [{"domain": d, "count": c} for d, c in top],
        })

    # Authority trend direction per domain
    authority_trend: dict[str, list[str]] = {"improving": [], "stable": [], "declining": []}
    for domain in top_domains:
        auths_over_time = []
        for aid in audit_ids:
            auths = domain_audit_authority[domain].get(aid, [])
            if auths:
                auths_over_time.append(sum(auths) / len(auths))
        if len(auths_over_time) >= 2:
            mid = len(auths_over_time) // 2
            first_half = sum(auths_over_time[:mid]) / mid if mid > 0 else 0
            second_half = sum(auths_over_time[mid:]) / (len(auths_over_time) - mid) if (len(auths_over_time) - mid) > 0 else 0
            diff = second_half - first_half
            if diff > 0.3:
                authority_trend["improving"].append(domain)
            elif diff < -0.3:
                authority_trend["declining"].append(domain)
            else:
                authority_trend["stable"].append(domain)

    return {
        "audits": [{"audit_id": a.id, "date": audit_dates[a.id], "total_sources": 0} for a in audits],
        "domain_trends": domain_trends,
        "platform_preferences": platform_preferences,
        "authority_trend": authority_trend,
    }


# ---------------------------------------------------------------------------
# P3.2: Competitor Positioning Map
# ---------------------------------------------------------------------------

async def get_competitor_positioning_map(db: AsyncSession, project_id: str) -> dict:
    """Brand positioning across mention frequency, sentiment, and authority."""
    # Load brands from the latest audit snapshot for this project
    latest_audit_result = await db.execute(
        select(Audit)
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
        .order_by(Audit.created_at.desc())
        .limit(1)
    )
    latest_audit = latest_audit_result.scalar_one_or_none()
    brands = [
        BrandData(
            id=b.get("id", ""),
            name=b.get("name", ""),
            aliases=b.get("aliases", []),
            is_competitor=b.get("is_competitor", False),
        )
        for b in ((latest_audit.brands_json or []) if latest_audit else [])
    ]

    if not brands:
        return {"brands": [], "quadrant_labels": {}}

    # Load audits
    audit_result = await db.execute(
        select(Audit)
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
        .order_by(Audit.created_at.asc())
    )
    audits = audit_result.scalars().all()
    audit_ids = [a.id for a in audits]
    audit_dates = {a.id: a.created_at.strftime("%Y-%m-%d") for a in audits}

    brand_map = {b.id: b for b in brands}

    # QueryResult counts per brand
    qr_result = await db.execute(
        select(QueryResult)
        .join(Audit, QueryResult.audit_id == Audit.id)
        .where(Audit.project_id == project_id)
    )
    query_results = qr_result.scalars().all()

    # Per-brand total responses and mentions
    brand_total_responses: dict[int, int] = defaultdict(int)
    brand_mentions: dict[int, int] = defaultdict(int)
    # Per-brand per-audit
    brand_audit_responses: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    brand_audit_mentions: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for qr in query_results:
        brand_total_responses[qr.brand_id] += 1
        brand_audit_responses[qr.brand_id][qr.audit_id] += 1
        if qr.mention_found:
            brand_mentions[qr.brand_id] += 1
            brand_audit_mentions[qr.brand_id][qr.audit_id] += 1

    # Load analyses for sentiment
    ra_result = await db.execute(
        select(ResponseAnalysis, PlatformResponseRecord.platform, PlatformResponseRecord.audit_id)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id.in_(audit_ids))
        .where(ResponseAnalysis.status == "completed")
    )
    ra_rows = ra_result.all()

    # Sentiment: primary brand from brand_sentiment, competitors from competitor_refs
    brand_positive_count: dict[int, int] = defaultdict(int)
    brand_sentiment_total: dict[int, int] = defaultdict(int)
    # Authority per brand
    brand_authority_scores: dict[int, list[float]] = defaultdict(list)
    # Per-audit sentiment for trajectory
    brand_audit_positive: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    brand_audit_sentiment_total: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for ra, platform, aid in ra_rows:
        # Primary brand sentiment
        if ra.brand_sentiment:
            # Find primary brand (first non-competitor)
            primary_brands = [b for b in brands if not b.is_competitor]
            for pb in primary_brands:
                brand_sentiment_total[pb.id] += 1
                brand_audit_sentiment_total[pb.id][aid] += 1
                if ra.brand_sentiment == "positive":
                    brand_positive_count[pb.id] += 1
                    brand_audit_positive[pb.id][aid] += 1

        # Competitor refs — flat list of strings
        for comp_name in (ra.competitor_refs or []):
            comp_name = str(comp_name).strip()
            for b in brands:
                if b.is_competitor and (b.name == comp_name or comp_name in (b.aliases or [])):
                    brand_sentiment_total[b.id] += 1
                    brand_audit_sentiment_total[b.id][aid] += 1
                    # For competitors, we can't extract per-competitor sentiment easily
                    # Default to neutral counting — they were referenced which is partial visibility
                    break

        # Authority from cited_sources
        for source in clean_cited_sources(ra.cited_sources):
            auth = source.get("authority_score", 3)
            # Associate with primary brands (source authority affects all brands in response)
            for pb in [b for b in brands if not b.is_competitor]:
                brand_authority_scores[pb.id].append(auth)

    # Build brand positioning data
    brand_data = []
    for b in brands:
        total = brand_total_responses.get(b.id, 0)
        mentions = brand_mentions.get(b.id, 0)
        mention_frequency = mentions / total if total > 0 else 0

        sent_total = brand_sentiment_total.get(b.id, 0)
        sent_positive = brand_positive_count.get(b.id, 0)
        sentiment_positive_rate = sent_positive / sent_total if sent_total > 0 else 0

        auths = brand_authority_scores.get(b.id, [])
        avg_authority = round(sum(auths) / len(auths), 1) if auths else 0

        # Trajectory
        trajectory = []
        for a in audits:
            audit_total = brand_audit_responses[b.id].get(a.id, 0)
            audit_mentions = brand_audit_mentions[b.id].get(a.id, 0)
            audit_mention_rate = audit_mentions / audit_total if audit_total > 0 else 0
            audit_sent_total = brand_audit_sentiment_total[b.id].get(a.id, 0)
            audit_sent_pos = brand_audit_positive[b.id].get(a.id, 0)
            audit_sent_rate = audit_sent_pos / audit_sent_total if audit_sent_total > 0 else 0
            trajectory.append({
                "audit_id": a.id,
                "date": audit_dates[a.id],
                "mention_rate": round(audit_mention_rate, 3),
                "sentiment_positive_rate": round(audit_sent_rate, 3),
            })

        brand_data.append({
            "name": b.name,
            "is_competitor": b.is_competitor,
            "mention_frequency": round(mention_frequency, 3),
            "sentiment_positive_rate": round(sentiment_positive_rate, 3),
            "avg_authority": avg_authority,
            "mention_count": mentions,
            "trajectory": trajectory,
        })

    quadrant_labels = {
        "q1": "领先区（高提及·高正面）",
        "q2": "声誉区（低提及·高正面）",
        "q3": "弱势区（低提及·低正面）",
        "q4": "曝光区（高提及·低正面）",
    }

    return {"brands": brand_data, "quadrant_labels": quadrant_labels}


# ---------------------------------------------------------------------------
# P3.3: Answer Structure Evolution
# ---------------------------------------------------------------------------

async def get_answer_structure_evolution(
    db: AsyncSession, project_id: str, limit: int = 10
) -> dict:
    """Track how AI platforms structure their answers over time."""
    # Load audits
    audit_result = await db.execute(
        select(Audit)
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
        .order_by(Audit.created_at.desc())
        .limit(limit)
    )
    audits = list(reversed(audit_result.scalars().all()))

    if not audits:
        return {
            "audits": [],
            "structure_distribution": {},
            "platform_structure": {},
            "correlation": {},
            "transitions": [],
        }

    audit_ids = [a.id for a in audits]
    audit_dates = {a.id: a.created_at.strftime("%Y-%m-%d") for a in audits}

    # Load analyses with platform and audit info
    ra_result = await db.execute(
        select(ResponseAnalysis, PlatformResponseRecord.platform, PlatformResponseRecord.audit_id)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id.in_(audit_ids))
        .where(ResponseAnalysis.status == "completed")
    )
    ra_rows = ra_result.all()

    # Per-audit structure distribution
    structure_audit: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    audit_total: dict[int, int] = defaultdict(int)

    # Per-platform structure
    platform_structure: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # Per structure type → QueryResult mention rates
    structure_brand_mentions: dict[str, list[bool]] = defaultdict(list)

    # Per (platform, audit_id) → structure (for transitions)
    platform_audit_structure: dict[str, dict[int, str]] = defaultdict(dict)

    for ra, platform, aid in ra_rows:
        structure = ra.answer_structure or "unknown"
        structure_audit[structure][aid] += 1
        audit_total[aid] += 1
        platform_structure[platform][structure] += 1
        platform_audit_structure[platform][aid] = structure

        # Get mention data for this PRR's QueryResults
        qr_result = await db.execute(
            select(QueryResult.mention_found)
            .join(Audit, QueryResult.audit_id == Audit.id)
            .where(Audit.project_id == project_id)
            .where(QueryResult.audit_id == aid)
        )
        # We just use the boolean directly for correlation
        structure_brand_mentions[structure].append(True)  # simplified

    # Build structure_distribution
    structure_distribution = {}
    for stype in ["list", "comparison", "narrative", "qa", "unknown"]:
        if stype not in structure_audit:
            continue
        data = []
        for aid in audit_ids:
            count = structure_audit[stype].get(aid, 0)
            total = audit_total.get(aid, 0)
            pct = round(count / total, 3) if total > 0 else 0
            data.append({"audit_id": aid, "count": count, "pct": pct})
        structure_distribution[stype] = data

    # Correlation: structure type vs mention rate
    # Load QueryResult mention rates per audit
    correlation = {}
    for stype in structure_audit:
        # Count mentions for queries in audits where this structure appeared
        audits_with_structure = [aid for aid in audit_ids if structure_audit[stype].get(aid, 0) > 0]
        if audits_with_structure:
            qr_result = await db.execute(
                select(QueryResult)
                .where(QueryResult.audit_id.in_(audits_with_structure))
            )
            qrs = qr_result.scalars().all()
            mentions = sum(1 for q in qrs if q.mention_found)
            total = len(qrs)
            mention_rate = round(mentions / total, 3) if total > 0 else 0
            avg_position = None
            positions = [q.mention_position for q in qrs if q.mention_found and q.mention_position]
            if positions:
                avg_position = round(sum(positions) / len(positions), 1)
            correlation[stype] = {"mention_rate": mention_rate, "avg_position": avg_position}

    # Transitions: structure changes between consecutive audits per platform
    transitions = []
    for platform, audit_struct in platform_audit_structure.items():
        prev_structure = None
        for aid in audit_ids:
            curr = audit_struct.get(aid)
            if curr and prev_structure and curr != prev_structure:
                transitions.append({
                    "audit_id": aid,
                    "platform": platform,
                    "prev_structure": prev_structure,
                    "new_structure": curr,
                })
            if curr:
                prev_structure = curr

    return {
        "audits": [{"audit_id": a.id, "date": audit_dates[a.id]} for a in audits],
        "structure_distribution": structure_distribution,
        "platform_structure": dict(platform_structure),
        "correlation": correlation,
        "transitions": transitions,
    }


# ---------------------------------------------------------------------------
# P3.4: Multi-Audit Comparison
# ---------------------------------------------------------------------------

async def get_multi_audit_comparison(
    db: AsyncSession, project_id: str, audit_ids: list[int]
) -> dict:
    """Compare multiple audits side-by-side with diffs."""
    if len(audit_ids) < 2 or len(audit_ids) > 5:
        return {"audits": [], "diffs": {}}

    # Validate audits belong to project
    audit_result = await db.execute(
        select(Audit)
        .where(Audit.id.in_(audit_ids))
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
        .order_by(Audit.created_at.asc())
    )
    audits = audit_result.scalars().all()

    if len(audits) < 2:
        return {"audits": [], "diffs": {}}

    valid_ids = [a.id for a in audits]
    audit_dates = {a.id: a.created_at.strftime("%Y-%m-%d") for a in audits}

    # Build brand lookup from the first audit's brands_json snapshot
    brand_lookup: dict[str, str] = {}  # brand_id -> brand_name
    if audits:
        for b in (audits[0].brands_json or []):
            bid = b.get("id", "")
            if bid:
                brand_lookup[bid] = b.get("name", "")

    snapshots = []
    all_sources_per_audit: dict[int, set[str]] = {}
    all_brand_mentions_per_audit: dict[int, dict[str, int]] = {}

    for audit_id in valid_ids:
        # Report scores
        report_result = await db.execute(
            select(Report).where(Report.audit_id == audit_id)
        )
        report = report_result.scalar_one_or_none()

        overall_score = report.overall_score if report else 0
        mention_rate = report.mention_rate if report else 0

        # Analysis aggregation
        ra_result = await db.execute(
            select(ResponseAnalysis)
            .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
            .where(PlatformResponseRecord.audit_id == audit_id)
            .where(ResponseAnalysis.status == "completed")
        )
        analyses = ra_result.scalars().all()

        sentiment_counter: Counter = Counter()
        structure_counter: Counter = Counter()
        topic_counter: Counter = Counter()
        source_set: set[str] = set()

        for ra in analyses:
            if ra.brand_sentiment:
                sentiment_counter[ra.brand_sentiment] += 1
            if ra.answer_structure:
                structure_counter[ra.answer_structure] += 1
            for t in (ra.topics_covered or []):
                topic_counter[t] += 1
            for source in clean_cited_sources(ra.cited_sources):
                source_set.add(source["domain"])

        all_sources_per_audit[audit_id] = source_set

        # Per-brand mention rates
        qr_result = await db.execute(
            select(QueryResult)
            .where(QueryResult.audit_id == audit_id)
        )
        query_results = qr_result.scalars().all()

        brand_total: dict[str, int] = defaultdict(int)
        brand_mentions_map: dict[str, int] = defaultdict(int)
        for qr in query_results:
            # Get brand name via brand_lookup (from brands_json snapshot)
            brand_name = brand_lookup.get(qr.brand_id)
            if brand_name:
                brand_total[brand_name] += 1
                if qr.mention_found:
                    brand_mentions_map[brand_name] += 1

        competitor_rates = []
        for name, total in brand_total.items():
            rate = round(brand_mentions_map.get(name, 0) / total, 3) if total > 0 else 0
            competitor_rates.append({"brand": name, "mention_rate": rate})

        all_brand_mentions_per_audit[audit_id] = {name: brand_mentions_map.get(name, 0) for name in brand_total}

        # Top sources
        top_sources = sorted(
            [{"domain": d, "count": 1} for d in source_set],
            key=lambda x: x["domain"],
        )[:20]

        snapshots.append({
            "audit_id": audit_id,
            "date": audit_dates[audit_id],
            "overall_score": overall_score,
            "mention_rate": mention_rate,
            "sentiment_breakdown": dict(sentiment_counter),
            "top_sources": top_sources,
            "competitor_mention_rates": competitor_rates,
            "structure_distribution": dict(structure_counter),
            "topic_distribution": dict(topic_counter.most_common(20)),
        })

    # Diffs: first vs last audit
    first_id = valid_ids[0]
    last_id = valid_ids[-1]
    first_snapshot = snapshots[0]
    last_snapshot = snapshots[-1]

    first_sources = all_sources_per_audit.get(first_id, set())
    last_sources = all_sources_per_audit.get(last_id, set())

    source_changes = {
        "added": sorted(last_sources - first_sources),
        "removed": sorted(first_sources - last_sources),
    }

    # Competitor changes
    competitor_changes = []
    first_brands = {c["brand"]: c["mention_rate"] for c in first_snapshot["competitor_mention_rates"]}
    last_brands = {c["brand"]: c["mention_rate"] for c in last_snapshot["competitor_mention_rates"]}
    all_brand_names = set(first_brands) | set(last_brands)
    for name in all_brand_names:
        delta = round(last_brands.get(name, 0) - first_brands.get(name, 0), 3)
        competitor_changes.append({"brand": name, "delta": delta})

    diffs = {
        "mention_rate_delta": round(last_snapshot["mention_rate"] - first_snapshot["mention_rate"], 3),
        "score_delta": round(last_snapshot["overall_score"] - first_snapshot["overall_score"], 1),
        "source_changes": source_changes,
        "competitor_changes": competitor_changes,
    }

    return {"audits": snapshots, "diffs": diffs}
