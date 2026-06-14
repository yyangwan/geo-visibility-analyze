# 真实用户仿真采集 - 实施进度

## 已完成

### Epic 1: 平台配置与数据模型
- **Issue 1.1**: 新增平台配置表
  - 创建 `PlatformConfig` 模型
  - 创建 `platform_config_service.py`
  - 提供 API: `GET /platforms/configs`、`GET /platforms/configs/{platform}`、`POST /platforms/configs/{platform}`
  - 完成迁移 `add_platform_configs_and_raw_response`

- **Issue 1.2**: 扩展平台响应记录表
  - `platform_response_records` 新增字段:
    - `raw_response` (JSON) - 完整原始响应
    - `raw_response_text` (Text) - 文本化原始响应
    - `search_metadata` (JSON) - 搜索状态、查询词、结果数
    - `request_params` (JSON) - 请求参数
    - `parse_error` (Text) - 解析失败信息

- **Issue 1.3**: 评估是否拆分 `raw_collection_records`
  - 结论: 先不拆
  - 现有 `platform_response_records` 已覆盖原始响应、搜索元数据、解析错误、请求参数和后续分析引用
  - 继续拆表会增加 join 和写入复杂度，没有明显收益

### Epic 2: 平台适配器与采集
- **Issue 2.1**: 适配器读取平台配置
  - `PlatformAdapter` 增加 `set_platform_config()` / `get_platform_config()`
  - `OpenAICompatAdapter._build_request_body()` 合并平台配置
  - `DeepSeekAdapter` 尊重配置而不是强制覆盖
  - `audit_service.py` 在查询前加载并注入配置

- **Issue 2.2**: 统一增强 `PlatformResponse`
  - 新增字段: `raw_response`、`raw_response_text`、`search_metadata`、`request_params`、`parse_error`
  - `OpenAICompatAdapter._query_single()` 填充这些字段

- **Issue 2.3**: Kimi 多轮 search 处理
  - 处理 Kimi 的 `tool_calls` / echo 流程
  - 单独处理 `$web_search`
  - 保存中间 tool call
  - 合并最终答案和搜索元数据

- **Issue 2.4**: 审计任务接入原始采集
  - 审计阶段保存原始响应
  - 保持现有分析链路继续运行

### Epic 3: 响应解析
- **Issue 3.1**: 引用提取器
  - 创建 `response_parser.py`
  - 支持 `search_results`、`tool_calls`、`markdown`、`none`
  - 提供 `_get_nested_value()`

- **Issue 3.2**: 搜索状态提取
  - 提取 `search_enabled`、`search_triggered`、`search_query`
  - 提取 `search_reasoning`、`search_results_count`
  - 支持 `tool_calls` 中的 `$web_search`

- **Issue 3.3**: 解析降级与错误记录
  - 捕获解析异常
  - 记录 `parse_error`
  - 保留 `raw_response`

### Epic 4: 原始归档
- **Issue 4.1**: 原始响应落库
  - `_upsert_platform_response_record()` 保存原始字段
  - 单次响应可完整还原

- **Issue 4.2**: 原始记录去重与引用关系
  - 保持 `audit_id + prompt_id + platform` 唯一约束
  - 分析层通过外键引用原始记录

### Epic 5: 分析层收敛
- **Issue 5.1**: 分析只读原始记录
  - 分析任务只读 `platform_response_records`
  - 结果写入 `response_analyses`

- **Issue 5.2**: 保持现有分析输出不变
  - 现有品牌提及、来源权威、竞品、趋势、建议输出结构保持不变

### Epic 8: 测试
- **Issue 8.1**: 单元测试
  - `test_platform_config_service.py`
  - `test_adapter_config_injection.py`
  - `test_response_parser.py`
  - `test_kimi_adapter.py`
  - `test_audit_workflow.py`
  
- **Issue 8.2**: 集成测试
  - 覆盖审计全链路
  - 单平台失败不影响其他平台
  - 归档和分析双写边界验证
  - 已新增 `test_audit_workflow.py` 的集成回归，覆盖平台失败隔离和 PRR -> 分析边界

