# 本地验证记录

## 2026-10-25 项目命名与 Git 发布准备

用户指定项目与公开仓库名统一为 `rc_adolph`。README、项目分发元数据、架构图源与演示图标题已同步，运行包仍为 `notifier`，不改启动命令。两张 PNG 重新渲染并目视检查；架构 SVG 校验、16 个本地链接检查及 3 项演示报告测试通过。本轮未改投递实现，未重新宣称全量数据库回归。

本地项目已初始化独立 `main` 仓库，以免提交父目录的个人材料。SSH agent 实际认证为 GitHub `ado1ph`；`git ls-remote git@github.com:ado1ph/rc_adolph.git` 返回 Repository not found。没有可用的现有 HTTPS/API token；网页创建操作未获得授权，因此未创建、未推送，也没有远端 CI 结果。本地初始提交使用 GitHub noreply 地址，不发布私有邮箱；密钥、数据库、虚拟环境与缓存继续排除。

## 2026-10-25 面试交付与演示整理

本轮停止扩展生产功能，整理主 README、SQLite 原型说明、提交清单和可追溯图源；新增 `make demo-production`，运行真实 PostgreSQL/API/Worker 与本机 Mock。演示只创建自己的随机容器、临时密钥与配置，不接收外部 DSN，并清除继承的 `NOTIFY_*` 设置。没有连接业务数据库或真实供应商。

| 检查 | 实际结果 |
| --- | --- |
| 干净依赖环境 | 新建 `/private/tmp/rc-yuki-interview-clean-venv`，使用 Python 3.12 按 `requirements.lock` 和 `--require-hashes` 安装成功。默认沙箱首次下载受网络限制，授权后完成；没有借用项目 venv 的包来证明干净环境。最终使用该环境实际执行 `make demo-production PYTHON=<干净 venv>/bin/python`，三场景再次通过。 |
| 真实端到端演示 | 干净环境下执行 `python -m notifier.production.demo --output ...`，并注入无效外部配置验证配置隔离：三场景全部完成，相同入口 key 复用回执。正常投递的调用/副作用为 1/1，503 恢复为 3/1，响应超时后人工核对为 1/1。超时场景使用 manual 契约，没有自动重发。 |
| 图像证据 | `docs/assets/demo-report.json` 是实际运行报告，时间为 `2026-10-25T08:57:04.915591+00:00`；结果图由白名单字段生成，不是后台截图。API 状态仅为实际轮询观察，不保证捕获所有瞬时状态。架构图和结果图分别为 1920×1020、1920×1410，已目视检查；SVG 可解析、16 个 README/新增文档本地链接有效。 |
| 完整回归 | 在本轮新建、仅绑定本机的 `notifier-interview-tests-20261025` PostgreSQL 17 隔离容器运行：**125 tests，23.241s，OK，0 skipped**。包含新增 3 项报告白名单、禁止覆盖和输出目录检查。既有 Starlette/httpx 弃用警告不改变断言结果。 |
| 兼容与静态检查 | 生产范围 Ruff 和 `compileall` 通过。新增测试最初在系统 Python 3.9 上触发 production 包的 3.12 版本限制，已增加版本门禁；3.9 下这 3 项明确跳过，而非声称生产模块支持 3.9。标准库 SQLite 原型演示单独通过。 |
| 中断与清理 | 对独立演示发送 SIGTERM，确认失败退出、不生成成功报告、无新增演示容器残留。退出清理还捕获 kill 后等待超时，避免中断其他资源清理；这条极端分支未做真实不可终止进程注入。最终删除本轮测试 PG；只读检查确认本轮测试容器和 `notifier-demo-*` 均不存在，没有清理既有服务。摘要 ZIP 更新后完整性检查通过。 |

图片不证明吞吐、HA、真实供应商契约或 exactly-once。没有触发远端 CI；当前无 Git remote、源码整体未跟踪。`artifacts/ai-session.zip` 仍明确为摘要，不替代完整原始对话。最终提交仍需本人完成仓库上传和真实会话导出；既有生产验收边界保持不变。

## 2026-10-25 独立复核与时间证据补测

本轮使用 `gpt-6-luna` 子 agent 做只读审查、准备测试与修复补丁，由主 agent 独立核对并执行。当前工作区已经存在 v7，因此没有按旧报告重新创建迁移，也没有修改 0006/0007、活动任务的调度逻辑或租约实现。

### 发现与修复

`scripts/inspect_outcome_uncertainty.sql` 原先只要求后续成功 attempt 的结束时间晚于歧义 attempt 的结束时间，缺少 v7 迁移已有的两个严格条件：成功开始时间晚于歧义结束时间、成功结束时间晚于自身开始时间。只读查询可能误报可清除 uncertainty，但实际迁移会保守转人工核对；这不是已发生数据误清理的证据。

