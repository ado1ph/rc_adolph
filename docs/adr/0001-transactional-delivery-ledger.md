---
status: accepted
---

# 第一阶段采用 PostgreSQL 投递账本与调度

在需要事务性接收、租户幂等、延迟重试、查询历史及人工重放，而尚无已验证高吞吐/多下游订阅需求的前提下，第一阶段将任务账本与调度状态置于专用 PostgreSQL 的同一事务中，使用短事务领取与租约 fencing。决策不是“组件越少越可靠”，而是先减少必须证明的跨存储一致性协议，接受自维护调度、轮询与数据库写放大的成本；代表性压测和真实 HA 验收不通过则不能发布。

## Considered Options

PostgreSQL outbox + RabbitMQ quorum queues 是正式备选，不是反模式；队列可独立扩展并提供成熟确认机制，但仍需 outbox relay、重复发布处置、任务账本和重试状态。组织已有 MQ 平台、需要独立队列故障域或实测 PG 调度不能满足目标时应重开决策。Redis 任务库和托管队列同样需要单独核实持久性及事务衔接。

## Consequences

不承诺严格 FIFO、跨供应商全局限流或供应商副作用 exactly-once。不包装“可以随时换 MQ”的空接口；迁移必须新增 Job + Outbox 原子写、确认 relay、消费 fencing 和单一调度权切换。架构及发布门槛见 `docs/architecture.md`，单机 Compose 不构成数据库 HA。