## 待完成

### Epic 6
- **Issue 6.1**: 平台参数校准
  - 为每个平台调整搜索和解析默认值
  - 记录默认搜索倾向
  - 校准引用与结果偏差
  - 已把默认校准基线固化到 `test_platform_config_service.py`
  - 真实平台 UI 差异仍需人工对照确认

- **Issue 6.2**: 对照样本集
  - 准备 10-20 个典型 query
  - 覆盖事实、选购、对比、方案、解释类问题
  - 保存平台 UI 结果样本
  - 已新增样本清单: `docs/user-like-llm-collection-sample-set.md`
  - 平台 UI 结果样本仍待补录

### Epic 8
- **Issue 8.3**: 对照测试
  - 与平台 UI 手工结果对照
  - 对比引用链路和搜索状态
  - 记录差异原因
  - 已新增对照记录模板: `docs/user-like-llm-collection-compare-log.md`
  - 平台 UI 手工结果仍待补录

## 技术要点

1. **配置优先级**: 平台配置(DB) > 适配器默认值 > 硬编码
2. **原始响应完整性**: `raw_response` 保存完整 API 响应，便于调试和后续分析
3. **解析容错**: 解析失败不阻断采集，记录 `parse_error`，保留原始数据
4. **表模型边界**: 现阶段不拆 `raw_collection_records`，`platform_response_records` 继续作为唯一归档表
5. **JSONPath 风格提取**: `_get_nested_value(data, "choices.0.message.content")`
6. **多格式引用支持**:
   - `search_results`: DeepSeek/Qwen 的搜索结果数组
   - `tool_calls`: Kimi 的 `$web_search` tool call 参数
   - `markdown`: 文本中的 `[text](url "title")`
7. **搜索状态判断**:
   - 字符串状态: `disabled` / `triggered` / 其他
   - 布尔状态: `True` / `False`
   - tool_calls: `$web_search` / `web_search`
   - 结果数量: `search_results` 数组长度

## 最近实现

2026-06-13: 完成 `raw_collection_records` 评估（Issue 1.3）
- 结论是不拆表，继续用 `platform_response_records` 作为唯一原始归档表
- 当前表已经覆盖原始响应、搜索元数据、解析错误、请求参数和后续分析引用
- 新拆表只会增加写入复杂度和查询 join 成本，没有明显收益

2026-06-13: 完成 `parse_error` 传播与落库
- `PlatformResponse` 新增 `parse_error`
- `OpenAICompatAdapter` 使用解析兜底，避免解析失败吞掉上下文
- `KimiAdapter` 将解析失败信息传到最终响应
- `audit_service.py` 将 `parse_error` 持久化到 `platform_response_records`
- `test_kimi_adapter.py` / `test_audit_workflow.py` 补齐回归测试
- 相关后端测试通过，累计 `123 passed`

## 下一步

按现有顺序继续：
1. `6.2` 补录平台 UI 对照样本
2. `8.3` 按对照记录模板填入 UI vs API 差异
3. `6.1` 如发现偏差再回头调整平台参数

## 备注

- 以后保存 checkpoint 时，请先对照本进度文件更新“已完成 / 待完成 / 下一步”。
- 新的 checkpoint 里要明确提醒下次会话: 先看 [user-like-llm-collection-progress.md](./user-like-llm-collection-progress.md)，再继续执行未完成项。
- `Issue 1.3` 已形成结论，不需要再回到“是否拆表”的评估。

## Current Alignment

- `Issue 6.1` is complete in code: platform defaults are in `backend/app/services/platform_config_service.py`, and the baseline is locked by `backend/tests/test_platform_config_service.py`.
- `Issue 6.2` is complete as an artifact: the sample set is in `docs/user-like-llm-collection-sample-set.md`.
- `Issue 8.3` still needs manual UI capture, but `docs/user-like-llm-collection-compare-log.md` now has a concrete capture table ready to fill.
- Frontend issues `7.1` and `7.2` stay unchanged by request.
