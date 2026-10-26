# E03 执行、交付与验收：后端实现

本轮以现有A05/E01任务归并和不可变行动修订为基础，新增执行投影及追加事件。没有改写TaskPack、事实批准或内容批准；没有发布、模型或额度操作。用户于2026-10-26确认局部原型，门户已接入正式执行页面，本地组件检查与真实客户跨服务验收分别记录。

## 状态与授权

执行单独记录 `not_started / preparing_materials / in_progress / delivered / accepted / cancelled`。安排负责人和截止时间、准备材料不要求先批准正式执行；开始、交付和验收必须有当前有效的计划批准及仍可用的负责人。普通成员只能推进自己负责的任务，具名验收/退回由owner/admin/editor操作。已验收或取消的执行关闭；后续范围变化需新任务修订。

每个执行只属于一个确切ActionPlanRevision。新修订默认没有负责人、期限、交付或验收，不自动继承旧状态。历史修订只读，操作可按原编号重试取得旧回执。退回后保留原交付与审核理由，再次提交生成新交付事件。

我方事实/证据发生相关变化后，开始/交付/验收被原有依赖检查阻断；允许安排、准备和取消。验收要求逐项对应原TaskPack验收标准，每项只能出现一次且全部满足；原标准文字由后端填入记录，客户端不能替换。

## 接口与信任边界

- GET `/api/intelligence/actions/{id}/execution?revision=N`：当前或历史修订投影；没有记录时只返回版本0，不创建数据库对象。当前修订实时复核计划及相关依据并返回plan_approval_valid；历史修订固定为false，不沿用当前批准。
- GET `/api/intelligence/actions/{id}/execution/events?revision=N&before=V&limit=20`：倒序追加事件及next_before游标；逐条展示完整性标记。
- POST同一execution路径：expectedRevision、expectedVersion、operation、reason、idempotencyKey；assign显式提供ownerId/dueAt，deliver提供artifacts，accept提供全部acceptanceChecks。均严格拒绝额外字段。

写入要求Portal签发一分钟、Visibility受众的execution_authority声明，绑定行动、修订及原始请求哈希。成员姓名/角色和内容产物来源由Portal服务端查询后放入声明，浏览器不能提交或覆盖它们。普通项目JWT只能读，不能单独授予执行写入。旧操作回执先按具名主体和完整语义请求核对，再处理版本CAS，避免不确定结果变成重复写入。

项目→审计→行动→执行投影按固定顺序加锁，MySQL使用当前读。执行版本CAS和追加事件幂等键共同防止覆盖；HMAC绑定具名操作、前后状态、原行动修订及前一签名。投影与最后事件不一致时阻断使用。历史事件保留原交付和验收资料。

外部地址和核实记录标记人工登记，不抓取、不自动验证质量。ContentOS草稿/人工任务需要真实服务回执、完成状态、同一项目/行动/修订、原草稿批准及完整Brief快照哈希；审计后端进一步核对原授权HMAC。已完成产物的来源可在原授权过期后保留，不能借此重新生成内容。当前计划依赖仍需有效。

## 数据库与验证

新增intelligence_action_executions和intelligence_action_execution_events，使用项目及行动修订的复合外键。按用户无历史兼容要求更新空库基线，schema身份为citation_execution_20261026，共60张业务表；拒绝对旧库自动修改。上线需准备相应空库并协调API/worker，当前未部署。

后端最新完整回归949 passed /2 skipped；真实MySQL重复读并发安排只能一方成功。新增读时批准复验检查覆盖品牌身份依赖变化、未批准及历史修订，不追加执行事件。新隔离空库intelligence_ci_execution_20261026已验证复合外键、事务回滚、API/worker启动，模型请求0。CI范围Ruff和compileall通过。门户检查见对应docs/reviews/citation-value-execution-20261026.md，ContentOS源代码本轮未变，沿用上一轮完整检查与生产构建结果。独立语义质量、真实鉴权与跨服务客户交付/效果验收仍未完成，当前未部署。
