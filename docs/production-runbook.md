# 生产运行手册

先读 `architecture.md`。以下区分“本机运行方式”和“真实生产发布”；Docker Compose 不会自动创建多 AZ、托管备份或真实值班系统。

## 1. 依赖与入口

生产模式使用 Python 3.12、FastAPI/Uvicorn、PostgreSQL 17、psycopg、Fernet、限定时长 DNS 和 Prometheus。依赖版本及文件哈希锁在 `requirements.lock`；SQLite 模式保留用于无依赖作业演示，两种数据库不共享 schema，不会偷偷将原 SQLite 文件视为生产数据库。

```sh
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
```

直接运行进程时必须配置 `NOTIFY_DATABASE_URL` 或 `NOTIFY_DATABASE_URL_FILE`、`NOTIFY_AUTH_FILE`、`NOTIFY_KEYS_FILE`、`NOTIFY_CREDENTIALS_FILE`、`NOTIFY_ALLOWED_ORIGINS`。生产 PG 连接强制 `sslmode=verify-full`，CA 文件可通过 DSN `sslrootcert=/path/to/ca.pem` 指定。密钥和凭证只读挂载，禁止烘焙进镜像。

```sh
python -m notifier.production migrate  # 用 schema owner，单独发布步骤
python -m notifier.production api --host 0.0.0.0 --port 9000
python -m notifier.production worker  # 独立部署，可多副本
```

API 的 `/healthz` 仅表示进程存活，`/readyz` 检查数据库和 schema，不把 Worker 暂时下线变成 API 拒绝接收。Worker 新鲜度由持久化 heartbeat 指标单独告警。数据库不可用时不得返回虚假的 202。Uvicorn 不记录包含 URL/查询参数的 access log；应用日志仅输出请求 ID、状态和耗时等元信息。

## 2. 本机容器体验

不要在已配置密钥的目录重复执行初始化。脚本用随机强 token/密钥、0600 文件和 0700 密钥目录；拒绝覆盖，也不在终端打印 token。

```sh
.venv/bin/python scripts/init_config.py --development --origin https://supplier.example
docker compose -f compose.yaml -f compose.local.yaml up -d db
docker compose -f compose.yaml -f compose.local.yaml run --rm migrate
docker compose -f compose.yaml -f compose.local.yaml up -d api worker
curl -sS http://127.0.0.1:9000/readyz
```

这里 origin 是占位符，**没有真实供应商、不会自动发送测试订单**。`.secrets/credentials.json` 最初为空，所以应先配置供应商契约再提交。原始 `make demo` 仍提供完整 Mock 故障演示；生产模式真实 PG + 独立 API/Worker + Mock 的链路由 `tests.test_production` 中集成测试自动启动和关闭。

本地 overlay 显式开启 development、使用单 PG、运行用户也是本地 DB owner。它只用于体验，生产必须移除 overlay，并使用下节的最小权限角色和 TLS。

2026-10-24 本机验收已生成开发 `.env` 与 `.secrets/`，不要重复初始化；本机 API 为 `http://127.0.0.1:9000`。网关测试另生成了一天有效的 localhost 自签证书，HTTPS 为 `https://localhost:9443`，只能用该测试证书作为 CA 验证，不能用于生产。测试配置没有真实供应商契约。

```sh
docker compose -f compose.yaml -f compose.local.yaml down
# 不使用 down -v，避免误删持久化数据库卷。
```

## 3. 真实生产配置

