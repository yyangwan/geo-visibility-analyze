# 智见分析后端

本仓库包含分析 API、独立 worker 及引用情报链路。引用情报当前处于预发布阶段，整体方案尚未完成；源码检查、镜像验证和本地联调结果不能代替真实设备采集、OSS 对象读写、独立人工质量评测或生产验收。

## 源码与镜像交付

后端位于 `backend/`。`.github/workflows/backend-ci.yml` 在相关 PR 上执行源码检查、全量回归、真实 MySQL 迁移与并发测试，再构建一次 API/worker 共用镜像并断网检查运行入口、OSS SDK、迁移 head、开发对象完整性及中文 PDF。

仅 `main` 的 workflow 发布通过检查的同一镜像，地址为 `ghcr.io/yyangwan/geo-visibility-analyze:<完整提交 SHA>`。部署身份使用完整 SHA 并核对镜像 revision 标签；此 workflow 不自动部署服务或迁移生产数据库。

`compose.intelligence-runtime.yml` 提供独立预发布环境，使用事先加载的镜像、明确指定的新数据库及私密运行配置，分别运行 API 和 worker。它没有源码挂载、构建或自动迁移，仅 API 绑定本机端口。实际运行命令、镜像传输和首次启用条件见下列指南。

## 引用情报操作指南

| 指南 | 内容 |
| --- | --- |
| [运行与首次启用](docs/citation-intelligence-runtime.md) | 配置、迁移、worker、实现记录及尚未完成的验收 |
| [镜像发布与独立联调](docs/citation-intelligence-image-release.md) | PR/main 镜像流程、镜像传输、独立 API/worker 与停止步骤 |
| [生产部署与回退](docs/citation-intelligence-production-deploy.md) | 手动 main 发布、新库保护、API/worker 蓝绿切换及首次 legacy 回退 |
| [本地 Docker MySQL](docs/citation-intelligence-local-mysql.md) | 专用空库、隔离测试库及本地验证边界 |
| [正文存储验收](docs/citation-intelligence-storage-check.md) | OSS 配置检查、显式对象探针及失败报告 |
| [近期真实输入准备](docs/citation-intelligence-acceptance-intake.md) | 只读冻结近期输入，准备人工核对 |
| [质量评测与独立标注](docs/citation-intelligence-evaluation.md) | 私有样本导出、双人标注、留出边界及指标解释 |
| [备份包与迁移互斥](docs/citation-intelligence-backup.md) | 后端数据库与正文备份完整性、尚未实现的恢复与保留能力 |

截至 2026-10-17，本地 Linux 镜像断网检查和连接专用 MySQL 的独立 API/worker 容器联调已验证，ECS 角色临时凭证通路已核对。真实 OSS 对象 PUT/GET、GitHub Actions 实际发布和生产启用仍待执行。成本结算、引用感知保留、跨系统恢复及真实质量闭环仍有未完成工作，不能据此宣称整体已验收。
