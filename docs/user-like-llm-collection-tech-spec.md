# 真实用户仿真采集技术方案

## 1. 背景

现有平台已经具备“平台适配器 + 审计任务 + 原始响应归档 + 分析结果”的基础能力，但当前链路更偏向“分析工具链”，而不是“真实用户在大模型自有平台里看到的搜索结果留存系统”。

本方案要解决的核心问题是：

1. 尽可能复现大模型自有平台的原生搜索结果。
2. 完整保存一次搜索的全部原始内容，供后续分析复用。
3. 保持搜索提示词采集现状，不把它作为本方案的重点。

## 2. 目标与边界

### 2.1 目标

- 让采集结果更接近豆包、DeepSeek、Kimi、文心一言、通义千问等平台 UI 的真实输出。
- 完整保留回答文本、搜索状态、引用网页、来源元数据、推理过程、token、请求参数和平台原始字段。
- 将“采集/留存”和“分析/聚合”彻底分层。
- 为平台差异保留独立配置，而不是用统一逻辑抹平差异。

### 2.2 不做的事

- 不把搜索提示词采集作为重点。
- 不把原始响应改造成分析型输出。
- 不把分析结果回写到原始采集记录。
- 不追求所有平台返回格式统一。

## 3. 当前系统现状

当前后端已经有以下基础组件：

- `PlatformAdapter` 抽象层，负责按平台查询。
- `PlatformResponse` 归一化结构，包含响应文本、引用、token、模型、结束原因、搜索开关等字段。
- `PlatformResponseRecord` 表，保存单次平台响应。
- `ResponseAnalysis` 表，保存单条响应的分析结果。
- `SourceCitation` 表，保存跨审计的来源聚合。
- `Audit` / `AuditPlatformRun` / `AuditStageRun`，管理一次审计任务的执行状态。

现有架构已经具备雏形，但缺少三个关键能力：

1. 平台配置独立管理。
2. 原始响应完整留存的字段更全。
3. 搜索结果一致性验证和平台原生行为校准机制。

## 4. 总体架构

```text
用户原话 / 手动录入 / AI 生成
          ↓
平台配置选择
          ↓
平台适配器执行查询
          ↓
抓取原始响应 + 标准化结果
          ↓
原始归档层持久化
          ↓
响应解析层抽取结构化信息
          ↓
分析层基于原始数据独立运行
```

### 4.1 核心分层

#### A. 平台配置层

负责描述“这个平台应该如何像真实用户那样被调用”。

#### B. 采集执行层

负责把用户原话送进目标平台，拿到尽量接近 UI 的结果。

#### C. 原始归档层

负责完整保存平台返回的事实，不做业务重写。

#### D. 响应解析层

负责将原始数据解析成可分析结构，解析失败时不影响留存。

#### E. 分析层

负责品牌提及、来源权威、内容结构、战略和竞品分析。

## 5. 平台配置设计

### 5.1 目的

平台配置层不是为了统一平台，而是为了保留平台差异：

- 搜索倾向
- 默认参数
- 引用格式
- 返回字段
- 原生工具调用方式

### 5.2 配置模型

建议配置以 JSON 存储，数据库表名为 `platform_configs`。

```json
{
  "platform": "doubao",
  "search": {
    "mode": "strong",
    "default_enabled": true,
    "forced_search": true
  },
  "parameters": {
    "temperature": 0.3,
    "top_p": 0.9,
    "max_tokens": 2048
  },
  "capture": {
    "citation_patterns": [
      "来源：(.+)",
      "\\[(\\d+)\\]"
    ],
    "preserve_fields": [
      "search_results",
      "source_urls",
      "video_sources",
      "reasoning"
    ]
  }
}
```

### 5.3 配置项说明

| 配置项 | 含义 |
|---|---|
| `search.mode` | `optional` / `strong` / `forced` / `off` |
| `search.default_enabled` | 默认是否启用搜索 |
| `search.forced_search` | 是否强制触发搜索 |
| `parameters.temperature` | 生成温度 |
| `parameters.top_p` | 采样上限 |
| `parameters.max_tokens` | 最大输出长度 |
| `capture.citation_patterns` | 引用提取规则 |
| `capture.preserve_fields` | 平台原始字段白名单 |

### 5.4 平台差异策略

