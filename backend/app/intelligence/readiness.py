"""Independent result prerequisites, projected from one immutable run."""

from app.intelligence.brand_presence import build_brand_presence
from app.intelligence.source_quality import usable_snapshot


def result_readiness(manifest, coverage, summary, status):
    presence = summary.get("brand_presence") or build_brand_presence(
        manifest, [], [], [], {}, {}
    )
    targets = presence["targets"]
    responses = manifest.get("responses", [])
    sources = manifest.get("sources", [])
    expected = manifest.get("expected_samples", len(responses))
    captured = sum(
        not row["error"] and row.get("answer_status", "complete") == "complete"
        for row in responses
    )
    total_segments = coverage.get("total_segments", 0)
    analyzed = coverage.get("segment_states", {}).get("completed", 0)
    processed = coverage.get("processed_segments", analyzed)
    span_defects = coverage.get("rejected_spans", 0)
    semantic_ok = (
        total_segments > 0
        and analyzed == total_segments
        and status in ("ready", "partial")
    )
    provider_code = coverage.get("analysis_error_code")
    cohort = manifest.get("evaluation_cohort")
    eligible = sum(
        cell["eligibility"] == "eligible" for cell in (cohort or {}).get("cells", [])
    )
    no_prompts = bool(cohort and cohort["status"] == "no_competitive_prompts")
    identity_count = sum(target["identity_status"] == "confirmed" for target in targets)
    overall = [row for row in presence["scope_scores"] if row["platform"] == "all"]
    scores = sum(row["total"]["score"] is not None for row in overall)
    errors = {
        "analysis_provider_insufficient_balance": "分析服务额度不足，需要管理员恢复后生成新分析修订。",
        "analysis_provider_credentials_invalid": "分析服务凭证不可用，需要管理员检查配置。",
        "analysis_provider_access_denied": "分析服务访问被拒绝，需要管理员核对权限。",
        "analysis_provider_rate_limited": "分析服务限流，任务将按重试策略处理。",
        "analysis_provider_unavailable": "分析服务暂不可用，已保存的材料不会删除。",
    }
    items = []

    def item(
        key, title, state, completed, total, reason_codes, detail, view, action, label
    ):
        items.append(
            {
                "key": key,
                "title": title,
                "state": state,
                "completed": completed,
                "total": total,
                "reason_codes": reason_codes,
                "detail": detail,
                "remedy": {"view": view, "action": action, "label": label},
            }
        )

    item(
        "capture",
        "回答采集",
        "ready"
        if expected and captured == expected
        else "partial"
        if captured
        else "pending",
        captured,
        expected,
        [] if expected and captured == expected else ["answer_capture_incomplete"],
        "失败和不完整回答不按品牌缺席处理。",
        "capture",
        "inspect_capture",
        "查看采集状态",
    )
    item(
        "semantics",
        "语义分析",
        "ready"
        if semantic_ok
        else "blocked"
        if provider_code or status == "failed"
        else "partial"
        if processed
        else "pending",
        processed,
        total_segments,
        []
        if semantic_ok
        else [
            provider_code or "semantic_spans_unresolved"
            if span_defects and processed == total_segments
            else provider_code or "semantic_coverage_incomplete"
        ],
        errors.get(
            provider_code,
            "文字已全部处理，部分证据片段待核实；相关缺项保留未知，不阻断无关品牌的已验证观察。"
            if processed == total_segments and span_defects
            else "保存文字的分析覆盖与原文章完整性分别判断；未分析不代表没有品牌。",
        ),
        "evidence",
        "inspect_analysis",
        "查看分析状态",
    )
    item(
        "identity",
        "品牌身份",
        "ready"
        if targets and identity_count == len(targets)
        else "partial"
        if identity_count
        else "pending",
        identity_count,
        len(targets),
        []
        if targets and identity_count == len(targets)
        else ["identity_review_required"],
        "未登记主体可以核实后比较；歧义只影响相关品牌。",
        "entities",
        "review_identity",
        "核实品牌身份",
    )
    item(
        "prompt_scope",
        "问题资格",
        "not_applicable"
        if no_prompts
        else "ready"
        if cohort and cohort["status"] == "reviewed"
        else "pending",
        eligible,
        len((cohort or {}).get("cells", [])),
        []
        if no_prompts or cohort and cohort["status"] == "reviewed"
        else ["prompt_review_required"],
        "本次没有适用竞争问题。"
        if no_prompts
        else "审核中性选型范围后生成新修订，旧报告分母保留。",
        "overview",
        "review_prompt_scope",
        "审核问题范围",
    )
    saved = sum(bool(source.get("snapshot_id")) for source in sources)
    usable = sum(usable_snapshot(source) for source in sources)
    unusable = saved - usable
    item(
        "sources",
        "来源正文",
        "partial" if sources else "not_applicable",
        usable,
        len(sources),
        (["source_saved_text_unusable"] if unusable else [])
        + (["original_article_completeness_unknown"] if sources else []),
        f"已保存 {saved} / {len(sources)} 条来源观察，其中 {unusable} 条正文不可用（验证页、空内容或疑似导航页），保留供核对，不作为正文证据。完整性未知时未命中不能证明全文未提及。"
        if unusable
        else "按来源观察计数；正文已保存不等于原文章完整，未命中不能证明全文未提及。",
        "sources",
        "inspect_sources",
        "核对来源状态",
    )
    item(
        "scoring",
        "四维评价",
        "not_applicable"
        if no_prompts
        else "ready"
        if targets and scores == len(targets)
        else "partial"
        if scores
        else "pending",
        scores,
        len(targets),
        []
        if no_prompts or targets and scores == len(targets)
        else ["score_prerequisites_incomplete"],
        "缺项保留未知，不改变固定权重；没有显式完整排序时位置不补零。",
        "entities",
        "inspect_scores",
        "查看评分依据",
    )
    diagnoses = summary.get("diagnoses", [])
    item(
        "insights",
        "可执行机会",
        "partial" if diagnoses else "pending",
        len(diagnoses),
        None,
        ["opportunity_review_required"]
        if diagnoses
        else ["opportunity_evidence_pending"],
        "有证据的单维观察可继续核对，不能将竞品证据直接写成我方事实。",
        "actions",
        "inspect_opportunities",
        "查看具体任务",
    )
    return {"version": "result-readiness-v1", "items": items}
