# rc_adolph

面向供应商 HTTP(S) API 的异步调用服务。

业务系统提交请求后即可获取回执，由 Worker 在后台完成投递。支持任务查询、失败重试和人工核对，适用于广告归因、CRM 更新、库存通知等场景，无需供应商改造接口。

![rc_adolph 演示结果](docs/assets/demo-results.png)

## 快速开始

环境要求：Python 3.12、Docker CLI，以及运行中的 Docker daemon。

```sh
git clone https://github.com/ado1ph/rc_adolph.git
cd rc_adolph

python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
make demo-production PYTHON=.venv/bin/python
```

演示自动启动临时 PostgreSQL、API、Worker 和 Mock 供应商，运行正常投递、临时故障恢复和超时核对三个场景，结束后清理本次资源，不使用已有数据库。首次运行需要下载依赖和镜像。

无 Docker 时，可运行 `make demo` 体验 [SQLite 演示](docs/sqlite-demo.md)。

## 系统架构

API 接收请求，Worker 执行投递，PostgreSQL 保存任务与执行记录，同时承担任务队列。API 与 Worker 分进程运行，不依赖 Redis 或 MQ。

[![rc_adolph 系统架构：业务系统、API、PostgreSQL 任务队列、Worker 与供应商接口](docs/assets/architecture.png)](docs/assets/architecture.svg)

## 文档

- [架构与设计](docs/architecture.md)
- [配置、接口与部署](docs/production-runbook.md)
- [测试与验证](docs/verification.md)
- [AI 协作记录](https://chatgpt.com/s/cx_6adeaee54fb48191b68ab28a5f32c1fe)
