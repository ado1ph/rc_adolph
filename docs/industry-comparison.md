# 与参考方案的优缺点

核对日期：2026-10-23。依据当前仓库实现及下列官方资料。未部署或压测 Svix、Outpost、Convoy、Asynq，也未对本项目做性能测试；不比较未经测量的吞吐、成本金额或可用性数字。

**历史评审归档。生产候选版的当前对照请读 [2026-10-24 业界复核](industry-review-2026-10-24.md)。** 特别是 Svix 的 Redis 可选与内存队列/缓存限制需一并阅读，不能据此推断无 Redis 部署已具备持久化及多副本幂等保障。

本表保留的是原始 SQLite MVP 的差距评审，不是生产候选实现的现状清单。后续已按该评审增加 PostgreSQL、ASGI、权限/审计、供应商隔离、凭证保护、配额/保留及告警配置；选型重审见 [architecture.md](architecture.md)，实际验证与仍待完成的生产门槛见 [verification.md](verification.md)。新增代码不等于这些差距都已在真实生产环境中被证明解决。

## 先纠正比较前提

“业界方案”不是统一架构。Svix 的自托管服务需要 PostgreSQL，Redis 是可选项；Outpost 的自托管架构需要 Redis、消息队列和日志数据库，但服务可以运行在一个进程中。因此，不能把“用了 MQ”当成成熟度或可靠性的必要条件，也不能把“少组件”直接等同于“更可靠”。[S1][S2]

Svix、Outpost、Convoy 是 webhook 平台，Asynq 是 Go/Redis 任务执行库，不是可直接替代通知 API 的成品。[S1][S3][S5][S6] 本项目的边界则是：可信内部调用方提交供应商已有 API 的请求快照，异步原样调用，不要求供应商接入我们定义的事件协议。[L1][L4]

## 对比结论

| 维度 | 当前 MVP 的优点 | 当前 MVP 的代价 / 差距 |
| --- | --- | --- |
| 请求适配 | 每条任务可指定白名单内 URL、Header、原始 UTF-8 Body；支持 POST/PUT/PATCH，直接演示 JSON 与表单。[L4][L5] | 不是无限制 HTTP 代理：无二进制、大文件、OAuth 刷新、动态签名和供应商业务响应解析。Outpost 文档中的 webhook 是配置目标的 POST + JSON `data`，不是相同的原始请求任务契约；这不意味着所有平台都无法适配。[S4][L4] |
| 提交一致性 | 请求记录本身就是队列，同一 SQLite 事务持久化，没有“存请求成功但 MQ 入队失败”的第二个提交点。[L2] | 仅覆盖本服务内部。业务系统提交前崩溃仍可能漏通知；换成任何独立通知服务都要单独设计源头 outbox 等衔接。[L1] |
| 故障恢复 | 具备租约、续租、过期恢复、结果 fencing、历史 attempt 和显式重放，不是只启动一个后台线程就宣称可靠。[L2][L3] | 本机磁盘/机器仍是单一故障域；多线程领取不等于跨机 HA。fencing 只能保护本地状态，不能撤销已发出的供应商请求。[L1][L2] |
| 重试与重复 | 有界重试、退避抖动、部分状态分类和 `Retry-After`；明确承认外部副作用可能重复。[L2][L4] | 默认一轮 5 次 claim、秒级退避，可能很快转失败，长期故障恢复依赖人；claim 后未发 HTTP 即崩溃也消耗预算。不是“每条通知至少一次成功”的承诺。[L1][L2] |
| 供应商隔离 | 固定 worker 数、一般网络 deadline 控制资源占用。[L3][L4] | 所有目标共享 worker；无目标级并发额度、公平调度、限流或熔断，坏目标能拖延好目标；DNS 不受应用层 deadline 严格限制。[L3][L4] |
| 运维与安全 | 有状态查询、attempt、重放与基础统计；查询不返回请求快照；出站白名单、DNS/IP 校验、固定连接 IP、不跟随跳转。[L2][L4] | 没有主动告警、完整分页审计、自动清理或配额；数据库明文保存 Body 和凭证；鉴权仅一个共享 token；`http.server` 仅用于 loopback 演示，不是公网生产入口。[L1][L2][L6] |
| 部署与维护 | Python 标准库 + SQLite，演示无需 pip、Docker、Redis 或账号，便于审阅和复现故障路径。[L1] | 组件少，但 HTTP 出站、安全校验、队列与租约是自维护代码；维护、升级、安全审计的成本并没有消失，只是从中间件运维转移到了项目内部。此项是工程判断。 |

## 具体借鉴对象