先扩充现有两个 PostgreSQL 测试，再修查询。定向红测运行 2 项：候选分类在 `overlap_success` 断言失败，实际返回 `clear_uncertainty_keep_succeeded` 而非待核对；现有 v7 迁移测试通过。补齐查询谓词后两项均通过。新增 fixture 覆盖时间重叠、结束早于开始、开始等于前次结束、结束等于自身开始；异常记录均保持未决并进入人工核对，正常成功正例保持可清除。

同时直接断言 v7 保留 pending/in_flight 的 `retry_requested`、`retry_scheduled`、attempt/cycle/replay 计数、调度时间及原有有效 lease；历史 retry 标志仍为 `legacy_unverified`，不作为新的发送授权。候选夹具容量改为实际任务数。补断言期间曾将 in_flight 的调度时间误写为 pending 的 `now - 1`；核对 INSERT 夹具后改为其实际原值 `now`，未修改生产行为。

### 实际验证

独立创建 `rc-yuki-independent-v7-20261025`，使用与既有验证记录相同 digest 的 PostgreSQL 17 镜像；实测版本 17.11，仅绑定本机临时端口。全套测试使用随机 schema；非 owner 烟测使用该新容器中原本空的 public schema。未使用既有容器的数据、业务数据库或真实供应商。

| 检查 | 实际结果 |
| --- | --- |
| 最终完整回归 | `NOTIFY_TEST_DATABASE_URL=<本轮新建隔离 PG DSN> PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`：**122 tests，19.212s，OK，0 skipped**。测试数量未增加，新增边界并入现有测试。包含 API/Worker 子进程及本机 Mock 的现有端到端测试。默认沙箱最初阻止本机 socket，授权访问隔离库和 Mock 后重跑通过。 |
| 非 owner runtime grants | 按实际 `deploy/runtime-grants.sql` 创建非 owner、非 superuser 的 runtime 角色，验证 readiness、提交、领取、续租、成功写回、歧义转核对、确认未送达且不重试、查询/审计、worker 心跳、指标、密钥轮换及维护入口烟测；DDL、改 schema version、篡改 reconciliation 三项操作均被权限拒绝。使用临时脚本 `/private/tmp/verify_rc_yuki_runtime_grants.py`，这是单次本地烟测，不是新增 CI 覆盖或生产授权验收。 |
| Ruff | `--no-cache` 检查 production 包、delivery/worker/validation、production/init_config 测试与初始化脚本：通过。 |
| 编译检查 | `compileall -q notifier tests scripts` 通过；缓存输出到独立临时目录。 |
| 告警 | 与 CI 相同 digest 的 Prometheus 镜像，禁网、仓库只读挂载；`check rules deploy/alerts.yml` 通过 12 条规则，`test rules tests/alerts.test.yml` 通过。 |
| 清理 | 测试后随机 schema 已清理；删除且确认不存在本轮新建的 PostgreSQL 容器及其测试数据。没有重启或清理既有 API、Worker 或容器。 |

### 尚未完成的发布验收

当前 `git remote -v` 为空，仓库内容整体未跟踪；没有可触发的远端 GitHub Actions，也没有推送、创建仓库或提交。CI 命令的本地等价运行不代表远端 CI 已通过；本轮代码差异通过修改前临时副本独立核对，不能将整个未跟踪目录视为本轮改动。

本轮完成本地可执行的缺陷修复及回归，不构成生产就绪结论。仍需部署 owner 完成远端 CI、生产规模迁移与负载演练、HA/PITR 与密钥恢复、生产最小权限及网络策略、告警实际送达、真实供应商幂等/业务成功契约验收。继续采用 PostgreSQL 任务账本，不引入 Redis/MQ；本地 fencing 不能撤销供应商副作用，也不承诺 exactly-once。

## 2026-10-25 v7 Review 跟进

本节记录当前源码在隔离环境中的实际验证，不代表生产验收。未连接业务数据库或真实供应商。

### 环境与迁移设计

创建独立 PostgreSQL 17 容器 `rc-yuki-v7-final-20261025`、`rc-yuki-v7-final2-20261025` 与最后复跑容器 `rc-yuki-v7-final3-20261025`，均使用镜像 `postgres:17-alpine@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb`、PostgreSQL 17.11，仅绑定 `127.0.0.1`。完整测试使用随机 schema；最终连接身份为测试数据库 `notifier` / `postgres`。本地既有隔离容器仅做只读身份核验及早期定向测试，未使用其既有 schema。最终容器测试结束后只保留 `public` schema；本轮新建容器及匿名测试卷已清理，原有容器仍运行。

