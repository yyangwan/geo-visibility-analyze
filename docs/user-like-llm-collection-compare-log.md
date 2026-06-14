# 用户风格 LLM 采集对照记录

这个文档用于 `Issue 8.3`。目标不是再定义新指标，而是把平台 UI 的手工结果和采集系统结果放在同一张表里，方便稳定对照。

## 适用范围

- 先用 `docs/user-like-llm-collection-sample-set.md` 里的 query。
- 同一个 query 分别跑平台 UI 和采集系统。
- 记录原始证据，不只记结论。
- 如果发现差异，先归类，再写原因。

## 字段定义

| 字段 | 说明 |
| --- | --- |
| query_id | 对应样本集编号 |
| platform | deepseek / qwen / kimi / doubao / wenxin / hunyuan |
| query | 原始问题 |
| ui_answer | 平台 UI 最终回答 |
| api_answer | 采集系统最终回答 |
| ui_citations | UI 侧引用列表 |
| api_citations | 采集系统引用列表 |
| ui_search_triggered | UI 是否触发搜索 |
| api_search_triggered | 采集系统是否触发搜索 |
| ui_search_trace | UI 侧搜索链路摘要 |
| api_search_trace | 采集系统搜索链路摘要 |
| diff_type | 差异类型 |
| diff_reason | 差异原因简述 |
| evidence_link | 截图、日志或原始响应位置 |

## 差异类型

- `match`: 结果一致，或差异不影响结论
- `citation_gap`: 引用链路不同
- `search_gap`: 搜索状态不同
- `answer_gap`: 最终回答结论不同
- `parser_gap`: 解析器漏掉了 UI 可见信息
- `config_gap`: 默认参数或平台配置不同
- `unknown`: 暂时无法解释

## 录入规则

1. 先填 `ui_answer`，再填 `api_answer`。
2. 引用只记稳定可定位的来源，不把主观总结当引用。
3. 搜索状态要写清楚是“未触发”“触发但无结果”还是“触发且有结果”。
4. 如果答案一致，但引用链路不同，也要记为差异。
5. 如果差异来自平台配置，优先回到 `Issue 6.1` 校准。
6. 如果差异来自样本设计，优先回到 `Issue 6.2` 补样本。

## 对照执行表

先按 `Issue 6.2` 的样本顺序跑，再把平台 UI 和采集系统结果填进来。

| query_id | platform | query | ui_answer | api_answer | ui_citations | api_citations | ui_search_triggered | api_search_triggered | ui_search_trace | api_search_trace | diff_type | diff_reason | evidence_link |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | deepseek | 北京到上海高铁最常见的出发车站有哪些 |  |  |  |  |  |  |  |  |  |  |  |
| 2 | deepseek | 2026 年中国新能源汽车渗透率大概是多少 |  |  |  |  |  |  |  |  |  |  |  |
| 3 | qwen | 5000 元以内适合办公和轻度游戏的笔记本推荐 |  |  |  |  |  |  |  |  |  |  |  |
| 4 | doubao | 1000 元左右无线耳机怎么选 |  |  |  |  |  |  |  |  |  |  |  |
| 5 | kimi | iPhone 16 和 小米 15 哪个更适合拍照 |  |  |  |  |  |  |  |  |  |  |  |
| 6 | kimi | DeepSeek 和 Qwen 做企业问答各有什么差别 |  |  |  |  |  |  |  |  |  |  |  |
| 7 | wenxin | 我想组 20 人团队做知识库，怎么选技术方案 |  |  |  |  |  |  |  |  |  |  |  |
| 8 | wenxin | 小团队做内容运营自动化，先做哪 3 个步骤 |  |  |  |  |  |  |  |  |  |  |  |
| 9 | hunyuan | 为什么搜索结果有时候和回答内容不一致 |  |  |  |  |  |  |  |  |  |  |  |
| 10 | hunyuan | 大模型回答里引用网页有什么局限 |  |  |  |  |  |  |  |  |  |  |  |
| 11 | deepseek | 2025 年上海地铁最新运营线路图怎么查 |  |  |  |  |  |  |  |  |  |  |  |
| 12 | qwen | 现在常见的 AI 代码审查工具有哪些 |  |  |  |  |  |  |  |  |  |  |  |

## 后续说明

- `Issue 6.2` 提供 query 样本。
- `Issue 8.3` 用这份记录做人工对照。
- 如果后面要做自动化校验，可以在这份表的基础上再加脚本导出。