1. 用非 root 部署账号运行 `scripts/init_config.py --origin https://真实供应商域名`，或由组织密钥管理系统生成同样格式的只读材料。生成文件仅为启动工具，不替代 KMS、访问审计和密钥灾备。
2. 由平台提供专用多 AZ 同步 PostgreSQL、可信 CA、故障切换地址及加密 PITR 备份；将运行 DSN 放进 `.secrets/database-url`。迁移使用单独 owner DSN；API/Worker 使用非 owner 的 `notifier_runtime` 角色，参考 `deploy/runtime-grants.sql`。示例库名为 notifier，其他库/schema 需逐项调整。
3. 由实际 CA 签发入口证书，放 `.secrets/tls/fullchain.pem` 与 `privkey.pem`；或复用组织现有 TLS 网关，不启用内置 ingress。供应商只允许 HTTPS；私网 API 当前不支持，不能临时关闭 SSRF 检查。
4. 默认 Compose 使用本机 9443 TLS 入口。真实网络入口应通过内部 LB/网关，保留 TLS、连接/请求速率限制、256 KiB 入站上限及请求体超时，禁止直接暴露 API 9000 和 `/metrics`。按实际 NAT/LB 拓扑调整基于 IP 的入口配额，默认 10r/s 只是初始保护值。
5. 至少两 API、两 Worker，分散到不同可用区；每副本数据库连接池默认最多 16，要按所有副本 + 迁移/运维余量核算 PG 连接预算。所有 Worker 必须使用一致的供应商并发/限流配置；禁止混配，否则不同副本会按不同阈值调度。
6. 平台出站网络策略限制供应商、DNS、PG、监控访问。应用 IP 校验不能代替出口控制。数据库连接账号、备份账号和迁移账号分离；审计表并非密码学防篡改账本，需平台日志/审计外送。
7. 先停止并排空所有旧 Worker 和 API 写入实例，确认无在途提交/核对/领取事务，再由 owner 执行迁移；迁移期间禁止旧应用写入，完成后确认 `/readyz` 为 schema version 7 再启动新版本。v1→v7 按序执行 `0002` 至 `0007`；不得修改已发布的 `0006_persist_outcome_uncertainty.sql`。v7 为核对记录新增 `retry_decision_source`：既有 v6 行标记为 `legacy_unverified`，因为 v6 的 `retry_scheduled` 是从当时状态/replay_count 推断，不能作为精确的原始用户决定。
8. v7 仅清除具有严格时间证据的旧不确定性：后续成功 attempt 或 delivered 核对可修正 succeeded；not_delivered 核对严格晚于所有完整 attempt 且之后没有新 attempt 时，pending 保持 pending、failed 保持 failed，只清除旧 uncertainty。它不改写 in_flight 的 lease/token/attempt、origin 并发数、次数预算、供应商 key 或排队状态；in_flight、paused、证据顺序不明/冲突以及没有充分解决证据的 terminal 任务保留 uncertainty，终态疑似不确定任务转入人工核对。v6 迁移审计只用于追溯，不是供应商结果证据。人工核对清楚后才由新版本记录的重试意图字段标为 `runtime_recorded`。
9. v7 在 schema owner 事务和 advisory migration lock 内添加列、扫描当前 uncertain job 所属的 attempts/reconciliations、更新任务、写审计并将 schema version 改为 7；失败整体回滚。大表扫描和任务/核对行锁等待尚未做生产规模演练：attempts 通过 `(notification_id, number)` 主键关联，核对通过任务索引关联，但 uncertain job 数量及旧历史规模决定实际扫描成本。先在生产规模副本演练，观察 `pg_stat_activity`、`pg_locks` 与复制/WAL，再安排维护窗口。默认迁移没有自动降级；失败可重试事务，迁移成功后使用向前修复或备份恢复，不直接回滚到不识别 v7 的应用/schema。旧应用 readiness 会拒绝 schema 7。
10. 镜像已锁定 digest；监控组件样例版本不是“永远最新/无漏洞”的声明。正式发布需扫描镜像及依赖、使用组织批准版本；CI 模板同样应由组织钉住 Actions commit SHA。

内置 Nginx 使用 Docker DNS `127.0.0.11` 与共享 upstream zone 刷新 API 地址；其他运行平台必须调整可信 resolver/服务发现。配置验证不代表已经验证了真实滚动升级的中断时间。

镜像非 root、只读根文件系统、移除 Linux capabilities、限制内存/PID，密钥目录 bind mount 只读。生成 `.env` 中 UID/GID 与文件拥有者一致；权限管理必须在部署主机验证，不能为修复权限把密钥改成 world-readable。

## 4. 供应商契约与提交

`credential_ref` 不只是一个 token 名，它引用服务器控制的供应商授权/重试配置；生产必须提供，且租户与目标 origin 都要匹配。即使无需认证，也配置空 `headers`，不要绕过授权。
每个 profile 还必须定义 `operations`，按 method、path template 和允许的精确 query 值限制操作。路径模板仅允许整段 `{parameter}`；query map 的值可用整段 `{parameter}` 表示非空动态值。默认禁止多余 query 参数、重复 query key、编码路径分隔符和路径穿越。优先使用供应商侧最小权限凭据；operation allowlist 是本系统额外防线，不会让过宽的 vendor token 自行变成最小权限。