v7 不改写已发布 v6。新增 `delivery_reconciliations.retry_decision_source`；v6 以前的记录统一为 `legacy_unverified`，因为旧 `retry_scheduled` 基于状态/replay_count 回填，不是原始决定。v7 不用该推断字段关闭任务或授权发送。严格证据修复仅改 `outcome_uncertain`：后续成功/送达结论保留 succeeded；严格晚于完整 attempts 的 not_delivered 且之后无新 attempt 时，pending 仍 pending、failed 仍 failed。in_flight、paused 和无充分证据的记录保留原状态与 uncertainty；有 uncertainty 的 failed/succeeded 转人工核对。迁移审计、数据更新、列变更和 schema version=7 在同一迁移事务中。

修复前新增红测：v6 将失败任务转为核对后，模拟旧恢复 SQL 将严格 not_delivered 且已闭合的任务设回 failed；旧 v7 迁移结果仍为 `reconciliation_required + outcome_uncertain=true`，与新预期 `failed + false` 不符。修复后同一用例通过。这里没有按 v6 推断的 `retry_scheduled` 值判断关闭，也不修改 pending/in_flight 的当前调度状态。

### 验证结果

| 检查 | 实际结果 |
| --- | --- |
| 只读候选分类 | PostgreSQL 集成测试通过；覆盖歧义后成功、歧义后确认送达/未送达、pending/in_flight 重试、重试后新歧义、paused、相同/缺失时间、冲突核对、v6 错标及旧核对标记来源。查询前后状态不变；v6 audit 仅作为追溯上下文。 |
| v1/v4 历史升级及 v7 状态转换 | 完整 PG 套件覆盖空 schema 初始化、v1→v7、v4→v7 和已应用 v6→v7。成功任务可进入正常清理；未决任务的快照、attempt、核对记录与审计保留；pending 可领取，in_flight 的 lease/attempt/origin 计数保留并由匹配 worker 完成。 |
| 幂等迁移与事务回滚 | v7 入口重复运行不重复审计或改版本。触发器注入 v7 审计插入失败后，确认 schema、状态、审计和版本全回滚；移除故障后重试通过。 |
| 定向 PG 回归 | 候选分类、v1/v4 升级、v7 状态保留、失败回滚及重复迁移测试通过；最终完整套件又覆盖了追加的迁移审计断言。更早一次测试因手工选择了不存在的 unittest 名称产生 2 个测试发现错误；按真实名称重跑通过，该选择错误不属于测试断言失败。 |
| 完整测试 | 最终源码/断言版本在 `rc-yuki-v7-final3-20261025` 上运行：`NOTIFY_TEST_DATABASE_URL=<新建隔离 PG 17 DSN> PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`：**122 tests，19.117s，OK，0 skipped**。包含真实 API/Worker 与本机 Mock 的现有端到端测试；没有真实供应商调用。 |
| Ruff | `.venv/bin/ruff check --no-cache notifier/production tests/test_production.py tests/test_init_config.py scripts/init_config.py`：通过。首次发现新增测试局部导入分组格式问题，修正后通过。 |
| Prometheus 告警 | 使用与 CI 完全相同的 `prom/prometheus:v3.2.1@sha256:6927e0919a144aa7616fd0137d4816816d42f6b816de3af269ab065250859a62`，`--network none`、仓库只读挂载、工作目录 `/work`；`check rules deploy/alerts.yml` 报告 12 条规则成功，`test rules tests/alerts.test.yml` 成功。 |

CI 已有 digest 固定的 `promtool check rules` 与 `promtool test rules` 命令，错误码会阻断 workflow；本轮使用相同镜像、路径和工作目录完成本地等价验证。没有远端推送或触发 GitHub Actions，远端 CI 未运行。

### 迁移边界与发布风险

v7 在 advisory migration lock 下的事务包含 reconciliation 列 DDL、针对 uncertain jobs 的 attempts/reconciliations 分类扫描、job 更新、审计和版本更新。attempt 按 `(notification_id, number)` 主键关联，核对按任务索引读取；实际扫描量受 uncertain jobs 与其历史量决定。本轮样本最多十余条候选，仅证明逻辑，不证明生产锁等待、WAL、复制延迟或迁移耗时。生产发布前必须排空旧 Worker 和所有 API 写事务，并在生产规模副本演练，观测 `pg_stat_activity`/`pg_locks`。

自动修复范围限定为严格证据能说明旧结果已解决的记录；时间相同、缺失、结论冲突、核对后有新 attempt/歧义、paused/in_flight 及 unresolved terminal 记录保留待人工核对。失败/成功旧终态仅在缺乏明确解决证据时转入 reconciliation_required，不清除 uncertainty。v7 无降级迁移；成功升级后不能直接运行不识别 schema 7 的旧应用。生产迁移、真实供应商幂等契约、非 owner grants、代表性负载、备份恢复演练及告警实际送达仍未验收。

