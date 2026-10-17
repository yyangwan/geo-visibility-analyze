"""Paginated Unicode reports. No HTML, external URLs, images or file fetches."""

import asyncio
import io
import json
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

RENDER_SLOTS = asyncio.Semaphore(2)
_RENDER_TASKS = set()


async def render_report(report, detail):
    slots = RENDER_SLOTS
    await slots.acquire()
    try:
        task = asyncio.create_task(asyncio.to_thread(render_pdf, report, detail))
    except BaseException:
        slots.release()
        raise
    _RENDER_TASKS.add(task)

    def completed(rendering):
        _RENDER_TASKS.discard(rendering)
        slots.release()
        # A cancelled request no longer awaits this result, but its thread still
        # owns a permit until completion. Retrieve any orphaned exception too.
        if not rendering.cancelled():
            rendering.exception()

    task.add_done_callback(completed)
    return await asyncio.shield(task)


METRICS = {
    "answer_presence": "回答提及",
    "cited_card_presence": "实际引用卡片提及",
    "cited_archive_presence": "已存引用正文提及",
    "candidate_card_presence": "检索候选卡片提及",
}
REASONS = {
    "incomplete_answer": "回答不完整",
    "ambiguous_identity": "回答实体归属待核对",
    "analysis_incomplete": "分析未完成",
    "source_panel_incomplete": "来源面板不完整",
    "source_response_mismatch": "来源与回答不匹配",
    "source_classification_unknown": "来源类别待确认",
    "source_card_incomplete": "来源卡片分析不完整",
    "cited_archive_incomplete": "正文缺失、分析不完整或完整性未确认",
    "ambiguous_source_identity": "来源实体归属待核对",
    "missing_baseline": "基线未取得回答",
    "missing_followup": "复测未取得对应回答",
    "ambiguous_sample": "样本对应不唯一",
    "incomplete_analysis": "回答分析不完整",
    "unknown_conditions": "实际采集条件未知",
    "conditions_changed": "实际采集条件变化",
    "observation_not_later": "复测时间不晚于基线",
    "prompt_version_changed": "提示词版本变化",
    "analysis_signature_changed": "分析口径变化",
    "entity_mapping_changed": "实体映射变化",
    "insufficient_rounds": "共同独立轮次不足",
}
RELATIONS = {
    "supports": "支持",
    "partial": "部分支持",
    "contradicts": "冲突",
    "unverified": "无法核验",
}
KINDS = {
    "answer_reference": "实际引用",
    "search_candidate": "检索候选",
    "text_link": "文本链接",
    "named_inference": "推测来源",
    "unknown": "类别未知",
}


def export_scope(report, detail):
    if detail == "full":
        return report
    return {
        **{
            key: report.get(key)
            for key in (
                "schema",
                "audit_id",
                "project_id",
                "run_id",
                "revision",
                "status",
                "coverage",
                "finished_at",
            )
        },
        "platforms": [
            {"platform": row["platform"], "samples": row["samples"], "metrics": []}
            for row in report.get("platforms", [])
        ],
    }