- **Svix**：官方 README 展示 PostgreSQL、可选 Redis、OpenTelemetry 和运维 webhook，并明确开源服务与托管服务存在差异。适合借鉴可观测性与运维事件，不应把托管版能力全部写成开源版已有，也没有依据断言其必然存在“数据库 + MQ 双写缺陷”。[S1]
- **Outpost**：文档明确允许重复，要求接收方按稳定事件 ID 去重；支持失败查询、手动重试、禁用目标及 attempt 运维事件。说明“有限自动重试 + 人工恢复”本身不是错误，当前 MVP 的短板是恢复窗口、告警和操作工具薄弱，而非没有无限重试。[S3] 它将 API、投递、日志分为可独立扩展的服务；完整采用会引入 Redis、MQ、日志数据库的配置、备份与监控责任。[S2]
- **Convoy**：官方 README 列出每 endpoint 限流、熔断、失败通知、批量重试，以及 server/agent 独立扩展。这些是当前缺失的生产能力。其许可为 ELv2，部分能力有版本或授权约束，不能把列表全部理解成无条件免费可用。[S5]
- **Asynq**：提供崩溃恢复、重试、多队列优先级、Redis Sentinel、Prometheus、管理 UI/CLI，可减少自写调度代码；但通知 API、供应商认证/适配、SSRF 与入口幂等仍要自己做。若另外写业务数据库再入队，还必须设计两个存储提交点的一致性。官方也提示 v0 API 变化与部分 Redis Cluster Lua 兼容性限制。[S6]

## 不能靠更换框架解决的边界

入口幂等只防重复提交，不等于供应商只产生一次副作用。请求成功但响应丢失时，重试可能重复；Outpost 也明确要求接收方去重。[S3][L2][L5]

当前实现把收到 2xx 响应头视为投递成功，不解析供应商业务响应。[L4] 如果真实供应商返回 `200` 但 Body 是业务失败，或者异步受理后失败，必须在供应商契约层另定判据，不能用“HTTP 成功”替代“业务完成”。

Mock 证明的是预设协议和故障路径可以运行。其幂等记录、副作用都在内存中，没有接入任何真实供应商，也不能证明真实供应商的去重保留期、冲突行为、超时提交边界、配额或幂等实现。[L5] 本项目现有测试和进程退出测试也不是断电、磁盘损坏、公网安全或生产可用性认证。[L1]

## 判断与下一步

**对作业：当前方案的优势是范围贴题、容易复现、语义可解释；对生产：它仍只是单机原型，不能与完整平台等价。**

建议按风险推进，而不是先换 MQ：

1. 明确真实负载、供应商数、最长可接受延迟、停机容忍度、凭证敏感度，以及供应商的幂等与业务成功契约。
2. 优先补积压/失败主动告警、磁盘容量保护和恢复流程；按故障恢复目标调整重试窗口，增加目标级隔离与限流。
3. 真实部署时改用成熟应用服务器/框架，配置入口和出口网络策略、凭证管理、保留策略与备份恢复验证。没有 Docker 不是不用成熟框架或 PostgreSQL 的充分理由。
4. 需要跨机 HA 或写入扩展时，再评估 PostgreSQL 队列、现有任务库或完整平台；引入独立 MQ 时明确 outbox 与消费恢复。数据库或 MQ 的部署方式、备份和故障切换仍需设计，换产品不自动获得 HA。

以上顺序是针对当前低基础设施约束、无明确生产 SLO 的工程判断，不是通用最优解。若团队已有可用 PostgreSQL/Redis、统一告警与平台运维，复用现有能力的边际成本可能更低，当前自维护方案的优势会缩小。

## 来源

官方来源均于本次通过 GitHub 文件接口重新读取；链接是可变默认分支，不是锁定的代码版本，实际复用前需复核。

- [S1: Svix README](https://github.com/svix/svix-webhooks/blob/main/README.md)
- [S2: Outpost 自托管架构](https://github.com/hookdeck/outpost/blob/main/docs/content/self-hosting/architecture.mdoc)
- [S3: Outpost 投递与重试](https://github.com/hookdeck/outpost/blob/main/docs/content/features/event-delivery.mdoc)
- [S4: Outpost Webhook 目标](https://github.com/hookdeck/outpost/blob/main/docs/content/destinations/webhook.mdoc)
- [S5: Convoy README](https://github.com/frain-dev/convoy/blob/main/README.md)
- [S6: Asynq README](https://github.com/hibiken/asynq/blob/master/README.md)
- [L1: 本项目 README](../README.md)
- [L2: 持久化与状态事务](../notifier/store.py)
- [L3: Worker 与租约续期](../notifier/worker.py)
- [L4: 请求校验](../notifier/validation.py)及[HTTP 投递](../notifier/delivery.py)
- [L5: Mock 供应商](../notifier/mock_supplier.py)
- [L6: 本地 API](../notifier/api.py)
