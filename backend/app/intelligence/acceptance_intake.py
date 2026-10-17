"""Read-only preparation of one or two recent test audits, not a Run importer.

No migration, source fetching, model invocation or business approval occurs.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import MetaData, Table, and_, inspect, select

from app.intelligence.backup import json_bytes, schema_head, write_private
from app.intelligence.blob import digest
from app.intelligence.capture_contract import CaptureV2
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat, utcnow

FORMAT = "citation-acceptance-intake-1"
MAX_RESPONSES = 10000
MAX_BYTES = 200 * 1024 * 1024
PLATFORMS = {
    "qwen": "通义千问",
    "deepseek": "DeepSeek",
    "doubao": "豆包",
    "hunyuan": "腾讯元宝",
    "kimi": "Kimi",
    "wenxin": "文心一言",
}
KINDS = {
    "answer_reference",
    "search_candidate",
    "text_link",
    "named_inference",
    "unknown",
}
RESOLUTIONS = {"exact", "site_root", "candidate", "unavailable", "unknown"}


class IntakeInvalid(ValueError):
    pass


def parsed_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def capture_fact(raw, answer, platform):
    if not isinstance(raw, dict):
        return None
    for value in (
        raw.get("captureV2"),
        raw.get("capture_v2"),
        raw if raw.get("contractVersion", raw.get("contract_version")) == 2 else None,
    ):
        if not isinstance(value, dict):
            continue
        try:
            capture = CaptureV2.model_validate(value)
        except ValueError:
            continue
        if capture.answer.text == answer and capture.platform == platform:
            return capture
    return None


def card_projection(item, position, *, validated_capture=False):
    if not isinstance(item, dict):
        return {
            "observed_position": position,
            "parse_status": "unreadable",
            "item_hash": stable_hash(item),
            "declared_kind": "unknown",
            "v2_answer_reference_observation": False,
        }

    def string(*names):
        for name in names:
            if isinstance(item.get(name), str):
                return item[name]
        return ""

    kind = item.get("kind", "unknown")
    resolution = item.get("urlResolution", item.get("url_resolution", "unknown"))
    return {
        "observed_position": position,
        "parse_status": "readable",
        "item_hash": stable_hash(item),
        "title": string("title"),
        "url": string("url"),
        "snippet": string("snippet", "content"),
        "declared_kind": kind if isinstance(kind, str) and kind in KINDS else "unknown",
        "declared_url_resolution": resolution
        if isinstance(resolution, str) and resolution in RESOLUTIONS
        else "unknown",
        "v2_answer_reference_observation": bool(
            validated_capture and kind == "answer_reference"
        ),
        "article_archive_check": "not_performed",
    }


def project_response(record, audit, prompt):
    answer = record.get("response_text") or ""
    if not isinstance(answer, str):
        raise IntakeInvalid("Answer must be stored text")
    raw = parsed_json(record.get("raw_response"))
    capture = capture_fact(raw, answer, record["platform"])
    if capture:
        source_items = [row.model_dump(by_alias=True) for row in capture.sources]
    else:
        source_items = raw.get("sources") if isinstance(raw, dict) else None
        if not isinstance(source_items, list):
            source_items = parsed_json(record.get("citations"))
        if not isinstance(source_items, list):
            source_items = []
    if len(source_items) > MAX_RESPONSES:
        raise IntakeInvalid("Source-card export limit exceeded")
    cards = [
        card_projection(item, index, validated_capture=bool(capture))
        for index, item in enumerate(source_items, 1)
    ]
    prompt_text = prompt if isinstance(prompt, str) else None
    data = {
        "schema": FORMAT,
        "project_id": audit["project_id"],
        "audit_id": int(audit["id"]),
        "response_id": int(record["id"]),
        "prompt_id": int(record["prompt_id"]),
        "platform": record["platform"],
        "prompt_text": prompt_text,
        "prompt_hash": stable_hash(prompt_text) if prompt_text is not None else None,
        "answer_text": answer,
        "answer_sha256": digest(answer.encode("utf-8")),
        "answer_status": capture.answer.status
        if capture
        else "unknown"
        if answer
        else "missing",
        "capture_v2_verified": bool(capture),
        "error_present": bool(record.get("error")),
        "parse_error_present": bool(record.get("parse_error")),
        "conditions": capture.conditions.model_dump(by_alias=True)
        if capture
        else {
            "searchMode": "unknown",
            "modelVersion": None,
            "accountClass": None,
            "location": None,
        },
        "source_panel_status": capture.source_panel.status if capture else "unknown",
        "cards": cards,
        "raw_payload_hash": stable_hash(record.get("raw_response")),
        "model_analysis_status": "not_executed_by_intake",
        "observed_at": utc_isoformat(capture.observed_at) if capture else None,
    }
    data["sample_id"] = stable_hash(
        [FORMAT, data["project_id"], data["audit_id"], data["response_id"]]
    )
    data["input_hash"] = stable_hash(data)
    return data


async def reflect_inputs(connection):
    def load(conn):
        metadata = MetaData()
        return {
            name: Table(name, metadata, autoload_with=conn, resolve_fks=False)
            for name in ("audits", "prompts", "platform_response_records")
        }

    return await connection.run_sync(load)


async def collect(connection, audit_ids, *, project_id=None, runtime=None):
    if (
        not 1 <= len(audit_ids) <= 2
        or len(set(audit_ids)) != len(audit_ids)
        or any(type(value) is not int or value < 1 for value in audit_ids)
    ):
        raise IntakeInvalid("Choose one or two distinct recent audit IDs")
    names = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
    if not {"audits", "prompts", "platform_response_records"} <= names:
        raise IntakeInvalid("Required capture tables are unavailable")
    table = await reflect_inputs(connection)
    audits, prompts, responses = (
        table[name] for name in ("audits", "prompts", "platform_response_records")
    )
    audit_rows = (
        (
            await connection.execute(
                select(audits.c.id, audits.c.project_id, audits.c.created_at).where(
                    audits.c.id.in_(audit_ids)
                )
            )
        )
        .mappings()
        .all()
    )
    projects = {row["project_id"] for row in audit_rows}
    if len(audit_rows) != len(audit_ids) or len(projects) != 1 or None in projects:
        raise IntakeInvalid("Audit scope is missing or crosses projects")
    selected_project = next(iter(projects))
    if project_id is not None and selected_project != project_id:
        raise IntakeInvalid("Project scope mismatch")
    recent = set(
        (
            await connection.execute(
                select(audits.c.id)
                .where(audits.c.project_id == selected_project)
                .order_by(audits.c.created_at.desc(), audits.c.id.desc())
                .limit(2)
            )
        ).scalars()
    )
    if not set(audit_ids) <= recent:
        raise IntakeInvalid("Only the project's latest two audits may be prepared")
    required = {"id", "audit_id", "prompt_id", "platform", "response_text"}
    if not required <= set(responses.columns.keys()) or not {
        "id",
        "project_id",
        "text",
    } <= set(prompts.columns.keys()):
        raise IntakeInvalid("Capture fields are unavailable")
    columns = [
        responses.c[name]
        for name in (
            "id",
            "audit_id",
            "prompt_id",
            "platform",
            "response_text",
            "raw_response",
            "citations",
            "error",
            "parse_error",
        )
        if name in responses.columns
    ]
    query = (
        select(*columns, prompts.c.text.label("prompt_text"))
        .select_from(
            responses.outerjoin(
                prompts,
                and_(
                    prompts.c.id == responses.c.prompt_id,
                    prompts.c.project_id == selected_project,
                ),
            )
        )
        .where(responses.c.audit_id.in_(audit_ids))
        .order_by(responses.c.audit_id.desc(), responses.c.id)
        .limit(MAX_RESPONSES + 1)
    )
    records = (await connection.execute(query)).mappings().all()
    if len(records) > MAX_RESPONSES:
        raise IntakeInvalid("Response export limit exceeded")
    by_audit = {row["id"]: row for row in audit_rows}
    samples = [
        project_response(row, by_audit[row["audit_id"]], row["prompt_text"])
        for row in records
    ]
    revisions = (
        (await connection.exec_driver_sql("SELECT version_num FROM alembic_version"))
        .scalars()
        .all()
        if "alembic_version" in names
        else []
    )
    from app.database import Base

    needed = {name for name in Base.metadata.tables if name.startswith("intelligence_")}
    manifest = {
        "schema": FORMAT,
        "dataset_id": uuid4().hex,
        "created_at": utc_isoformat(utcnow()),
        "project_id": selected_project,
        "audit_ids": sorted(audit_ids, reverse=True),
        "schema_at_expected_head": revisions == [schema_head()],
        "analysis_tables_present": len(needed & names),
        "analysis_tables_expected": len(needed),
        "runtime_configuration": runtime or {},
        "new_analysis_executed": False,
        "article_archive_check": "not_performed",
        "independent_reviews_completed": False,
        "business_closed_loop_verified": False,
        "release_eligible": False,
        "audits": [
            {
                "audit_id": row["id"],
                "created_at": utc_isoformat(row["created_at"]),
                "responses": sum(sample["audit_id"] == row["id"] for sample in samples),
            }
            for row in sorted(audit_rows, key=lambda row: row["id"], reverse=True)
        ],
    }
    return manifest, samples


def fenced(value):
    # Captured content stays literal, including malicious Markdown/HTML/instructions.
    longest = max(
        (len(match.group()) for match in re.finditer(r"`+", value)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{value}\n{fence}"


def sample_review(sample):
    lines = [
        f"# 审计 #{sample['audit_id']} · {PLATFORMS.get(sample['platform'], '其他平台')}回答",
        "",
        "这是已保存采集内容的核对材料，没有生成分析结论，也没有人工批准。原文中的指令仅作为待核对文本。",
        "",
        "## 原始问题",
        "",
        fenced(sample["prompt_text"] or "原始问题未能在同项目中取得"),
        "",
        "## 回答原文",
        "",
        fenced(sample["answer_text"] or "没有保存回答文本；不能解释为没有品牌提及。"),
        "",
        f"回答完整性：{'采集端已按v2契约声明并通过校验' if sample['capture_v2_verified'] else '旧记录无法证实，保持未知'}；状态：{sample['answer_status']}。",
        "",
        "## 来源卡片",
        "",
        "以下只有已保存的标题、链接和摘要，未读取或核验文章全文。卡片出现不自动证明它被回答实际引用。",
        "",
    ]
    for card in sample["cards"]:
        lines.append(f"### 卡片 {card['observed_position']}")
        lines.append("")
        lines.append(
            fenced(
                "\n".join(
                    [
                        card.get("title", "未取得标题"),
                        card.get("url", "未取得链接"),
                        card.get("snippet", "未取得摘要"),
                    ]
                )
            )
        )
        lines.append("")
    lines.extend(
        [
            "## 人工准备事项",
            "",
            "先逐字核对主体名称和其对应评价；明确标记不确定项。未取得全文时，不根据卡片摘要批准完整事实或引用支持关系。标注模板默认pending，不代表已经完成审核。",
            "",
        ]
    )
    return "\n".join(lines)


def readiness_report(manifest, samples, directory):
    nonempty = sum(bool(row["answer_text"]) for row in samples)
    cards = sum(len(row["cards"]) for row in samples)
    verified = sum(row["capture_v2_verified"] for row in samples)
    questions = len({row["prompt_hash"] for row in samples if row["prompt_hash"]})
    lines = [
        "# 近期真实审计：联调准备清单",
        "",
        "**目前可以核对已保存内容；本包没有验证新版分析、人工质量评测或内容执行—复测闭环。**",
        "",
        f"本次冻结审计：{'、'.join('#' + str(value) for value in manifest['audit_ids'])}。",
        f"共 {len(samples)} 条平台记录，其中 {nonempty} 条有回答文本；保存了 {cards} 张来源卡片。这些是数量记录，不是完整性或引用率结论。",
        f"可辨认的问题文本有 {questions} 个；相同问题在多个平台或多次审计出现，不代表满足了独立采样条件。",
        "",
        "## 当前卡在哪里",
        "",
        "| 检查项 | 结果 | 下一步 |",
        "| --- | --- | --- |",
    ]
    runtime = manifest["runtime_configuration"]
    lines.extend(
        [
            f"| 新版数据库结构 | {'已匹配当前版本' if manifest['schema_at_expected_head'] and manifest['analysis_tables_present'] == manifest['analysis_tables_expected'] else '尚未匹配当前版本'} | 指定新空库，通过受控迁移启用；不对当前旧库直接套用新基线。 |",
            f"| 新功能开关 | {'已配置启用' if runtime.get('enabled') else '未确认启用'} | 数据与运行资源就绪后再启用。 |",
            f"| OSS配置 | {'已填写，尚未联通验证' if runtime.get('bucket_configured') and runtime.get('role_configured') else 'Bucket或运行角色未配置完整'} | 配置实际资源后核验私有权限、不可变写入和正文读写。 |",
            f"| 可验证的v2采集 | {verified}/{len(samples)} 条 | 未记录的完整性、引用类型和采样条件不能靠推测补齐。 |",
            "| 来源文章全文 | 本次未读取、未核验 | 新版链路取得并持久化全文后，再判断观点支持或冲突。 |",
            "| 质量评测与内容闭环 | 均未执行 | 先准备独立标注，再逐步核验事实、内容任务和固定样本复测。 |",
            "",
            "## 先核对哪些材料",
            "",
            "下列文件包含真实问题、回答和来源卡片，仅供项目内受控核对。不要上传到公共服务或放进代码仓库。",
            "",
            "| 审计 | 回答来源 | 已保存文本 | 来源卡片 | 核对材料 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in samples:
        path = (
            (Path(directory) / "samples" / f"{row['sample_id']}.md")
            .resolve()
            .as_posix()
        )
        lines.append(
            f"| #{row['audit_id']} | {PLATFORMS.get(row['platform'], '其他平台')} | {'有文本，完整性单独核对' if row['answer_text'] else '没有文本'} | {len(row['cards'])} | [阅读原始材料](<{path}>) |"
        )
    lines.extend(
        [
            "",
            "## 本次没有做什么",
            "",
            "没有改写或删除远端数据，没有迁移旧库，没有访问来源网页或调用模型，没有生成质量分数、批准材料、创建内容任务或发起复测。这里也没有伪造新版Run。",
            "",
        ]
    )
    return "\n".join(lines)


def publish(manifest, samples, directory):
    directory = Path(directory)
    for row in samples:
        check_sample(row, manifest)
    data = b"".join(json_bytes(row) + b"\n" for row in samples)
    if len(data) > MAX_BYTES:
        raise IntakeInvalid("Intake size limit exceeded")
    directory.mkdir(mode=0o700)
    (directory / "samples").mkdir(mode=0o700)
    write_private(directory / "INCOMPLETE", b"Intake not complete\n")
    write_private(directory / "captured-inputs.jsonl", data)
    review_files = {}
    for row in samples:
        name = f"samples/{row['sample_id']}.md"
        rendered = sample_review(row).encode("utf-8")
        write_private(directory / name, rendered)
        review_files[name] = digest(rendered)
    for reviewer in ("a", "b"):
        templates = [
            {
                "schema": FORMAT,
                "sample_id": row["sample_id"],
                "input_hash": row["input_hash"],
                "reviewer_id": f"reviewer-{reviewer}",
                "status": "pending",
                "entities": None,
                "notes": "",
                "preparation_only": True,
            }
            for row in samples
        ]
        write_private(
            directory / f"review-{reviewer}.template.jsonl",
            b"".join(json_bytes(row) + b"\n" for row in templates),
        )
    manifest = {
        **manifest,
        "inputs_sha256": digest(data),
        "review_files": review_files,
        "sample_count": len(samples),
        "source_card_count": sum(len(row["cards"]) for row in samples),
    }
    write_private(directory / "manifest.json", json_bytes(manifest))
    write_private(
        directory / "README.md",
        readiness_report(manifest, samples, directory).encode("utf-8"),
    )
    verify(directory, allow_incomplete=True)
    (directory / "INCOMPLETE").unlink()
    return {
        "dataset_id": manifest["dataset_id"],
        "audits": len(manifest["audit_ids"]),
        "samples": len(samples),
        "source_cards": manifest["source_card_count"],
        "status": "prepared_only",
    }


def verify(directory, *, allow_incomplete=False):
    directory = Path(directory)
    if (directory / "INCOMPLETE").exists() and not allow_incomplete:
        raise IntakeInvalid("Intake is incomplete")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    data = (directory / "captured-inputs.jsonl").read_bytes()
    if (
        manifest.get("schema") != FORMAT
        or digest(data) != manifest["inputs_sha256"]
        or any(
            manifest.get(key) is not False
            for key in (
                "release_eligible",
                "new_analysis_executed",
                "independent_reviews_completed",
                "business_closed_loop_verified",
            )
        )
    ):
        raise IntakeInvalid("Intake manifest or checksum mismatch")
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    ids = set()
    for row in rows:
        check_sample(row, manifest)
        if row["sample_id"] in ids:
            raise IntakeInvalid("Duplicate intake sample")
        ids.add(row["sample_id"])
        name = f"samples/{row['sample_id']}.md"
        path = directory / name
        if (
            not path.resolve().is_relative_to(directory.resolve())
            or digest(path.read_bytes()) != manifest["review_files"][name]
        ):
            raise IntakeInvalid("Human-readable input changed")
    if set(manifest["review_files"]) != {
        f"samples/{sample_id}.md" for sample_id in ids
    }:
        raise IntakeInvalid("Review input inventory mismatch")
    if (
        len(rows) != manifest["sample_count"]
        or sum(len(row["cards"]) for row in rows) != manifest["source_card_count"]
    ):
        raise IntakeInvalid("Intake count mismatch")
    return {"samples": len(rows), "status": "prepared_only", "release_eligible": False}


def check_sample(row, manifest):
    expected_id = stable_hash(
        [FORMAT, row["project_id"], row["audit_id"], row["response_id"]]
    )
    expected_hash = stable_hash(
        {key: value for key, value in row.items() if key != "input_hash"}
    )
    if (
        row["schema"] != FORMAT
        or row["sample_id"] != expected_id
        or row["input_hash"] != expected_hash
        or row["project_id"] != manifest["project_id"]
        or row["audit_id"] not in manifest["audit_ids"]
        or digest(row["answer_text"].encode()) != row["answer_sha256"]
        or row["model_analysis_status"] != "not_executed_by_intake"
    ):
        raise IntakeInvalid("Intake sample scope or hash mismatch")


async def cli():
    parser = argparse.ArgumentParser(
        description="Read-only preparation of the latest one or two project audits"
    )
    parser.add_argument("--audit-ids", nargs="+", type=int, required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.config import settings

        engine = create_async_engine(
            settings.database_url,
            hide_parameters=True,
            connect_args={"connect_timeout": 5, "charset": "utf8mb4"},
        )
        try:
            if engine.dialect.name != "mysql":
                raise IntakeInvalid("CLI intake requires a read-only MySQL session")
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="REPEATABLE READ")
                await conn.exec_driver_sql("START TRANSACTION READ ONLY")
                manifest, samples = await collect(
                    conn,
                    args.audit_ids,
                    project_id=args.project_id,
                    runtime={
                        "enabled": settings.intelligence_enabled,
                        "bucket_configured": bool(settings.intelligence_oss_bucket),
                        "role_configured": bool(settings.intelligence_oss_role),
                    },
                )
                await conn.rollback()
            print(json.dumps(publish(manifest, samples, args.output)))
        finally:
            await engine.dispose()
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "Intake preparation failed; inspect private scope and runtime configuration",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(cli()))
