# 引用情报基础链路：本地运行与首次启用

截至 2026-10-17，当前推进到第三十二批，整体方案仍未完成。代码在本地分支 codex/citation-intelligence。本地 Docker MySQL 新库已完成真实迁移、并发测试及 API/worker 容器联调；OSS Bucket、专用角色与 ECS 绑定已配置，容器内 IMDSv2 临时凭证通路已验证。真实 OSS 对象读写、GitHub Actions 镜像发布、真实设备与质量验收、生产切换仍待执行。

新镜像和独立联调环境见 [镜像发布说明](citation-intelligence-image-release.md)，本地数据库见 [本地 MySQL 说明](citation-intelligence-local-mysql.md)。下文保留各批实现与验证记录；早期批次的“未执行”描述代表当时状态，以后续记录和本段当前状态为准。

## 初始实现（第六批）

- 新项目作用域模型：冻结分析Run、持久AnalysisJob、引用观测、来源文章/抓取/快照、全文段落、纯文本语义缓存、每个owner的分析应用、候选实体与原文提及。
- 独立worker：DB条件认领、租约token/version、心跳、过期收敛、提交/检查点事务内fencing及提交前再次检查时效。网络和模型请求在事务外执行。
- 采集完成的最终状态提交时，启用条件下同时登记Run和任务；HTTP查询可以单独触发，返回202并保持幂等。
- 原文：公网IP实际连接固定、原域名TLS与Host、逐跳重定向验证、禁代理/凭证/非公网目的地、字节和时间限制；只支持静态HTML/纯文本，完整文章质量保守为unknown。
- 本地开发不可变文件，以及私有OSS adapter。正文与原始材料压缩保存、实际字节SHA-256验证，ETag不当作正文hash。补采创建新修订，不更改旧Run。
- 全量分段、未知实体发现、逐回答去重和逐平台计数。来源提及与回答提及分列。模型输出只能使用原文逐字片段；无模型或额度不足明确deferred。请求数量上限在DB预占，重试不重置；这不是完整金额/usage结算系统。
- Portal新 `/intelligence` 页面与受权限/订阅约束的BFF：分析总览、平台覆盖/同题回答、全部候选实体、来源文章、证据片段及安全原文分页。

## 首次运行条件

1. 使用经过明确指定的新空数据库。`backend/alembic.ini`已改为`alembic/schema`的静态完整基线（53表，当前另有worker心跳扩展迁移，head共54表）；旧revision文件仍在`alembic/versions`存档，但不再是活动迁移历史。不能对既有数据库运行此基线、stamp伪装通过或删除旧库。
2. 单执行器运行 `python -m alembic upgrade head`，然后启动API。API/worker只检查revision，不自动建表/升级。目标环境需要为新数据库建立账号及备份；本地专用空库已迁移，生产新库尚未配置。
3. OSS：乌兰察布、标准ZRS、私有ACL、同地域内网Endpoint。当前adapter使用forbid-overwrite，因此Bucket必须从未开启版本控制；开启/暂停版本控制会让该Header失效，worker拒绝启动。未来支持version_id绑定后才能选择版本控制恢复。应用快照自身仍是不可变版本。
4. 运行角色需对象PUT/GET/HEAD与Bucket ACL/版本状态读取权限；使用ECS角色临时凭证，SDK强制IMDSv2并禁用v1回退。主机全局元数据策略与SDK配置分别核验，当前未修改主机的v1兼容策略。抓取worker的任意外链出口仍不能访问元数据；分析进程与抓取受信出口分离需要实际环境验收。
5. 默认 `AISCOPE_INTELLIGENCE_ENABLED=0`。M0的真实OSS/内网/角色和MySQL验收未通过前不启用，不静默回退本地盘。

配置名称（不含凭证）：

```text
AISCOPE_INTELLIGENCE_ENABLED=1
AISCOPE_INTELLIGENCE_ENVIRONMENT=production
AISCOPE_INTELLIGENCE_BLOB_BACKEND=oss
AISCOPE_INTELLIGENCE_OSS_BUCKET=<实际私有Bucket>
AISCOPE_INTELLIGENCE_OSS_ROLE=<实际ECS角色>
AISCOPE_INTELLIGENCE_OSS_ENDPOINT=https://oss-cn-wulanchabu-internal.aliyuncs.com
AISCOPE_INTELLIGENCE_MODEL_REQUEST_LIMIT=500
AISCOPE_INTELLIGENCE_SOURCE_BYTE_LIMIT=4194304
```

本地开发改environment=development及blob_backend=local，blob_root指定开发目录。不要用production环境启用LocalBlobStore。

API按既有uvicorn入口启动；独立worker在backend目录运行：

```sh
python -m app.intelligence.worker
```

SDK依赖锁定为oss2==2.19.1与alibabacloud_credentials==1.0.12。尚未执行真实云端PUT/GET测试。部署必须经过本地源检查、PR和CI，不能直接在服务器修改源代码。本批没有修改生产Compose、前端蓝绿流程或ContentOS。

## 验证与已发现问题

新增SQLite故障测试不替代真实MySQL并发。`tests/test_intelligence_mysql.py`仅允许显式配置的本地intelligence_ci专用库；CI建立MySQL8.0服务，执行真实空库迁移和八worker争抢单任务。新增backend-ci workflow同时运行全套回归及构建API/worker共用镜像；尚未在GitHub执行。

后端已有16项测试在改动前也失败，已用HEAD代码归档复跑确认：旧认证字段/表单格式和已移除本地项目CRUD。测试已改为当前契约与Portal项目作用域检查，认证实现没有改动。测试默认清空模型API密钥，避免开发.env的付费凭证进入回归；需要模型的mock测试显式注入假密钥。

本机Docker引擎不可用，因此未执行MySQL实库和镜像运行检查。静态Alembic SQL生成可以检查DDL输出，但不等于真实迁移通过。

## 继续实施

当前链路尚不是上线完成状态，也没有切掉原分析页面。新功能默认未启用；后续整体切换时移除旧分析消费者，不提供历史兼容或回填。

