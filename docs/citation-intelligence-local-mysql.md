# 本地 Docker 空库联调

2026-10-17 已实际运行。使用专用 Compose 项目，保留原来的容器、数据库和数据卷，不修改工作区已有 `.env` 或远程数据库。

## 资源

| 项目 | 实际值 |
| --- | --- |
| Compose 文件 | `backend/compose.intelligence-local.yml` |
| 项目 / 容器 | `genilink-intelligence-local` / `genilink-intelligence-local-mysql` |
| 镜像 | `mysql:8.0` |
| 端口 | `127.0.0.1:13316` → 容器 `3306`，不向局域网开放 |
| 持久卷 | `genilink-intelligence-local-mysql-data` |
| 开发库 | `intelligence_dev_local` |
| 独立测试库 | `intelligence_ci_local` |
| 应用用户 | `intelligence_local`，仅上述两库权限 |
| 字符集 / 时区 | `utf8mb4` / UTC |

首次启动前已确认两库表数均为零。初始化 SQL 只在专用新卷首次创建时执行；再次启动会保留数据。不要对其他卷执行初始化，不要使用 `down -v` 清除这个环境。

## 私有配置与启动

本工作区已生成随机 root/应用口令，保存于仓库根目录 `.gstack/intelligence-local.env`，被 git 忽略。不在文档、日志或提交中写入口令，不覆盖共享 `.env`。

从 backend 目录运行：

```powershell
docker compose --env-file ../.gstack/intelligence-local.env -f compose.intelligence-local.yml config --quiet
docker compose --env-file ../.gstack/intelligence-local.env -f compose.intelligence-local.yml up -d
docker compose --env-file ../.gstack/intelligence-local.env -f compose.intelligence-local.yml ps
```

此机器的私有辅助脚本 `.gstack/intelligence-local-run.py` 从该文件读取口令，在子进程中构造数据库连接，避免打印口令或修改现有环境文件。它不是产品服务，也不提交仓库。使用项目 `.venv` Python，从 backend 执行：

```powershell
../.venv/Scripts/python.exe ../.gstack/intelligence-local-run.py inspect
../.venv/Scripts/python.exe ../.gstack/intelligence-local-run.py migrate
../.venv/Scripts/python.exe ../.gstack/intelligence-local-run.py mysql
../.venv/Scripts/python.exe ../.gstack/intelligence-local-run.py test
../.venv/Scripts/python.exe ../.gstack/intelligence-local-run.py smoke
```

`migrate` 仅指向本地开发库；`mysql` 和 `test` 为 MySQL 专项用例显式指定独立 CI 库。CI 用例仍拒绝非本机或名称不以 `intelligence_ci` 开头的数据库。其余原有单元测试继续使用隔离 SQLite。不要将任何现有审计或真实业务数据写入 CI 库。

所有辅助命令清空模型服务密钥，默认关闭分析功能。本地 smoke 单独为临时 API/worker 子进程启用功能，明确选择 development/local 正文存储；两进程验收完成后均退出，不留后台任务。它不会替代生产 OSS，也不会读取云角色凭证。输出日志在根目录 `test-results/intelligence-batch30-*.txt`，同样不提交。

## 本次实际验收范围

- 两个新库成功执行基线和 worker 心跳迁移，到 `intelligence_worker_20261016`，每库 54 张应用表及 `alembic_version`。
- 四项真实 MySQL 验证通过：完整迁移表清单；8 个并发消费者只有一个获得同一任务；schema 锁竞争/释放；备份一致性快照不混入另一连接稍后提交的数据，并核对引用对象字节。
- 实际启动两个独立的本地 API/worker 进程：`127.0.0.1:18016/api/health` 返回数据库 connected，worker 在 MySQL 写入 idle 心跳、本地存储状态和磁盘探测。停止后状态为 stopped，进程正常退出。
- 完整后端回归最终为 **577 passed、2 skipped**（193.12秒）；跳过项为已明确停用的 Kimi 旧路径。首次完整运行发现原产品网站趋势测试把“一小时前、两小时前”误当成同一天；现已冻结请求时钟，并把同日数据明确放在前一日10/11点，凌晨/正午/深夜三种时刻均通过，相关模块18项通过。应用日期分组逻辑没有修改。
- 没有抓取外部来源、调用模型或 OSS、导入旧审计、迁移远程数据库或部署代码。

这验证了本地 MySQL 和进程基础链路。ECS 角色取凭证、真实 OSS 不可覆盖写入及 SHA 回读、设备 capture-v2、正式分析与人工质量评测仍需分别验收。