更新日期：2026-10-25，Asia/Shanghai。以下为历史验证记录。

## 2026-10-24 v5 Review 后续 P2 修复

### 环境与隔离

复核 `rc-yuki-review-20261024`：`postgres:17-alpine`、无数据卷挂载、仅绑定 `127.0.0.1:56755`；只读连接确认 `notifier_review` / `review` / PostgreSQL 17.11。测试在随机 schema 中运行并清理；结束后只剩预先存在的 `public` 与 `review_environment`。未操作该 schema、业务数据库、现有 API/Worker 进程或真实供应商。端到端 HTTP 仅访问本机 Mock。

### 修复与验证

| 检查 | 实际结果 |
| --- | --- |
| 修复前回归 | 4 项定向用例在原实现下确认 2 项状态断言失败、1 项重试上限核对抛出 Conflict；“无历史不确定性的预发送授权失败保持 failed”通过，验证没有把所有预发送故障误升为人工核对。 |
| 定向修复测试 | 7 项通过，覆盖末次租约失效、先前结果不明后授权拒绝、无历史歧义的预发送失败、显式不重试、上限终态核对、v1 历史迁移和 API 核对响应。 |
| 空库及历史迁移 | 隔离 schema 空库初始化、v1→v6、v4→v6 均通过。v6 将强证据任务转为 `reconciliation_required` 并留审计；迁移测试覆盖先前 ambiguous attempt 后授权失败，以及旧 503 attempt 后普通 failed 的保守升级。v6 不把所有旧 failed 状态改成人工核对。 |
| 完整测试 | `NOTIFY_TEST_DATABASE_URL=<隔离 PG 17 DSN> .venv/bin/python -m unittest discover -s tests -q`：**118 tests，17.511s，OK，0 skipped**。包括现有真实 API/Worker 子进程到本机 HTTP Mock 的端到端测试。 |
| 生产范围 Ruff | `.venv/bin/ruff check --no-cache notifier/production tests/test_production.py tests/test_validation.py scripts/init_config.py`：通过。 |
| 编译 | `PYTHONPYCACHEPREFIX=/private/tmp/rc-yuki-pycache .venv/bin/python -m compileall -q notifier/production`：通过。 |
| Prometheus 告警 | 缓存的 `prom/prometheus:v3.2.1`：`promtool check rules deploy/alerts.yml`，12 条规则通过；`promtool test rules tests/alerts.test.yml` 成功。覆盖 paused 满容量、pending/in_flight/paused 混合、多 API 副本去重、hold 延时触发与解除、空队列无误报。 |

迁移影响：新增 `0006_persist_outcome_uncertainty.sql`，schema version 为 6。未决状态用 Job 标记、Attempt 历史和同事务 audit 持久保留；只有成功供应商响应或证据核对清除。HTTP 核对现在区分事实 `outcome` 与 `retry_after_reconciliation` 意图，响应包含 `retry_requested` 和 `retry_scheduled`。旧客户端省略该字段时维持原有的预算内重试语义；上限/快照/key 不允许时改为证据入库并终态关闭，不增加发送。终态关闭的任务不能由普通 replay 重开。

边界：证据引用与人工结论仍由授权 reconciler 提供，服务不会自动核验供应商后台；收到非成功 HTTP 响应不证明之前的 attempt 未产生副作用。后续成功按同一不可变任务与其供应商契约关闭任务结果，但不承诺外部 exactly-once。没有执行生产迁移、负载/HA/PITR、真实供应商幂等契约或值班接收器的 firing/resolved 验收。测试输出仍有 Starlette/httpx TestClient 弃用警告。

## 2026-10-24 最新 Code Review 整改验收

### 环境

复核并使用用户指定的 `rc-yuki-review-20261024` 容器：Docker inspect 显示 `running`、`postgres:17-alpine`、无挂载卷，宿主端口绑定 `127.0.0.1:56755`。只读 SQL 连接确认数据库 `notifier_review`、角色 `review`、PostgreSQL **17.11**。所有测试与端到端 schema 均为随机临时 schema；结束后确认 `e2e_*` 与 `test_*` schema 为空。未触碰 `review_environment` schema、旧 API/Worker、业务数据库或真实供应商。

### 回归与验证

