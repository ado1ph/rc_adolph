# 生产候选方案与业界的一手资料复核

核对日期：2026-10-24。对象是 `notifier/production`，不是原 SQLite 演示。

本轮实际读取了下列官方仓库文件，通过 GitHub contents API 的 raw 响应获得全文；网页搜索工具返回空结果，没有据此编造网页引用。链接指向可变默认分支，不是锁定版本。未部署、压测或审计这些平台，官方能力声明不等于本地实测；不比较未经测量的吞吐、成本金额或可用性。

## 1. 先区分两类业务契约

本项目是“调用供应商已有 API”：供应商决定 HTTP method、原始 JSON/表单、认证方式、业务响应和幂等规则。Webhook 平台主要解决“把自己的事件发给接收方”。两者都需要持久化、重试、查询和隔离，但不代表能零成本互换。[L1]

Outpost 官方 webhook 文档规定 HTTP POST、JSON `data` 作为 Body，并支持自定义 Header、签名模板及稳定事件 ID。它明确要求消费者按事件 ID 去重。这是可定制的 webhook 协议，不等于已经适配供应商的 PATCH、表单、OAuth 刷新或业务错误码。[S3][S4]

因此，本项目保留原始请求和区分入口 key / 供应商 key 更贴题；但“更贴题”不代表“更成熟”。真实供应商 Adapter、自动查询对账和业务完成判定仍是缺口。签名的作用是证明消息来源，不能替代供应商侧副作用去重。[L1][S4]

## 2. 当前可证实的比较

| 维度 | 已读取的业界依据 | 对本项目的判断 |
| --- | --- | --- |
| 队列选型 | Svix 要求 PG，Redis 对队列/缓存可选，但配置列出的 queue backend 是 memory / redis / rediscluster，默认 Redis；其配置明确警告多 API 实例使用 memory cache 可能影响幂等。不能把“Redis 可选”解读为该内存模式适合持久化生产。[S1][S2] Outpost 自托管使用 Redis + 支持的 MQ + PG 或 ClickHouse 日志存储。[S5] | PG ledger 的原子接收和少一个一致性协议是合理选择，但不是借 Svix 宣称同架构已被证明。自维护状态机、锁、清理和升级仍有成本。[L1] |
| PG 不是反行业方案 | pg-boss 官方列出同 DB 事务创建任务、SKIP LOCKED、重试、死信 redrive、调度和背压；Graphile Worker 是 Node.js 上的 PG 后台任务库。[S7][S8] Convoy 当前 README 也列出 PG queue，但注明 experimental 且需要 license。[S6] | 用户要求不加 Redis/MQ并非错误前提。更应该比较“自写调度”与“成熟 PG 任务库”的维护成本，而不是把“生产级”直接等同“引入 MQ”。这些 Node.js 库也不是当前 Python 服务的无成本替换。 |
| Python 生态替代 | PgQueuer 是 Python/PostgreSQL 队列，项目文档列出事务入队、`SKIP LOCKED`、并发限制、LISTEN/NOTIFY、Prometheus/trace/dashboard；Procrastinate 提供 PostgreSQL schema、worker、CLI 与 psycopg connector。[S14][S15] | 这是比 Node.js queue 更贴近当前运行时的真实候选，应该在继续扩大自研调度前做集成 spike。现在不直接替换：`delivery_jobs` 同时是调用方幂等账本、投递状态/attempt/audit、容量配额及核对记录；若库使用独立任务表，会引入双状态和事务协调，需先证明同事务接入和既有语义可保留。未做 POC，不能据 README 宣称库一定更快或更可靠。 |
| 重试及重复 | Outpost 声明 at-least-once、可能重复、有限自动重试、手动重试、禁用目标及 attempt 事件。[S3] Convoy README 列出带抖动退避、按 endpoint 限速和熔断。[S6] | 本项目把不确定结果转人工、约束供应商幂等窗口，是针对不能修改接收方契约的保守取舍，代价是可用性和人工成本。不能把有限重试直接解释成“至少一次成功”，更不能宣称平台为供应商提供 exactly-once。[L1] |
| 公平与隔离 | Convoy 文档可证实 endpoint 限流和熔断；pg-boss README 可证实 priority 与 concurrency/rate policies。但本轮未验证任何平台严格的 tenant 公平性或坏目标资源保底。[S6][S7] | 本项目现有 origin 额度、共享速率/熔断及同 origin tenant 轮转；仍不等于账号/路径级配额隔离，也未证明跨 origin 公平。应按供应商真实配额定义并压测 scope。[L1][L2] |
| 运维与可观测性 | Svix README 有 OpenTelemetry、运维 webhook 和 Redis DLQ 指标/redrive，且提醒自托管与托管版有差异。[S1] Outpost 有 attempt 查询、Admin UI / tenant portal 和操作事件。[S3] pg-boss 列出 OTel 与 dashboard。[S7] | 本项目已有 Prometheus、脱敏日志、审计、reconciliation API 和未决告警；仍缺端到端 tracing、供应商级故障归因和专用处置 UI。只加仪表盘不能替代语义/证据流程。[L2] |
| 凭证及供应商适配 | Convoy 当前 README 声明 OAuth2、mTLS、签名和 rolling secrets；具体配置、授权限制及故障语义本轮未继续验证。[S6] Outpost 支持自定义 Header、签名格式、旧新签名共存和 forward proxy。[S4] | 本项目按 method/path/query 限定操作，并把供应商账号声明及授权/操作/重试契约摘要固定到加密任务快照；同账号静态 Header 可轮换。账号声明不能自动证明供应商侧身份，OAuth 刷新互斥、动态签名、mTLS、业务错误判定/异步回执仍需按真实供应商设计；不能因 README 有对应名词就宣称能直接适配。 |
| 数据保留与诊断 | Svix 默认配置把过期 payload / contents 清理与保留 message 行、后续 prune 区分；Outpost 记录状态、响应和 attempt，并限制响应体存储大小。[S2][S3][S4] | 本项目 payload / metadata 分期保留有合理依据，但完全不保存响应也损失业务诊断能力。后续若增加响应证据，应有大小上限、脱敏、加密和短保留，不能默认全量记录秘密。清理积压、任务表膨胀、WAL/磁盘仍需单独监控。[L1][L2] |