- **DeepSeek**：偏推理，搜索不强制，保留推理和引用链。
- **豆包**：强搜索倾向，保留视频/图文/字节系来源特征。
- **Kimi**：长文本+多轮工具调用，重点保留 search tool 过程。
- **文心一言**：百度系搜索痕迹明显，保留来源结构和引用格式。
- **通义千问**：阿里系内容偏好明显，保留平台偏好的结果排序。

## 6. 采集执行设计

### 6.1 输入策略

采集输入保持用户原话，不做分析型改写。

规则：

- 默认单轮。
- 不补“请输出 JSON”。
- 不补“请列表格”。
- 不用分析型 prompt 代替真实用户表达。

### 6.2 执行方式

平台适配器按平台配置发起查询。对每个平台，适配器负责：

1. 构造请求。
2. 发起调用。
3. 处理平台特有的工具调用/流式/多轮响应。
4. 归一化成 `PlatformResponse`。
5. 同时附带原始响应和原始元数据。

### 6.3 与现有代码的关系

当前 `PlatformAdapter.query(prompts: list[str]) -> list[PlatformResponse]` 这个抽象可以保留，但建议补充两点：

- 支持按平台配置决定是否启用搜索。
- 支持在 `PlatformResponse` 中携带更完整的原始元数据。

## 7. 原始归档设计

### 7.1 目标

原始归档层保存“用户看到的第一手结果”。

要求：

- 完整。
- 可追溯。
- 不可逆改写。
- 解析失败也不能丢。

### 7.2 表结构建议

现有 `platform_response_records` 表需要扩展，建议增加以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `search_triggered` | bool | 是否触发搜索 |
| `search_query` | text | 实际搜索词 |
| `reasoning_process` | text | 推理/分析过程 |
| `search_results` | json | 原始搜索结果列表 |
| `search_metadata` | json | 搜索引擎、耗时、结果量等 |
| `request_params` | json | temperature/top_p/max_tokens 等 |
| `request_id` | varchar | 平台请求 ID |
| `raw_response` | json | 平台原始响应对象 |
| `raw_response_text` | longtext | 原始响应文本快照 |

现有字段保持：

- `response_text`
- `citations`
- `prompt_tokens`
- `completion_tokens`
- `response_model`
- `finish_reason`
- `search_enabled`
- `error`

### 7.3 主键与唯一性

建议唯一约束仍以一次审计中的 `audit_id + prompt_id + platform` 为核心，但原始响应本体要保留多字段快照，避免同一轮结果被后续分析覆写。

### 7.4 归档原则

- 原始响应只追加，不重写。
- 分析结果不能回写原始响应。
- 同一个响应记录可以被多个分析表引用。

## 8. 响应解析设计

### 8.1 目标

从原始响应中提取可分析数据，但不影响原始存档。

### 8.2 解析职责

- 识别响应格式。
- 提取 citations。
- 提取 search 状态与搜索关键词。
- 提取推理过程。
- 提取平台特有字段。
- 记录解析失败原因。

### 8.3 解析结果结构

建议解析层输出统一对象：

```json
{
  "parse_success": true,
  "citation_format": "numeric",
  "citations": [
    {
      "title": "xxx",
      "url": "https://example.com",
      "domain": "example.com",
      "snippet": "摘要",
      "position": [120, 156]
    }
  ],
  "search_triggered": true,
  "search_query": "手冲咖啡壶 推荐",
  "search_metadata": {
    "engine": "baidu",
    "search_time_ms": 234
  },
  "reasoning_process": "..."
}
```

### 8.4 失败策略

解析失败时：

- 仍然保存 raw_response。
- 仍然保存 response_text。
- 记录 parse_error。
- 不阻断审计流程。

## 9. 分析层设计

### 9.1 原则

分析层只读原始数据，不写回原始记录。

### 9.2 分析对象

- 品牌提及
- 来源权威
- 内容结构
- 战略趋势
- 竞品对比

### 9.3 现有表关系

建议保留 `response_analyses` 作为单响应分析表，`source_citations` 作为跨审计来源聚合表。

分析层读取：

- `platform_response_records`
- `response_analyses`
- `source_citations`
- `query_results`

### 9.4 计算边界

- `response_analyses` 只对一条响应负责。
- `source_citations` 负责跨审计聚合。
- `reports` 负责审计级统计。
- `suggestions` 负责策略建议，不参与原始证据链。

## 10. 数据模型建议

### 10.1 新增表

#### `platform_configs`

用于存平台原生行为配置。