| 检查 | 实际结果 |
| --- | --- |
| 新增用例修复前红测 | PostgreSQL/HTTP Mock 探针分别复现 manual hold 重放、完整连接 timeout 永久失败、hold 阻断过期租约回收、replay 上限阻止终态核对。另一次迁移测试先因测试错误地把默认 tuple row 当 dict 而报错，修正测试 row access 后重跑通过。 |
| PostgreSQL 集成类 | `NOTIFY_TEST_DATABASE_URL=<隔离 PG 17 DSN> PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_production.ProductionDatabaseTests -q`：**40 tests，12.121s，OK**。 |
| 完整套件 | `NOTIFY_TEST_DATABASE_URL=<隔离 PG 17 DSN> PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q`：**114 tests，16.935s，OK，0 skipped**。第一次完整运行出现两条旧断言不匹配：演示库期望把已发送超时称为预算耗尽；另一个测试未启用 manual profile 却期待 manual 转核对。依据真实调用状态及 profile 修正断言/测试前置条件后复跑全通过；未放宽重试限制。 |
| 生产范围 Ruff | `.venv/bin/ruff check notifier/production notifier/delivery.py notifier/worker.py notifier/validation.py tests/test_production.py tests/test_validation.py tests/test_integration.py scripts/init_config.py --no-cache`：通过。 |
| 编译 | `PYTHONPYCACHEPREFIX=/private/tmp/rc-yuki-pycache-final .venv/bin/python -m compileall -q notifier tests scripts`：通过。 |
| 真实 API/Worker/Mock E2E | 临时脚本 `/private/tmp/rc-yuki-review-20261024/verify_current_e2e.py` 启动当前源码的独立 API、Worker 子进程和本机 Mock；独立随机 schema，脚本正常退出并清理进程/schema。结果：`ok` succeeded/1 attempt/1 effect；`flaky` 503 重试 succeeded/3 attempts/1 effect；`reject` failed/1 attempt/0 effect；`rate_limit` succeeded/2 attempts/1 effect；`timeout_once` 提交后超时、供应商幂等重试 succeeded/2 attempts/1 effect。重复入口 key 返回原 ID；详情/列表 API JSON 解析拒绝非有限常量。 |
| 维护与原服务 | 新 e2e/test schema 清理后只读确认为空。原报告 supervisor/API/Worker PID 28862/28863/28864 身份复核未变且仍运行；本次未停止、改配或复用它们。 |

迁移：空库初始化和现有 v1→v5 升级由完整 PG 类覆盖；额外 v4→v5 测试验证旧 manual `paused` 被转成 `reconciliation_required` 并审计，幂等 `paused` 与 origin hold 保持不变。新迁移 `0005_quarantine_ambiguous_cooldown_jobs.sql` 不改写旧迁移。

结果说明：这些是隔离 PostgreSQL 17.11、本机 HTTP Mock 和当前本机进程链路的验证，不代表供应商 idempotency key 契约已被外部核实，也不证明公网 TLS、生产 HA、负载、非 owner grants、备份恢复或 exactly-once。迁移前仍需排空旧 Worker 和演练备份/升级。

## 历史记录：2026-10-24 P2 修复复验

| 检查 | 结果 |
| --- | --- |
| 完整测试 `.venv/bin/python -m unittest discover -s tests -q`（仅本地 Mock/TLS） | **109 tests，4.370s，OK；74 passed，35 skipped** |
| 生产范围 Ruff | `.venv/bin/ruff check notifier/production notifier/delivery.py notifier/worker.py notifier/validation.py notifier/demo.py tests/test_production.py tests/test_validation.py tests/test_integration.py scripts/init_config.py`，通过 |
| `compileall` | `PYTHONPYCACHEPREFIX=/private/tmp/rc-yuki-pycache .venv/bin/python -m compileall -q notifier tests scripts`，通过 |
| PostgreSQL 迁移/持久化/并发验收 | **未完成**：`NOTIFY_TEST_DATABASE_URL` 未设置；本机没有 PostgreSQL 初始化/控制工具，Docker daemon socket 不可用。35 项生产数据库测试均跳过，不能视为通过 |

本轮新增/更新用例覆盖：连接与 TLS 超时可证明尚未发送、部分写入和响应丢失为可能已发送、预算/租约失效的本地分类、紧贴 `request()` 前预算复核、失租标记不等待网络发送锁、重试契约决策，以及 PostgreSQL 端预发送重试/本地预算不污染供应商熔断/幂等窗口不足/租约回收旧 worker 写回 fencing。后四类持久化状态用例需要 PostgreSQL，本轮未执行。

全量测试最初在默认沙箱失败 20 项，原因是本机 Mock/TLS 测试绑定 loopback socket 被拒，非代码断言失败；获准只对本地 Mock 运行后暴露两项演示测试仍期待对结果不明请求自动重放。修正为没有显式供应商安全契约时停止重放后，全量再运行出现一处错误分类断言；实际响应等待耗尽整体 deadline，正确分类为 `execution_budget_exhausted` 且保持可能已发送状态，更新断言后最终全套通过。没有通过放宽生产重试策略掩盖这些失败。

