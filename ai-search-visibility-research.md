# AI搜索可见性研究调研汇总

> 调研时间：2026-05-08
> 触发文章：Semrush《We Analyzed 248K Reddit Posts: What Drives Visibility in AI Search》
> 核心问题：Semrush声称分析217K个AI搜索prompt的数据是怎么获取的？国内有没有类似服务？

---

## 一、Semrush研究真实性评估

### 文章核心数据
- 声称分析了217,000个独特prompt（Google AI Mode / Perplexity / ChatGPT Search）
- 识别出248,000个被引用的Reddit URL
- 数据于2025年10月刷新

### 真实性判断

| 维度 | 评分 | 说明 |
|------|------|------|
| 数据规模可行性 | 8/10 | 217K prompt在技术上完全可行，API+无头浏览器成本$8K-$24K，对Semrush($3.5亿+年收入)微不足道 |
| 结论方向正确性 | 8/10 | Reddit是AI搜索重要引用源，被多个独立研究验证 |
| 方法论透明度 | 3/10 | 只有一段话，未披露技术细节、prompt来源、验证方法 |
| 数据精确度 | 5/10 | 模拟查询而非真实用户数据，具体百分比存疑 |

### 标题误导
标题说"analyzed 248K Reddit Posts"，实际分析的是217K个AI搜索结果中引用的Reddit链接，并非直接分析Reddit帖子。

### 数据获取技术路径

```
Semrush内部prompt数据库（声称1亿+）
         │
         ▼ 筛选217K个prompt
    ┌────────┼────────┐
    ▼        ▼        ▼
 Perplexity  Google   ChatGPT
 (官方API)  AI Mode  Search
    │      (无头浏览器) (无头浏览器)
    ▼        ▼        ▼
  解析AI响应中的引用链接、品牌提及、情感倾向
```

| 平台 | 技术手段 | 成本估算(217K请求) |
|------|---------|-------------------|
| Perplexity | 官方API (~$0.014-$0.04/请求) | ~$3K-$8.7K |
| Google AI Mode | 无头浏览器+代理IP轮换 | ~$5K-$15K |
| ChatGPT Search | 无头浏览器或第三方抓取服务 | ~$5K-$15K |

关键点：这些prompt是Semrush**自己合成/筛选**的，不是真实用户数据。独立评价称其"directionally accurate but not fully precise"。

### 已有商业抓取服务
- ScrapeLLM — 批量抓取ChatGPT/Perplexity/Gemini
- thruuu LLM API — 同时查询多个AI平台
- Apify AI Search Visibility Tracker
- Infatica AI Search Data API
- A-Parser for AI

---

## 二、国内GEO服务生态

### 行业背景
- 2026年被称为"GEO元年"
- AI搜索渗透率超74%（据新浪财经）
- 国内主流AI平台：豆包、DeepSeek、Kimi、文心一言、通义千问、腾讯元宝
- 国内AI平台大多有官方API且价格极低，批量查询成本远低于海外

### 核心服务商

#### 1. 犀帆 (Seenify) — 综合评分最高
- **官网：** https://www.seenify.cn/
- **所属公司：** 杭州念响科技有限公司（2019年成立，2025年上线Seenify）
- **团队背景：** 核心来自字节跳动、百度、阿里达摩院
- **覆盖：** 30+国内外AI平台（豆包、DeepSeek、Kimi、通义千问、文心一言、腾讯元宝、ChatGPT、Gemini等）
- **方法论：** TDOG闭环 — Track → Diagnose → Optimize → Generate
- **指标体系：** "4+1维"（可见性、理解度、偏好度、推荐度、风险指数）
- **Prompt管理：** 手动添加 + CSV批量导入 + 系统自动推荐
- **商业模式：** 工具免费体验 + 定制化GEO优化服务
- **客户续约率：** 90%+
- **CSDN火山引擎评测：** 97/100分（核心功能29/30，技术实力24/25）
- **案例：** 某宠物食品品牌可见性提升180%；某口腔医院本地排名从第15升至第2；中宠集团AI推荐率提升60%
- **特点：** 不维护独立prompt数据库，用户驱动定义监控prompt；国内API直连，不需要无头浏览器抓取