1. M0：真实主平台capture-v2 fixture、100组标注、OSS/IAM/内网和MySQL验收、正文完整性规则及实际成本。
2. M1：独立AuditSample/PromptVersion/attempt及设备原子绑定、全局ResourceLease与完整金额预占/不确定usage结算；来源逐文任务、刷新事件/节流、BlobObject引用与GC租约、保留/恢复及schema检查运维。
3. M2：受控渲染/PDF处理，实体别名/身份消歧/merge/split；ClaimEvidence支持与冲突核验；真正同提示词版本/条件可比的逐平台诊断和建议。当前统计分平台，尚不能把差异宣称为平台偏好或因果。
4. M3：ProjectBrand角色单写、context outbox、行动审批签名、ContentOS证据Brief-v2和能力分流。
5. M4：固定cohort复测、资产登记、完整8页及既有仪表盘/报告消费者整体切换、人工评测和恢复演练。

没有因为接口存在而把全部17项任务标记完成；详细状态记录在Portal工程任务清单与实施进度文档。

## 第二批补充（2026-10-14）

已接入PromptVersion/AuditSample/独立attempt、capture-v2、来源逐文任务及durable刷新事件、ClaimEvidence待人工核验和Portal采集表单。创建入口POST /api/intelligence/projects/{project_id}/audits；进度GET /api/intelligence/audits/{audit_id}/capture；正文重试POST /api/intelligence/sources/{document_id}/retry。所有写入校验项目和角色，重试保持请求编号幂等。

默认AISCOPE_INTELLIGENCE_REFRESH_DEBOUNCE_SECONDS=60，AISCOPE_INTELLIGENCE_AUTO_REFRESH_LIMIT=3（每审计24小时）。达到上限后待刷新任务保留到下一窗口，不丢失事件。刷新会为新URL派发初次fetch_document任务；同审计同文章只初派一次。手动补采使用独立编号，完成后仍遵循刷新窗口。

采集条件未知必须保持unknown。当前web入口限qwen，app入口允许五个平台；这些入口不等于真实capture-v2设备验收已完成。新契约由网关白名单格式化，任何cookie、凭证、请求条件都不自动提升为观察事实。平台差异暂按提示词版本和实际条件分组描述，不能声称具有统计意义或平台偏好。

观点核验只连接同回答同平台的实际answer_reference，精确URL的搜索候选可以抓取背景全文但不能冒充引用证据。最多选择5个候选段落，未覆盖部分及预算不足要保留限制；模型判定均needs_review。当前没有审批或自动执行内容修改。Portal审计创建额度可同请求编号重放收敛，主动后台对账未完成；ContentOS对账已限定content_generation。

前述继续实施清单的M1/M2已部分推进，任务状态仍in_progress；未完成项与最终本地验证以Portal实施进度文档为准。

## 第三批补充（2026-10-14）

POST /api/intelligence/runs/{run_id}/identity-decisions接收候选确认/排除、正式名称、理由、expectedVersion和idempotencyKey，操作人必须从实名JWT sub取得。项目写入权限、实体在Run中的原文提及、已发布ready/partial状态均校验。追加事件，不修改旧Run；entity_resolutions冻结于后续Run，变更触发durable刷新。GET Run返回独立identity_reviews供复核版本对照；固定summary仍使用自己的冻结决定。候选级复核不替代按提及消歧或正式品牌注册。

平台规则诊断按提示词版本、平台、实际条件和入口分组，绑定本组证据。采集缺口、候选身份、负面对象、实际引用冲突、引用集中度生成待核实任务和材料要求；状态proposed/needs_review，不能当成批准执行。诊断规则版本与语义signature绑定，当前PIPELINE_VERSION=citation-intelligence-3。尚未接入实体alias/merge/split、BrandFact、ProjectBrand/context outbox、Action审批/Brief/cohort。

静态完整基线39表，SQL生成通过；仍待明确新库和真实MySQL环境验收。最终第三批本地检查以Portal实施进度和test-results/intelligence-batch3-full.txt为准。

## 第四批补充（2026-10-14）

PlatformDiagnosis/ActionPlan/ActionApproval持久化，基线42表。GET /api/intelligence/runs/{id}/actions查询本Run计划及实时stale；POST同路径从diagnosisId创建计划；POST /api/intelligence/actions/{id}/approve批准开展investigation_plan，需expectedRevision、理由、idempotencyKey及实名JWT sub。固定计划由服务器诊断记录生成，不接受浏览器平台/证据覆盖。

审批签名使用运行环境已有secret_key的HMAC-SHA256，不输出密钥。绑定行动任务/目标/修订、输入hash与模型规则signature、最新Run/未处理输入和实体映射、操作人及理由。密钥轮换会使旧签名无效；应重新复核，不静默重签。新输入或模型配置变化返回stale。没有内容发布副作用，publication_allowed恒false；正式Brief消费必须另外接入人工观点与事实复核，当前不可执行。

本批计划每个诊断一个目标；资产归并/ActionTarget子表、编辑修订、批准事实与正式内容、ContentOS Brief/cohort仍待实施。PIPELINE_VERSION=citation-intelligence-4。实际MySQL/OSS/设备验收仍未通过，不能启用生产新链路。

## 第五批补充（2026-10-14）

ClaimReview/BrandFact追加记录，静态基线44表。POST /runs/{id}/evidence/{evidence_id}/review记录人工关系判定，expectedVersion及idempotencyKey控制并发/重放；快照、文章、回答和平台来源链及逐字quote严格检查。GET Run另返回claim_reviews，不覆盖模型判定。

POST /runs/{id}/facts批准最新人工supports来源片段；需确认实体、适用scope、带时区validFrom/validUntil及理由。服务器陈述来自逐字原文，模型判定不能直接进入事实。GET /projects/{id}/facts显示最新有效状态；POST /facts/{id}/withdraw追加撤回版本。事实到期、review/identity/source依据变化不能继续计为有效，旧版本仍保留。人工判断没有替代来源真实性/完整性验收。

新Run冻结approved_facts；行动stamp纳入人工review及effective facts（包括过期/撤回），审批随材料变化失效。PIPELINE_VERSION=citation-intelligence-5。尚无正式发布授权或ContentOS调用，publication_allowed仍false。真实MySQL/OSS/设备验收仍未完成，默认关闭新链路。

## 第六批补充（2026-10-14）

