# SQLite 零依赖演示

这是早期单机原型，只用于无需 Docker 的快速体验。主实现和状态机见[项目 README](../README.md)，不要把下述配置、权限和存储边界套用于 PostgreSQL。

## 自动运行

Python 3.9+，只使用标准库：

```sh
make demo
```

程序自动使用临时 SQLite 文件及随机 loopback 端口，启动 API、Worker 与本机供应商，演示完成后关闭。它不是后台服务，不依赖供应商账号。

此路径覆盖正常投递、临时与永久失败、预算耗尽、响应超时，以及明确安全时的重试或显式重放。SQLite 没有 PostgreSQL 独立的 `reconciliation_required` / `paused` 状态，不应用它演示生产核对闭环。

## 手动调用

在两个终端分别执行：

```sh
make supplier
```

```sh
make serve
```

供应商默认 `http://127.0.0.1:9100`，API 默认 `http://127.0.0.1:9000`。Makefile 设置的 `local-demo-secret` 只是本地演示 token。端口被占用时，可用 `python3 -m notifier supplier --port ...` 和 `python3 -m notifier serve --port ...` 调整，并同步 origin 白名单与示例请求。

```sh
curl -sS http://127.0.0.1:9000/v1/notifications \
  -H 'Authorization: Bearer local-demo-secret' \
  -H 'Idempotency-Key: submit-order-1001' \
  -H 'Content-Type: application/json' \
  --data-binary @examples/order.json

curl -sS http://127.0.0.1:9000/v1/notifications/NOTIFICATION_ID \
  -H 'Authorization: Bearer local-demo-secret'

curl -sS http://127.0.0.1:9100/admin/state
```

`NOTIFICATION_ID` 使用提交返回的 ID。表单格式示例见 `examples/contact.json`。相同入口 key 修改请求内容会返回 409，不能用它更新旧任务。

## Mock 供应商

| 接口 | 固定的供应商协议 |
| --- | --- |
| `POST /vendor/orders` | `X-API-Key`；JSON `order_id`、正整数 `amount_cents`；支持供应商 `Idempotency-Key` |
| `POST /vendor/legacy-orders` | 同样的 JSON 和认证，故意不支持幂等 |
| `POST /vendor/contact` | `X-Vendor-Token`；表单 `contact_id`、`state=paid` |

订单 query `scenario` 可取 `ok`、`flaky`、`reject`、`always_fail`、`rate_limit`、`redirect`、`timeout_once`。`flaky` 前两次返回 503；`timeout_once` 首次先提交副作用再等待 0.5 秒，模拟“供应商成功但本地超时”。管理接口和去重记录仅在内存中，不是生产供应商保证。

## 原型限制

- 标准库 HTTP server、单 SQLite 文件、无多租户授权、跨主机 HA、保留配额或主动告警。
- 数据库保存原始请求和供应商凭据，不具备 PostgreSQL 主实现的加密快照与凭据引用。
- 进程崩溃恢复不等于断电、磁盘损坏或业务副作用 exactly-once。
- 使用 `journal_mode=DELETE`、`synchronous=EXTRA`，不将其作为可横向扩展的队列平台。

正式评审优先运行 `make demo-production PYTHON=.venv/bin/python`；本原型是备用体验入口，不是另一个需要一起部署的服务。
