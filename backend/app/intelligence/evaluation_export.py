"""Read-only recent-audit export and offline scoring. Never fills human gold."""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, select, tuple_

from app.intelligence.backup import json_bytes, write_private
from app.intelligence.blob import digest
from app.intelligence.evaluation import (
    FORMAT,
    Annotation,
    EvaluationInvalid,
    Mention,
    Prediction,
    evaluate,
    freeze_case,
    render_report,
)
from app.intelligence.models import (
    ClaimEvidence,
    ContentSegment,
    IntelligenceRun,
    SegmentAnalysis,
    SemanticCache,
)
from app.intelligence.semantics import SEGMENT_VERSION, stable_hash
from app.models.models import Audit
from app.utils.timezone import utc_isoformat, utcnow

MAX_CASES = 10000


def append_case(cases, predictions, case, **prediction):
    if len(cases) >= MAX_CASES:
        raise EvaluationInvalid(
            "Export exceeds the explicit case limit; choose a smaller audit scope"
        )
    cases.append(case.model_dump())
    predictions.append(
        Prediction(
            case_id=case.case_id,
            input_hash=case.input_hash,
            signature=case.signature,
            **prediction,
        ).model_dump()
    )


async def export_recent(db, project_id, *, audit_limit=2, split="holdout"):
    if audit_limit not in (1, 2) or split not in ("holdout", "development"):
        raise EvaluationInvalid("Choose one or two recent audits and an explicit split")
    audits = (
        await db.scalars(
            select(Audit)
            .where(Audit.project_id == project_id)
            .order_by(Audit.created_at.desc(), Audit.id.desc())
            .limit(audit_limit)
        )
    ).all()
    cases, predictions, selection = [], [], []
    for audit in audits:
        run = await db.scalar(
            select(IntelligenceRun)
            .where(
                IntelligenceRun.project_id == project_id,
                IntelligenceRun.audit_id == audit.id,
            )
            .order_by(IntelligenceRun.revision.desc(), IntelligenceRun.id.desc())
            .limit(1)
        )
        entry = {
            "audit_id": str(audit.id),
            "run_id": run.id if run else None,
            "revision": run.revision if run else None,
            "status": run.status if run else "no_run",
            "cases": 0,
            "owners_without_segments": 0,
            "relation_scope": "stored_pairs_only",
            "evidence_coverage": run.coverage.get("evidence_alignment", {})
            if run
            else {},
            "answer_status_counts": dict(
                Counter(
                    "failed"
                    if row.get("error")
                    else row.get("answer_status", "unknown")
                    for row in run.manifest.get("responses", [])
                )
            )
            if run
            else {},
            "source_status_counts": dict(
                Counter(
                    row.get("fetch_status", "unknown")
                    for row in run.manifest.get("sources", [])
                )
            )
            if run
            else {},
            "source_cards_without_archives": sum(
                not row.get("snapshot_id") for row in run.manifest.get("sources", [])
            )
            if run
            else 0,
        }
        selection.append(entry)
        if run is None or run.status not in ("ready", "partial"):
            continue  # Latest queued/failed revision blocks old success fallback.
        responses = {row["id"]: row for row in run.manifest.get("responses", [])}
        sources = {row["id"]: row for row in run.manifest.get("sources", [])}
        contexts = {}
        for response in responses.values():
            if not response.get("error"):
                contexts[("answer", response["id"])] = [(response, None)]
        for source in sources.values():
            response = responses.get(source["response_id"])
            if response is None:
                raise EvaluationInvalid("Source is outside its frozen answer scope")
            if source.get("title") or source.get("snippet"):
                contexts[("citation", source["id"])] = [(response, source)]
            if source.get("snapshot_id"):
                contexts.setdefault(("snapshot", source["snapshot_id"]), []).append(
                    (response, source)
                )
        if not contexts:
            continue
        rows = (
            await db.execute(
                select(ContentSegment, SegmentAnalysis, SemanticCache)
                .outerjoin(
                    SegmentAnalysis,
                    and_(
                        SegmentAnalysis.project_id == project_id,
                        SegmentAnalysis.run_id == run.id,
                        SegmentAnalysis.segment_id == ContentSegment.id,
                    ),
                )
                .outerjoin(
                    SemanticCache,
                    and_(
                        SemanticCache.project_id == project_id,
                        SemanticCache.id == SegmentAnalysis.cache_id,
                        SemanticCache.signature == run.signature,
                        SemanticCache.segment_hash == ContentSegment.segment_hash,
                    ),
                )
                .where(
                    ContentSegment.project_id == project_id,
                    ContentSegment.extractor_version == SEGMENT_VERSION,
                    tuple_(ContentSegment.owner_kind, ContentSegment.owner_id).in_(
                        list(contexts)
                    ),
                )
                .order_by(
                    ContentSegment.owner_kind,
                    ContentSegment.owner_id,
                    ContentSegment.ordinal,
                )
                .limit(MAX_CASES + 1)
            )
        ).all()
        if len(rows) > MAX_CASES:
            raise EvaluationInvalid("Export exceeds the explicit segment limit")
        segments = {}
        count_before = len(cases)
        seen_owners = set()

        def case_fields(segment, response, source, run=run):
            return {
                "group_id": stable_hash(["prompt-group", response["prompt_hash"]]),
                "origin": "stored_audit",
                "split": split,
                "platform": response["platform"],
                "signature": run.signature,
                "run_id": run.id,
                "owner_kind": segment.owner_kind,
                "owner_id": segment.owner_id,
                "segment_id": segment.id,
                "observation_id": source["id"] if source else response["id"],
                "global_offset": segment.start_offset,
                "text": segment.text,
                "prompt": response.get("prompt", ""),
                "source_kind": source["kind"] if source else "answer",
                "content_status": source.get("fetch_status", "unknown")
                if source
                else response.get("answer_status", "unknown"),
            }

        for segment, application, cache in rows:
            if digest(segment.text.encode()) != segment.segment_hash:
                raise EvaluationInvalid("Stored segment integrity does not match")
            segments[segment.id] = segment
            seen_owners.add((segment.owner_kind, segment.owner_id))
            available = (
                application
                and application.status in ("completed", "partial")
                and cache is not None
            )
            entities = []
            if available:
                fields = Mention.model_fields
                entities = [
                    {key: value for key, value in item.items() if key in fields}
                    for item in cache.result.get("entities", [])
                ]
            for response, source in contexts[(segment.owner_kind, segment.owner_id)]:
                case = freeze_case(
                    task="entities", **case_fields(segment, response, source)
                )
                append_case(
                    cases,
                    predictions,
                    case,
                    status=application.status
                    if available
                    else "deferred"
                    if application and application.status == "deferred"
                    else "missing",
                    entities=entities,
                )
        entry["owners_without_segments"] = len(set(contexts) - seen_owners)
        evidence_rows = (
            await db.scalars(
                select(ClaimEvidence)
                .where(
                    ClaimEvidence.project_id == project_id,
                    ClaimEvidence.run_id == run.id,
                )
                .order_by(ClaimEvidence.id)
                .limit(MAX_CASES + 1)
            )
        ).all()
        if len(evidence_rows) > MAX_CASES:
            raise EvaluationInvalid("Export exceeds the explicit pair limit")
        for evidence in evidence_rows:
            answer, source_segment = (
                segments.get(evidence.answer_segment_id),
                segments.get(evidence.source_segment_id),
            )
            citation = sources.get(evidence.citation_id)
            response = responses.get(answer.owner_id) if answer else None
            if (
                not answer
                or not source_segment
                or not citation
                or not response
                or answer.owner_kind != "answer"
                or source_segment.owner_kind != "snapshot"
            ):
                raise EvaluationInvalid(
                    "Evidence record is outside the frozen project/run segments"
                )
            eligible = (
                citation["response_id"] == answer.owner_id
                and citation["platform"] == response["platform"]
                and citation["kind"] == "answer_reference"
                and citation.get("url_resolution") == "exact"
                and citation.get("snapshot_id") == source_segment.owner_id
                and citation.get("fetch_status") == "captured"
                and evidence.answer_quote in answer.text
            )
            case = freeze_case(
                task="relation",
                answer_quote=evidence.answer_quote,
                evidence_quote=evidence.source_quote,
                citation_eligible=eligible,
                **{
                    **case_fields(source_segment, response, citation),
                    "observation_id": evidence.id,
                },
            )
            append_case(
                cases,
                predictions,
                case,
                status="completed",
                relation=evidence.relation,
                source_quote=evidence.source_quote,
            )
        entry["cases"] = len(cases) - count_before
    return {
        "schema": FORMAT,
        "dataset_id": uuid4().hex,
        "created_at": utc_isoformat(utcnow()),
        "project_id": project_id,
        "selection": selection,
        "development_group_ids": [],
        "cases": cases,
        "predictions": predictions,
    }