POST /actions/{id}/draft-approvals批准content_draft材料；owner/admin/editor及实名JWT、有效调查计划、当前平台引用范围内的有效事实均校验。GET /draft-approvals/{id}/brief实时检查HMAC、截止、最新授权版本、Plan与事实stale，返回EvidenceBriefV2。采集/身份任务不能转正式草稿Brief。静态基线45表，expires_at采用MySQLDATETIME(fsp=6)防止签名时间精度漂移。

Portal传ContentOS自己的content受众JWT及独立visibility受众JWT；ContentOS POST /api/evidence-briefs/preview先认证项目，再用临时X-Visibility-Authorization向智见重读授权，不信任浏览器材料。CONTENT_EVIDENCE_BRIEF_ENABLED默认非true关闭；VISIBILITY_SERVICE_URL应为受信服务地址。准备前时间/范围/schema/能力全部复核，无静默V1降级。网站等仅manual_task，publicationAllowed固定false。临时JWT不写数据库/日志/契约。

本批只有材料授权和准备生成输入，无模型请求、持久workflow或发布。后续持久创建、usage未知对账、真正生成前复核和cohort仍必需。

## 第八批补充：固定样本复测（2026-10-14）

新增四个持久模型，静态新空库基线49表。本批没有执行真实数据库迁移；已有部署必须整体采用明确的新空库安装方案，不能仅在旧库stamp新基线。前一批ContentOS持久草稿工作流的实际代码/门禁详见Portal实施进度与ContentOS docs/evidence-brief-v2-runtime.md。

API：GET/POST /projects/{id}/cohorts；POST /cohorts/{id}/retests；GET同路径读取关联审计与最新已发布分析；GET/POST /cohorts/{id}/results；GET/POST /projects/{id}/assets（均在/api/intelligence前缀）。所有查询验证项目，写入要求writer、实名sub及intelligence_enabled。CohortRequest只允许baselineRunId/title/entityIds/idempotencyKey；RetestRequest只有idempotencyKey；CompareRequest只有followupRunId。资产只接已发布URL、channel/contentHash/publishedAt/actionId/reason/idempotencyKey，禁止caller actor/publish authorization。

cohort冻结已有capture-v2审计的全部计划槽位，独立PromptVersion版本和实体名单；最多1,000槽位、20个实体。重新调度直接使用旧PromptVersion，而非当前可编辑Prompt；实际版本全文hash改变时409。复测创建新审计，采集重试沿用独立轮次，不扩大分母。同一cohort+请求编号重复回放同一审计。创建HTTP请求只安排持久采集，不调用设备或模型。

首批统计版本paired-answer-presence-1。按独立回答计数，以本Run的所有分段覆盖完整正文且状态completed为依据，来源正文提及不能增加回答提及率。基线/复测按prompt_version_id/platform/surface/sample_index匹配，实际已知searchMode/modelVersion/accountClass/location必须一致，分析signature与冻结实体映射必须一致。缺失/重复/不完整/条件unknown/不晚于基线/变更输入等均记录included或excluded样本及回答IDs。两期同audit补采修订直接422，不能当效果复测。

至少20个共同问题簇且每题至少3轮才输出比例差和2,000次问题簇重采样95%区间，否则insufficient、descriptive_only或undetermined，difference/interval为null。这是试点展示门槛，需真实样本功效校准；即使区间不含0也不声明内容行动造成变化。不同平台独立输出，没有自动重归一化的总分。ROI/点击/转化保持unknown。

MeasurementResult冻结cohort specification hash、baseline/followup Run IDs与input hashes及comparison signature，重复查询/计算保留原结果，不能用补采覆盖。若需要不同口径分析，创建新的已发布Run再比较；自动同口径重分析尚未实现。PublishedAsset仅是human_reported记录，不自动访问或发布链接、不修改行动审批/稿件状态，未来的发布授权与实验时间窗关联需独立实现。

本批针对性18 passed；完整429 passed、4 skipped（含2项未运行真实MySQL测试）、36 warnings。Ruff/compileall、49表静态SQL生成通过。默认关闭、无实际OSS/设备/数据库操作；真实MySQL并发/镜像仍等待CI和资源验收。

## 第十批补充：实体级别名归并

新增EntityAliasEvent，静态新空库基线50表，PIPELINE_VERSION=citation-intelligence-6。POST /api/intelligence/runs/{id}/aliases只接受sourceEntityId/targetEntityId（merge时）/decision(merge或unlink)/reason/expectedVersion/idempotencyKey，实名项目writer且intelligence_enabled；GET /projects/{id}/aliases只读取当前项目最新映射。没有修改EntityMention/IntelligenceEntity身份哈希或旧Run，解除也不删历史。

原始名称必须在请求Run真实出现，目标为本项目已确认同类型根实体；禁止循环、隐藏redirect目标和过长链。审计先锁、项目实体按ID顺序锁，版本/唯一键保护并发；相同请求回放，其他内容同键409。当前真实MySQL并发仍待CI，SQLite测试不证明行锁行为。

prepare_run冻结entity_aliases，build_summary应用冻结且根身份confirmed的映射，按回答ID集合合并，不按提及次数相加。每条summary mention保留observed_entity_id/name及原quote，entity row保留成员ID/原名；自有/竞品角色冲突标明role_conflict且不指定单一角色。模型原始候选、来源片段和cache应用始终保留。

实时alias snapshot不等于Run冻结快照时，行动审批/草稿材料失效，批准事实需要刷新后的Run，原事实转needs_review；没有放宽事实逐字引文/主体约束。复测specification加入alias signature，两期映射不同没有可比效果输出。解链或修改理由也产生新版本，保守要求重新复核。

当前仅刷新被操作Run的审计，其他审计审批会stale但不会自动批量收费重分析。局部同名词/提及级消歧和split尚未实现，无法判断整体别名对应时应保留候选、不要整体归并。ProjectBrand角色单写方/context/outbox同步也尚未实现。

本批6项新针对性、38项已有事实/审批/复测集成检查通过；最终全量回归与Portal门禁见第十批检查点。未执行真实数据库迁移、模型、设备、云资源或部署操作。


## 逐片段归属（第十一批）