PostgreSQL 没有可用隔离实例，因此 migration v1→v4、旧 Infinity/NaN 修复、真实详情/列表路由 JSON、共享 hold 恢复事务、撤销域名后旧回执、租约回收及并发状态转换均尚未验证。没有连接或修改任何数据库。全项目宽范围 Ruff 另报两个既存 import 排序问题，位于未修改的 `notifier/api.py`、`notifier/mock_supplier.py`；本轮要求的生产范围 Ruff 通过。测试输出包含 Starlette/httpx TestClient 弃用警告。

## 历史记录：2026-10-24 R1-R6 / A1 整改复验

| 检查 | 结果 |
| --- | --- |
| 定向纯单元与安全回归 | 18 tests，全部通过，含 Retry-After OWS/超范围、DNS 耗尽预算不连接、租约丢失阻止 HTTP 调用、根路径 operation |
| 完整测试 `.venv/bin/python -m unittest discover -s tests -q` | 93 tests，4.676s，OK；63 passed，30 skipped |
| Ruff 生产范围 | `.venv/bin/ruff check notifier/production notifier/delivery.py notifier/worker.py notifier/validation.py tests/test_production.py tests/test_validation.py tests/test_integration.py scripts/init_config.py`，通过 |
| `compileall` 与 `git diff --check` | 通过 |
| 隔离 PostgreSQL 迁移/事务/并发 | **未运行，不能记为通过**：`NOTIFY_TEST_DATABASE_URL` 未设置，本机无 `postgres`/`pg_ctl`/`initdb`，Docker daemon socket 不可用 |

完整套件在获准使用本机临时 socket 后运行；相关网络仅为测试中的本机 Mock/TLS 服务，没有真实供应商调用。30 项跳过项来自 PostgreSQL 集成测试，包含新增的 v1→v3 清理快照核对、profile 撤销后原回执、Retry-After 共享 hold、熔断分类和维护并发/容量计数用例。A1 的集合 SQL 及 migration 0003 因此仍需在隔离 PG 17 上执行。

首轮完整套件曾有 1 项失败：已有测试把超时错误精确断言为 `network_error`；超时现明确记录为 `network_timeout`，已更新断言并复跑通过。这不是绕过失败。测试输出另有 Starlette/httpx TestClient 弃用警告，当前测试通过但依赖升级前需处理。

发布状态：**不满足用户要求的完整验收**。应在专用测试 PG 17 上设置 `NOTIFY_TEST_DATABASE_URL`，至少运行：

```sh
NOTIFY_TEST_DATABASE_URL=postgresql://<isolated-test-only-dsn> \
  .venv/bin/python -m unittest tests.test_production.ProductionDatabaseTests -q
NOTIFY_TEST_DATABASE_URL=postgresql://<isolated-test-only-dsn> \
  .venv/bin/python -m unittest discover -s tests -q
```

测试套件会为迁移/并发用例创建随机 schema 并在 teardown 删除；DSN 必须指向专用、可创建/删除 schema 的隔离数据库，禁止填正在使用的开发/生产库。完成上述 PG 验证并检查锁等待/容量计数后，才能关闭 R2/A1 以及 R3-R5 的事务验收。

## 历史记录：2026-10-24 整改复测

使用 Python 3.12.14 与新建、无宿主机数据卷的 PostgreSQL 17 容器，执行：

```sh
NOTIFY_TEST_DATABASE_URL=postgresql://postgres:<test-only-password>@127.0.0.1:<test-port>/notifier \
  .venv/bin/python -m unittest discover -s tests -q
```

完整 PostgreSQL 17 集成套件：**85 tests，26.159s，OK**，包括 v1→v2 迁移、旧 ambiguous 失败任务回填、租户轮转领取、执行窗口边界、核对闭环、角色权限及供应商账号/operation 契约漂移保护。之后对 HMAC 摘要增加用途域分隔；最终版本的定向账号/operation 回归通过，Ruff 通过。尝试再跑完整套件时 Docker daemon 已停止，未能重启临时 PG；原 Compose 数据库、API、Worker 与网关未重建或迁移。

新增回归覆盖：