def publish_dataset(dataset, directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    write_private(directory / "INCOMPLETE", b"Evaluation export incomplete\n")
    files = {}
    for name, rows in (
        ("cases.jsonl", dataset["cases"]),
        ("predictions.jsonl", dataset["predictions"]),
    ):
        data = b"".join(json_bytes(row) + b"\n" for row in rows)
        write_private(directory / name, data)
        files[name] = digest(data)
    for reviewer in ("a", "b"):
        # Intentionally do not copy any model entities, sentiment or relation.
        rows = [
            Annotation(
                case_id=case["case_id"],
                input_hash=case["input_hash"],
                reviewer_id=f"reviewer-{reviewer}",
                status="pending",
            ).model_dump()
            for case in dataset["cases"]
        ]
        write_private(
            directory / f"annotations-{reviewer}.template.jsonl",
            b"".join(json_bytes(row) + b"\n" for row in rows),
        )
    write_private(
        directory / "ANNOTATION_GUIDE.md",
        (
            "# 独立标注说明\n\n将cases.jsonl和各自模板分别交给两位标注者，不分享predictions.jsonl。"
            "填写不同reviewer_id，互不查看对方结果。完成后将status设为reviewed；未审阅保持pending且标签为null。\n\n"
            "entities任务标注段落内全部实体，不限自有品牌或竞品。start/end是当前text内Python Unicode字符偏移，"
            "end不含边界；quote必须逐字匹配，name必须在quote中。情感只针对该实体。不含实体时明确填[]，不能用null替代。\n\n"
            "relation任务对照answer_quote、evidence_quote及完整来源text，判断提供的证据片段是否支持该观点："
            "supports完整支持，partial范围/时间/数字等仅部分一致，contradicts明确冲突，unverified不足以判断。"
            "证据片段为空或不在原文中时应标unverified；不要用段落其他内容替一个无关证据片段补证。"
            "引用是否真实属于该回答另由citation_eligible核验，不因文章内容支持就把搜索候选当作实际引用。\n\n"
            "只对当前段落标注，不用外部知识补全；global_offset用于回查全文，不能加到本地start/end。"
            "分歧不自动投票或按模型答案解决，需要人工核对后保留可追溯的复核记录。"
            "先登记曾用于提示词或规则调优的group_id，禁止同题其他平台或其他轮次进入留出集。"
            "本包含私有原文，请在受控环境标注，不上传公共服务。\n"
        ).encode(),
    )
    manifest = {
        key: value
        for key, value in dataset.items()
        if key not in ("cases", "predictions")
    }
    manifest["files"] = files
    write_private(directory / "manifest.json", json_bytes(manifest))
    (directory / "INCOMPLETE").unlink()
    return {
        "dataset_id": dataset["dataset_id"],
        "case_count": len(dataset["cases"]),
        "audits": len(dataset["selection"]),
    }


def read_rows(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def score_dataset(directory, annotation_files, development_groups=()):
    directory = Path(directory)
    if (directory / "INCOMPLETE").exists():
        raise EvaluationInvalid("Evaluation export is incomplete")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != FORMAT:
        raise EvaluationInvalid("Unsupported evaluation format")
    for name in ("cases.jsonl", "predictions.jsonl"):
        if digest((directory / name).read_bytes()) != manifest["files"][name]:
            raise EvaluationInvalid("Evaluation input or prediction file changed")
    report = evaluate(
        read_rows(directory / "cases.jsonl"),
        read_rows(directory / "predictions.jsonl"),
        [row for filename in annotation_files for row in read_rows(filename)],
        development_group_ids=[*manifest["development_group_ids"], *development_groups],
    )
    report.update(dataset_id=manifest["dataset_id"], selection=manifest["selection"])
    return report


async def cli():
    parser = argparse.ArgumentParser(
        description="Read-only recent audit export and offline quality measurement"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    export = commands.add_parser("export")
    export.add_argument("--project-id", required=True)
    export.add_argument("--audit-limit", type=int, choices=(1, 2), default=2)
    export.add_argument("--split", choices=("development", "holdout"), required=True)
    export.add_argument("--output", required=True)
    score = commands.add_parser("score")
    score.add_argument("--directory", required=True)
    score.add_argument("--annotations", nargs="+", required=True)
    score.add_argument(
        "--development-groups",
        help="JSON array of prompt group IDs used during development",
    )
    score.add_argument("--output-report", required=True)
    args = parser.parse_args()
    try:
        if args.action == "export":
            from app.database import async_session, engine

            try:
                async with async_session() as db:
                    dataset = await export_recent(
                        db,
                        args.project_id,
                        audit_limit=args.audit_limit,
                        split=args.split,
                    )
                result = publish_dataset(dataset, args.output)
            finally:
                await engine.dispose()
        else:
            development = (
                json.loads(Path(args.development_groups).read_text(encoding="utf-8"))
                if args.development_groups
                else []
            )
            if not isinstance(development, list) or any(
                not isinstance(key, str)
                or len(key) != 64
                or any(char not in "0123456789abcdef" for char in key)
                for key in development
            ):
                raise EvaluationInvalid("Invalid development group list")
            report = score_dataset(args.directory, args.annotations, development)
            report_path = Path(args.output_report)
            if (
                report_path.suffix != ".json"
                or report_path.exists()
                or report_path.with_suffix(".md").exists()
            ):
                raise EvaluationInvalid("Choose a new JSON report and Markdown sibling")
            write_private(report_path, json_bytes(report))
            write_private(
                Path(args.output_report).with_suffix(".md"),
                render_report(report).encode("utf-8"),
            )
            result = {
                "dataset_id": report["dataset_id"],
                "case_count": report["case_count"],
                "release_eligible": False,
            }
        print(json.dumps({"status": "completed", **result}))
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "Evaluation failed; inspect private inputs, scope and hashes",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(cli()))