GET projects/{project_id}/mention-bindings返回项目当前追加决定快照；POST runs/{run_id}/mention-bindings接受mentionId、assign/reset、assign时targetEntityId、reason、expectedVersion、idempotencyKey。writer实名操作，Run须ready/partial。原始mention与片段owner必须属于该项目和Run冻结manifest；逐字quote/全局offset由服务端核验，不接受用户替代引文。目标仅限当前已确认同类型根实体。

MentionBindingEvent绑定segment ID/text/start、原始实体和quote/span指纹，追加历史可回放；新回应不同segment ID不会继承旧片段归属。prepare_run冻结mention_bindings，build_summary先采用具体归属、否则整体alias；reset恢复自动归属。旧Run/raw mentions不重写。行动与事实当前性、复测口径签名包含该映射；PIPELINE_VERSION=citation-intelligence-7。MySQL基线51表，并添加mentions(project_id,id)唯一索引供项目复合FK引用；尚未实际迁移或真实并发验证。

暂不创建新拆分主体、不修改模型原始类型、不排除片段计数、不授予事实或发布权限。不同类型目标必须先完成后续身份纠错流程。真实设备、OSS、新库与完整端到端验收仍待环境准备；不继承历史兼容要求。


## 独立同名主体创建（第十二批）

POST runs/{run_id}/mention-bindings新增decision=split、newEntityName；不能传targetEntityId。已核验该Run/project/manifest片段与逐字位置后，在同一事务创建同类型独立主体、初始实名confirmed EntityResolutionEvent及split MentionBindingEvent。名称须非空白、不同于任何已有实体名或已确认规范名；已存在主体请assign，不再创建。请求key重复回放原主体，改名同键409。没有竞品注册、事实批准、额度消耗或发布授权。

split_snapshot从持久化split事件关联原始mention得到source_entity_id/target_entity_id谱系，reset不删除历史主体与谱系。prepare_run冻结entity_splits，summary单个split提及适用该身份，原始observed_entity_id/name/quote保持不变。identity复核接受该Run冻结assign/split关系下的目标，可排除并再次确认，不改写原始模型归属。

已拆分源候选的其他未人工分配提及标记identity_ambiguity，进入平台identity_review而不生成归属不明的negative_observation。固定复测保守排除此回答，排除理由ambiguous_identity；来源-only提及歧义不会污染回答覆盖。alias_signature包含entity_aliases、mention_bindings、entity_splits；pipeline版本citation-intelligence-8。基线仍51表，无新schema改变或实际数据库操作。

尚不支持跨类型身份纠错、局部排除、实体层级或自动推广片段归属；新回答须逐片段核对。确认身份只表达实名用户的归属判定，不替代事实支持证据/人工事实复核，也不声明普遍真实性或品牌优化因果。


## 已注册品牌上下文（第十三批）

GET/POST projects/{project_id}/brand-context使用project-scoped Portal JWT；POST须writer、namedsub、intelligenceenabled。ContextRequest只接受version>=1和最多200条distinctID品牌快照，空列表表示不再使用任何已注册角色。后端独立ProjectContextHead锁定版本，首次头创建使用savepoint+唯一键，事件append-only；同version同hash回放，同version不同hash/版本间隙409，旧事件回放不改变头。规范品牌列表按id排序，stable_hash规范JSON keys排序/UTF8/no空格，与Portal有固定测试值。

Portal ProjectIntelligenceContext与ProjectContextOutbox保存事务配置/创建人/版本/完整快照/确认时间；按顺序最多20项显式投递，当前项目权限JWT即时生成且不持久化。回执version/hash完全匹配才ack；未知状态保留可重试。源配置读取Portal已有ProjectBrand关联/未删除Brand名称别名isCompetitor，浏览器不能提供注册品牌或actor。没有新增品牌注册或额度消耗。

prepare_run冻结brand_context与brands；行动/事实当前性及复测signature包括当前已接收配置。没有已接收配置时brand_context=null、仅使用原采集品牌快照，暂未强制所有旧消费者改用新配置。配置修改不自动付费重分析，应显式创建新修订。pipeline version citation-intelligence-9；静态新库基线53表，未实际执行。

本批并非完整T09：项目专属角色/实体绑定、品牌CRUD和关联增删自动outbox、后台cron/reconciliation、注册事件inbox与实际额度行为仍待后续。当前仅手动确认最新注册配置并重试持久任务。实际PostgreSQL/MySQL并发、应用迁移和端到端验收未运行。


## 项目品牌身份绑定（第十六批）

ContextBrand新增analysis_role=registered/comparison、entity_id、identity_version（后两者成对且版本>=1）。Portal数据来自当前项目Brand关联及实名review事件，不接受浏览器角色登记权限或身份版本。GETprojects/{project}/entities/{entity}/binding-proof只返回项目内当前已确认brand/organization/product根身份id/version/name；person/other或已归并/排除目标拒绝。Portal存储绑定与outbox、receipt同事务，registered仍由Brand.isCompetitor与全局注册额度分类，comparison仅改变项目内分析，原注册额度保留。

接收上下文校验目标在该项目且存在指定版本confirmed身份事件；即使之后身份排除或改名，旧队列快照仍可幂等依序接收，不能把FIFO卡在已失效凭据。新Run的有效角色同时要求当前冻结identity决策confirmed、version完全一致、根未重定向，才采用显式entity_id指定的own/competitor/comparison；名称和别名不自动赋予角色。同名的其他候选继续保留，不被错误归为已注册主体。原context=null采集快照路径暂保留待消费者迁移。

pipeline版本citation-intelligence-10，JSON快照扩展无需新后端表，空库基线仍53表。新角色配置/identity变化继续使旧事实、建议与材料失效，复测口径签名已包含brand_context。没有事实自动批准或发布授权。实际数据库/HTTP/模型/设备联调尚未进行，不能视为生产验收。

## 来源提及复测（第十七批）

measurement metric_version=paired-source-presence-2。原answer_presence之外，source_metrics提供cited_card_presence（answer_reference卡片）、cited_archive_presence（answer_reference保存正文）、candidate_card_presence（search_candidate卡片）。按entity/response计一次，不按文章数量累加，搜索候选不能进入实际引用。每项独立计算completed/hits/unusable_reasons，再复用固定cohort、平台、轮次、条件、身份口径及配对问题簇bootstrap；不同指标不能共用分母或沿用回答指标的比较状态。