```sql
CREATE TABLE platform_configs (
    platform VARCHAR(50) PRIMARY KEY,
    config JSON NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

### 10.2 扩展表

#### `platform_response_records`

扩展原始响应留存字段：

```sql
ALTER TABLE platform_response_records
  ADD COLUMN search_triggered BOOLEAN DEFAULT NULL,
  ADD COLUMN search_query TEXT NULL,
  ADD COLUMN reasoning_process LONGTEXT NULL,
  ADD COLUMN search_results JSON NULL,
  ADD COLUMN search_metadata JSON NULL,
  ADD COLUMN request_params JSON NULL,
  ADD COLUMN request_id VARCHAR(100) NULL,
  ADD COLUMN raw_response JSON NULL,
  ADD COLUMN raw_response_text LONGTEXT NULL;
```

### 10.3 是否需要新增原始采集表

如果希望未来与现有 `platform_response_records` 的分析责任进一步分离，可以新增一张 `raw_collection_records` 表。

建议判断标准：

- 如果现有表扩展后仍能清晰承载原始数据，则可先扩展现有表。
- 如果希望分析表和归档表彻底分开，则新增原始采集表更稳妥。

当前更推荐：

- 短期：扩展 `platform_response_records`
- 中期：如果字段越来越多，再演进出 `raw_collection_records`

## 11. API 设计

### 11.1 平台配置 API

建议增加：

- `GET /api/platforms/configs`
- `GET /api/platforms/configs/{platform}`
- `PUT /api/platforms/configs/{platform}`
- `POST /api/platforms/configs/{platform}/reset`

用途：

- 查看平台配置
- 修改平台搜索倾向和参数
- 回滚到默认配置

### 11.2 原始响应查询 API

建议增加：

- `GET /api/audits/{audit_id}/raw-responses`
- `GET /api/audits/{audit_id}/raw-responses/{record_id}`
- `GET /api/platform-response-records/{record_id}`

用途：

- 直接看单次原始响应。
- 供后台审计与调试使用。
- 检查解析失败案例。

### 11.3 采集执行 API

现有审计 API 可继续保留：

- `POST /api/audits`
- `GET /api/audits/{id}`
- `GET /api/audits/{id}/results`
- `GET /api/audits/{id}/events`

建议在审计创建参数中补充：

- `collection_mode`
- `raw_capture_level`
- `platform_profile_version`

## 12. 执行流程

```text
1. 选择项目和平台
2. 读取平台配置
3. 准备用户原话
4. 由平台适配器执行查询
5. 收集原始响应和结构化字段
6. 写入原始归档表
7. 解析引用、搜索状态和来源
8. 写入分析表
9. 生成报告和建议
```

### 12.1 审计执行时序

```text
Audit PENDING
  -> QUERYING: 向各平台发起查询
  -> PERSISTING: 保存 raw response
  -> CALCULATING: 进行 response analysis
  -> FINALIZING: 汇总 report / suggestions
  -> COMPLETED or PARTIAL or FAILED
```

### 12.2 关键约束

- 查询阶段失败不应丢原始响应。
- 解析失败不应阻断后续平台查询。
- 任一平台失败后，其他平台结果仍可保存。

## 13. 适配器实现要求

### 13.1 抽象接口

现有接口可继续保留，但建议补充配置读取能力：

```python
class PlatformAdapter(ABC):
    platform_name: str = ""

    @abstractmethod
    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...

    def get_default_config(self) -> dict:
        return {}
