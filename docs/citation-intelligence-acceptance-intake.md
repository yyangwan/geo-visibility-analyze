# 最近审计的真实输入准备

`app.intelligence.acceptance_intake` 是内部操作者工具，用于冻结最近一两次审计已有的提示词、回答和来源卡片，准备人工核对。它不导入旧数据、不创建分析 Run、不执行模型分析，也不验证来源文章归档或证明业务效果。

## 使用

在 backend 目录使用已配置的数据库连接运行，输出必须是尚不存在的私有目录：

```powershell
python -m app.intelligence.acceptance_intake --audit-ids 37 36 --output <新的私有目录>
```

仅支持 MySQL，通过只读 REPEATABLE READ 事务查询。必须显式指定同一项目最近两次审计中的一至两次，可附加 `--project-id` 核对范围。记录超过上限直接失败，不静默截断，不回退更早数据。连接信息、凭证和原始请求参数不输出。

输出包含 README、冻结输入 JSONL、逐条可读原文、两份待标注模板和哈希清单。提示词、回答和卡片在可读文件中按原文代码块展示；不执行其中的 HTML、链接或指令。JSONL 与可读原文均校验 SHA。已有目录不覆盖，失败保留 INCOMPLETE。目录包含私人业务信息，不能提交 git、上传公共附件或写入 CI 日志。

人工从 README 打开各平台原文，分别核对全部实体（包括未登记品牌），独立填写事实和疑点。模板默认 pending/null，不含模型预测；这里的准备模板不能直接当作质量评测的 gold。新版分析运行后，应使用 evaluation_export 重新生成绑定 Run、段落和预测签名的正式评测包。

## 已完成的真实准备

2026-10-17 只读整理审计 #37、#36：5 个平台、10 条记录、9 条非空回答、126 张来源卡片。文件完整性复核通过，状态为 prepared_only。

当前数据不含可验证的新版采集契约。旧卡片类型、搜索模式、模型和采集完成状态不能推断为新版事实，缺失信息保持 unknown。即使新版契约字段校验成功，也不等于验证了设备租约或签封。卡片不是来源文章全文；正文归档检查为 not_performed。

当前数据库未达到新版 schema，功能开关未启用，OSS Bucket 和运行角色未配置。后续需要准备明确的新空数据库并应用新版迁移，配置同地域私有 OSS 和运行角色，启用设备采集与 worker 后重新采集，再执行正式分析、独立标注、内容执行和固定样本复测。不要对当前旧库直接升级，不要伪造成功 Run 或把此次准备包当作已验收报告。

本包 new_analysis_executed、independent_reviews_completed、business_closed_loop_verified、release_eligible 均为 false。它只证明已有输入已冻结并通过文件完整性检查。