以上是设计比较，不是平台排名；完整 webhook 管理门户、多目标订阅和事件扇出在本题不是必需品，不应为追平功能列表而增加复杂度。

## 3. 首次审计时的补齐建议

以下是首次审计时的工程建议，不声称所有对照平台都已解决；其中发送预算、核对状态、operation allowlist 和基础租户轮转已在本轮实现。剩余事项见设计审计整改记录。

1. **发布前补业务语义**：operation method/path/query allowlist 与未知结果状态已实现；还需为真实 `200 + 业务错误`、异步受理、OAuth/签名等供应商行为逐个定义 Adapter 和契约测试。幂等起算点及窗口需供应商确认。
2. **发布前补对账强度**：人工闭环已有独立角色、证据引用和审计；高风险操作再评估供应商查询 Adapter 或双人复核。文本证据引用不是自动事实核验。
3. **容量前补调度契约**：定义 tenant / credential-account / origin / endpoint 哪层限额与公平；按供应商延迟、坏目标数量和租户倾斜压测，再决定保留容量、加权轮转或独立 worker pool。严格顺序不是本题既定需求，不应暗中承诺。
4. **运维前补证据**：供应商/失败类别聚合、执行延迟及重试放大、清理落后、锁等待、备份恢复和真实告警联通；跟踪 cardinality 和敏感信息，避免以无界 URL / tenant label 解决观测。
5. **架构演进有条件**：当前无需先上 MQ。若代表性负载证明 PG 调度无法满足预算，先评估现成 PG 库的状态/迁移语义，再评估 ledger + outbox + MQ；不能只替换队列类而保留双调度或双写漏洞。

## 4. 证据与未核实边界

本地阅读：[L1] `docs/architecture.md`；[L2] `docs/production-runbook.md`。历史 `industry-comparison.md` 已标为 SQLite MVP 评审，不能再把其中“没有隔离/加密/告警”作为生产候选的当前缺陷。

本次实际读取的一手来源：

