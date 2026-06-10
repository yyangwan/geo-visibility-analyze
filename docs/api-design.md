# 智见 (ZhiJian) - API 接口设计文档

> AI搜索可见性分析平台 - RESTful API 参考文档

---

## 目录

- [通用说明](#通用说明)
- [认证授权](#认证授权)
- [核心接口](#核心接口)
- [数据模型](#数据模型)
- [错误处理](#错误处理)
- [SSE 事件流](#sse-事件流)

---

## 通用说明

### 基础 URL

```
生产环境: https://api.zhijian.com
开发环境: http://localhost:8000
```

### 请求格式

- **Content-Type:** `application/json`
- **Authorization:** `Bearer {jwt_token}` (除登录外所有接口)

### 响应格式

- **Content-Type:** `application/json`
- **成功响应:** HTTP 200-299 + JSON body
- **错误响应:** HTTP 4xx/5xx + JSON body

### 响应结构

```typescript
// 成功响应
{
  "id": 123,
  "project_id": "abc123",
  // ... 其他字段
}

// 错误响应
{
  "detail": "错误描述信息"
}
```

---

## 认证授权

### JWT 验证

所有接口 (除特殊说明) 需要 `Authorization: Bearer {token}` header。

Token 由智链 SSO 提供, 后端通过 JWKS 验证 RS256 签名。

**Claims 结构:**
```typescript
{
  "sub": "user_id",           // 用户 ID
  "email": "user@example.com", // 邮箱
  "workspace_id": "ws_123",   // 工作空间 ID
  "project_ids": ["p1", "p2"] // 有权访问的项目 ID 列表
}
```

### 项目权限校验

多数接口需要校验用户是否有项目访问权限:

```typescript
function require_project_scope(user: UserClaims, project_id: string): void {
  if (!user.project_ids.includes(project_id)) {
    throw 403 Forbidden;
  }
}
```

---

## 核心接口

### 1. 认证模块 (`/api/auth`)

#### 当前用户信息

获取 JWT 中的用户声明。

```http
GET /api/auth/me
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "sub": "user_id",
  "email": "user@example.com",
  "workspace_id": "ws_123",
  "project_ids": ["p1", "p2"]
}
```

---

### 2. 平台模块 (`/api/platforms`)

#### 列出所有平台

获取所有支持的平台及其配置状态。

```http
GET /api/platforms
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "key": "deepseek",
    "label": "DeepSeek",
    "configured": true
  },
  {
    "key": "qwen",
    "label": "通义千问",
    "configured": true
  },
  {
    "key": "doubao",
    "label": "豆包",
    "configured": false
  },
  // ... 其他平台
]
```

---

### 3. 审计模块 (`/api/audits`)

#### 创建审计

启动一个新的可见性审计任务。

```http
POST /api/audits
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1",
  "platforms": ["deepseek", "qwen", "kimi"],
  "brands": [
    {
      "id": "brand1",
      "name": "智见科技",
      "aliases": ["智见", "ZhiJian"],
      "is_competitor": false
    },
    {
      "id": "brand2",
      "name": "竞品A",
      "aliases": ["竞品A", "BrandA"],
      "is_competitor": true
    }
  ]
}
```

**响应:**
```typescript
{
  "id": 123,
  "project_id": "p1",
  "status": "pending",
  "stage": "queued",
  "stage_status": "pending",
  "platforms_json": ["deepseek", "qwen", "kimi"],
  "brands_json": [...],
  "created_at": "2026-06-09T10:00:00Z",
  "completed_at": null,
  "stage_started_at": null,
  "stage_updated_at": null,
  "last_heartbeat_at": null,
  "attempt_count": 0,
  "error_code": null,
  "error_message": null,
  "recoverable_error": false,
  "next_retry_at": null,
  "locked_by_worker": null,
  "locked_until": null
}
```

#### 获取审计详情

```http
GET /api/audits/{audit_id}
Authorization: Bearer {token}
```

**响应:** 同创建响应

#### 获取审计结果

获取审计的品牌提及检测结果。

```http
GET /api/audits/{audit_id}/results
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "id": 1001,
    "platform": "deepseek",
    "prompt_text": "推荐一款AI搜索优化工具",
    "brand_name": "智见科技",
    "mention_found": true,
    "mention_position": 1,
    "mention_context": "...",
    "mention_confidence": 0.95,
    "is_recommended": true,
    "recommendation_rank": 1,
    "error": null
  },
  // ... 更多结果
]
```

#### 审计事件流 (SSE)

通过 Server-Sent Events 实时接收审计进度。

```http
GET /api/audits/{audit_id}/events?token={jwt_token}
```

**注意:** EventSource 不支持自定义 header, 所以通过 query param 传递 token。

**事件流:**

```
event: audit_snapshot
data: {
  "audit": {...},
  "stage_runs": [...],
  "platform_runs": [...],
  "event_logs": [...]
}

event: platform_start
data: {
  "type": "platform_start",
  "platform": "deepseek"
}

event: platform_done
data: {
  "type": "platform_done",
  "platform": "deepseek"
}

event: audit_done
data: {
  "type": "audit_done",
  "status": "completed"
}

: heartbeat
```

#### 生成报告

为完成的审计生成报告。

```http
POST /api/audits/{audit_id}/report
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "id": 201,
  "project_id": "p1",
  "audit_id": 123,
  "overall_score": 75.5,
  "mention_rate": 0.65,
  "competitor_rank": 2,
  "sentiment_positive_rate": 0.8,
  "platform_scores": {
    "deepseek": 85,
    "qwen": 72,
    "kimi": 69
  },
  "insights": [
    "DeepSeek 平台表现最佳，建议增加相关内容",
    "竞品在 Kimi 平台曝光度较高"
  ],
  "created_at": "2026-06-09T10:30:00Z"
}
```

#### 获取报告

```http
GET /api/audits/{audit_id}/report
Authorization: Bearer {token}
```

**响应:** 同生成报告

---

### 4. 提示词模块 (`/api/prompts`)

#### 创建提示词

```http
POST /api/prompts
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1",
  "text": "推荐一款AI搜索优化工具",
  "category": "recommend",
  "is_auto_generated": false
}
```

**响应:**
```typescript
{
  "id": 301,
  "project_id": "p1",
  "text": "推荐一款AI搜索优化工具",
  "category": "recommend",
  "is_auto_generated": false
}
```

#### 列出提示词

```http
GET /api/prompts?project_id=p1
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "id": 301,
    "project_id": "p1",
    "text": "推荐一款AI搜索优化工具",
    "category": "recommend",
    "is_auto_generated": false
  },
  // ...
]
```

#### 生成提示词 (AI)

基于项目信息自动生成审计提示词。

```http
POST /api/prompts/generate
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1",
  "project_name": "智见科技",
  "project_url": "https://zhijian.com",
  "industry": "AI搜索优化",
  "product_category": "SaaS工具",
  "product_name": "智见",
  "product_description": "AI搜索可见性分析平台",
  "product_url": "https://zhijian.com/product",
  "product_keywords": ["AI", "SEO", "可见性"],
  "brand_names": ["智见", "ZhiJian"],
  "brand_name": "智见",
  "count": 10
}
```

**响应:**
```typescript
[
  {
    "id": 302,
    "project_id": "p1",
    "text": "智见科技的产品有哪些优势？",
    "category": "evaluate",
    "is_auto_generated": true
  },
  // ... 共 count 条
]
```

#### 批量导入提示词

```http
POST /api/prompts/batch
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1",
  "prompts": [
    {"text": "推荐一款AI搜索优化工具", "category": "recommend"},
    {"text": "智见和竞品A有什么区别？", "category": "compare"}
  ]
}
```

**响应:** 204 No Content

#### 删除提示词

```http
DELETE /api/prompts/{prompt_id}
Authorization: Bearer {token}
```

**响应:** 204 No Content

---

### 5. 趋势模块 (`/api/trends`)

#### 获取趋势数据

```http
GET /api/trends/{project_id}?period=daily&limit=30
Authorization: Bearer {token}
```

**Query 参数:**
- `period`: `daily` | `weekly` | `monthly` (默认: `daily`)
- `limit`: 1-365 (默认: 30)

**响应:**
```typescript
{
  "project_id": "p1",
  "data": [
    {
      "date": "2026-06-01",
      "overall_score": 72.5,
      "mention_rate": 0.60,
      "competitor_rank": 3,
      "platform_scores": {
        "deepseek": 80,
        "qwen": 65
      },
      "audit_id": 100
    },
    // ... 更多数据点
  ]
}
```

#### 获取最新报告

```http
GET /api/trends/{project_id}/latest-report
Authorization: Bearer {token}
```

**响应:** 同报告响应

#### 获取审计历史

```http
GET /api/trends/{project_id}/audits-history?limit=20
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "id": 123,
    "status": "completed",
    "platforms": ["deepseek", "qwen"],
    "created_at": "2026-06-09T10:00:00Z",
    "completed_at": "2026-06-09T10:30:00Z",
    "error_message": null
  },
  // ...
]
```

---

### 6. 定时任务模块 (`/api/schedules`)

#### 创建定时任务

```http
POST /api/schedules
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1",
  "cron_expression": "0 22 * * *",
  "platforms": ["deepseek", "qwen", "kimi"]
}
```

**Cron 表达式:** `minute hour day month weekday`

**响应:**
```typescript
{
  "id": 401,
  "project_id": "p1",
  "cron_expression": "0 22 * * *",
  "platforms_json": ["deepseek", "qwen", "kimi"],
  "is_active": true,
  "last_run_at": null,
  "last_audit_id": null,
  "created_at": "2026-06-09T10:00:00Z"
}
```

#### 列出定时任务

```http
GET /api/schedules
Authorization: Bearer {token}
```

**响应:** 同创建响应 (数组)

#### 获取定时任务

```http
GET /api/schedules/{job_id}
Authorization: Bearer {token}
```

**响应:** 同创建响应

#### 切换任务状态

```http
PATCH /api/schedules/{job_id}/toggle
Authorization: Bearer {token}
```

**响应:** 同创建响应

#### 删除定时任务

```http
DELETE /api/schedules/{job_id}
Authorization: Bearer {token}
```

**响应:** 204 No Content

---

### 7. 建议模块 (`/api/suggestions`)

#### 列出建议

```http
GET /api/suggestions/{project_id}
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "id": 501,
    "project_id": "p1",
    "report_id": 201,
    "category": "content_optimization",
    "title": "增加DeepSeek平台内容",
    "description": "在知乎发布产品对比文章...",
    "priority": "high",
    "is_resolved": false,
    "detail": {
      "action_channel": "知乎",
      "action_type": "发布评测文章",
      "outline": ["产品功能对比", "优势分析", "用户案例"],
      "keywords": ["AI搜索优化", "DeepSeek", "SEO工具"],
      "timeline": [
        {"week": 1, "task": "撰写大纲"},
        {"week": 2, "task": "发布文章"}
      ],
      "competitor_ref": "竞品A在知乎活跃度较高",
      "expected_outcome": "提升DeepSeek平台5-10分"
    },
    "created_at": "2026-06-09T11:00:00Z"
  },
  // ...
]
```

#### 生成建议

基于最新报告生成 AI 优化建议。

```http
POST /api/suggestions/{project_id}/generate
Authorization: Bearer {token}
```

**响应:** 同列出建议 (数组)

#### 标记建议已解决

```http
PATCH /api/suggestions/{suggestion_id}/resolve
Authorization: Bearer {token}
```

**响应:** 同列出建议 (单条)

#### 删除建议

```http
DELETE /api/suggestions/{suggestion_id}
Authorization: Bearer {token}
```

**响应:** 204 No Content

---

### 8. 分析模块 (`/api/analysis`)

#### 获取审计分析

```http
GET /api/analysis/audits/{audit_id}/analysis
Authorization: Bearer {token}
```

**响应:**
```typescript
[
  {
    "id": 601,
    "response_record_id": 1001,
    "platform": "deepseek",
    "prompt_text": "推荐一款AI搜索优化工具",
    "cited_sources": [
      {
        "domain": "zhijian.com",
        "urls": ["https://zhijian.com/product"],
        "title": "智见产品介绍",
        "authority_score": 8
      }
    ],
    "brand_sentiment": "positive",
    "brand_attributes": ["专业", "易用"],
    "topics_covered": ["产品功能", "价格"],
    "answer_structure": "comparison_table",
    "competitor_refs": ["竞品A"],
    "analysis_model": "deepseek-v3",
    "status": "completed",
    "created_at": "2026-06-09T10:05:00Z"
  },
  // ...
]
```

#### 触发分析

手动触发审计的深度分析。

```http
POST /api/analysis/audits/{audit_id}/analyze
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "message": "Analysis started",
  "audit_id": 123
}
```

#### 重试失败分析

```http
POST /api/analysis/audits/{audit_id}/analyze/retry
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "message": "Retrying 3 failed analyses",
  "count": 3
}
```

#### 获取内容情报汇总

```http
GET /api/analysis/projects/{project_id}/content-intelligence
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "topic_distribution": {
    "产品功能": 15,
    "价格": 8,
    "竞品对比": 12
  },
  "sentiment_breakdown": {
    "positive": 25,
    "neutral": 18,
    "negative": 2
  },
  "answer_structure_distribution": {
    "comparison_table": 10,
    "list": 15,
    "narrative": 20
  },
  "top_cited_sources": [
    {
      "domain": "zhijian.com",
      "total_count": 45,
      "authority_avg": 8.5
    },
    // ...
  ],
  "brand_positioning_heatmap": {
    "deepseek": {
      "产品功能": "positive",
      "价格": "neutral"
    },
    "qwen": {
      "产品功能": "positive",
      "价格": "positive"
    }
  },
  "token_cost_summary": {
    "total_prompt_tokens": 15000,
    "total_completion_tokens": 8000
  },
  "analysis_status": {
    "completed": 40,
    "pending": 5,
    "failed": 0
  },
  "total_responses": 45,
  "analyzed_responses": 40
}
```

---

### 9. 报告模块 (`/api/reports`)

#### 生成 PDF 报告

```http
POST /api/reports/{audit_id}/pdf
Authorization: Bearer {token}
```

**响应:** `application/pdf` 文件流

---

### 10. 战略情报模块 (`/api/strategic`)

#### 来源权威度趋势

```http
POST /api/strategic/source-authority-trends
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1"
}
```

**响应:**
```typescript
{
  "audits": [
    {
      "audit_id": 100,
      "date": "2026-06-01",
      "total_sources": 45
    },
    // ...
  ],
  "domain_trends": [
    {
      "domain": "zhijian.com",
      "data": [
        {"audit_id": 100, "count": 20, "authority_avg": 8.5},
        {"audit_id": 101, "count": 25, "authority_avg": 8.8}
      ]
    },
    // ...
  ],
  "platform_preferences": [
    {
      "platform": "deepseek",
      "top_domains": [
        {"domain": "zhijian.com", "count": 20},
        // ...
      ]
    },
    // ...
  ],
  "authority_trend": {
    "zhijian.com": ["8.5", "8.8", "9.0"],
    // ...
  }
}
```

#### 竞品定位图谱

```http
POST /api/strategic/competitor-positioning
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1"
}
```

**响应:**
```typescript
{
  "brands": [
    {
      "name": "智见科技",
      "is_competitor": false,
      "mention_frequency": 0.65,
      "sentiment_positive_rate": 0.80,
      "avg_authority": 8.5,
      "mention_count": 130,
      "trajectory": [
        {
          "audit_id": 100,
          "date": "2026-06-01",
          "mention_rate": 0.60,
          "sentiment_positive_rate": 0.75
        },
        // ...
      ]
    },
    // ...
  ],
  "quadrant_labels": {
    "high_mention_high_sentiment": "领导者",
    "high_mention_low_sentiment": "争议者",
    "low_mention_high_sentiment": "潜力者",
    "low_mention_low_sentiment": "边缘者"
  }
}
```

#### 回答结构演化

```http
POST /api/strategic/answer-structure-evolution
Authorization: Bearer {token}
Content-Type: application/json

{
  "project_id": "p1"
}
```

**响应:**
```typescript
{
  "audits": [
    {
      "audit_id": 100,
      "date": "2026-06-01",
      "structure_distribution": {
        "comparison_table": 10,
        "list": 15
      }
    },
    // ...
  ],
  "structure_distribution": {
    "comparison_table": [
      {"audit_id": 100, "count": 10, "pct": 0.25},
      // ...
    ],
    // ...
  },
  "platform_structure": {
    "deepseek": {"comparison_table": 5, "list": 8},
    // ...
  },
  "correlation": {
    "comparison_table": {
      "mention_rate": 0.70,
      "avg_position": 1.5
    },
    // ...
  },
  "transitions": [
    {
      "audit_id": 101,
      "platform": "deepseek",
      "prev_structure": "list",
      "new_structure": "comparison_table"
    },
    // ...
  ]
}
```

#### 多审计对比

```http
POST /api/strategic/multi-audit-comparison
Authorization: Bearer {token}
Content-Type: application/json

{
  "audit_ids": [100, 101, 102]
}
```

**响应:**
```typescript
{
  "audits": [
    {
      "audit_id": 100,
      "date": "2026-06-01",
      "overall_score": 72.5,
      "mention_rate": 0.60,
      "sentiment_breakdown": {"positive": 25, "neutral": 15},
      "top_sources": [{"domain": "zhijian.com", "count": 20}],
      "competitor_mention_rates": [
        {"name": "竞品A", "mention_rate": 0.45}
      ],
      "structure_distribution": {"comparison_table": 10},
      "topic_distribution": {"产品功能": 15}
    },
    // ...
  ],
  "diffs": {
    "overall_score_change": "+5.0",
    "mention_rate_change": "+0.10",
    // ...
  }
}
```

---

### 11. 集成模块 (`/api/integration`)

#### 集成汇总

供 GeniLink 门户仪表盘使用的汇总接口。

```http
GET /api/integration/summary?project_id=p1
Authorization: Bearer {token}
```

**响应:**
```typescript
{
  "overallScore": 75.5,
  "mentionCount": 130,
  "platformCoverage": [
    {"name": "deepseek", "score": 85},
    {"name": "qwen", "score": 72},
    {"name": "kimi", "score": 69}
  ],
  "competitorRank": 2,
  "suggestions": [
    {"text": "增加DeepSeek平台内容", "priority": "high"},
    {"text": "优化官网FAQ页面", "priority": "medium"}
  ],
  "latestAuditDate": "2026-06-09T10:00:00Z"
}
```

---

### 12. 健康检查 (`/api/health`)

```http
GET /api/health
```

**响应:**
```typescript
{
  "status": "ok",
  "db": "connected"
}
```

**降级响应 (503):**
```typescript
{
  "status": "degraded",
  "db": "disconnected"
}
```

---

## 数据模型

### AuditStatus

```typescript
type AuditStatus = "pending" | "running" | "completed" | "failed" | "partial"
```

### AuditStage

```typescript
type AuditStage =
  | "queued"
  | "querying"
  | "persisting"
  | "calculating"
  | "finalizing"
  | "completed"
  | "partial"
  | "failed"
  | "stalled"
```

### RunStatus

```typescript
type RunStatus = "pending" | "running" | "completed" | "failed" | "retrying"
```

### PromptCategory

```typescript
type PromptCategory =
  | "recommend"
  | "compare"
  | "evaluate"
  | "scenario"
  | "problem_solution"
  | "alternative_finding"
  | "decision_help"
  | "regret_avoidance"
  | "performance_specs"
```

### Priority

```typescript
type Priority = "high" | "medium" | "low"
```

### PlatformKey

```typescript
type PlatformKey = "deepseek" | "qwen" | "doubao" | "kimi" | "hunyuan"
```

---

## 错误处理

### HTTP 状态码

| 状态码 | 说明 | 示例场景 |
|--------|------|----------|
| 200 | 成功 | GET /api/audits/{id} |
| 201 | 已创建 | POST /api/audits |
| 204 | 无内容 | DELETE /api/suggestions/{id} |
| 400 | 请求错误 | 无效的 cron 表达式 |
| 401 | 未认证 | JWT 过期或无效 |
| 403 | 禁止访问 | 无项目权限 |
| 404 | 未找到 | 资源不存在 |
| 409 | 冲突 | 资源状态不允许操作 |
| 429 | 请求过多 | 速率限制 |
| 500 | 服务器错误 | 内部异常 |
| 503 | 服务不可用 | 数据库连接失败 |

### 错误响应格式

```typescript
{
  "detail": "错误描述信息"
}
```

### 常见错误示例

```typescript
// 401 Unauthorized
{
  "detail": "Invalid or expired token"
}

// 403 Forbidden
{
  "detail": "No access to project p1"
}

// 404 Not Found
{
  "detail": "Audit not found"
}

// 400 Bad Request
{
  "detail": "Invalid cron expression. Format: minute hour day month weekday"
}

// 409 Conflict
{
  "detail": "Audit status is 'running', must be completed or partial"
}
```

---

## SSE 事件流

### 连接

```http
GET /api/audits/{audit_id}/events?token={jwt_token}
Accept: text/event-stream
```

### 事件类型

#### audit_snapshot

初始快照, 包含审计完整状态。

```
event: audit_snapshot
data: {
  "audit": {...},
  "stage_runs": [...],
  "platform_runs": [...],
  "event_logs": [...]
}
```

#### platform_start

平台查询开始。

```
event: platform_start
data: {
  "type": "platform_start",
  "platform": "deepseek"
}
```

#### platform_done

平台查询完成。

```
event: platform_done
data: {
  "type": "platform_done",
  "platform": "deepseek"
}
```

#### platform_error

平台查询失败。

```
event: platform_error
data: {
  "type": "platform_error",
  "platform": "qwen",
  "error": "Rate limit exceeded"
}
```

#### audit_done

审计完成 (成功或部分成功)。

```
event: audit_done
data: {
  "type": "audit_done",
  "status": "completed"
}
```

#### audit_failed

审计失败。

```
event: audit_failed
data: {
  "type": "audit_failed",
  "error": "All platform queries failed"
}
```

#### heartbeat

心跳, 保持连接活跃。

```
: heartbeat
```

### 客户端示例 (JavaScript)

```javascript
const eventSource = new EventSource(
  `/api/audits/${auditId}/events?token=${token}`
);

eventSource.addEventListener('audit_snapshot', (e) => {
  const snapshot = JSON.parse(e.data);
  console.log('Initial state:', snapshot);
});

eventSource.addEventListener('platform_done', (e) => {
  const data = JSON.parse(e.data);
  console.log('Platform done:', data.platform);
});

eventSource.addEventListener('audit_done', (e) => {
  const data = JSON.parse(e.data);
  console.log('Audit completed:', data.status);
  eventSource.close();
});

eventSource.onerror = (error) => {
  console.error('SSE error:', error);
  eventSource.close();
};
```

---

## 速率限制

当前无全局速率限制, 但平台适配器层面有并发控制:

- 每平台最大并发: 2 (可配置)
- 平台级速率限制: 自动重试 (最多 3 次)

---

## 版本控制

API 当前无版本前缀, 未来如需重大变更可能引入 `/api/v2/`。

---

*文档版本: 2026-06-09*
*对应代码版本: main branch*