完整来源面板须status=complete、capturedCount等于冻结来源数，expectedCount若提供也须一致，来源ID唯一且platform匹配，unknown/named_inference类别排除。卡片必须连续全段完成且与冻结title/snippet逐字相同；正文另须精确URL、captured状态、对应document/snapshot、integrity_status=verified与全段文本UTF8 SHA256匹配。缺失或歧义均未知并排除；已确认完整面板内没有相应类别来源时为观察零。正文指标只覆盖已保存文本，原文章完整度unknown不能被推断为全文采集完成，也不证明模型使用了正文内容。

platform.source_populations按指标保留全量共同样本/排除与问题簇一次，metric保留全量paired_positive_response_ids及每阶段最多20条实体证据示例；仅证据展示有界，分子/分母计算不截断。traces包含citation/snapshot/owner/segment/mention/span及article_completeness，必须使用对应基线或复测Run的项目鉴权内容接口回查。完整原始提及仍在EntityMention。区间为探索性逐项95%区间，未作多重检验校正；无因果/收益承诺。无需schema迁移，pipeline版本仍10。

## 在线报告（第十八批）

GET /api/intelligence/audits/{audit_id}/report只读，project-scoped审计归属与功能开关检查。schema=citation-report-1，audit_id/project_id固定来自数据库。按revision降序选最新Run，不限历史审计总数，不自动转换/回填旧报告或采集。无Run返回needs_analysis；queued/running/failed返回该修订状态，不回退较旧成功结果。

ready/partial通过actions.stamp的当前signature/inputrevision/context/identity/alias/mention绑定核验；缺state或不一致返回stale但保留历史观察。run_snapshot提供分平台回答及三个来源提及指标，各自included/excluded/实体分子分母，不把不同指标样本合并；回答不完整或分析未全覆盖明确排除。报告不复用旧QueryResult得分、不输出置信区间/跨审计变化或因果主张。正文仅指已保存文本，文章全文完整度不可推断。

报告保留全部观察实体及冻结角色、diagnoses待复核任务、claim_evidence模型判定与最新人工review分别展示、来源分类/保存状态和回答元数据。完整原文仍从Run内容分页接口读取，报告不暴露存储key、运行凭据或原始HTML。Portal按报告订阅级别裁剪详情，pending context outbox覆盖stale；旧PDF尚未接入时409，不能回落旧报告。无新表/迁移，pipeline版本仍10。近期测试数据按用户范围只需1–2次，不对更早测试审计回填，实际资源和新契约采集仍需联调。

## 指定修订 PDF（第十九批）

GET /api/intelligence/audits/{auditId}/report/pdf?run_id={32hex}&detail=basic|full，默认basic。与在线报告同一个audit_report投影，拒绝非当前Run、缺分析/排队/失败/stale；排版后expire旧session状态并重读当前修订/签名/配置/身份，变化返回409。detail由Portal按auditReport capability设置，用户经Portal不能提权；分析服务仍必须置于既有Portal JWT可信服务边界，订阅额度由Portal负责。

reportlab==4.4.9是新增依赖，不依赖WeasyPrint/GTK。A4/STSong-Light CID中文、页码、原始文字转义，禁止外部资源/HTML执行，完整正文仍从精确Run内容接口回查。每进程2个排版槽位，序列化报告超过100万字符422而非截断；其他渲染异常通用503且不记录来源段落。返回application/pdf、attachment、no-store和X-Report-Run-Id。中文CID阅读器支持需预发布检查；可按后续目标阅读器需求引入可分发的嵌入字体。

Portal导出改为显式POST+runId+Idempotency-Key，GET405。feature=pdf_export预占使用现有Serializable账本，hash绑定项目/审计/Run/用户和服务端detail。接收最大20MiB、PDF类型/修订回执/头尾完整后再核对Portal待同步上下文并commit；生成失败release，commit/release未知状态保留相同key重试。committed重放不重复扣，未生成有效文件不扣，无法保证用户网络已收到下载，浏览器失败时使用原key恢复。没有持久化PDF对象，本批不包含后端导出结果长期缓存或额外对账worker；使用现有账本状态/告警，新增真实故障联调仍需要。

## 最新审计概览（第二十批）

GET /api/intelligence/projects/{projectId}/dashboard，project JWT scope检查。schema=citation-dashboard-1，enabled/processing/latest_audit/report；功能关闭不读取审计，空项目明确null。按Audit.created_at desc、ID desc选最新一条，包含pending/running/failed审计，不回落旧成功审计或按Run创建时间选错旧审计。audit_report继续在同审计内选最新revision并校验口径；输出overview省去sources/evidence/responses/source_states，详细证据仍从正式报告及精确Run内容接口读取。

processing来自最新审计pending/running或同项目active queued/leased/retry_wait任务：analyze相关run_id限定本审计、fetch_document payload.audit_id限定本审计、refresh_run subject_id限定本审计；不读其他项目/审计任务，也不把已失败任务当作还在运行。不产生新工作、旧报告或模型请求。

Portal首页和可见性页改用此概览，auditReport基础/完整裁剪与正式报告共用函数；Portal未同步context覆盖stale，can_capture按writer/contentInsights full/功能启用/无待同步算出，后台POST仍再次授权。每次processing读取完成后10秒再只读刷新，终止/错误/换项目停止，不叠加请求；没有自动付费分析或采集。未知平台分母不记零，固定cohort之外不宣称跨审计提升。单次审计采集只由明确配置的capture-v2 UI发起。无新schema/迁移，pipeline版本仍10。

## 独立分析工作台（第二十一批）

GET /api/intelligence/projects/{projectId}/workspace?audit_id={可选正整数}，project JWT范围校验，schema=citation-workspace-1。未指定时选择最新Audit，包含pending/running/failed，不回退旧成功；指定时强制本项目审计，不存在404。选择器列最近30条，显式选中的更早审计额外保留，audits_truncated说明列表截取，不限制数据保留。功能关闭返回enabled=false显式空状态。

report复用audit_report当前修订/过期判断，processing复用audit_processing同项目同审计任务状态。ready/partial/stale观察投影来自对应immutable Run.summary的mentions/claims/prompt_platforms。observations每项加contexts，answer按response匹配，citation按source ID，snapshot匹配所有关联source，保留platform/response_id/source_id/kind；共享正文不归给首个平台，不把检索候选当作实际引用。没有冻结上下文的观察不返回为已知平台观察。