`.secrets/credentials.json` 示例（不是实际可用凭证）：

```json
{
  "orders-v1": {
    "supplier_account": "orders-primary",
    "tenants": ["default"],
    "origins": ["https://orders.vendor.example"],
    "operations": [
      {"method": "POST", "path": "/v1/orders", "query": {}},
      {"method": "PATCH", "path": "/v1/orders/{order_id}", "query": {}}
    ],
    "headers": {"X-API-Key": "由密钥管理系统提供"},
    "retry_mode": "idempotent",
    "deduplication_window_seconds": 86400
  },
  "legacy-orders": {
    "supplier_account": "legacy-primary",
    "tenants": ["default"],
    "origins": ["https://legacy.vendor.example"],
    "operations": [
      {"method": "POST", "path": "/v1/orders", "query": {"mode": ["create"]}}
    ],
    "headers": {"Authorization": "Bearer 由密钥管理系统提供"},
    "retry_mode": "manual_on_ambiguous"
  }
}
```

`supplier_account` 是由接入方核实并维护的稳定账号标识，不是凭据，也不会返回给调用方。入队时，系统把账号标识、租户/目标授权、operation allowlist 与重试契约的 HMAC 摘要存进加密快照；发送时重新计算并比较。密钥 Header 可在同一账号且权限范围不变时轮换；修改账号标识、授权范围、操作范围或重试契约会阻止旧任务发送，应使用新的版本化 `credential_ref`。这能发现配置漂移，但不能验证 Header 在供应商侧实际属于哪个账号，也不能从 Header 识别权限变化；账号与权限映射仍须由供应商控制台/契约核实。

升级时，现有 profile 必须补齐已核实的 `supplier_account`。早于快照摘要功能创建的任务没有账号/operation 摘要；在这些任务排空或人工处置前，不得把其 `credential_ref` 指向其他账号或改动既有操作范围。代码层无法反推出旧任务最初使用的供应商账号。

`deduplication_window_seconds` 必须来自供应商已验证契约，不是复制示例数字。窗口由首次领取时启动，以早于实际 HTTP 请求的本地时刻保守计时；任务必须至少剩余 DNS/HTTP 超时预算和安全余量才能领取下一次尝试。**不能证明一个已在其他系统使用过的 Supplier Key 仍有完整有效期**，业务方必须保证它在该 profile 的供应商账号及业务操作内唯一。窗口不足时任务进入 `reconciliation_required`，不继续自动发送。

请求示例：

```json
{
  "url": "https://orders.vendor.example/v1/orders",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json",
    "Idempotency-Key": "tenant-default-order-1001"
  },
  "body": "{\"order_id\":\"1001\",\"amount_cents\":100}",
  "credential_ref": "orders-v1"
}
```

入口仍使用 `Authorization: Bearer <sender-token>` 与另一个入口 `Idempotency-Key`。供应商认证 Header 不允许内联；通用 Header 限于 Content-Type、Accept、Idempotency-Key、X-Correlation-ID，其他字段由服务器契约提供。URL/Body 仍可能敏感，因此整个快照加密，读取 API 不返回它。

模式及不可变契约在接收时冻结；后续相同提交返回原任务，不修改旧任务模式。凭证 Header 执行时读取，可原子替换文件进行同账号轮换；账号/授权/operation/重试契约变化会使旧任务 fail closed，供应商契约升级应使用新的版本化引用并完成旧任务处置。部署需保证配置文件为原子更新，不能边写边让进程读取半份 JSON。重启 Mock 会清空其内存去重记录，不能当成真实供应商去重承诺。

## 5. 权限和运维接口

auth 文件是 principal 列表，字段为 `name`、`tenant`、`role`、`token_sha256`。一个 token 对应一个租户及角色；可并存多个 principal 轮换。角色：

- `sender`：本租户提交、列表、详情、统计。
- `operator`：除上述能力，可重放失败任务、读取本租户审计。
- `reconciler`：可查询本租户任务，并提交有证据引用的供应商结果核对；不可创建或重放任务。
- `metrics`：仅 `/metrics`，可见全服务聚合指标，不可读取业务任务。

