# 价值分析 v3：空库初始化与本地验证

本分支直接建立最新结构，当前 schema identity 为 `citation_execution_20261026`，共 60 张业务表。不升级旧业务库、不复制历史审计、不提供旧镜像访问新结构的兼容层。下列 2026-10-24 本地验证记录对应首轮 58 表基线；执行扩展验证见 [E03 实现记录](citation-value-execution-20261026.md)，本页不据此宣称已切换生产。

## 结构边界

- 最新静态基线位于 `backend/alembic/value_schema/20261024_value_baseline.py`，覆盖全部 60 张已注册应用表。`alembic.ini` 只启用这一套基线；旧目录保留用于代码考证，不是升级链。
- `ContextAnalysis` 保存语境分析的版本和结果；`OwnedContentAsset` 保存自有材料修订与对象引用；`ActionPlanRevision` 保存任务计划的幂等修订。这些表已有项目隔离外键、修订/幂等唯一约束，业务接口会在后续里程碑接入。
- `BrandFact` 允许“引用复核”和“自有材料修订”两个互斥来源分支。自有材料事实不需要伪造引用、提及实体或复核记录；项目、材料 ID 和版本必须匹配。数据库 CHECK/FK 同时约束。
- 项目上下文增加 `business_preferences`。门户 Prisma JSON 默认 `{}`；新冻结回执将品牌和偏好一同递归规范化并签名，偏好变动产生新上下文版本。业务重要度不修改固定 30/30/20/20 评分规则。
- 来源快照 `quality` 和发布材料 `provenance` 为 JSON。快照的 `body_hash` 校验解压后的文字，`text_sha256` 校验对象存储中的压缩字节，二者不能混用。

## 执行入口

凭证通过运行环境注入，不写进命令、仓库、镜像或日志。在 `backend` 目录执行：

```bash
python -m scripts.initialize_value_database --expected-database <明确指定的空MySQL库名>
python -m scripts.check_intelligence_image
```

初始化器核对实际配置的库名和预期名称，拒绝任何非空旧结构；已是本基线时只读核验表/列后返回 `already_initialized`。基线也在执行 DDL 前拒绝已有应用表。没有 drop、旧库 upgrade 或 stamp 入口。MySQL DDL 可能不能事务回滚；初始化中断时保留现场，不以删除表修复。

本地空库证明时，门户在独立 PostgreSQL 中执行 `npx prisma db push`，再执行 Prisma generation。2026-10-30 发布准备另补加法迁移 `20261030000000_add_business_preferences`，只为门户项目上下文增加默认 `{}` 的 JSONB 字段，保留账号、项目及身份；生产由门户不可变镜像执行 `prisma migrate deploy`，不在服务器运行 `db push`。

仅限显式指定的 loopback 开发/测试库执行 API/worker 证明：

```bash
python -m scripts.check_value_database --expected-database <本地测试库名>
```

该命令验证三张新表读写、来源分支、项目/版本 FK、幂等唯一约束、API lifespan 与 `/api/health`、worker 新心跳。业务样例在事务内回滚；心跳记录保留。模型请求预算为零，不能把该结果解释为语义质量合格。生产环境拒绝执行这个测试。

## 本地验证结果

2026-10-24 使用任务专属容器 `genilink-value-mysql-20261024`（MySQL 8.0，loopback 13316）和 `genilink-value-postgres-20261024`（PostgreSQL 16，loopback 15446），使用专属数据卷，未清理现有容器或卷。

- MySQL 全部 58 张应用表初始化成功；重复初始化只读返回已就绪；12 项约束/API/worker 检查通过，模型调用 0 次。
- PostgreSQL 最新 Prisma 结构初始化成功；真实数据库测试证明偏好更新形成新冻结版本及 durable outbox，跨工作空间访问被拒绝，样例事务回滚。
- PostgreSQL 产生的两份规范化回执在 MySQL 接收端逐份验签、重放幂等验证通过；跨语言字段排序一致。固定合成样例回执为 `75b8c5e3cbd6d5429ec93aabbd3f6b4da0d823d196c51dcd33fa740f7a15475e`。
- 独立 `intelligence_ci_value_20261024` MySQL 测试库的 7 项真实并发、租约、重试、快照备份和部署心跳测试通过。测试库名称限制保持不变。
- 备份枚举全部新表及自有材料对象引用；未采集材料允许尚无对象，已保存的对象必须有同项目路径和有效校验值。缺失对象不会生成可验证成功的备份。

凭证、完整数据回执及原始日志只在本地私有目录保存，不纳入 git。

## 2026-10-30 发布准备

本地部署代码已将 v3 目标固定为独立空库 `intelligence_value_20261030`，新配置存于 `.intelligence-value.env`；原 `.intelligence.env` 和旧库保留。发布初始化使用显式库名保护，active/previous 同时记录镜像、slot 及 env 路径，回退使用前版自己的库和配置，不让旧镜像理解新表。完整流程见[生产部署与回退](citation-intelligence-production-deploy.md)。

本轮源码检查、部署回归、PR/CI、生产配置准备及实际切换仍以发布记录为准，本地实现不等于已部署。Q01/Q02 的独立语义、真实四平台采集、OSS 与客户行动价值闭环仍需验收；不以旧数据搬迁作为前置任务。
