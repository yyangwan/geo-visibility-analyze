# 引用分析镜像与独立联调环境

本流程交付可验证的 API/worker 共用镜像。镜像通过检查不代表 OSS 权限、真实设备采集或分析质量已经通过验收，也不会自动替换现有生产服务。

## CI 的交付边界

`.github/workflows/backend-ci.yml` 在 PR 上完成后端源码检查、全量回归、MySQL 实库迁移与并发测试，再构建一次镜像。断网运行 `python -m scripts.check_intelligence_image`，检查 API/worker/存储工具入口、OSS SDK、唯一迁移 head、开发用不可变对象读写及中文 PDF。配置与密钥不打包进镜像。

同一镜像经过检查后，只有 `main` 的 workflow 才发布 `ghcr.io/yyangwan/geo-visibility-analyze:<完整提交 SHA>`。PR 不推送镜像。镜像记录 `org.opencontainers.image.revision`；部署时核对该标签和目标 SHA，不使用 `latest`。该 workflow 暂不自动部署或迁移数据库。

已授权的正式生产切换使用单独手动流程，见 [生产部署与回退](citation-intelligence-production-deploy.md)。它要求当前 main 的成功镜像发布、固定新库及私密配置，先验证候选 API/worker，最后切换独立内部入口。

## 独立联调环境

### 单独验证真实 OSS

`.github/workflows/intelligence-storage-check.yml` 提供仅手动触发的 `Citation OSS acceptance` 流程。选择 `main` 分支并传入完整 `release_sha`；流程检查该提交属于 main 且已有成功的 main 镜像发布记录，再在 runner 拉取、核对 revision 并以带 keepalive 的 SSH 流式加载到 ECS。它不更新服务器应用源码、不连接数据库、不启动 API/worker，也不切换入口。

需要在后端仓库配置四个 Actions secret：`CITATION_STORAGE_HOST`、`CITATION_STORAGE_USER`、专用传输私钥 `CITATION_STORAGE_SSH_KEY` 和预先通过可信通道核对的主机公钥 `CITATION_STORAGE_KNOWN_HOSTS`。不要复用或公开个人私钥、云 AccessKey；不通过未核对的即时 keyscan 接受目标主机。当前仓库尚未配置这些凭据，因此新增流程本身不代表完成云端验收。

传输前要求 Docker 所在文件系统至少有 3 GiB 可用、可用内存至少 768 MiB；不足时直接停止，不自动清理。独立探针容器限制 CPU、内存和进程数，以只读根目录运行，使用 ECS IMDSv2 临时角色凭证；容器退出后删除该临时容器，镜像和非业务探针对象保留。私有报告保存在 `/opt/genilink-storage-checks/<SHA>-<run_id>-<attempt>/report.json`，不会上传 Actions 制品，也不输出临时凭证。重复执行使用独立目录。

报告通过仍仅证明对象存储验收，不构成产品上线。完整 API/worker 联调需要下述独立配置和新数据库。

### API 与 worker

`compose.intelligence-runtime.yml` 是独立 Compose 项目，不能作为旧生产 Compose 的覆盖文件使用。没有 build、源码挂载或自动迁移；`pull_policy: never` 要求事先加载经 CI 验证的镜像。仅 API 绑定 `127.0.0.1:18016`（可通过 `INTELLIGENCE_API_PORT` 调整），不切换现有入口。

在目标环境的私密配置文件中提供新数据库 URL、服务认证配置和 OSS 配置。文件不得入库或上传 CI 制品。至少包括：

```dotenv
AISCOPE_DATABASE_URL=<新建专用数据库的运行环境 URL>
AISCOPE_SECRET_KEY=<运行环境中的服务密钥>
AISCOPE_INTELLIGENCE_ENABLED=true
AISCOPE_INTELLIGENCE_ENVIRONMENT=production
AISCOPE_INTELLIGENCE_BLOB_BACKEND=oss
AISCOPE_INTELLIGENCE_OSS_BUCKET=genilink-citation-evidence
AISCOPE_INTELLIGENCE_OSS_ROLE=GenilinkCitationWorkerRole
AISCOPE_INTELLIGENCE_OSS_ENDPOINT=https://oss-cn-wulanchabu-internal.aliyuncs.com
```

项目 JWT 的 issuer/audience/JWKS、模型服务与设备配置仍按已批准的环境注入；不能从本地开发文件直接复制到生产。ECS 角色自动提供临时凭证，不创建长期 AccessKey。完整配置要求见 [运行说明](citation-intelligence-runtime.md)。

先由 GitHub runner 拉取 SHA 镜像，再通过 `docker save` 和带 keepalive 的 SSH 传到 ECS 执行 `docker load`。不要在 ECS 构建、安装依赖或复制未落库源码，也不要让 ECS 直接拉取大型 GHCR 镜像。生产源码只能从远端 `main` 快进更新。

以下命令在镜像已加载、配置文件已配置、新数据库已确认可用于本次方案后执行：

```bash
export INTELLIGENCE_IMAGE=ghcr.io/yyangwan/geo-visibility-analyze:<完整提交SHA>
export INTELLIGENCE_ENV_FILE=/absolute/private/intelligence.env

# 只对明确指定的新数据库执行一次；不得指向既有业务库。
docker compose -f compose.intelligence-runtime.yml --profile maintenance run --rm migrate

# 不需要先启动 API；独立验证同一镜像、同一运行角色的真实 OSS。
docker compose -f compose.intelligence-runtime.yml run --rm --no-deps \
  -v /absolute/private/storage-checks:/checks api \
  python -m app.intelligence.storage_check --write-probe --output /checks/new-report.json

docker compose -f compose.intelligence-runtime.yml up -d --no-build api
docker compose -f compose.intelligence-runtime.yml --profile worker up -d --no-build worker
```

存储验收文件必须使用新名称。写入的少量非业务对象保留，不要求删除权限；七项校验的定义与失败处理见 [存储检查说明](citation-intelligence-storage-check.md)。失败时保留报告和原服务，不启动业务流量。

启动后核对 API `/api/health`、数据库 head、worker 新鲜心跳与存储状态，再执行真实采集。关闭本次独立联调服务用 `docker compose -f compose.intelligence-runtime.yml --profile worker stop worker api`；不清理数据库、卷、对象或现有服务。

## 本地验证的限度

本地开发验证可以使用 `local` 存储和 `development` 环境及专用 Docker MySQL。容器内七项对象检查只证明开发适配器行为；不证明真实 OSS 的 IAM 权限、同地域网络或账单流量。独立联调环境没有自动接入 Portal/ContentOS，也不构成业务上线或质量认证。
