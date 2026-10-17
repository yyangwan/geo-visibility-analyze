# Changelog

## [1.1.0.1] - 2026-10-18

### Added

- Add an immutable-image production deployment workflow, isolated database guard, candidate-specific worker acceptance, and verified blue/green rollback.

### Fixed

- Disable development authentication in production and expose the exact deployment revision in health checks.

## [1.1.0.0] - 2026-10-17

### Added

- 保存回答、引用观测与来源正文，分析全部已发现实体，并按平台分别提供提及、引用、证据与复测结果。
- 提供原文复核、事实授权、优化行动、内容 Brief、中文报告及 PDF 接口，结论可追溯至对应输入修订和原文片段。
- 增加独立持久任务 worker、私有 OSS 与开发存储、运行诊断、评测导出及数据库/正文联合备份校验工具。
- 提供构建后验收的完整 SHA 镜像和独立联调环境；发布镜像不自动切换生产或迁移数据库。

### Changed

- 新方案使用专用新数据库，启用前执行受审基线迁移；API 与 worker 启动时仅验证 schema，不原地升级旧库。

### Fixed

- 取消 PDF 请求后，实际渲染线程继续持有并发许可，防止突破并发上限并遗漏后台异常。
- 允许显式恢复最新失败分析，保留原始输入与请求预算并拦截旧租约；自动刷新不重置耗尽任务。
- 手动分析重新核验 MySQL 当前状态，避免旧事务快照导致重复请求冲突或恢复已被替代的分析。

本版为预发布源码与镜像工具交付。真实 OSS、设备采集、独立质量评测和生产启用仍须完成相应验收。