- [S1] Svix README：运行依赖、Redis 持久化、OTel、运维事件、托管差异、DLQ。https://github.com/svix/svix-webhooks/blob/main/README.md
- [S2] Svix 默认配置：queue/cache backend、多 API 幂等警告、重试与过期清理。https://github.com/svix/svix-webhooks/blob/main/server/svix-server/config.default.toml
- [S3] Outpost Event Delivery & Retries：重复、有限重试、手动处置、attempt 事件。https://github.com/hookdeck/outpost/blob/main/docs/content/features/event-delivery.mdoc
- [S4] Outpost Webhook：POST/JSON 契约、事件 ID、自定义签名、凭证轮换、响应体上限。https://github.com/hookdeck/outpost/blob/main/docs/content/destinations/webhook.mdoc
- [S5] Outpost Architecture：三服务及自托管运行依赖。https://github.com/hookdeck/outpost/blob/main/docs/content/self-hosting/architecture.mdoc
- [S6] Convoy README：endpoint 限流/熔断、OAuth2/mTLS、PG queue experimental、ELv2 与授权边界。https://github.com/frain-dev/convoy/blob/main/README.md
- [S7] pg-boss README：事务入队、SKIP LOCKED、任务策略、重试/死信、OTel/dashboard、Node 运行时。https://github.com/timgit/pg-boss/blob/master/README.md
- [S8] Graphile Worker README：PG + Node.js 后台任务库定位。https://github.com/graphile/worker/blob/main/README.md

未核实：各平台当前发布版与默认分支的差异、许可具体条款适用、Graphile 的详细 retry/job_key 语义、严格租户公平性、真实供应商 OAuth/响应契约、数据合规认证、HA/PITR 表现和任何性能数字。README 的功能声明不是源码级正确性证明；以上库也不因此得到对外部 HTTP 副作用 exactly-once 的背书。

## 5. 2026-10-24 专项复核补充

以下是同日后续修改时重新核对的规范与供应商原文，不把前文“建议补齐”误认为修改后仍完全缺失。来源事实和工程判断仍不等于部署验收；实际应用测试结果见 `docs/verification.md`。

| 议题 | 一手事实 | 本项目设计判断及限制 |
| --- | --- | --- |
| PG 队列领取 | PG 17 SELECT 的 Locking Clause 说明 `SKIP LOCKED` 跳过不能立即锁定的行，可降低多个消费者访问 queue-like table 的争用，但会提供不一致视图，且不免除表级锁。[S9] | 适合短事务领取，不证明 FIFO、公平、故障切换不丢数据或外部副作用 exactly-once。配额、租约和状态事务仍须独立验证。 |
| 不确定副作用 | RFC 9110 §9.2.2 不建议自动重试非幂等请求，除非知道实际语义幂等，或能确认原请求未应用。[S10] | `reconciliation_required`、显式核对结论和证据引用符合该风险方向，但规范未要求具体表结构/人工流程。当前 `reconcile()` 仍信任获权操作员陈述，证据格式校验不等于自动核实供应商事实；高风险业务可另加查询 Adapter 或双人复核。 |
| 去重期限与错误 | Stripe 当前 API 文档区分 v1/v2：v1 在执行开始后保存首个结果（包括 500），相同 key 返回保存结果；key 至少 24 小时后可清理，清理后复用会创建新请求，并检查同 key 参数一致性。[S11] | 稳定 key 不保证最终成功，也不是永久去重。`claim()` 留足完整执行预算再发请求、过窗转核对，是本项目保守策略；不能照抄 Stripe 的时长给其他供应商，也不能假定所有供应商缓存 500。还应确认首次使用位置、供应商计时起点、时钟及超时后服务端继续执行的边界。 |
| Retry-After | RFC 9110 §10.2.3 允许 HTTP-date 或非负十进制整数秒；RFC 6585 §4 允许 429 携带该 Header，但不定义按资源、全服务还是认证主体计数。[S10][S12] | 不应把合法长等待截短后提前重试；超出任务预算应终止自动调度或转核对。当前 parser 有一年数值上限，这不是 RFC 上限，属于实现防护；其安全性还依赖任务窗口小于该值。origin 级共享冷却只是保守策略，可能误伤同域其他账号，也可能无法覆盖跨域共享配额。 |
| 公平与配额 | AWS SQS fair queues 官方以安静租户的 dwell time、在途分布和 noisy-neighbor 影响定义公平，明确不按租户限制消费速率，也不保证消息顺序。[S13] | 这是定义参考，不是建议引入 SQS，也不是本项目与 SQS 等价。当前同 origin 内 tenant 轮转区别于仅有积压配额，但不保证跨 origin 按租户总成本公平，慢任务仍可能占用更多资源。需测安静租户延迟及在途占比，而非只测领取序列。 |

