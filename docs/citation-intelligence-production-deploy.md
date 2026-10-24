# 引用情报生产部署与回退

> 2026-10-24 价值分析 v3 分支采用 `citation_value_20261024` 独立空库基线。本页下述固定 `intelligence_production` 配置属于现有部署流程，不能直接用于初始化本轮新结构。先执行[空库验证与发布前置说明](citation-value-fresh-database-20261024.md)，在 Q02 完成并验证新库目标、独立运行配置和各版本回退配置后才能发布。当前未对生产旧库执行升级或切换。

本流程启用新的独立数据库、API 和 worker。旧 `geo-visibility-analyze-backend-1` 容器、旧数据库及旧公网入口不在该脚本操作范围。Portal 与 ContentOS 的接入需要协调发布；仅启动内部服务不会自动切换原公网 API。

## 上线准备

- 专用新库名称固定为 `intelligence_production`。在生产 checkout `/opt/geo-visibility-analyze/.intelligence.env` 配置数据库、认证、模型和设备参数，文件权限必须为 `600`。必须显式设置 `AISCOPE_DEBUG=0`，关闭开发注册、登录和 `local.*` 假身份；缺省或任何其他值均不能通过部署检查。生产启用 `AISCOPE_INTELLIGENCE_ENABLED=true`、`AISCOPE_INTELLIGENCE_ENVIRONMENT=production`、`AISCOPE_INTELLIGENCE_BLOB_BACKEND=oss`，使用已验证的 ECS RAM 角色及同地域私有 OSS。该文件不能提交、复制到镜像或上传 Actions。
- Docker 外部网络 `geo-visibility-analyze_default` 必须已存在，新库需能从该网络访问。
- 使用当前 main 完整 SHA 且成功运行 `Backend source and worker` 的 main push 工作流。发布镜像为 `ghcr.io/yyangwan/geo-visibility-analyze:<SHA>`，不使用 `latest`。
- 后端仓库临时配置 `CITATION_STORAGE_HOST`、`CITATION_STORAGE_USER`、`CITATION_STORAGE_SSH_KEY`、`CITATION_STORAGE_KNOWN_HOSTS`。专用 SSH 身份需能执行此已落库部署流程；之前仅允许存储探针的受限身份不能直接用于切换。完成后撤销临时凭据。

## 手动部署

在 GitHub Actions 选择 `Deploy citation intelligence`，分支选择 `main`，填写当前 main 完整 SHA。工作流核对 main HEAD 和成功源码/镜像 CI；runner 拉取不可变镜像、核 OCI revision，再 `docker save | gzip` 经启用 keepalive、固定主机公钥的 SSH 流式传到 ECS `docker load`。服务器只允许 `git pull --ff-only origin main`；如果 main 已变化或 checkout 不匹配则停止。

服务器执行已落库的：

```bash
bash /opt/geo-visibility-analyze/deploy/deploy-intelligence.sh deploy <完整SHA>
```

脚本持有独占发布锁，在目标镜像内验证有效配置的库名和 OSS 模式，然后显式执行 Alembic upgrade。不得指向旧库、stamp 伪装版本或在失败时删除数据库。迁移失败不触碰入口。

API/worker 使用同一 SHA 镜像，分别限制 `640 MiB` 内存和 `0.75 CPU`，重启策略为 `unless-stopped`。Compose 项目 blue/green 分别是 `genilink-intelligence-blue`、`genilink-intelligence-green`；API 仅绑定 `127.0.0.1:18016`、`127.0.0.1:18017`，不覆盖旧 Compose 项目。

先启动候选 API，检查 Docker healthy 和 `/api/health` 的 `status=ok`、`db=connected`、`deploymentVersion=<完整SHA>`。再读取 UTC 数据库时间水位，等待跨过秒精度边界后启动候选 worker。要求新 `started_at`、45 秒内心跳、存储 `configuration_verified`、临时盘 `ok`，不会以原 worker 的旧行通过。

通过后写入 `/etc/nginx/conf.d/genilink-intelligence-upstream.inc`，由 `/etc/nginx/conf.d/genilink-intelligence.conf` 声明 `genilink_intelligence` upstream 和仅本机监听的 `127.0.0.1:18018`。验证 `nginx -t`、reload 后内部入口健康及准确 SHA，再记录 `/opt/geo-visibility-deploy/intelligence` 的 active/previous slot 和镜像，最后停止旧的独立 intelligence slot；保留其停止容器用于回退。初次部署先只读验证 `geo-visibility-analyze-backend-1` 正在运行、记录其 Docker image ID，并确认本机 `8000` 健康，然后将它登记为 `previous=legacy`；不启停或修改旧容器。

上线后核对两个容器资源/状态、内部健康 SHA、worker 新鲜心跳、Nginx upstream 与 active 记录一致，并核对 Portal/ContentOS 协调发布结果。需要真实采集链路验收才能判断业务功能可用；心跳中的存储状态只证明配置，不替代此前真实 OSS 读写校验。

## 失败与回退

候选 API、worker、Nginx 或切换后健康失败，脚本恢复原配置并停止失败候选；原 active 服务在整个验证期间保持运行。第一次安装失败会恢复“配置原本不存在”的状态。若 Nginx 恢复本身失败，保留两边实例和私有 `attempt.*` 恢复文件，避免停止可能仍承接流量的容器；需要人工按保存配置处理。

后续成功发布保留前一 slot，可执行：

```bash
bash /opt/geo-visibility-analyze/deploy/deploy-intelligence.sh rollback
```

回退到 previous blue/green 镜像时，重新验证配置、API、worker 和准确 SHA；不执行数据库降级。首次回退到 `legacy` 时，不调用新库、迁移或新镜像探针，而是核验原容器运行中、image ID 未变化、`8000` HTTP 健康，再把动态 upstream 指回 `8000`，通过 Nginx 和 `18018` HTTP 健康后才停止新的独立 API/worker。旧 API 没有 `deploymentVersion` 合约，不要求它返回新 SHA。该回退同时恢复所有使用 `genilink_intelligence` upstream 的内部或公网代理；Portal 的旧前端容器保留原 `8000` 配置，前端回退仍可访问旧接口。从 `active=legacy` 再部署可正常启动 blue slot。

未来不向后兼容的迁移必须单独设计数据恢复和回退窗口；若 previous 新版 API 因 schema head 不匹配拒绝启动，保持当前 active，不强行切换。发布脚本不自动删除日志、镜像、卷、数据库或备份。