- 供应商幂等窗口按数据库首次 claim 启动，领取/重放预留 HTTP timeout + 3 秒预算；剩余安全窗口不足时进入待核对。
- ambiguous 结果不能由普通 operator 直接重放；reconciler 必须提供结论、证据引用和原因。确认送达关闭任务，确认未送达只在旧 key 仍安全时恢复。
- v1→v2 会将旧 `manual_reconciliation_required` 失败任务转为未决、更新最后一次 attempt 并写迁移审计，避免旧数据绕过新状态机。
- method/path/query allowlist 精确匹配，拒绝未授权方法、路径、额外及重复 query 参数；发送时会再次核对 profile。
- 加密任务快照固定声明的供应商账号、租户/目标授权、operation allowlist 与重试契约；同账号密钥 Header 可轮换，账号/操作契约漂移则 fail closed。
- `supplier_account` 是运维声明；自动测试不能验证该标识是否对应供应商控制台里的真实账号。
- `Retry-After: 7200` 保留两小时而不截成一小时；本地仍设 365 天实现上限，超长值不宣称符合无限期等待。
- 同 origin 内对当前可执行的租户做轮转。该测试不证明跨 origin、账号级公平或供应商真实配额隔离。

| 检查 | 结果 |
| --- | --- |
| Python 3.12 完整测试（隔离 PostgreSQL 17，HMAC 域分隔小改动前） | 85 tests，26.159s，全部通过 |
| 最终代码：供应商账号/operation 契约漂移定向回归 | 通过 |
| Ruff：生产包、相关测试及初始化脚本（最终代码） | 通过 |
| Compose 当前运行环境 | 未重建/迁移；运行环境的 schema 版本仍需发布前确认 |

测试过程先后发现 claim SQL 的参数绑定数量错误，以及 PostgreSQL 不接受在 `SELECT DISTINCT` 查询中按未投影的轮转表达式排序；均已修正，并由完整 PG 套件复测通过。测试依赖仍有 Starlette 对 httpx TestClient 的弃用警告。

这次验证仅证明本机测试环境中的代码路径。它不替代真实供应商契约、负载与长期稳定性、非 owner DB grants、Compose 滚动升级、HA/PITR、网络分区、真实告警或安全审计。

## 历史验证记录

此前 78 项测试及下列 Docker/Compose 结果是历史基线，不应覆盖上面的最新结果。设计审计和首轮整改状态见 [design-review-2026-10-24.md](design-review-2026-10-24.md)。

## 生产候选版验收

本地 Python 3.12.14、隔离 PostgreSQL 17 Docker 数据库，实际运行：

```sh
NOTIFY_TEST_DATABASE_URL=postgresql://postgres:integration-only-password@127.0.0.1:55432/notifier \
  .venv/bin/python -m unittest discover -s tests -q
```

结果：当时为 **78 tests，13.832s，OK**。其中原演示 42 项、生产安全与 PostgreSQL 集成 32 项、初始化配置 4 项。测试数据库密码仅用于本机隔离测试；不要用于部署。

生产版覆盖：

- 两个独立连接池的并发接收和领取、事务幂等、租约恢复与旧 worker fencing。
- 真实子进程强制退出后的恢复；API 与 worker 分进程启动、投递及 SIGTERM 生命周期。
- 实际 HTTP 超时已产生副作用：幂等供应商自动重试仍只产生一次副作用；非幂等供应商转人工核对。
- 去重窗口到期阻止重放、供应商契约变更不可静默降低安全性。
- 目标隔离、跨 worker 限流、熔断、共享 Retry-After、容量背压、失败与重放审计。
- 租户与角色隔离、严格请求解析、存储故障不确认接收、元数据查询不返回敏感快照。
- 快照加密、密钥轮换、保留期清理、终态仍计入存储配额。
- 本地自签 CA 的真实 TLS 握手及 hostname 验证，不是公网证书链验收。
- 密钥文件 0600、目录 0700、重复初始化拒绝覆盖、拒绝 root、控制台不输出 token。

本轮新测试发现初始化脚本错误地向 `Path.open` 传入 `opener`，首次 78 项运行有两项错误；修复为内置 `open` 后重跑，得到上述全通过结果。没有把失败记录当作成功。

其他实际结果：

| 检查 | 结果 |
| --- | --- |
| Ruff：生产包、生产测试、初始化脚本及其测试 | 通过 |
| `pip check` | 无依赖冲突 |
| `pip-audit --disable-pip --no-deps -r requirements.lock` | 当次数据库未检出已知漏洞；不包含基础 OS 镜像扫描 |
| 生产与测试依赖锁 | 已生成完整哈希锁；镜像使用 `--require-hashes` 成功安装 |
| `docker build -t rc-yuki-notifier:local .` | 成功，当前主机 ARM64；未构建 AMD64 |
| Compose 基础与本地 override 合并配置 | 校验通过 |
| Compose 开发数据库及生产镜像迁移命令 | 历史验证，schema version 1；本轮未改该运行环境 |
| Compose API / worker 实际启动 | 非 root UID 501、只读根文件系统；就绪接口 200，鉴权指标报告 1 个健康 worker |
| 官方 `promtool check rules` | 10 条规则校验通过 |
| 官方 `promtool check config` | 完整抓取配置及规则校验通过 |
| 官方 `amtool check-config` | 示例配置校验通过；未发出真实告警 |
| 非 root Nginx `nginx -t` | TLS 与动态 upstream 解析配置通过 |
| 本机 HTTPS 网关到 API | `curl --cacert` 校验证书及 localhost 主机名后，返回 `{"ready":true}` |
| 原 SQLite Mock 演示复跑 | 通过；幂等/非幂等超时场景的副作用仍为 1 / 2 |

