# 一手资料与采用边界

核对日期：2026-10-23。未实际部署或压测参考项目，也未核实行业市场份额/企业采用规模。以下链接指向可变文档/默认分支；实际复用源码前应锁定提交、复核许可。

## 项目参考

- [Svix README](https://github.com/svix/svix-webhooks/blob/main/README.md) 与 [MIT LICENSE](https://github.com/svix/svix-webhooks/blob/main/LICENSE)：Rust、PostgreSQL、可选 Redis、内网目标默认限制、托管/开源差异。借鉴安全边界；没有声称开源版包含托管版全部能力。
- [Svix Attempt 模型](https://github.com/svix/svix-webhooks/blob/main/javascript/src/models/messageAttemptOut.ts) 与 [查询接口](https://github.com/svix/svix-webhooks/blob/main/javascript/src/api/messageAttempt.ts)：消息与 attempt 分离。此项目独立实现，没有复制其 Rust dispatcher。
- [Outpost 投递语义](https://github.com/hookdeck/outpost/blob/main/docs/content/features/event-delivery.mdoc)、[架构](https://github.com/hookdeck/outpost/blob/main/docs/content/self-hosting/architecture.mdoc)、[Webhook 目标](https://github.com/hookdeck/outpost/blob/main/docs/content/destinations/webhook.mdoc)：参考有限重试、稳定事件 ID、失败可查与人工恢复；不照搬多目标平台及 Redis + MQ + 日志存储架构。
- [Convoy README](https://github.com/frain-dev/convoy/blob/main/README.md) 与 [ELv2 LICENSE](https://github.com/frain-dev/convoy/blob/main/LICENSE)：作为目标限流、熔断等未来能力的对照，不作为本版依赖；许可说明仅指向官方文本，不构成法律判断。
- [Asynq README](https://github.com/hibiken/asynq/blob/master/README.md) 与 [MIT LICENSE](https://github.com/hibiken/asynq/blob/master/LICENSE)：Go/Redis 任务执行库可减少调度代码，但当前没有既有 Go/Redis 约束，未引入。

详细前期调研另保留在工作区的 `API通知系统-开源项目调研.md`。本文件将本项目需要的依据独立保存在项目内，提交时不依赖上级目录。

## 生产候选版依据

- [PostgreSQL 17 SELECT](https://www.postgresql.org/docs/17/sql-select.html)：`SKIP LOCKED` 用于多个消费者领取队列记录；不能当作通用一致性快照。
- [PostgreSQL 同步与异步 standby](https://www.postgresql.org/docs/17/warm-standby.html)：HA 数据丢失边界必须结合复制和提交配置，不仅取决于应用先写库。
- [Psycopg 连接池](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)：池生命周期、连接检查及等待上限。
- [AWS Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)：未来引入 MQ 时，避免业务数据库与消息发布的裸双写；消费者仍需要去重。
- [RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms) 与 [quorum queues](https://www.rabbitmq.com/docs/quorum-queues)：发布确认与消费确认是不同责任，重投可能重复；本项目没有部署 RabbitMQ。
- [Nginx upstream](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)：共享 zone、resolver 与 `resolve` 支持运行时更新后端地址。当前配置中的 `127.0.0.11` 仅用于 Docker 网络；迁移 Kubernetes 或其他平台必须替换为对应可信 DNS/服务发现。

以上资料用于解释方案边界，不构成本项目的性能或 HA 实测。运行中读取的官方资料与当前可变网页可能存在版本差异。

## 历史演示依据

- [SQLite WAL 文档，第 11 节](https://www.sqlite.org/wal.html#the_wal_reset_bug)：官方记录 2026-03-03 修复 WAL-reset 缺陷；可能影响 3.7.0-3.51.2，3.51.3 及特定回移版本修复。问题只在 WAL 及特定并发条件下触发，不推断每个旧版本用户都会遇到。为兼容本机 3.43.2，当前选择 rollback journal。
- [SQLite 同步级别](https://www.sqlite.org/pragma.html#pragma_synchronous)：解释 EXTRA 与回滚日志删除时目录同步的关系；不把它当作抵抗磁盘毁损的承诺。
- [SQLite 原子提交](https://www.sqlite.org/atomiccommit.html) 与 [锁模型](https://www.sqlite.org/lockingv3.html)：持久化和短写事务设计的依据。回滚日志并不是我们自行实现的存储协议。
- [Python http.server](https://docs.python.org/3/library/http.server.html)：标准库服务仅适用于当前 loopback 演示，不作为生产入口选择。

许可证仅针对参考项目做事实标记。本项目没有复制这些项目的业务源码，也未声称它们的许可自动适用于本项目。
