# rc_adolph

面向供应商已有 HTTP(S) API 的可靠异步调用服务。业务系统提交 URL、Header 和原始 Body，服务持久化后返回回执，由独立 Worker 投递。适用于广告归因、CRM 更新、库存通知等场景，不要求供应商改造接口。

## 快速演示

环境要求：Python 3.12、Docker CLI，以及运行中的 Docker daemon。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
make demo-production PYTHON=.venv/bin/python
```

命令自动启动临时 PostgreSQL、API、Worker 和 Mock 供应商，结束后清理本次资源，不使用已有数据库。首次运行需要下载依赖和镜像。

| 场景 | 预期结果 |
| --- | --- |
| 正常投递 | 接收后异步成功；相同入口 key 复用同一回执 |
| 供应商临时故障 | Mock 返回两次 503 后恢复，三次调用产生一次副作用 |
| 发送后响应超时 | 无安全重试契约时进入待核对，不盲目重发；核对 Mock 已送达后关闭 |

![PostgreSQL、API、Worker 与 Mock 的真实演示结果](docs/assets/demo-results.png)

上图由实际运行的[演示报告](docs/assets/demo-report.json)生成。无 Docker 时可运行 `make demo` 体验 [SQLite 原型](docs/sqlite-demo.md)；本文以 PostgreSQL 实现为主。

## 架构与核心流程

![rc_adolph 逻辑架构](docs/assets/architecture.png)

1. **接收**：鉴权、目标校验、入口幂等与容量检查；任务和加密请求快照在同一事务中保存，提交成功才返回 202。
2. **执行**：Worker 通过 `SKIP LOCKED` 领取任务，记录 attempt 和租约；HTTP 调用在数据库事务外执行。
3. **恢复**：按结果及供应商契约重试、终止或转人工核对；租约 token 阻止过期 Worker 写回。

PostgreSQL 任务记录本身就是队列，暂不引入 Redis/MQ。API 与 Worker 分进程运行，共享同一任务账本。

### 请求契约

调用方携带 Bearer token 和入口 `Idempotency-Key`，提交到 `POST /v1/notifications`：

```json
{
  "url": "https://orders.vendor.example/v1/orders",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json",
    "Idempotency-Key": "supplier-order-1001"
  },
  "body": "{\"order_id\":\"1001\",\"amount_cents\":100}",
  "credential_ref": "orders-v1"
}
```

`credential_ref` 引用服务端配置的凭据。首次接收返回 202；相同入口 key 和请求复用原回执并返回 200；同 key 不同请求返回 409。入口 key 与供应商 Header 中的 key 分别负责本服务去重和供应商幂等。

任务查询、重放与核对接口见[运行手册](docs/production-runbook.md)。

## 系统边界

- 支持 POST/PUT/PATCH、原始 UTF-8 Body（最大 128 KiB）；提供租户隔离、凭据引用、出站目标限制和容量保护。
- 可靠性从请求被持久化接收开始；业务事务到提交之间的事件丢失，需要调用方通过 outbox 解决。
- 不提供严格顺序、订阅扇出或二进制流。通用契约将 2xx 视为成功；200 内嵌业务失败、异步回执及动态签名需增加供应商 Adapter。

## 可靠性与失败处理

采用有限重试和显式核对，**不承诺外部副作用 exactly-once**。供应商重试模式分为：已验证幂等窗口、业务容忍重复、结果不明转人工。

| 结果 | 决策 |
| --- | --- |
| 2xx | 标记成功 |
| 可确认尚未发送的网络故障 | 在租约、截止时间和次数预算内重试 |
| 408/425/429/5xx、部分发送、响应丢失 | 幂等窗口覆盖下次完整发送预算，或业务允许重复时才自动重试；否则待核对 |
| 永久拒绝、安全校验失败、预算耗尽 | 无历史未决结果时失败；不能抹除更早 attempt 的不确定性 |
| `Retry-After` | 尊重供应商冷却时间；超长等待进入暂停或核对，不提前重试 |
| Worker 崩溃 | 回收过期租约；本地 fencing 不能撤销供应商已发生的副作用 |

## 设计取舍

| 选择 | 理由与代价 |
| --- | --- |
| 数据库即任务队列 | 接收、幂等与调度状态可原子提交，减少维护组件；代价是自维护轮询、租约、索引及清理，并承担数据库竞争 |
| 模块化单体，API/Worker 分进程 | 支持分别运行和扩容，避免过早拆分微服务 |
| 有限重试与人工核对 | 避免永久错误无限积压，也避免不明结果盲目重发；代价是人工处置成本 |

对 AI 方案的取舍：未采纳缺少需求依据的订阅平台、Kafka 和多地域投递，也不接受 exactly-once 承诺；保留可执行的投递主链路与故障恢复。具体过程见 [AI 协作记录](docs/ai-collaboration.md)。

后续先通过队龄、锁等待和数据库 IO 确认瓶颈。若数据库调度无法满足实测需求，再以事务 outbox 接入 MQ；真实供应商契约、压测和 HA/恢复演练是上线前需要补齐的验证。

## 测试

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-test.lock
make test PYTHON=.venv/bin/python

# 完整数据库回归：仅使用专用测试库
NOTIFY_TEST_DATABASE_URL='postgresql://<isolated-test-only-dsn>' \
  make test PYTHON=.venv/bin/python
```

未提供 `NOTIFY_TEST_DATABASE_URL` 时，PostgreSQL 用例会跳过。测试包含状态机、迁移、租约竞争、HTTP/TLS 故障及真实 API/Worker 与 Mock 的端到端场景。

更多细节：[架构设计](docs/architecture.md) · [运行手册](docs/production-runbook.md) · [行业方案对比](docs/industry-review-2026-10-24.md) · [验证记录](docs/verification.md)。