def render_pdf(report, detail="full"):
    report = export_scope(report, detail)
    # Reject oversized reports explicitly; never truncate evidence or metrics.
    if len(json.dumps(report, ensure_ascii=False)) > 1_000_000:
        raise ValueError("report_too_large")
    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body = ParagraphStyle(
        "body",
        fontName="STSong-Light",
        fontSize=10,
        leading=16,
        wordWrap="CJK",
        alignment=TA_LEFT,
        spaceAfter=7,
        splitLongWords=True,
    )
    heading = ParagraphStyle(
        "heading",
        parent=body,
        fontSize=15,
        leading=22,
        spaceBefore=16,
        textColor=colors.HexColor("#17354d"),
        keepWithNext=True,
    )
    story = []

    def p(text, title=False):
        # Paragraph markup must never interpret source content as links/tags.
        safe = "".join(char for char in str(text) if ord(char) >= 32 or char in "\n\t")
        story.append(
            Paragraph(escape(safe).replace("\n", "<br/>"), heading if title else body)
        )

    p(f"AI 可见性审计报告 #{report['audit_id']}", True)
    p(
        f"项目：{report['project_id']}　修订：{report['revision']}　Run：{report['run_id']}"
    )
    p(
        f"分析完成时间：{report.get('finished_at') or '未记录'}　状态：{'部分完成' if report['status'] == 'partial' else '已完成'}"
    )
    p(
        "本报告为指定修订的观测结果。指标按回答计数，各项分母独立；未知不记为零。已存正文不等于原文章全文，也不证明模型采用了正文。检索候选不等于实际引用。单次审计不作因果、收益或提升承诺。"
    )
    coverage = report.get("coverage") or {}
    p(
        f"分析字符：{coverage.get('analyzed_characters', 0)}/{coverage.get('total_characters', 0)}；已存来源正文：{coverage.get('captured_sources', 0)}/{coverage.get('sources', 0)}"
    )
    if detail == "basic":
        p(
            "基础报告：包含状态、覆盖及平台样本；完整实体、证据、来源和诊断需完整报告权限。"
        )
    entities = {row["id"]: row for row in report.get("entities", [])}
    roles = {
        "own": "自有品牌",
        "competitor": "已登记竞品",
        "comparison": "仅对比",
        "unregistered": "未登记主体",
    }
    for platform in report.get("platforms", []):
        p(f"平台：{platform['platform']} / {platform['samples']} 次回答", True)
        for metric in platform["metrics"]:
            p(
                f"{METRICS.get(metric['metric'], metric['metric'])}：有效 {len(metric['included'])}，排除 {len(metric['excluded'])}",
                True,
            )
            for value in metric["entities"]:
                entity = entities.get(value["entity_id"], {})
                ratio = (
                    f"{value['numerator']}/{value['denominator']} ({value['numerator'] / value['denominator']:.1%})"
                    if value["denominator"]
                    else "未知（无有效样本）"
                )
                p(
                    f"{entity.get('name', value['entity_id'])} / {roles.get(entity.get('role'), '未登记主体')} / 身份 {entity.get('identity_status', '待核对')}：{ratio}"
                )
            p("有效回答ID：" + ("；".join(metric["included"]) or "无"))
            for item in metric["excluded"]:
                p(
                    f"排除 {item['response_id']}：{REASONS.get(item['reason'], item['reason'])}"
                )
    for item in report.get("diagnoses", []):
        p(f"分平台优化方向 / {item['platform']} / 待人工复核", True)
        p(item.get("prompt", "平台观察"))
        p(item["finding"])
        if item.get("action"):
            action = item["action"]
            p("建议任务：" + action["task"])
            p("所需材料：" + "；".join(action["materials_needed"]))
            p("验证方式：" + action["success_check"])
        p("回答ID：" + "；".join(item.get("response_ids", [])))
    for item in report.get("evidence", []):
        p(f"证据对照 / {item['platform']} / {item['id']}", True)
        p("模型判定：" + RELATIONS.get(item["relation"], "无法核验"))
        p("回答：" + item["answer_quote"])
        p("来源：" + (item.get("source_quote") or "没有可核验片段"))
        review = item.get("human_review")
        if review:
            p(
                f"人工复核：{RELATIONS.get(review['relation'], '无法核验')} / v{review['version']} / {review['actor_id']}"
            )
            p(review["source_quote"])
            p(review["reason"])
        else:
            p("尚未人工复核，不作为已批准事实。")
        p("保存正文ID：" + item["snapshot_id"])
    for item in report.get("sources", []):
        p(f"来源 / {item['platform']} / {KINDS.get(item['kind'], '类别待核对')}", True)
        p(item.get("title") or "无标题来源")
        p(item.get("snippet") or "无卡片摘要")
        p(
            f"卡片ID：{item['id']}；回答ID：{item['response_id']}；保存状态：{item['fetch_status']}；正文ID：{item.get('snapshot_id') or '未保存'}"
        )
    for item in report.get("responses", []):
        p(f"提示词 / {item['platform']} / 回答 {item['id']}", True)
        p(item.get("prompt") or "未记录提示词")
        p(
            f"采集时间：{item.get('observed_at') or '未记录'}；回答完整度：{item.get('answer_status') or '未知'}"
        )
    measurements = report.get("measurements")
    if measurements is not None:
        p("本修订关联的固定样本复测", True)
        p(
            "仅包含以本修订作为基线或复测的已保存对比。观测变化不证明由内容行动造成；95%区间为探索性单项区间，未做多重比较校正。正文只描述已保存文本，候选不等于实际引用。"
        )
        if measurements["truncated"]:
            p(
                "本报告展示最近10条关联对比；更多已保存结果请在趋势与复测中按方案回查。各条统计未截断样本。"
            )
        if not measurements["results"]:
            p("本修订尚无已保存的关联对比，不使用其他审计结果填充。")
        for result in measurements["results"]:
            current = result["snapshot_current"] and report.get(
                "snapshot_current", True
            )
            p(
                f"复测方案：{result.get('cohort_title', result['cohort_id'])} / 记录 {result['id']}",
                True,
            )
            p(
                f"基线审计 {result['baseline_audit_id']} / Run {result['baseline_run_id']}；复测审计 {result['followup_audit_id']} / Run {result['followup_run_id']}"
            )
            p(
                f"本报告对应{'基线' if result['report_role'] == 'baseline' else '复测'}修订；记录时间 {result['created_at']}"
            )
            if not current:
                p("分析修订、配置或口径已变化，仅供历史回查，不输出当前变化结论。")
            paired_entities = {item["id"]: item["name"] for item in result["entities"]}
            for platform in result["platforms"]:
                p(
                    f"复测平台 {platform['platform']} / 回答共同有效 {len(platform['included'])}/{platform['expected_samples']} / 问题簇 {platform['prompt_clusters']} / 排除 {len(platform['excluded'])} / 额外样本 {len(platform['out_of_cohort'])}",
                    True,
                )
                for metric in platform["metrics"]:
                    population = platform.get("source_populations", {}).get(
                        metric["metric"], platform
                    )
                    status = metric.get("status", platform["status"])
                    values = (
                        metric["descriptive"]
                        if status == "descriptive_only"
                        else metric
                    )

                    def ratio(phase, values=values):
                        value = values[phase]
                        return (
                            f"{value['numerator']}/{value['denominator']} ({value['numerator'] / value['denominator']:.1%})"
                            if value["denominator"]
                            else "未知（无有效样本）"
                        )

                    p(
                        f"{paired_entities.get(metric['entity_id'], metric['entity_id'])} / {metric['entity_id']} / {METRICS.get(metric['metric'], metric['metric'])}"
                    )
                    state_label = {
                        "valid_comparison": "可作观测对比",
                        "insufficient": "共同样本不足",
                        "descriptive_only": "仅可描述",
                        "undetermined": "无法判断",
                    }.get(status, "待核对")
                    p(
                        f"计算时状态：{state_label}；基线 {ratio('baseline')}；复测 {ratio('followup')}"
                    )
                    if current and metric.get("difference") is not None:
                        interval = metric.get("interval95")
                        p(
                            f"观测变化 {metric['difference'] * 100:.1f} 个百分点；95%区间 "
                            + (
                                f"{interval[0] * 100:.1f} 至 {interval[1] * 100:.1f} 个百分点"
                                if interval
                                else "未取得"
                            )
                        )
                    else:
                        p("不输出当前变化结论。")
                    p(
                        f"本指标共同有效 {len(population['included'])}；排除 {len(population['excluded'])}"
                    )
                    p(
                        "共同样本索引："
                        + json.dumps(population["included"], ensure_ascii=False)
                    )
                    for item in population["excluded"]:
                        p(
                            f"排除样本 {item['baseline_sample_id']}：{REASONS.get(item['reason'], '条件或完整性待核对')}"
                        )
                    for phase, by_response in metric.get("traces", {}).items():
                        for response_id, traces in by_response.items():
                            for trace in traces:
                                p(
                                    f"证据索引 / {phase} Run {result[phase + '_run_id']} / 回答 {response_id} / {trace['owner_kind']} {trace['owner_id']} / 提及 {trace['mention_id']}"
                                )
                p(
                    "证据索引为保存的有限示例，统计仍使用该指标全部有效样本；完整原文按各端Run和内容ID在线回查。"
                )
    p(
        "原始完整回答与保存正文请使用在线报告按本报告 Run 和内容ID回查；本PDF包含报告观察、证据片段和内容索引。"
    )
    story.append(Spacer(1, 10))
    output = io.BytesIO()

    def footer(canvas, doc):
        canvas.setFont("STSong-Light", 8)
        canvas.drawString(
            36,
            22,
            f"审计 {report['audit_id']} / 修订 {report['revision']} / 第 {doc.page} 页",
        )

    SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=32,
        bottomMargin=38,
        title=f"Citation report {report['audit_id']}",
        author="Genilink",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
