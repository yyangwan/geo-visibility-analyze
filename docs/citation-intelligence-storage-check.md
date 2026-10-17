# 引用正文存储验收命令

内部操作者命令 `python -m app.intelligence.storage_check` 不读取或写入数据库，不启动分析任务，不调用模型或抓取网站。它使用与 worker 相同的 `configured_store()`，生产环境没有本地盘回退。

## 配置检查

在已经通过正常发布流程部署新版依赖和代码的 API/worker 镜像中，使用现有角色与 OSS 配置运行：

```sh
python -m app.intelligence.storage_check --output /private-reports/storage-config-unique.json
```

默认不创建云对象，只读取私有 ACL 和版本控制状态；必须为 private、从未启用版本控制才通过。本地开发存储明确返回 development_local，不冒充 OSS 配置检查。

输出文件必须不存在；命令先独占创建私有报告，才访问存储。报告文件创建失败时不会发起存储调用。不要复用已有文件名，也不要把报告目录挂载到公网静态服务。

## 显式读写探针

```sh
python -m app.intelligence.storage_check --write-probe --output /private-reports/storage-write-unique.json
```

只有 `--write-probe` 才创建压缩的非业务测试对象，固定在 `projects/storage-acceptance/operations/storage-probes/<随机ID>/probe.gz`；不能通过参数指定真实业务对象或项目路径。每次运行使用独立随机 ID。

检查依次覆盖：不可覆盖写入、同字节重试、SHA256 回读、HEAD 大小和 SHA 元数据、错误 SHA 拒绝、不同字节覆盖拒绝、拒绝覆盖后原字节仍可读。OSS 上传仍使用适配器的 forbid-overwrite、AES256 和 SHA 元数据；不把 ETag 当 SHA。任意检查失败都不能报告验收通过。

成功状态为 storage_probe_verified。报告记录测试 key、SHA、大小及检查项。探针对象保留，不调用删除，也不扩大 IAM 权限；失败报告保留计划 key 和“可能已写入”状态，便于人工追踪部分执行。命令 stdout 只输出状态，SDK 异常和凭证不进入报告。所有结果 release_eligible=false，不能代替设备、数据库、内容分析或发布验收。

## 2026-10-17 实际情况

- 通过 SSH 只读检查现有 ECS 后端容器：`oss2` 与 `alibabacloud_credentials` 尚未安装。没有在服务器临时安装依赖、复制新版源码、构建镜像或重启服务。
- 在同一现有容器内，以标准库向阿里云 `100.100.100.200` 元数据服务请求 IMDSv2 token、检查目标角色和临时凭证必要字段：IMDSv2 通过、`GenilinkCitationWorkerRole` 存在、临时凭证可获取。token 和凭证仅在进程内使用，没有输出、落盘或传回本地。此项验证没有调用 OSS SDK 或 OSS API。
- 新验收命令已在本地 development/local 真实执行读写探针并通过。OSS 适配器的模拟 Bucket 测试验证请求包含禁止覆盖和 AES256 头，但这不是实际云端读写。
- 20项针对性测试及静态/编译检查通过；包含本地真实 MySQL 的完整后端回归为 **591 passed、2 skipped、36 warnings**，203.53秒。两项跳过为明确停用的Kimi旧路径。实际 OSS 上传、SHA 回读和权限范围验证仍待新版镜像经规定发布流程部署后执行。

所需两个 SDK 已列入本地 requirements.txt；不要为了运行本命令在生产容器中 ad-hoc pip install。云资源已经准备，无需重新创建 Bucket、RAM 角色或长期 AccessKey。