`GET /v1/notifications?limit=50&cursor=...&status=failed` 使用 keyset 分页，cursor 来自上一页；status 也可过滤 `paused`。详情 `?after_attempt=50&limit=50` 分页读取 attempt。`GET /v1/audit?before=...&limit=50` 读取审计。`paused` 表示本地无法表示供应商要求的超长冷却；origin hold 与任务结果确定性分开。供应商确认冷却结束只允许解除共享 hold，不证明某个任务未产生副作用。manual 模式任务必须先由 reconciler 记录有证据的结论；不能用运维 SQL 直接把它改成 `pending`。

重放：`POST /v1/notifications/{id}/replay`，Body `{"reason":"供应商修复并已确认未重复创建"}`，要求 operator。先核对供应商业务副作用，再重放；最多 10 轮、保留累计 attempt 及审计。已清理快照 410，状态不允许或去重窗口过期 409；API 错误响应不包含凭证、供应商响应或数据库连接串。

结果核对：`POST /v1/notifications/{id}/reconcile`，Body 为 `{"outcome":"delivered|not_delivered","evidence_ref":"INC-123","reason":"...","retry_after_reconciliation":true|false}`，最后一项可省略；旧客户端省略时，确认未送达会在快照/key/容量/次数/截止条件允许时按旧行为重新排队。`outcome` 是人工确认的业务事实，`retry_after_reconciliation` 是要求重试的意图；响应返回 `retry_requested`、`retry_scheduled` 和 `next_action`。重试上限、快照缺失或 Supplier Key 窗口不足时，仍保存事实、证据和审计并终态关闭，不会新增发送。`retry_after_reconciliation:false` 可明确关闭而不重试；已关闭核对不能通过普通 replay 重新打开。供应商核对证据应由有权限人员验证，API 只校验引用格式，不自动验证外部事实。证据字段仅应填工单/供应商查询记录标识，不要填响应体、密钥或客户数据。若旧 Supplier Key 窗口已过，响应建议使用新的入口 key 和新的 Supplier Key；若快照已清理，响应要求提交新的通知，不能重建或重放旧快照。尚无法判断时不提交 reconcile，保留在未决队列并持续告警。

## 6. 监控与处置

1. 将 `deploy/alertmanager.example.yml` 作为格式参考，在 `.secrets/alertmanager.yml` 配置真实值班接收器；示例 `.invalid` 地址不能使用。
2. `docker compose --profile monitoring up -d prometheus alertmanager`，或直接接入既有监控平台。Prometheus 使用单独 metrics token，内部访问 api:9000；不公开监控端口。
3. 告警规则覆盖 API 抓取失败、Worker 心跳消失、队龄、活跃队列/快照/保留数量配额、失败任务、未决供应商结果、Retry-After hold 及熔断。活跃容量 numerator 与数据库准入均包含 `pending`、`in_flight`、`paused`；API 实例暴露的是同一全局 DB gauge，PromQL 先按 `job,state` 取 `max` 去重再求和，不能跨副本求和。Retry-After hold 持续 10 分钟发 warning 到默认 `oncall` 接收器；值班确认供应商冷却解除后按审计 runbook 清 hold，不能据此将未决任务标成未送达。使用 `promtool test rules tests/alerts.test.yml` 验证 paused/混合容量、多实例、hold firing/恢复及空队列。启动前实际制造一次故障，验证 firing 通知和 resolved 通知到达值班人员。
4. 另外接入平台 PG 磁盘/WAL/复制延迟/连接数/锁等待/备份新鲜度和容器 OOM 告警。应用快照字节配额**不是物理磁盘总量**，不含索引、WAL、日志等。
5. 失败告警基于保留的失败状态，未处置会持续提醒；恢复成功的重放可解除。真实接入应按团队流程调整抑制/确认策略，避免静音掩盖新失败。

指标来自数据库，API 多副本会报告同一全局值。告警表达式对全局 gauge 使用 max/去重，不对各 API 的 gauge 求和。`NotificationRetryAfterHold` 使用 warning + `for: 10m`，指引为核实供应商冷却、再按单一审计 SQL 解除共享 hold；hold 持续期间不能人工放开新发送。聚合 SQL 受 statement timeout 限制，大数据量下监控查询本身也是容量预算的一部分。

## 7. 重试、限流与保留