#### 2. BotGEO.AI — 营销团队优选
- **综合评分：** 89分
- **核心优势：** 提示词精细化管理、CSV批量导入、API集成、可视化仪表盘
- **覆盖：** 豆包、DeepSeek、千问、元宝、文心一言等
- **特点：** 支持Prompt地理位置标注、标签管理；72小时AI算法变动预测

#### 3. 百分数 Generforce — 中文轻量监测
- **综合评分：** 91分
- **核心优势：** 情感分析精准、易上手
- **短板：** 无海外覆盖、智能优化策略基础

#### 4. 悠易科技 Mentis — 大型企业首选
- **入选中国信通院数字化转型全景图**
- **独创三大工程：** 意图工程、认知工程、信誉工程
- **特色：** AI编辑器一键改写为"高引用概率"格式；品牌专属向量知识库（可导入CRM/CMS）
- **案例：** 罗技合作获艾菲奖，核心产品AI权威引用率6个月提升35%

#### 5. 万数科技
- **综合评分：** 95.8分（另一评测体系）
- **自研：** DeepReach垂直模型 + "天机图"系统
- **客户续约率：** 92%
- **案例：** 品牌AI推荐率从8%提升至45%+

#### 6. 其他
- **GT-GEO：** 覆盖6大国内AI平台
- **杭州爱搜索人工智能：** 中小企业高性价比之选，续约率85%
- **新榜智汇：** 新榜旗下，15款国内+20+海外AI平台
- **灵狐科技：** 率先布局GEO赛道

### 海外工具（不覆盖国内AI平台）
- **Peec AI：** ChatGPT/Perplexity/Claude/Gemini
- **Dageno：** 全渠道跟踪
- **Profound：** 500M+ prompt数据库
- **Gauge：** 跨语言海外平台监测（$49/月起）

---

## 三、国内vs海外批量查询对比

| 维度 | 海外(Semrush) | 国内(犀帆等) |
|------|-------------|-------------|
| 目标平台 | ChatGPT/Perplexity/Google AI Mode | 豆包/DeepSeek/Kimi/通义千问/文心一言 |
| API可用性 | 仅Perplexity有API，其他需无头浏览器抓取 | 大多有官方API，成本极低 |
| 217K prompt成本 | ~$8K-$24K | ~$500-$2,000（低1-2个数量级） |
| 技术门槛 | 高（无头浏览器+代理IP+反爬） | 低（直接调API） |
| prompt来源 | 公司自建数据库，程序化合成 | 用户定义+CSV导入+自动推荐 |
| 覆盖平台数 | 3个 | 30+个 |
| 商业模式 | SaaS订阅($139+/月) | 工具免费+定制化服务 |

---

## 四、关键数据源
- Semrush原文：https://www.semrush.com/blog/reddit-ai-search-visibility-study/
- Semrush AI Toolkit方法论：https://www.semrush.com/kb/1493-ai-visibility-toolkit
- Profound对Semrush评测：https://www.tryprofound.com/blog/semrush-ai-visibility-toolkit-review
- CSDN火山引擎评测：https://adg.csdn.net/6972f281437a6b40336b58e2.html
- 掘金GEO深度评测：https://juejin.cn/post/7595167566144339987
- 博客园犀帆口碑评价：https://www.cnblogs.com/newjpz/p/19759314
- 火山引擎服务商对比：https://developer.volcengine.com/articles/7615988029970219062
- 知乎GEO排名工具指南：https://zhuanlan.zhihu.com/p/1956381519598813941
- IT之家GEO服务商排名：https://www.ithome.com/0/946/150.htm
- 界面新闻十大GEO厂商：https://m.jiemian.com/article/14303455.html