Portal独立insights/suggestions分别使用contentInsights/optimizationAdvice guard与基础详情白名单，原文观察仅完整insights返回；suggestions完整报告中的证据用于诊断回查。can_act要求writer、视图完整权限、contentInsights full、当前快照及无Portal待同步context；can_review仅完整insights writer且有可回查的完成/部分/过期Run，实际证据写入仍由既有API授权核验。两页不自动生成/扣次，原文仍按精确Run分页纯文本读取。没有新增schema或迁移，pipeline版本仍10。

## 已保存固定复测历史与页面迁移（第二十二批）

GET /api/intelligence/projects/{projectId}/measurement-history?cohort_id={可选32hex}&history_days={1..36500}，project JWT作用域，schema=citation-measurement-history-1。history_days默认365，由Portal可信BFF从订阅trendHistoryDays传入；直接后端API须保持既有服务可信边界。方案与结果created_at分别按UTC since筛选，固定基线作为核对依据不替换为范围内其他Run。默认最新方案，包括无结果新方案；精确方案不在项目/时间范围404。最近50个方案+范围内显式更早选择，最近10条结果，截取标记不代表统计采样截断。关闭功能为enabled=false显式空状态。

读取检查checked_spec版本/hash和每条MeasurementResult对应project、cohort、两个不同审计Run、两端input_hash、冻结specification_hash、metric版本paired-source-presence-2与比较signature；不匹配409，不生成新结果或回填历史。两端分别检查最新revision（不排除排队/失败）和stamp当前配置/签名/身份/绑定/别名，计算snapshot_current，旧数据保留原Run引用。单次读取缓存每个Run的当前性，不跨请求缓存。

Portal趋势BFF要求contentInsights full，服务端历史范围不可由caller参数提升，校验项目/方案/metric版本。pending context或后端非当前时清除各指标difference/interval95后返回，仍保留独立分母/排除/描述值/证据用于回查。UI再次抑制过期变化，不绘制旧综合分折线，不自动启动复测。正文查看按基线或复测各自精确Run，审计报告链接明确为当前报告。

compare/strategic复用既有workspace接口，Portal分别执行competitorComparison和strategicIntelligence订阅，基础详情裁剪和pending上下文规则不变。全部实体按稳定ID区分同名；平台同指标且分母有效一致时输出同次审计描述差距。战略用来源分类/保存状态/证据核验与已有诊断组织工作，不推算域名权威或跨平台总分。无新schema、迁移或模型调用，pipeline版本仍10。

## 正式报告的关联复测与导出一致性（第二十三批）

reporting.audit_report默认include_measurements=True；正式在线报告与PDF添加measurements={scope_run_id,results,truncated}，仅联表项目内MeasurementCohort.baseline_run_id==报告Run或MeasurementResult.followup_run_id==报告Run的已保存记录。取最近10条，保留每条全部独立样本统计；空关联不附加项目最新方案。dashboard和workspace显式include_measurements=False，不拉取无关复测附件。

measurement_history.saved_measurements共享冻结输入核验、distinct audit、版本/hash/signature和两端最新全部状态修订+stamp检查；用于history及report_measurements。deepcopy后仅过期投影清除metric difference/interval95，保存记录不被读取更改。结果保留精确baseline/followup Run、audit、cohort_title、report_role；不自动计算新结果或做历史回填。

Portal按auditReport完整范围返回关联复测，基础白名单删去；验证scope_run_id与每条两个Run至少一个等于报告Run。scopeMeasurements统一处理Portal pending context或报告非当前的过期裁剪。报告和趋势复用同一读取组件，证据按各端Run与owner回查；当前审计报告链接不会冒称冻结修订。

完整report_pdf增加复测内容，保留独立指标的有效/排除样本与已有有限证据索引，说明无因果结论和区间未校正；基础PDF完全省略。PDF API排版前后比较export_scope(report,detail)稳定hash，除Run当前性外检查该授权范围所有报告内容变化；生成中新增附件、复核或配置/状态变化409。基础导出不因未返回的完整附件变化而拒绝。Portal原有PDF完整验证与quota reserve/commit/release保持，无新增表或迁移。

Portal10个旧score/strategy/trend/content-intelligence入口改静态410，不调用本服务或扣次；分析服务的旧内部API及内容工作台的旧建议桥接尚未退役。真实最近1–2次审计仍需新资源联调，不对历史测试数据回填或删除，pipeline版本仍10。

导出并发复核额外保证：排版后await db.rollback()结束本GET只读事务，然后重新get_audit_for_project/audit_report；仅expire_all不足以保证可重复读连接取得新事务快照。路由没有事务内写入。测试断言rollback被await并保留附件变化409验证；真实MySQL隔离级别及并发场景仍需CI隔离库/预发布联调。

## 获批内容材料收件箱（第二十四批）

GET /api/intelligence/projects/{projectId}/content-workbench?cursor={可选32hex}&approval_id={可选32hex}，project JWT作用域。cursor与approval_id互斥422；跨项目/缺失游标或精确审批404。默认每个action仅取最大version的DraftApproval，created_at desc+id desc每页30，limit31探测next_cursor；同时间记录不跳漏，精确旧审批可回查但不能绕过latest验证。schema=citation-content-workbench-1，publication_allowed始终false，关闭功能为空且enabled=false。

收件箱只读，不补state/创建job或Brief。签名不符返回invalid且不展示被篡改标题；已签名材料经过export_brief重新核验事实、来源、角色、期限/上下文，返回ready/stale/expired/invalid。摘要不含facts正文/签名，具体获批事实只在显式ContentOS预览时读取。export_brief不再由state_for_audit创建/加锁状态：读取现有AuditIntelligenceState，缺失409；检查本审计最新全部状态修订等于授权Run且完成/部分完成，新queued也使旧授权不能导出。plan_view可传已读取state，GET链保持无写入。

