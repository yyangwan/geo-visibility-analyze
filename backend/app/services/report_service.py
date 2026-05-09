"""Report generation service.

Aggregates audit results into a comprehensive report with:
- Overall visibility score (0-100)
- Mention rate across platforms (per-brand, then averaged)
- Per-platform scores (using platform-specific confidence)
- Competitor ranking
- Actionable insights
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Brand, QueryResult, Report


async def generate_report(db: AsyncSession, audit) -> Report:
    """Generate a report from a completed audit."""
    project_id = audit.project_id
    audit_id = audit.id

    # Load all results for this audit
    result = await db.execute(
        select(QueryResult).where(QueryResult.audit_id == audit_id)
    )
    results = result.scalars().all()

    # Load brands for this project
    brand_result = await db.execute(
        select(Brand).where(Brand.project_id == project_id)
    )
    brands = brand_result.scalars().all()
    brand_map = {b.id: b for b in brands}

    # Basic counts
    total_queries = len(results)
    if total_queries == 0:
        report = Report(
            project_id=project_id,
            audit_id=audit_id,
            overall_score=0,
            mention_rate=0,
            platform_scores={},
            insights=["无查询结果，无法生成分析"],
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report

    # Per-brand mention rates (avoids dilution from competitor count)
    brand_mention_rates: dict[int, float] = {}
    for brand in brands:
        brand_results = [r for r in results if r.brand_id == brand.id]
        if brand_results:
            rate = sum(1 for r in brand_results if r.mention_found) / len(brand_results)
            brand_mention_rates[brand.id] = rate
    mention_rate = (
        sum(brand_mention_rates.values()) / len(brand_mention_rates)
        if brand_mention_rates
        else 0
    )

    # Per-brand recommendation rates
    brand_recommend_rates: dict[int, float] = {}
    for brand in brands:
        brand_results = [r for r in results if r.brand_id == brand.id]
        if brand_results:
            rate = sum(1 for r in brand_results if r.is_recommended) / len(brand_results)
            brand_recommend_rates[brand.id] = rate
    recommend_rate = (
        sum(brand_recommend_rates.values()) / len(brand_recommend_rates)
        if brand_recommend_rates
        else 0
    )

    # Per-platform scores (using platform-specific confidence)
    platforms: set[str] = {r.platform for r in results}
    platform_scores = {}
    for platform in platforms:
        p_results = [r for r in results if r.platform == platform]
        p_total = len(p_results)
        p_mentions = sum(1 for r in p_results if r.mention_found)
        p_recommended = sum(1 for r in p_results if r.is_recommended)

        # Per-platform confidence (only for this platform's results)
        p_confidence_results = [r for r in p_results if r.mention_confidence is not None]
        p_avg_confidence = (
            sum(r.mention_confidence for r in p_confidence_results) / len(p_confidence_results)
            if p_confidence_results
            else 0
        )

        p_mention_rate = p_mentions / p_total if p_total > 0 else 0
        p_recommend_rate = p_recommended / p_total if p_total > 0 else 0

        # Platform score: weighted combination
        # 50% mention rate + 30% recommendation rate + 20% confidence
        p_score = (
            p_mention_rate * 50 + p_recommend_rate * 30 + p_avg_confidence * 20
        )
        platform_scores[platform] = round(p_score, 1)

    # Overall score: average of platform scores, scaled to 0-100
    overall_score = 0.0
    if platform_scores:
        overall_score = sum(platform_scores.values()) / len(platform_scores)
    overall_score = round(overall_score, 1)

    # Competitor rank: how does the primary brand compare to competitors
    competitor_rank = _compute_competitor_rank(results, brand_map)

    # Generate insights
    insights = _generate_insights(
        overall_score, mention_rate, recommend_rate, platform_scores, brands, brand_map, results
    )

    report = Report(
        project_id=project_id,
        audit_id=audit_id,
        overall_score=overall_score,
        mention_rate=round(mention_rate, 3),
        competitor_rank=competitor_rank,
        sentiment_positive_rate=round(
            sum(r.mention_confidence for r in results if r.mention_confidence is not None)
            / max(len([r for r in results if r.mention_confidence is not None]), 1),
            3,
        ) if any(r.mention_confidence is not None for r in results) else 0,
        platform_scores=platform_scores,
        insights=insights,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


def _compute_competitor_rank(
    results: list[QueryResult], brand_map: dict[int, Brand]
) -> int | None:
    """Compute the primary brand's rank vs competitors."""
    brand_mentions: dict[int, int] = {}
    for r in results:
        if r.mention_found:
            brand_mentions[r.brand_id] = brand_mentions.get(r.brand_id, 0) + 1

    if not brand_mentions:
        return None

    sorted_brands = sorted(brand_mentions.items(), key=lambda x: x[1], reverse=True)

    for rank, (brand_id, _) in enumerate(sorted_brands, 1):
        brand = brand_map.get(brand_id)
        if brand and not brand.is_competitor:
            return rank

    return None


def _generate_insights(
    overall_score: float,
    mention_rate: float,
    recommend_rate: float,
    platform_scores: dict[str, float],
    brands: list[Brand],
    brand_map: dict[int, Brand],
    results: list[QueryResult],
) -> list[str]:
    """Generate actionable insights from the audit data."""
    insights = []

    if overall_score >= 70:
        insights.append(f"品牌整体可见性优秀，综合得分 {overall_score} 分")
    elif overall_score >= 40:
        insights.append(f"品牌可见性中等，综合得分 {overall_score} 分，有提升空间")
    else:
        insights.append(f"品牌可见性较低，综合得分仅 {overall_score} 分，需要重点关注")

    insights.append(f"品牌提及率 {mention_rate:.1%}，AI 推荐率 {recommend_rate:.1%}")

    if platform_scores:
        best_platform = max(platform_scores, key=platform_scores.get)  # type: ignore[arg-type]
        worst_platform = min(platform_scores, key=platform_scores.get)  # type: ignore[arg-type]
        if best_platform != worst_platform:
            insights.append(
                f"表现最佳平台: {best_platform} ({platform_scores[best_platform]}分)，"
                f"最需改进: {worst_platform} ({platform_scores[worst_platform]}分)"
            )

    primary_mentions = 0
    competitor_mentions = 0
    for r in results:
        brand = brand_map.get(r.brand_id)
        if brand and not brand.is_competitor and r.mention_found:
            primary_mentions += 1
        elif brand and brand.is_competitor and r.mention_found:
            competitor_mentions += 1

    if competitor_mentions > 0:
        if primary_mentions > competitor_mentions:
            insights.append("品牌提及次数超过竞品，表现领先")
        else:
            insights.append("部分竞品提及次数超过本品牌，需要优化内容策略")

    return insights
