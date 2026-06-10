# 智见 (ZhiJian) - 后端系统架构设计

> AI搜索可见性分析平台 - 后端技术架构参考文档

---

## 目录

- [架构概览](#架构概览)
- [技术栈](#技术栈)
- [代码结构](#代码结构)
- [核心模块](#核心模块)
- [数据模型](#数据模型)
- [关键流程](#关键流程)
- [配置管理](#配置管理)
- [扩展点](#扩展点)

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                      客户端层 (Vue 3 前端)                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP / JWT
┌───────────────────────────▼─────────────────────────────────┐
│                     API 网关层 (FastAPI)                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │  Auth    │ │  Audits  │ │ Reports  │ │Scheduler │         │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│  │ Trends   │ │ Prompts  │ │Analysis  │ │Strategic │         │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘         │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                       服务层 (Services)                       │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐           │
│  │ audit_service│ │report_service│ │suggestion_  │           │
│  │              │ │              │ │service       │           │
│  └──────────────┘ └──────────────┘ └──────────────┘           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐           │
│  │  scheduler   │ │prompt_gen_   │ │response_     │           │
│  │              │ │service       │ │analysis_     │           │
│  └──────────────┘ └──────────────┘ └──────────────┘           │
│  ┌──────────────┐ ┌──────────────┐                            │
│  │   detect     │ │source_       │                            │
│  │              │ │extraction    │                            │
│  └──────────────┘ └──────────────┘                            │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      适配器层 (Adapters)                        │
│  ┌──────────────────────────────────────────────────────┐     │
│  │          PlatformAdapter (抽象基类)                    │     │
│  │  - query(prompts) -> list[PlatformResponse]          │     │
│  │  - health_check() -> bool                             │     │
│  └────────────────────┬─────────────────────────────────┘     │
│                       │                                         │
│  ┌────────────────────┴─────────────────────────────────┐     │
│  │     OpenAICompatAdapter (共享基类)                    │     │
│  │  - 速率限制重试 (429)                                  │     │
│  │  - 并发控制 (Semaphore)                                │     │
│  │  - 统一错误处理                                        │     │
│  └────────────────────┬─────────────────────────────────┘     │
│                       │                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │DeepSeek  │ │  Qwen   │ │ Doubao  │ │  Kimi   │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│  ┌──────────┐ ┌──────────┐                                     │
│  │ Wenxin   │ │ Hunyuan  │                                     │
│  └──────────┘ └──────────┘                                     │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTPS
┌───────────────────────────▼─────────────────────────────────┐
│                  外部 AI 平台 (OpenAI 兼容)                    │
│    DeepSeek │ 通义千问 │ 豆包 │ Kimi │ 文心一言 │ 腾讯元宝     │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                    数据存储层 (MySQL 8.0+)                      │
│  users │ audits │ platform_response_records │ query_results  │
│  reports │ suggestions │ scheduled_jobs │ source_citations   │
└─────────────────────────────────────────────────────────────┘
```

---

## 技术栈

### 核心框架

| 组件 | 技术 | 版本 | 用途 |
|------|------|------|------|
| Web 框架 | FastAPI | 0.115+ | 异步 RESTful API |
| ORM | SQLAlchemy | 2.0+ | 数据库抽象, async 模式 |
| 数据库 | MySQL | 8.0+ | 关系型数据存储 |
| 异步驱动 | aiomysql | - | Async MySQL 连接 |
| 认证 | python-jose | - | JWT RS256 验证 |
| 密码哈希 | passlib | - | bcrypt 哈希 |
| 日志 | structlog | - | 结构化 JSON 日志 |
| HTTP 客户端 | httpx | - | 异步 HTTP 请求 |
| 数据验证 | Pydantic | 2.0+ | 请求/响应模型 |

### 中间件与工具

- **CORS:** 允许跨域请求配置
- **请求日志:** `RequestLoggingMiddleware` 记录所有 API 调用
- **Alembic:** 数据库迁移管理

---

## 代码结构

```
backend/app/
├── main.py                      # FastAPI 应用入口
├── config.py                    # 环境配置管理
├── database.py                  # SQLAlchemy async session
├── middleware.py                # 请求日志中间件
├── logging_config.py            # structlog 配置
│
├── api/                         # API 路由层
│   ├── __init__.py
│   ├── auth.py                  # JWT 认证依赖
│   ├── access.py                # 权限校验辅助
│   ├── schemas.py               # Pydantic 模型
│   ├── audits.py                # /api/audits
│   ├── analysis.py              # /api/analysis
│   ├── trends.py                # /api/trends
│   ├── reports.py               # /api/reports
│   ├── schedules.py             # /api/schedules
│   ├── suggestions.py           # /api/suggestions
│   ├── prompts.py               # /api/prompts
│   ├── platforms.py             # /api/platforms
│   ├── strategic.py             # /api/strategic
│   └── integration.py           # /api/integration
│
├── services/                    # 业务服务层
│   ├── audit_service.py         # 审计执行编排
│   ├── report_service.py        # 报告生成
│   ├── scheduler.py             # 定时任务调度
│   ├── suggestion_service.py    # AI 建议生成
│   ├── prompt_gen_service.py    # AI 提示词生成
│   ├── detect.py                # 品牌提及检测
│   ├── source_extraction.py    # 来源引用提取
│   ├── response_analysis_service.py  # 响应深度分析
│   ├── audit_events.py          # SSE 事件管理
│   └── genilink_auth.py         # 智链 SSO 验证
│
├── adapters/                    # 平台适配器层
│   ├── base.py                  # PlatformAdapter 基类
│   ├── openai_compat.py        # OpenAI 兼容基类
│   ├── registry.py              # 适配器注册表
│   ├── deepseek.py              # DeepSeek 适配器
│   ├── qwen.py                  # 通义千问适配器
│   ├── doubao.py                # 豆包适配器
│   ├── kimi.py                  # Kimi 适配器
│   ├── wenxin.py                # 文心一言适配器
│   └── hunyuan.py               # 腾讯元宝适配器
│
├── models/                      # 数据模型层
│   ├── __init__.py
│   └── models.py                # SQLAlchemy ORM 定义
│
├── utils/                       # 工具函数
│   ├── timezone.py              # 时区处理
│   └── __init__.py
│
└── constants.py                 # 常量定义
```

---

## 核心模块

### 1. 审计执行服务 (`audit_service.py`)

审计执行是系统的核心流程，负责编排完整的可见性检测任务。

#### 执行状态机

```
PENDING → RUNNING → [QUERYING] → [PERSISTING] → [CALCULATING] → [FINALIZING] → COMPLETED
              │                                                              │
              └──────────────────────────────────────→ FAILED ←──────────┘
                                                              │
                                                         PARTIAL ←────────┘
```

#### 阶段定义

| 阶段 | 说明 | 输入 | 输出 |
|------|------|------|------|
| `QUEUED` | 等待执行 | - | - |
| `QUERYING` | 查询 AI 平台 | prompts, platforms | PlatformResponse[] |
| `PERSISTING` | 持久化响应 | PlatformResponse[] | PlatformResponseRecord[] |
| `CALCULATING` | 品牌检测 | brands, responses | QueryResult[] |
| `FINALIZING` | 收尾统计 | 统计数据 | final status |
| `COMPLETED` | 全部成功 | - | - |
| `PARTIAL` | 部分失败 | - | - |
| `FAILED` | 全部失败 | - | - |

#### 关键函数

```python
async def claim_audit(db: AsyncSession, audit_id: int) -> Audit | None:
    """原子获取审计执行锁
    使用 UPDATE ... WHERE status=PENDING 确保只有一个 worker 执行
    """

async def run_audit(audit_id: int) -> None:
    """审计执行入口, 设计为后台任务运行
    流程: claim → execute → publish event
    """

async def _execute_audit(db: AsyncSession, audit: Audit) -> None:
    """核心执行逻辑
    1. 加载 prompts 和 brands (从 audit.brands_json 快照)
    2. 并发查询各平台适配器
    3. 存储 PlatformResponseRecord (去重: audit_id + prompt_id + platform)
    4. 提取来源引用
    5. 检测品牌提及, 生成 QueryResult
    6. 计算最终状态 (COMPLETED/PARTIAL/FAILED)
    """
```

#### Worker 锁机制

- `locked_by_worker`: 当前执行者标识 (`local-{pid}`)
- `locked_until`: 锁过期时间 (默认 15 分钟)
- `last_heartbeat_at`: 心跳时间戳
- 用于检测和恢复崩溃 worker 遗留的审计

### 2. 平台适配器层 (`adapters/`)

统一的国内 AI 平台接口抽象，基于 OpenAI 兼容协议。

#### 基类设计

```python
class PlatformAdapter(ABC):
    """平台适配器抽象基类"""

    platform_name: str = ""
    search_enabled: bool = False

    @abstractmethod
    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        """批量查询, 返回与 prompts 同长度列表"""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass
```

#### OpenAI 兼容基类

```python
class OpenAICompatAdapter(PlatformAdapter):
    """所有国内平台的共享基类
    特性:
    - 速率限制重试 (429, 最多 3 次)
    - 并发控制 (Semaphore, 默认每平台 2 并发)
    - 统一错误映射 (AUTH_FAILED, RATE_LIMITED, TIMEOUT, etc.)
    """

    def _build_request_body(self, prompt: str) -> dict:
        """子类可重写以启用 search mode"""

    def _extract_citations(self, data: dict) -> list[dict]:
        """子类可重写以提取平台特定引用格式"""
```

#### 适配器注册表

```python
# adapters/registry.py
_ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "deepseek": DeepSeekAdapter,
    "qwen": QwenAdapter,
    "doubao": DoubaoAdapter,
    "kimi": KimiAdapter,
    "hunyuan": HunyuanAdapter,
}

def get_adapter(platform: str) -> PlatformAdapter:
    """工厂方法: 按名称获取适配器实例"""
```

### 3. 调度器 (`scheduler.py`)

轻量级 cron 调度器，内嵌在 FastAPI 进程中。

#### Cron 表达式

标准 5 字段格式: `minute hour day month weekday`

| 字段 | 范围 | 示例 |
|------|------|------|
| minute | 0-59 | `0`, `*/15`, `30,45` |
| hour | 0-23 | `9`, `22`, `*/6` |
| day | 1-31 | `1`, `15`, `*` |
| month | 1-12 | `1`, `6,12` |
| weekday | 0-6 (0=周日) | `1`, `1-5`, `*` |

示例:
- `0 22 * * *` - 每天晚上 10 点
- `0 9 * * 1-5` - 工作日早上 9 点
- `*/30 * * * *` - 每 30 分钟

#### 调度循环

```python
async def scheduler_loop() -> None:
    """主循环: 每 60 秒检查一次"""
    while True:
        now = datetime.now(tz)
        for job in active_jobs:
            if should_run_now(job.cron_expression, now):
                await _execute_scheduled_job(job)
        await asyncio.sleep(60)

async def _execute_scheduled_job(job: ScheduledJob, db) -> None:
    """执行定时任务: 创建审计 → 后台运行 → 自动生成报告"""
```

### 4. 品牌检测服务 (`detect.py`)

基于关键词和别名的品牌提及检测。

```python
def detect_mentions(text: str, brand_name: str, aliases: list[str], industry: str) -> list[Mention]:
    """
    1. 构建匹配模式: brand_name + aliases + 大小写变体
    2. 扫描文本, 提取匹配位置的上下文窗口
    3. 计算置信度: 基于 match_quality + position + context_relevance
    4. 识别推荐标记: 检测 "推荐"、"首选" 等关键词
    """
```

### 5. 来源提取服务 (`source_extraction.py`)

支持两种来源引用提取模式:

1. **API Citation:** 平台直接返回结构化引用 (`resp.citations`)
2. **启发式解析:** 从响应文本中提取 URL 并解析域名

### 6. AI 建议服务 (`suggestion_service.py`)

两阶段 LLM 调用生成优化建议。

#### Pass 1: 战略建议

- 输入: 报告数据、平台评分、竞品对比
- 输出: 5-8 条战略建议 (category, title, description, priority, target_platforms, action_channel)

#### Pass 2: 详细方案

- 输入: 单条建议 + 审计背景
- 输出: 详细执行方案 (outline, keywords, timeline, competitor_ref, expected_outcome)

### 7. 提示词生成服务 (`prompt_gen_service.py`)

基于 GeniLink 框架的四阶段提示词生成:

1. **Category Stage:** 确定提示词类别
2. **Intent Stage:** 提取用户意图
3. **Format Stage:** 设计输出格式
4. **Final Stage:** 生成最终提示词

### 8. 响应分析服务 (`response_analysis_service.py`)

对 AI 响应进行深度分析:

- `cited_sources`: 提取的来源列表
- `brand_sentiment`: 品牌情感 (positive/neutral/negative)
- `brand_attributes`: 品牌属性标签
- `topics_covered`: 覆盖话题列表
- `answer_structure`: 回答结构类型
- `competitor_refs`: 竞品提及

### 9. 报告服务 (`report_service.py`)

```python
async def generate_report(db: AsyncSession, audit: Audit) -> Report:
    """生成审计报告
    1. 聚合 QueryResult 数据
    2. 计算总体评分 (各平台加权平均)
    3. 计算提及率 (mention_count / total_count)
    4. 提取洞察 (基于低分平台和高排名品牌)
    5. 存储 Report
    """
```

### 10. 事件服务 (`audit_events.py`)

SSE (Server-Sent Events) 事件发布/订阅:

```python
async def publish(audit_id: int, event: PlatformEvent):
    """发布审计事件到所有订阅者"""

async def subscribe(audit_id: int) -> asyncio.Queue:
    """订阅审计事件流"""

def unsubscribe(audit_id: int, queue: asyncio.Queue):
    """取消订阅"""
```

### 11. 认证服务 (`genilink_auth.py`)

智链 SSO 集成:

- **JWKS 验证:** 从智链 `.well-known/jwks.json` 获取公钥
- **RS256 签名:** 验证 JWT 签名
- **Claims 提取:** `sub` (user_id), `workspace_id`, `project_ids[]`

---

## 数据模型

### 核心实体关系

```
Project (外部 SSO)
    │
    ├─→ Audit (1:N)
    │       │
    │       ├─→ PlatformResponseRecord (1:N)
    │       │       │
    │       │       └─→ ResponseAnalysis (1:1)
    │       │
    │       ├─→ QueryResult (1:N)
    │       │
    │       ├─→ SourceCitation (1:N)
    │       │
    │       ├─→ AuditStageRun (1:N)
    │       │
    │       ├─→ AuditPlatformRun (1:N)
    │       │
    │       ├─→ AuditEventLog (1:N)
    │       │
    │       └─→ Report (1:1)
    │               │
    │               └─→ Suggestion (1:N)
    │
    └─→ Prompt (1:N)

    └─→ ScheduledJob (1:N)
```

### 关键表结构

#### `audits` - 审计任务主表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID (FK 来自 SSO) |
| status | Enum | pending/running/completed/failed/partial |
| stage | Enum | queued/querying/persisting/calculating/finalizing/... |
| stage_status | Enum | pending/running/completed/failed/retrying |
| platforms_json | JSON | 目标平台列表 `["deepseek", "qwen"]` |
| brands_json | JSON | 品牌快照 `[{id, name, aliases, is_competitor}]` |
| created_at | DateTime | 创建时间 |
| completed_at | DateTime | 完成时间 |
| stage_started_at | DateTime | 当前阶段开始时间 |
| stage_updated_at | DateTime | 当前阶段更新时间 |
| last_heartbeat_at | DateTime | 心跳时间 |
| attempt_count | Integer | 重试次数 |
| error_code | String(50) | 错误代码 |
| error_message | Text | 错误详情 |
| recoverable_error | Boolean | 是否可恢复 |
| next_retry_at | DateTime | 下次重试时间 |
| locked_by_worker | String(100) | 锁持有者 |
| locked_until | DateTime | 锁过期时间 |

#### `platform_response_records` - 原始响应存储

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| audit_id | Integer | FK → audits.id |
| prompt_id | Integer | FK → prompts.id |
| platform | String(50) | 平台名称 |
| response_text | Text | 响应内容 |
| citations | JSON | 引用列表 `[{domain, urls, title}]` |
| prompt_tokens | Integer | 输入 token 数 |
| completion_tokens | Integer | 输出 token 数 |
| response_model | String(100) | 模型名称 |
| finish_reason | String(20) | 结束原因 |
| search_enabled | Boolean | 是否启用搜索 |
| error | Text | 错误信息 |

**唯一约束:** `(audit_id, prompt_id, platform)`

#### `query_results` - 品牌提及检测结果

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| audit_id | Integer | FK → audits.id |
| prompt_id | Integer | FK → prompts.id |
| brand_id | String(50) | 品牌 ID |
| platform | String(50) | 平台名称 |
| response_record_id | Integer | FK → platform_response_records.id |
| mention_found | Boolean | 是否发现提及 |
| mention_position | Integer | 提及位置 |
| mention_context | Text | 提及上下文 |
| mention_confidence | Float | 置信度 |
| is_recommended | Boolean | 是否被推荐 |
| recommendation_rank | Integer | 推荐排名 |
| error | Text | 错误信息 |

#### `reports` - 报告汇总

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID |
| audit_id | Integer | FK → audits.id (唯一) |
| overall_score | Float | 总体评分 (0-100) |
| mention_rate | Float | 提及率 (0-1) |
| competitor_rank | Integer | 竞品排名 |
| sentiment_positive_rate | Float | 正面情感占比 |
| platform_scores | JSON | 各平台评分 `{"deepseek": 85, "qwen": 72}` |
| insights | JSON | 洞察列表 |

#### `suggestions` - AI 优化建议

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID |
| report_id | Integer | FK → reports.id |
| category | String(50) | 分类 |
| title | String(200) | 标题 |
| description | Text | 描述 |
| priority | String(20) | 优先级 (high/medium/low) |
| is_resolved | Boolean | 是否已解决 |
| detail | JSON | 详细方案 (Pass 2 结果) |

#### `scheduled_jobs` - 定时任务

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID |
| cron_expression | String(100) | Cron 表达式 |
| platforms_json | JSON | 目标平台列表 |
| is_active | Boolean | 是否激活 |
| last_run_at | DateTime | 上次运行时间 |
| last_audit_id | Integer | 上次生成的审计 ID |

#### `response_analyses` - 深度分析结果

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| response_record_id | Integer | FK → platform_response_records.id (唯一) |
| cited_sources | JSON | 来源列表 |
| brand_sentiment | String(20) | 品牌情感 |
| brand_attributes | JSON | 品牌属性 |
| topics_covered | JSON | 话题列表 |
| answer_structure | String(20) | 回答结构 |
| competitor_refs | JSON | 竞品提及 |
| analysis_model | String(100) | 分析模型 |
| status | String(20) | 状态 (pending/completed/failed) |

#### `source_citations` - 来源域名统计

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID |
| audit_id | Integer | FK → audits.id |
| domain | String(200) | 域名 |
| urls | JSON | URL 列表 |
| citation_count | Integer | 引用次数 |
| platform | String(50) | 平台名称 |

**唯一约束:** `(project_id, audit_id, domain, platform)`

#### `prompts` - 审计提示词

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | PK |
| project_id | String(50) | 项目 ID |
| text | Text | 提示词内容 |
| category | Enum | 类别 (recommend/compare/evaluate/scenario/etc.) |
| is_auto_generated | Boolean | 是否 AI 生成 |

#### 辅助表

| 表名 | 用途 |
|------|------|
| `audit_stage_runs` | 阶段执行记录 (用于追踪和调试) |
| `audit_platform_runs` | 平台执行记录 (用于重试和错误追踪) |
| `audit_events_log` | 审计事件日志 (用于调试和监控) |

### 枚举类型

```python
class QueryStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"

class AuditStage(str, PyEnum):
    QUEUED = "queued"
    QUERYING = "querying"
    PERSISTING = "persisting"
    CALCULATING = "calculating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    STALLED = "stalled"

class RunStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"

class PromptCategory(str, PyEnum):
    RECOMMEND = "recommend"
    COMPARE = "compare"
    EVALUATE = "evaluate"
    SCENARIO = "scenario"
    PROBLEM_SOLUTION = "problem_solution"
    ALTERNATIVE_FINDING = "alternative_finding"
    DECISION_HELP = "decision_help"
    REGRET_AVOIDANCE = "regret_avoidance"
    PERFORMANCE_SPECS = "performance_specs"
```

---

## 关键流程

### 1. 审计创建与执行

```
┌────────────────────────────────────────────────────────────────┐
│ POST /api/audits {project_id, platforms, brands}               │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ create_audit()                                                 │
│   - 创建 Audit(status=PENDING)                                  │
│   - 存储平台列表到 platforms_json                               │
│   - 存储品牌快照到 brands_json                                  │
│   - asyncio.create_task(run_audit(audit_id))                    │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ Background Task: run_audit(audit_id)                            │
│   │                                                             │
│   ├─→ claim_audit() [原子锁]                                    │
│   │     UPDATE audits SET status=RUNNING WHERE id=? AND status=PENDING │
│   │                                                             │
│   └─→ _execute_audit()                                         │
│         │                                                       │
│         ├─ 1. 加载 prompts (WHERE project_id=?)                 │
│         │                                                       │
│         ├─ 2. 加载 brands (从 audit.brands_json)                │
│         │                                                       │
│         ├─ 3. _start_stage(QUERYING)                            │
│         │      publish(platform_start)                          │
│         │                                                       │
│         ├─ 4. 并发查询各平台                                     │
│         │      adapters = get_adapters(platforms_json)          │
│         │      responses = await asyncio.gather(*queries)        │
│         │      publish(platform_done / platform_error)          │
│         │                                                       │
│         ├─ 5. _start_stage(PERSISTING)                          │
│         │      for resp in responses:                           │
│         │        prr = _upsert_platform_response_record()        │
│         │        sources = extract_sources(resp)                  │
│         │        _upsert_source_citations(sources)                │
│         │                                                       │
│         ├─ 6. _start_stage(CALCULATING)                         │
│         │      for brand in brands:                             │
│         │        for resp in responses:                         │
│         │          mentions = detect_mentions(resp, brand)       │
│         │          create QueryResult(...)                       │
│         │                                                       │
│         └─ 7. _start_stage(FINALIZING)                           │
│                if all_failed: status=FAILED                      │
│                elif some_failed: status=PARTIAL                   │
│                else: status=COMPLETED                            │
│                publish(audit_done / audit_failed)                 │
└────────────────────────────────────────────────────────────────┘
```

### 2. SSE 事件流

```
┌────────────────────────────────────────────────────────────────┐
│ GET /api/audits/{audit_id}/events?token=xxx                    │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ verify_genilink_token(token)                                   │
│   - 解析 JWT, 获取 user context                                 │
│   - 校验 project_id 权限                                         │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ subscribe(audit_id) → queue                                    │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ Stream Loop:                                                   │
│   1. yield audit_snapshot (当前完整状态)                        │
│   2. if audit.done: yield audit_done; return                    │
│   3. while True:                                               │
│        event = await queue.get(timeout=30s)                      │
│        yield event (platform_start, platform_done, audit_done)    │
│        if event.type == "audit_done": break                      │
│   4. unsubscribe(audit_id, queue)                               │
└────────────────────────────────────────────────────────────────┘
```

### 3. 报告生成

```
┌────────────────────────────────────────────────────────────────┐
│ POST /api/audits/{audit_id}/report                             │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ generate_report(db, audit)                                     │
│   │                                                             │
│   ├─ 1. 聚合 QueryResult                                        │
│   │      by (brand_id, platform)                                │
│   │                                                             │
│   ├─ 2. 计算平台评分                                            │
│   │      platform_score = (mention_count / total) * 100         │
│   │                                                             │
│   ├─ 3. 计算总体评分                                            │
│   │      overall_score = avg(platform_scores)                   │
│   │                                                             │
│   ├─ 4. 计算提及率                                              │
│   │      mention_rate = mention_count / (brand_count * prompt_count) │
│   │                                                             │
│   ├─ 5. 提取洞察                                                │
│   │      - 低分平台 (< 60)                                       │
│   │      - 高排名品牌 (mention_position < 3)                      │
│   │                                                             │
│   └─ 6. 存储 Report                                            │
└────────────────────────────────────────────────────────────────┘
```

### 4. 定时审计

```
┌────────────────────────────────────────────────────────────────┐
│ [FastAPI startup]                                               │
│   start_scheduler()                                             │
│     asyncio.create_task(scheduler_loop())                       │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ scheduler_loop() (每 60 秒)                                     │
│   │                                                             │
│   ├─ 1. now = datetime.now(tz)                                  │
│   │                                                             │
│   ├─ 2. 查询 active ScheduledJob                                │
│   │                                                             │
│   └─ 3. for job in jobs:                                       │
│          if should_run_now(job.cron_expression, now):           │
│            _execute_scheduled_job(job)                          │
│              - create Audit(status=PENDING)                      │
│              - create_task(run_audit())                          │
│              - 完成后自动生成 Report                              │
└────────────────────────────────────────────────────────────────┘
```

### 5. AI 建议生成

```
┌────────────────────────────────────────────────────────────────┐
│ POST /api/suggestions/{project_id}/generate                     │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ generate_suggestions(db, report)                              │
│   │                                                             │
│   ├─ [收集上下文]                                                │
│   │   - audit.brands_json (品牌列表)                              │
│   │   - report.platform_scores (平台评分)                        │
│   │   - ResponseAnalysis 数据 (情感、话题、竞品)                 │
│   │                                                             │
│   ├─ [Pass 1: 战略建议]                                          │
│   │   LLM call → 5-8 条建议                                     │
│   │   {category, title, description, priority,                  │
│   │    target_platforms, action_channel}                         │
│   │                                                             │
│   ├─ [Pass 2: 详细方案]                                          │
│   │   for stub in stubs:                                        │
│   │     LLM call → {outline, keywords, timeline,                 │
│   │                    competitor_ref, expected_outcome}          │
│   │                                                             │
│   └─ [存储]                                                      │
│       for stub, detail in zip(stubs, details):                   │
│         Suggestion(                                              │
│           category=stub.category,                                │
│           title=stub.title,                                      │
│           description=stub.description,                           │
│           priority=stub.priority,                                │
│           detail=detail,  # Pass 2 结果                          │
│         )                                                        │
└────────────────────────────────────────────────────────────────┘
```

### 6. 智链 SSO 验证

```
┌────────────────────────────────────────────────────────────────┐
│ [前端] Authorization: Bearer {jwt_token}                         │
└─────────────────────────────┬──────────────────────────────────┘
                              │
┌─────────────────────────────▼──────────────────────────────────┐
│ get_current_user(token)                                        │
│   │                                                             │
│   ├─ 1. 解码 JWT (无签名验证)                                    │
│   │                                                             │
│   ├─ 2. 获取 kid (key id)                                       │
│   │                                                             │
│   ├─ 3. 从 JWKS 获取公钥                                        │
│   │      GET https://genilink.com/.well-known/jwks.json         │
│   │      cached 3600s                                           │
│   │                                                             │
│   ├─ 4. 验证 RS256 签名                                         │
│   │                                                             │
│   └─ 5. 返回 claims                                            │
│        {sub (user_id), email, workspace_id, project_ids[]}      │
└────────────────────────────────────────────────────────────────┘
```

---

## 配置管理

### 环境变量

| 变量名 | 用途 | 默认值 | 说明 |
|--------|------|--------|------|
| `AISCOPE_DATABASE_URL` | MySQL 连接串 | `mysql+aiomysql://aiscope:aiscope@localhost:3306/aiscope` | 使用 `+aiomysql` 驱动 |
| `AISCOPE_SECRET_KEY` | JWT 签名密钥 | (必填) | 用于本地开发时的 fallback |
| `AISCOPE_DEEPSEEK_API_KEY` | DeepSeek API | - | DashScope 托管的 DeepSeek |
| `AISCOPE_DEEPSEEK_BASE_URL` | DeepSeek 端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | |
| `AISCOPE_DEEPSEEK_MODEL` | DeepSeek 模型 | `deepseek-v3` | |
| `AISCOPE_QWEN_API_KEY` | 通义千问 API | - | |
| `AISCOPE_QWEN_BASE_URL` | 通义千问端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | |
| `AISCOPE_QWEN_MODEL` | 通义千问模型 | `qwen-plus` | |
| `AISCOPE_DOUBAO_API_KEY` | 豆包 API | - | |
| `AISCOPE_DOUBAO_BASE_URL` | 豆包端点 | `https://ark.cn-beijing.volces.com/api/v3` | |
| `AISCOPE_DOUBAO_MODEL` | 豆包模型 | `ep-20260531082219-dkkgl` | |
| `AISCOPE_KIMI_API_KEY` | Kimi API | - | |
| `AISCOPE_KIMI_BASE_URL` | Kimi 端点 | `https://api.moonshot.cn/v1` | |
| `AISCOPE_KIMI_MODEL` | Kimi 模型 | `moonshot-v1-8k` | |
| `AISCOPE_WENXIN_API_KEY` | 文心 API | - | |
| `AISCOPE_WENXIN_BASE_URL` | 文心端点 | `https://qianfan.baidubce.com/v2` | |
| `AISCOPE_WENXIN_MODEL` | 文心模型 | `ernie-4.0-8k` | |
| `AISCOPE_HUNYUAN_API_KEY` | 混元 API | - | |
| `AISCOPE_HUNYUAN_BASE_URL` | 混元端点 | `https://api.hunyuan.cloud.tencent.com/v1` | |
| `AISCOPE_HUNYUAN_MODEL` | 混元模型 | `hunyuan-lite` | |
| `AISCOPE_LLM_API_KEY` | 内部 LLM API 覆盖 | (继承 DEEPSEEK) | 用于建议生成等内部任务 |
| `AISCOPE_LLM_BASE_URL` | 内部 LLM 端点覆盖 | (继承 DEEPSEEK) | |
| `AISCOPE_LLM_MODEL` | 内部 LLM 模型覆盖 | (继承 DEEPSEEK) | |
| `AISCOPE_QUERY_TIMEOUT_SECONDS` | 查询超时 | `60` | 单平台总超时 |
| `AISCOPE_MAX_CONCURRENT_PER_PLATFORM` | 单平台并发 | `2` | Semaphore 容量 |
| `AISCOPE_TZ` | 调度器时区 | `Asia/Shanghai` | |
| `AISCOPE_CORS_ORIGINS` | CORS 允许来源 | `*` | 逗号分隔 |
| `AISCOPE_DEBUG` | 调试模式 | `0` | 启用 console 日志 |

### 配置加载

```python
class Settings(BaseSettings):
    model_config = {
        "env_file": ".env",           # 从项目根目录加载
        "env_prefix": "AISCOPE_",     # 所有变量需此前缀
    }

    def get_llm_config(self) -> tuple[str, str, str]:
        """返回 (api_key, base_url, model) 用于内部 LLM 任务"""
        return (
            self.llm_api_key or self.deepseek_api_key,
            self.llm_base_url or self.deepseek_base_url,
            self.llm_model or self.deepseek_model,
        )
```

---

## 扩展点

### 添加新平台适配器

1. **创建适配器文件** `adapters/new_platform.py`:
   ```python
   from app.adapters.openai_compat import OpenAICompatAdapter

   class NewPlatformAdapter(OpenAICompatAdapter):
       platform_name = "new_platform"
       base_url = "https://api.example.com/v1"
       api_key = settings.new_platform_api_key
       model = "model-name"
       search_enabled = True  # 如果支持联网搜索

       def _build_request_body(self, prompt: str) -> dict:
           """如果需要特殊请求格式, 重写此方法"""
           return {
               "model": self.model,
               "messages": [{"role": "user", "content": prompt}],
               "tools": [{"type": "web_search"}],  # 启用搜索示例
           }

       def _extract_citations(self, data: dict) -> list[dict]:
           """如果返回特殊引用格式, 重写此方法"""
           return data.get("search_results", [])
   ```

2. **注册到 registry**:
   ```python
   # adapters/registry.py
   from app.adapters.new_platform import NewPlatformAdapter

   _ADAPTERS: dict[str, type[PlatformAdapter]] = {
       ...
       "new_platform": NewPlatformAdapter,
   }

   PLATFORM_LABELS: dict[str, str] = {
       ...
       "new_platform": "新平台名称",
   }
   ```

3. **添加配置**:
   ```python
   # config.py
   new_platform_api_key: str = ""
   new_platform_base_url: str = "https://..."
   new_platform_model: str = "model-name"
   ```

### 添加新 API 端点

1. **创建路由文件** `api/new_feature.py`:
   ```python
   from fastapi import APIRouter, Depends
   from app.api.auth import get_current_user
   from app.api.access import require_project_scope

   router = APIRouter()

   @router.get("/{project_id}/data")
   async def get_data(
       project_id: str,
       current_user: dict = Depends(get_current_user),
   ):
       require_project_scope(current_user, project_id)
       # 业务逻辑
       return {"data": "..."}
   ```

2. **注册到 main**:
   ```python
   # main.py
   from app.api.new_feature import router as new_feature_router

   app.include_router(new_feature_router, prefix="/api/new-feature", tags=["new-feature"])
   ```

### 添加新审计阶段

1. **定义枚举值**:
   ```python
   # models/models.py
   class AuditStage(str, PyEnum):
       ...
       NEW_STAGE = "new_stage"
   ```

2. **插入执行逻辑**:
   ```python
   # services/audit_service.py
   async def _execute_audit(db: AsyncSession, audit: Audit) -> None:
       ...
       # 在 CALCULATING 之后插入
       new_stage = await _start_stage(db, audit, AuditStage.NEW_STAGE)
       try:
           # 阶段逻辑
           await _finish_stage(db, audit, new_stage, RunStatus.COMPLETED)
       except Exception as e:
           await _finish_stage(db, audit, new_stage, RunStatus.FAILED, error_message=str(e))
   ```

3. **更新前端映射** (如果需要显示):
   ```typescript
   // frontend 前端代码
   const stageLabels = {
       new_stage: "新阶段名称",
   }
   ```

---

## 关键文件索引

| 文件 | 行数估算 | 核心职责 |
|------|----------|----------|
| `main.py` | ~160 | FastAPI 应用入口, 路由注册, 生命周期钩子 |
| `config.py` | ~96 | 环境配置管理, LLM 配置获取 |
| `database.py` | ~30 | SQLAlchemy async session 工厂 |
| `middleware.py` | ~30 | 请求日志中间件 |
| `logging_config.py` | ~60 | structlog 配置 (JSON/Console) |
| `api/auth.py` | ~40 | JWT 验证依赖 `get_current_user()` |
| `api/audits.py` | ~310 | 审计 CRUD, SSE 事件流 |
| `api/analysis.py` | ~220 | 内容分析 API |
| `api/trends.py` | ~150 | 历史趋势数据 API |
| `api/suggestions.py` | ~85 | AI 建议管理 API |
| `api/schedules.py` | ~95 | 定时任务管理 API |
| `api/prompts.py` | ~100 | 提示词管理 API |
| `api/platforms.py` | ~40 | 平台列表与健康检查 |
| `api/strategic.py` | ~200 | 战略情报 API |
| `api/integration.py` | ~140 | GeniLink 集成汇总 API |
| `api/schemas.py` | ~270 | Pydantic 请求/响应模型 |
| `services/audit_service.py` | ~855 | 审计执行编排 (核心) |
| `services/report_service.py` | ~150 | 报告生成 |
| `services/scheduler.py` | ~163 | 定时任务调度 |
| `services/suggestion_service.py` | ~303 | AI 建议生成 |
| `services/prompt_gen_service.py` | ~200 | AI 提示词生成 |
| `services/detect.py` | ~150 | 品牌检测 |
| `services/source_extraction.py` | ~100 | 来源提取 |
| `services/response_analysis_service.py` | ~200 | 响应深度分析 |
| `services/audit_events.py` | ~80 | SSE 事件管理 |
| `services/genilink_auth.py` | ~150 | 智链 SSO 验证 |
| `adapters/base.py` | ~80 | PlatformAdapter 抽象基类 |
| `adapters/openai_compat.py` | ~173 | OpenAI 兼容基类 |
| `adapters/registry.py` | ~45 | 适配器注册表 |
| `models/models.py` | ~373 | SQLAlchemy ORM 定义 |

---

*文档版本: 2026-06-09*
*对应代码版本: main branch*