Portal新BFF要求contentInsights full和内容订阅，校验项目/游标/精确审批/禁止发布标记；未同步context令ready材料stale且执行为空。can_prepare要求启用/writer/无pending，can_manage_tasks要求启用/writer，后端POST仍独立授权和当前性检查。/content工作台和intelligence审批页共用EvidenceDraftExecution，只发送projectId/approvalId/action/幂等键，由ContentOS服务端复取材料。显式预览后再显式创建生成任务或人工任务，不发布；生成仍走既有额度与持久工作流，人工任务取消不被pending context阻断，开始/交付需复验。

Portal旧suggestions GET/POST、suggestions/[id] GET/PATCH/DELETE与briefs/from-suggestion POST改静态410。旧内部分析/建议服务和V1纯辅助代码尚未删除；新工作台无旧建议读取或Brief创建调用。本批无新schema/迁移、真实付费调用或部署，pipeline版本仍10。


## 第25批：运行诊断与独立进程心跳

新增GET /api/intelligence/projects/{project_id}/operations及Portal /intelligence/operations。读取只聚合已有DB记录，无模型、来源抓取、OSS操作、任务认领或租约回收。队列和来源统计仅当前项目；共享worker只提供近期在线/配置/临时盘汇总，不返回主机名、其他项目ID或任务信息。未接入实际费用、OSS流量或联合备份恢复，不将未知当作通过。

新增intelligence_worker_heartbeats，扩展revision intelligence_worker_20261016接在53表基线之后，head共54表。单个已指定执行器完成迁移后再启动相同head的API/worker；应用不自动建表，带DB锁的迁移互斥尚未实现。本批只生成静态MySQL SQL，未执行真实迁移。freeze_schema.py排除扩展表，避免基线重复创建。

独立worker每15秒写DB时间心跳，即使无任务也持续；每60秒检查Bucket私有ACL/禁版本控制配置及系统临时目录所在盘是否剩余256MiB。配置探测10秒超时，心跳独立；查询只接受90秒内idle/working心跳及90秒内探测，停止/过期/未来时钟不计为在线。配置通过不代表对象PUT/GET或SHA完整性，快照状态仅保存的入库记录。启动验证失败退出且仅记录类名；运行中探测失败可见，不自动清理、禁用任务或替代租约fencing。退出取消探测/心跳后台任务，再尽力写stopped；强杀或DB故障只能按心跳过期识别。

完整指标口径及故障步骤见Portal仓库docs/designs/citation-intelligence-operations.md。联合MySQL/OSS备份、引用感知保留、云账单流量、迁移互斥、真实资源/设备/PDF/MySQL并发验收仍未完成。


## 第26批：迁移互斥与后端联合备份包

在线Alembic及数据库导出共用同一MySQL实例/数据库GET_LOCK会话锁（默认60秒）。拿锁后先COMMIT消除隐式读取事务，再运行迁移或REPEATABLE READ导出；操作结束释放，不确定则废弃连接。离线SQL不连接数据库，API/worker仍只校验head。该锁不支持跨多主实例的全局互斥，也不能阻止绕过入口的手工DDL。

新增python -m app.intelligence.backup export/verify：从同一快照导出全部54应用表和alembic_version，实际逐键核验并保存被引用的压缩正文对象，SHA去重但不跳过相同SHA的其他key检查。专用数据库表清单、全部InnoDB、head、项目key边界和SHA均校验，失败保留INCOMPLETE，不覆盖或自动清理。类型标签保留原始JSON/Unicode/日期时间/Decimal/bytes，清单/行数/对象SHA可重新核验。输出只含随机ID/数量/状态，完整包必须私有加密保存，不进仓库或日志。

当前能力为后端联合备份包及文件完整性校验，不包含Portal/ContentOS、数据库导入、异地备份调度、保留/恢复窗口策略或恢复演练；restore_test仍not_performed，诊断页不冒充已接入。详见同目录citation-intelligence-backup.md。未执行真实数据库迁移/备份或真实OSS读取；MySQL竞争/快照测试仅在显式隔离CI库运行，本地未配置则跳过。


## 第27批：离线质量评测与独立标注

新增app.intelligence.evaluation/evaluation_export。CLI仅按项目读取最近1–2次审计的最新全部状态修订，完整回答/摘要/正文段落与已保存观点对进入私有标注包，不过滤未登记品牌、不自动付费分析。待处理/失败最新修订不回退旧成功；缺少正文/段落/缓存/deferred均明确计数。段落SHA、项目/Run/签名/文本hash和引用链再次校验。

cases与predictions分文件，双人模板pending/null，不能把模型输出当人工标签。两名不同ID reviewed一致才评语义，分歧/单人/未标注单列；支持关系要求所选quote逐字有效，同题跨平台/轮次/审计/项目按prompt_hash归组防开发/留出泄漏。签名/平台/owner/来源/采集状态/split/origin分层，实体另按kind，保留所有分子分母与missing，空分母不算0或100%。同时输出JSON和中文Markdown及针对实际错误的核查方向。

全部报告release_eligible=false，不能代替实名审核、真实100组留出评测或P0阈值冻结；仅已保存观点对，不能声称观点/候选召回率或消歧/建议可执行性已测。纯合成4例样报告仅验证工具。详细命令及标注/隐私/解释见citation-intelligence-evaluation.md。无新表/迁移或pipeline版本变化，未调用真实DB/OSS/模型/设备，真实内容闭环仍未验收。

## 第29批：最近真实审计输入准备

新增 acceptance_intake 内部 CLI，显式限定同项目最近两次中的一至两次审计，用 MySQL 只读一致性事务投影现有提示词、回答及全部来源卡片。只输出白名单字段，任意原始请求和错误体不复制；原始负载只记录摘要。新版契约匹配平台/回答并通过校验才记录已验证字段，其余搜索/完成状态/卡片归属保持未知。无新 Run、模型请求、正文获取、数据库写入或兼容导入。

已在2026-10-17对 #37/#36 生成本地私有准备包，10条记录/9条非空回答/126张卡片/5个平台，JSON及逐条可读原文哈希复核通过。双人模板仍pending，全部分析/独立审核/业务闭环/发布资格均未通过。现库缺新版schema，开关未启用、Bucket/角色未配置；需明确的新空库和OSS后重新采集验证，不迁移当前旧库。详见citation-intelligence-acceptance-intake.md。

## 2026-10-17：OSS 云资源配置完成