测试依赖出现 Starlette 对 httpx TestClient 的弃用警告；当前测试通过，后续升级应迁移对应测试客户端，不应通过屏蔽警告掩盖兼容性变化。

本次生成的 `.env`、`.secrets/` 为本地 development 随机配置，已被 Git/Docker 构建忽略。TLS 证书为一天有效的自签 localhost 测试证书，不能当作生产证书。Compose 的 API/worker/DB/网关保留供本机体验。公网证书链、实际滚动升级时后端 IP 变化的恢复时间仍未验收。

## 历史 SQLite 演示

这是实际运行结果的整理，不是完整终端原始日志，也不是压测或生产认证。服务均仅监听本机临时端口；测试/演示完成后已关闭。

## 自动化测试

| 环境 | 命令 | 结果 |
| --- | --- | --- |
| 系统 Python 3.9.6 | `python3 -m unittest discover -s tests -v` | 42 tests，OK |
| 工作区 Python 3.12.14 | 同上，使用工作区 Python 可执行文件 | 42 tests，OK |

系统 Python 使用的 SQLite 版本为 3.43.2；测试确认 `journal_mode=delete`、`synchronous=3`（EXTRA）。

覆盖：

- 并发入口去重、不同快照冲突、只在存储成功后确认接收。
- 不读取确认响应后重试，仍返回原通知 ID，不重复入队。
- 并发领取、提交后重开数据库、真实子进程 `os._exit(17)` 后租约恢复。
- 续租、旧 token fencing、预算耗尽、重放保留 attempt 历史。
- 外部调用成功后本地写回失败，仍由租约恢复，不静默丢弃。
- JSON 订单与表单 CRM 的实际 HTTP 请求。
- 503 后成功、400 不重试、429 采纳 Retry-After、302 不跟随。
- 供应商已处理后超时，分别验证有/无供应商幂等时的副作用。
- 精确 origin 白名单、私网/DNS 混合地址与多播限制、Header 注入与 envelope 大小限制。
- TLS 连接固定目标 IP，但仍使用原 hostname 校验的接口行为（使用 mock，不是真实公网 TLS 测试）。
- 真实 `python -m notifier serve --port 0` 子进程启动、完成投递、SIGTERM 正常退出。

首次在 sandbox 内运行时，本地 socket bind 被拒绝；经权限审批后重新运行通过。环境权限失败没有被记为业务测试通过。

## 演示

实际执行 `make demo`，最终结果：

```text
success              status=succeeded attempts=1
transient            status=succeeded attempts=3
permanent            status=failed    attempts=1
outage               status=failed    attempts=3
timeout-dedup        status=succeeded attempts=2
timeout-no-dedup     status=succeeded attempts=2
Supplier effects: {"success": 1, "timeout-dedup": 1, "timeout-no-dedup": 2, "transient": 1}
Manual replay: succeeded; prior attempt history preserved
PASS: durable delivery, retries, terminal failures, replay, duplicate boundary
```

重点是 `timeout-dedup` 与 `timeout-no-dedup` 都发起两次请求，但供应商副作用分别为一次和两次。这个结果只证明当前 Mock 的契约和代码路径，不代表任意真实供应商都会去重。

## 仍未验证及上线门禁

没有做机器断电、文件系统故障、物理磁盘满/损坏、公网 TLS 全链路、真实第三方 API、网络分区、吞吐与长稳测试、HTTP fuzzing 或渗透测试。存储不可用测试使用故障注入，而不是耗尽真实磁盘。

多可用区同步复制切换、PITR 恢复与 RPO/RTO、真实供应商幂等时长及业务对账、报警到达真实值班人员均未验收。架构文档中的 SLO 是待验证目标，不是测试结论。单机 Compose 不提供 HA。镜像 digest 锁定只解决可复现性，不等于镜像没有漏洞；上线需完成镜像扫描、组织 CI 供应链策略及访问控制评审。

没有创建 GitHub 仓库或推送代码。AI ZIP 是摘要及本记录，不包含完整原始对话，正式提交前需补。