默认 8 个执行线程、单 origin 2 并发、5 次尝试/秒、24 次 claim、24 小时调度窗口；退避 10 秒起、最多 1 小时，抖动 50%。Retry-After 的数字秒数先去除首尾空格/制表符，HTTP-date 按其时间解析；有效值不受指数退避上限截短。一年是本地支持的最大有限数字等待，超出范围的纯数字不会被截短或当成零，而会将共享 origin 设为 `cooldown_hold=true`，相关任务设为 `paused`，或在 manual 模式结果不明时进入 `reconciliation_required`；不自动重发，并导出 `notify_retry_after_holds` 指标。该 hold 没有自动超时；确认冷却解除只解除 origin 门禁，不会把 manual 任务判为未送达。其他任务在 hold 期间不能新发送，但过期租约仍会被回收并完成 attempt/in_flight 对账。测试环境不可用时不得在生产 DB 验证恢复操作。常规任务的有效 Retry-After 即便超过该任务截止时间，也会阻止同 origin 其他任务提前发送。5 个可重试供应商故障打开目标熔断 60 秒；本地授权/配置故障不增加或清零共享供应商健康计数。

这些是可调初始值，不是已验证供应商配额。多副本共享限制状态，后续需按供应商账号级配额模型演进；多个坏 origin 仍可同时占满所有执行资源，强隔离需求应部署不同 worker pool/队列，不把域名额度当成无限隔离。

默认活跃任务 10,000、本租户活跃 1,000、保留任务 100,000、加密快照 1 GiB。超过返回 429 + Retry-After，重复 key 仍返回原结果。配额计数与接收在同一事务；同一 admission 锁也保护清理计数，调度/投递不持有该锁。共享 origin 内按可执行任务进行跨租户轮转；所有租户仍共享该 origin 总并发/速率熔断额度，此设计不提供按供应商账号或接口的独立 SLA。

超范围 Retry-After 的解除必须由 on-call 在供应商侧确认冷却结束后，通过隔离审批的 owner 会话执行单一事务，并填实际工单编号作为 `:ticket`、值班身份作为 `:actor`、精确 origin 作为 `:origin`。有 attempt 或已有 uncertainty 的 manual `paused` 任务转为 `reconciliation_required`，保守保留结果不明状态和原始 attempt；仅能证明从未尝试的 manual 任务可作为未发送任务终结。其他未决任务在截止/次数/key 窗口不足时转核对，不清除 `outcome_uncertain`。只有 `idempotent`（完整 Supplier Key 窗口覆盖发送预算）或 `duplicate_tolerant`（重复副作用已获业务授权）且仍有预算的任务才恢复 `pending`，原有 uncertainty 标记保持不变。该示例需用客户端绑定参数执行，不能拼接用户输入：

```sql
BEGIN;
SELECT origin FROM delivery_origins WHERE origin = :origin FOR UPDATE;
-- Never rewrite attempts. A manual-mode attempt may have reached the supplier.
WITH quarantined AS (
    UPDATE delivery_jobs
    SET status='reconciliation_required',
        outcome_uncertain=true,
        last_error='legacy_paused_manual_outcome_unknown',
        updated_at=extract(epoch FROM clock_timestamp())
    WHERE origin=:origin AND status='paused'
      AND retry_mode='manual_on_ambiguous'
      AND (outcome_uncertain OR attempt_count>0)
    RETURNING tenant,id
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,:actor,'manual_paused_task_quarantined',id,:ticket,
       extract(epoch FROM clock_timestamp())
FROM quarantined;
WITH blocked AS (
    UPDATE delivery_jobs
    SET status = CASE
            WHEN outcome_uncertain THEN 'reconciliation_required'
            ELSE 'failed'
        END,
        last_error = CASE
            WHEN outcome_uncertain THEN 'supplier_outcome_unresolved_after_hold'
            ELSE 'delivery_deadline_or_attempt_budget_exhausted'
        END,
        updated_at = extract(epoch FROM clock_timestamp())
    WHERE origin = :origin AND status = 'paused'
      AND (
          deadline <= extract(epoch FROM clock_timestamp()) + :send_budget
          OR cycle_attempts >= max_attempts
          OR retry_mode='manual_on_ambiguous'
          OR (retry_mode = 'idempotent' AND attempt_count > 0
              AND (key_expires_at IS NULL OR key_expires_at <=
                   extract(epoch FROM clock_timestamp()) + :send_budget))
      )
    RETURNING tenant,id
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,:actor,'retry_after_hold_recovery_blocked',id,:ticket,
       extract(epoch FROM clock_timestamp())
FROM blocked;
UPDATE delivery_origins
SET cooldown_hold = false,cooldown_reason = NULL,
    next_allowed = extract(epoch FROM clock_timestamp())
WHERE origin = :origin AND cooldown_hold;
WITH transitioned AS (
    UPDATE delivery_jobs
    SET status = 'pending',last_error = 'retry_after_hold_released',
        next_attempt_at = extract(epoch FROM clock_timestamp()),
        updated_at = extract(epoch FROM clock_timestamp())
    WHERE origin = :origin AND status = 'paused'
      AND retry_mode IN ('idempotent','duplicate_tolerant')
      AND deadline > extract(epoch FROM clock_timestamp()) + :send_budget
      AND cycle_attempts < max_attempts
      AND (retry_mode <> 'idempotent' OR
           (attempt_count = 0 AND key_expires_at IS NULL) OR
           (key_expires_at IS NOT NULL AND key_expires_at >
            extract(epoch FROM clock_timestamp()) + :send_budget))
    RETURNING tenant,id
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,:actor,'retry_after_hold_released',id,:ticket,
       extract(epoch FROM clock_timestamp())
FROM transitioned;
COMMIT;
```