经用户明确确认，在阿里云控制台创建并核对以下资源。此处记录控制台配置，不代表应用已启用或实际对象读写已验证。

- Bucket：`genilink-citation-evidence`；地域 `cn-wulanchabu`；标准同城冗余、私有 ACL、阻止公共访问开启、OSS 托管 AES256、版本控制从未启用。未开启额外日志/备份/HDFS 服务，未设置自动删除。
- ECS RAM 角色：`GenilinkCitationWorkerRole`，信任 `ecs.aliyuncs.com`，已绑定平台 ECS；具体实例与地址保留在私有部署记录中。未操作其他实例。
- 唯一关联自定义策略 `GenilinkCitationEvidenceAccess`：`oss:PutObject`、`oss:GetObject` 仅作用于 `acs:oss:*:*:genilink-citation-evidence/projects/*`；`oss:GetBucketAcl`、`oss:GetBucketVersioning` 仅作用于该 Bucket。本次未授予删除、列表、全量 OSS 或其他云产品管理权限。控制台创建校验错误/安全警告均为零，保存后源代码及角色关联再次核对。
- 实例元数据通道已启用，当前为普通模式和加固模式并存，支持 IMDSv2；仅查看，没有更改全机模式。应用凭证客户端仍要求 IMDSv2，不回退 IMDSv1。容器实际可达性、凭证获取和网络出口隔离尚待运行验证，控制台绑定不等于这些检查通过。

待接入运行环境的非敏感配置如下；功能继续保持关闭，直到明确的新空库、设备及独立 worker 验收通过：

```text
AISCOPE_INTELLIGENCE_ENABLED=0
AISCOPE_INTELLIGENCE_BLOB_BACKEND=oss
AISCOPE_INTELLIGENCE_OSS_BUCKET=genilink-citation-evidence
AISCOPE_INTELLIGENCE_OSS_ROLE=GenilinkCitationWorkerRole
AISCOPE_INTELLIGENCE_OSS_ENDPOINT=https://oss-cn-wulanchabu-internal.aliyuncs.com
```

本次没有修改服务器运行环境、部署源码、迁移数据库或上传业务对象；没有创建或复制 AccessKey。后续通过规定发布流程接入配置，再以角色身份验证私有 ACL/版本状态、不可覆盖写入、回读及 SHA 校验。

## 第30批：Docker 本地空库与真实进程验证

用户启动 Docker 并允许使用本地空库后，新增独立 `compose.intelligence-local.yml`，仅绑定 `127.0.0.1:13316`，独立持久卷，随机口令保存在 git 忽略的私有文件。创建 `intelligence_dev_local` 和隔离的 `intelligence_ci_local`，首次查询均零表；随后两个库均成功应用新版迁移，到 `intelligence_worker_20261016`，54 应用表加版本表。没有启动或修改原有旧 MySQL 容器/卷，没有连接远程库执行迁移。

四项真实 MySQL 专项测试全部通过，补齐先前未执行的并发抢占、迁移互斥及单快照备份检查。另启动独立 API 与 worker 子进程，健康检查数据库 connected，worker 在真实 MySQL 写入 idle 心跳、development_local 存储状态和正常磁盘状态；两进程正常退出，worker 记录 stopped。模型密钥清空，无外部采集/OSS 调用或虚构分析 Run。完整回归结果见检查点，环境操作见 citation-intelligence-local-mysql.md。开发库保留供后续本地验证，CI 库不得承载真实业务数据。

## 第31批：ECS 角色通道验证与存储验收 CLI

只读检查实际平台后端容器，确认 IMDSv2 和目标 ECS 角色临时凭证获取正常，凭证未输出或落盘；现有镜像缺少两个新版 OSS SDK，没有临时安装或修改线上环境。因此实际 OSS API 读写还未验收。

新增 storage_check 操作者 CLI：默认只检查私有 ACL/从未版本化；显式 write-probe 才在固定非业务命名空间创建随机测试对象，检查不可覆盖写入、相同重试、SHA 回读/HEAD、错误 SHA 及覆盖拒绝后原字节保留。报告独占私有创建，已有文件阻止一切调用；失败记录计划 key/可能已写入，错误脱敏。无数据库、模型或对象删除，全部 release_eligible=false。

本地 development/local 实际探针通过；20项针对性检查包括模拟 OSS 请求头/已有 key 处理，不能冒充真实云端结果。详细运行与边界见 citation-intelligence-storage-check.md。下一步依规定流程发布包含 SDK 的新版镜像后验证真实 OSS；设备与正式质量闭环仍未完成。

## 第32批：可发布镜像、独立容器联调与失败恢复

镜像发布与独立运行步骤见 [镜像发布说明](citation-intelligence-image-release.md)。CI 在源码和真实 MySQL 检查后构建一次镜像，断网验证 API/worker/存储 CLI 入口、OSS SDK、唯一 head、开发对象完整性和中文 PDF，然后仅在 main 发布同一完整 SHA 镜像，不自动部署或迁移生产。

本地真实镜像及独立 API/worker 容器已验证：连接专用开发 MySQL，API 健康，worker idle 心跳、development_local 和磁盘状态正常，开发对象探针通过，停止后记录 stopped、退出码 0。没有模型调用、外部来源抓取、云端对象写入或生产切换。

PDF 渲染任务被取消时保留实际线程的并发许可，线程完成才释放，后台异常也被回收。新增租约丢失/取消、schema 缺失/过期只读拒绝、重启恢复不干扰 SamplingAudit、来源流式体积/编码/重定向限制及线程并发回归。

最终失败的分析可通过已有 writer 手动分析 POST 恢复：只重置同一最新失败 Run 的终止 analyze_run 任务，输入/修订不变，使用计数与预算保留，租约版本递增，失败历史追加，抓取任务不重置。默认 prepare_run 和自动刷新不恢复耗尽任务。恢复记录不完整、任务正在被其他事务处理或 Run 已被新修订替代时返回 409，刷新后重试。API 授权只读阶段结束旧事务，再进入审计锁内读取，避免 MySQL REPEATABLE READ 的旧快照重复触发或恢复过期 Run；两个真实双连接回归验证该边界。

整体产品仍未全部完成；本地对象探针、源码回归或镜像检查均不能代替真实 OSS、设备采集、独立质量和跨系统恢复验收。
