# EvidenceBrief v3：确切行动修订与我方事实

本轮接入E02，不新增数据库表或历史兼容路径。所有开发和验证在本地工作树进行，未部署。

## 接口

- `GET /api/intelligence/actions/{id}/eligible-facts?expected_revision=N&offset=0&limit=100`：共同事实资格投影，包含 revision_id、projection_hash、total、next_offset。必须完整读取同一投影。列出同一自有品牌候选及原选项；原选失效项可移除，不会泄露其他项目的事实内容。资格不等于任务适用性的自动证明。
- `GET /api/intelligence/actions/{id}/draft-approvals?expected_revision=N`：只读返回当前行动修订、latest_version、最新授权编号和是否绑定当前修订；authorization_state始终not_verified，不能用此元数据代替实时验证。
- 现有草稿授权POST新增必需 `expectedRevision` 和严格布尔 `factsScopeReviewed=true`。factIds必须与当前批准TaskPack的选择完全一致；事实更改必须先创建行动修订并重批。expectedVersion仍对授权版本做CAS。
- 现有Brief导出返回唯一schemaVersion3。HMAC绑定授权ID/版本、具名主体、理由、期限和完整载荷；当前行动修订、计划批准及事实重新复验。仅具备内容编辑角色者可以正式授权。

## 数据合同

`brief_contracts.py` 保存 `actionRevision / taskPack / aiTargets / diagnosticContext / ownFacts / approval`。每个目标保留原始问题hash、完整问题、平台、场景、评价维度，以及逐次原回答及未规范化的冻结条件。任务包保留交付物、提纲、受众、渠道依据、验收、材料缺口和诊断EvidenceRef。

我方事实只接受已批准、当前版本且在有效期内的所属品牌事实；来源是共享FactSource双分支。竞争观察、假设和反证与事实分字段。任务的事实ID和顺序必须完全匹配，跨项目/品牌、失效、篡改或调查/渠道拓展任务均不能生成宣传内容。限制2 MB，不裁剪后续问题。结构Schema本身不授予内容或发布权限。

`fact_qualification.py` 被事实列表、行动依赖、事实选择和内容授权共用。相关身份、审核、来源快照及当前材料版本变化会失效；不因无关品牌、偏好或另一个新Run撤销旧批准。内容使用时实际读入材料，引用原文校验压缩对象、原文哈希和Unicode范围。存储暂不可用返回服务错误并阻断模型请求。

## 三方同步

`backend/contracts/content-evidence-brief-v3.schema.json` 与Portal/ContentOS同名文件逐字节一致，fixture来自实际本地导出后设置测试期限/签名，仅用于结构验证，不能作为实时授权。接收端能力版本为3；Portal先检查版本/渠道再预占额度；ContentOS仅按实时授权编号取Brief、校验完整快照哈希并消费ownFacts。正式发布必须接收端先就绪，再协调审计API/worker和门户；本轮不发布。

## 验证边界

新增共同资格篡改/范围、只读查询、具名确认、历史修订、无关上下文、跨语言黄金合同、超过五个原问题、实际引用对象完整性和真实MySQL并发CAS检查。最终结果见Portal同日review记录；Q01独立语义与Q02真实客户交付/效果仍未验收。