```

### 13.2 OpenAI 兼容平台

适用于：

- DeepSeek
- 通义千问
- 豆包
- 腾讯混元

实现要点：

- 使用兼容 API 发起请求。
- 统一封装 messages / tools / search 参数。
- 解析返回中的 citations / usage / finish_reason。

### 13.3 Kimi 特殊处理

Kimi 的 `web_search` 通常是多轮工具调用，建议单独处理：

1. 发起工具声明。
2. 接收 tool_calls。
3. 继续完成 tool echo。
4. 合并最终回答和原始 search metadata。

### 13.4 DeepSeek 特殊处理

DeepSeek 官方接口与带联网能力的平台接口可能存在差异，建议：

- 在配置里显式标注搜索能力。
- 不默认假设所有 DeepSeek 端点都支持 web search。
- 需要时通过适配器能力判断决定是否启用搜索相关字段。

## 14. 前后端配合

### 14.1 后端

后端负责：

- 审计调度。
- 平台调用。
- 原始留存。
- 解析。
- 分析。

### 14.2 前端

- 前端用户关注的是最终分析结果，展示内容、模块和交互可以保持现有状态不变。
- 前端不需要直接暴露大模型平台搜索结果一致性、原始响应对照和平台校准细节。
- 如果后续需要补充能力，优先只做现有分析结果的详情增强，不新增原始搜索对照页。

### 14.3 前端展示重点

前端展示重点应保持在业务结果层，且不改变现有内容和模块边界：

- 最终分析结论。
- 品牌提及和来源权威趋势。
- 竞品对比和策略建议。
- 审计状态与处理进度。

平台原生搜索一致性属于智见平台内部的审计和质量控制项，应由后台负责，不应作为前端用户的主要关注点。

## 15. 测试策略

### 15.1 单元测试

覆盖：

- 平台配置读取。
- citation 提取。
- search 状态识别。
- raw_response 存储。
- 解析失败降级。

### 15.2 集成测试

覆盖：

- 审计创建到完成的完整链路。
- 单个平台失败不影响其他平台。
- 原始响应和分析结果的双写边界。

### 15.3 端到端验证

覆盖：

- 与平台 UI 手工结果对照。
- 引用列表一致性。
- 搜索状态一致性。
- 原始字段留存率。

说明：

- 这些对照是后台审计和质量验证的一部分，不属于前端用户主流程。
- 前端 E2E 重点仍应放在现有分析结果展示、报告生成和建议查看，模块不需要因这次改动而调整。

### 15.4 回归测试样本

建议固定 10 到 20 个典型 query：

- 事实查询
- 选购查询
- 对比查询
- 方案查询
- 解释查询

每个 query 在不同平台上保留标准化对照样本。

## 16. 观测与质量指标

### 16.1 采集质量指标

- 响应文本覆盖率
- citations 留存率
- search 状态留存率
- raw_response 留存率
- 平台配置命中率

### 16.2 一致性指标

- 平台 UI 与系统结果相似度
- 引用链路相似度
- 搜索行为一致性
- 平台差异保留率

说明：

- 这些指标用于后台审计质量评估，不是前端交互层的核心指标。
- 前端报表和模块结构不需要围绕这些指标单独暴露新视图。

### 16.3 失败监控

建议监控：

- 解析失败率
- 平台 API 超时率
- search tool 调用失败率
- raw_response 缺失率

## 17. 迁移方案

### Phase 1: 字段和配置落地

- 新增平台配置表。
- 扩展原始响应表字段。
- 保留现有查询链路。

### Phase 2: 解析层增强

- 补充各平台 citation 解析规则。
- 统一 search 状态提取。
- 解析失败降级保存。

### Phase 3: 平台对照校准

- 引入手工 UI 对照样本。
- 调整平台参数。
- 校准默认搜索倾向。

### Phase 4: 分析层收敛

- 确保分析只读原始数据。
- 关闭任何会重写原始记录的路径。

## 18. 风险与应对

### 18.1 平台行为变化

风险：平台搜索策略和返回字段会变化。

应对：

- 平台配置版本化。
- 定期 UI 对照验证。
- 解析规则可热更新。

### 18.2 API 与 UI 不一致

风险：API 结果和 UI 结果不同。

应对：

- 记录采集来源。
- 关键平台优先对照 UI。
- 报告里显式展示偏差。

### 18.3 原始数据膨胀

风险：原始响应和搜索结果体积增长。

应对：

- 大字段对象化存储或压缩。
- 历史归档。
- 分层存储 raw 与 analysis。

### 18.4 解析规则失效

风险：平台格式变化导致解析失败。

应对：

- 解析失败不阻断采集。
- 永远保留 `raw_response`。
- 增加解析规则回归测试。

## 19. 验收标准

- 采集结果更接近平台 UI 搜索结果。
- 原始响应、引用、搜索状态、分析过程都能完整保存。
- 平台差异得到保留，而不是被统一逻辑抹平。
- 分析层不再污染原始采集层。
- 解析失败时系统仍可完成采集并保留原始证据。
- 前端现有分析结果展示流程、内容口径和模块结构可以保持不变，用户无需感知平台对照和搜索一致性细节。

## 20. 建议结论

如果按优先级排序，建议按以下顺序实现：

1. 先把平台配置层和原始响应留存补齐。
2. 再增强响应解析。
3. 再做平台 UI 对照校准。
4. 最后把分析层完全收敛到原始数据之上。

这条路线最符合当前目标：先让结果像平台，再让证据留得全，最后让分析站在可信证据之上。