执行恢复前应核对 origin 下暂停任务、`deadline`、`cycle_attempts/max_attempts`、`attempt_count`、`key_expires_at`，并用应用当前配置计算 `:send_budget = timeout_seconds + 3`。示例使用命名参数仅表示绑定变量，需按组织运维工具改写为参数化 SQL。任务更新、审计和 hold 清除必须在同一事务及 origin 行锁内完成；完成后核对仍暂停与待核对任务。若查不到该 origin 或 `cooldown_hold` 未被清除，应回滚并调查；审计记录要能关联工单。不要使用 `notifier_runtime` 账号执行 owner 运维操作。

Worker 每分钟分批清理最多 500 条；终态快照/attempt 默认保留 30 天，元数据/入口 key/audit 90 天。过期不是瞬时删除保证，清理落后必须告警/运维追踪。元数据删除后同 key 可视为新任务；调用方不能把入口幂等理解成永久去重。终态更新时间随人工重放变化，保留期会延长；全生命周期配额用于兜底。origin 行不自动删，避免并发接收外键竞态，规模受配置白名单约束。

`reconciliation_required` 任务不参加自动调度或普通终态清理，必须先人工闭环。它持续占用保留任务及快照配额并触发 critical 告警；长期未处置会使准入背压，这是有意暴露未决业务风险，不自动丢弃证据。运营需设置处理时限与负责人。

## 8. 密钥轮换、备份和恢复

Fernet key ring 第一个为写入密钥，旧密钥用于读取。独立 fingerprint key 不能随加密 key 一起替换，否则入口幂等指纹失效。

1. 将新 Fernet key 放在 key ring 第一位，保留所有仍用于数据/备份的旧 key，原子发布。
2. 执行 `python -m notifier.production rotate`，按 UUID 游标分批轮换全部保留快照，不修改创建时间或保留期。
3. 验证所有快照可读、待执行任务正常、备份解密材料齐全，再按备份保留策略决定何时撤下旧 key。不能仅因在线数据轮换完成就销毁恢复旧备份所需密钥。

生产使用托管 PG 的加密全量备份 + 持续 WAL/PITR，密钥在独立权限域备份。额外逻辑备份示例只用于演练，不代替 PITR：

```sh
# 使用受限备份账号的 .pgpass / PGSERVICE，避免将生产密码写进命令或日志。
pg_dump --dbname=service=notifier_backup --format=custom --file=notifier.dump
pg_restore --list notifier.dump
pg_restore --dbname=service=notifier_restore --no-owner --no-acl notifier.dump
```

恢复验收必须在隔离库与隔离网络中，先暂停所有 Worker，恢复 schema/数据及解密材料，核对任务数量/配额计数、抽样解密和租约状态，再核对备份时间以后供应商的副作用。恢复旧备份可能让已成功任务重新出现为待执行；任何 DB/MQ 均无法替你撤销供应商已处理的请求。没有确认隔离和对账前禁止让恢复库连接真实供应商。

## 9. 待环境验收

代表性压测、真实供应商契约测试、真实告警联通、多 AZ 主库切换、PITR/密钥恢复、最小权限及公网安全测试必须单独签收。当前代码和本机测试不能替代它们。没有这些证据时，正确名称是“生产候选版本”，不是“已经证明生产级可用”。