本节实际读取来源（官方页面经 `curl` 获取，未使用第三方概述）：

- [S9] PostgreSQL 17 `sql-select.html`，Locking Clause，`SKIP LOCKED` 段落。https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE
- [S10] RFC 9110，§9.2.2 Idempotent Methods、§10.2.3 Retry-After。https://www.rfc-editor.org/rfc/rfc9110.txt
- [S11] Stripe API，Idempotent requests；本节引用 v1 语义，不外推到 v2。https://docs.stripe.com/api/idempotent_requests
- [S12] RFC 6585，§4 429 Too Many Requests。https://www.rfc-editor.org/rfc/rfc6585.txt
- [S13] AWS SQS Developer Guide，Amazon SQS fair queues，noisy neighbor、消费速率与 `MessageGroupId` 说明。https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-fair-queues.html
- [S14] PgQueuer 官方 GitHub README：Python/PostgreSQL 队列、事务入队、并发、通知、可观测性及支持的驱动。https://github.com/janbjorge/pgqueuer
- [S15] Procrastinate 官方 GitHub Quickstart：PostgreSQL schema、psycopg connector、worker 与操作 CLI。https://github.com/procrastinate-org/procrastinate/blob/main/docs/quickstart.md

当前源码交叉阅读位置：`notifier/production/store.py` 的 `_recover()` / `claim()` / `reconcile()`，`notifier/production/config.py` 的 profile digest，`notifier/delivery.py` 的 `retry_after_seconds()`。隔离 PostgreSQL 17 完整测试 85 项通过；其后的小型 HMAC 域分隔改动有定向回归覆盖，完整套件未能重跑（Docker daemon 停止）。这些结果不证明真实供应商契约、严格公平、人工证据真实性或真实 HA 已验证。

### 同日最终交叉核对

- **公平不是锁选项**：重新读取 pg-boss 官方 README 的 Summary，确有 priority、rate / concurrency policies，但该列表没有承诺严格租户公平。[S7] 当前本项目按 `last_claimed` 选择 origin，再按 `last_tenant` 轮转该 origin 内的到期租户；这是明确定义的两级调度，不是全局按租户均分线程/执行时间，也没有严格 FIFO 承诺。`SKIP LOCKED` 只解决行锁争用，不能为上述公平性背书。[S9]
- **配额维度仍需供应商证据**：origin 的速率和冷却状态是当前保守策略，不应改名为“真实供应商配额”。RFC 6585 §4 明确不规定 429 如何识别主体和统计请求，因此真实接入必须记录配额维度（账号、API key、endpoint、区域或共享账户）及验证依据。[S12] 缺少信息时不能从同域名自动推出同配额，也不能从不同域名自动推出独立配额。
- **等待上限目前有额外保护**：当前配置把任务自动调度窗口限制在最多 604800 秒（7 天），小于 parser 的一年数值上限。因此合法超长 Retry-After 不会在该任务窗口内被提前重试；这一判断依赖现有配置上限，未来放宽必须同步复查。它是实现推导，不是 RFC 自带保证，也不意味着应该将该任务的冷却广播给同域所有独立账号。

### 本次比较结论

- **事实：PG 库不必引入非事务双写。** 本次重新读取 PgQueuer 官方默认分支 README，明确支持既有 PostgreSQL connection，并演示在同一个 `connection.transaction()` 内同时写业务数据和 `enqueue()`。[S14] 前表中的“双状态和事务协调”是本项目领域状态映射的集成问题，不意味着使用库必须独立数据库或失去原子提交；需通过 POC 验证现有同步/异步连接、表结构及租约恢复语义。
- **判断：现在是有针对性保护的生产候选，不是已证明优于业界的平台。** 项目的优势是贴合任意供应商请求及“不明结果不可盲重放”的领域约束；现成队列的优势是复用调度、通知和观测工具。本项目继续自研需要持续承担状态机、迁移及故障恢复维护成本，现有测试数不能换算成相对可靠性或吞吐排名。
- **未验证：幂等发送预算仍非全链路闭环。** 当前预算检查位于数据库领取阶段；领取后配置读取、进程暂停及发起出站前延迟仍可能消耗剩余窗口。尚缺紧贴出站的绝对截止时间复核及相应故障注入测试；未复现重复副作用，也不能据此宣称不存在该风险。真实供应商去重/配额契约、自动对账、负载与恢复验收仍是上线边界。
