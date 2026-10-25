# 当前工作区代码与架构审查

日期：2026-10-24，Asia/Shanghai。范围为当前 `notifier/production`、共享 Worker/HTTP/校验模块、迁移、部署配置及相关测试，不是某个已提交版本的 diff。只审查并记录，不修改应用逻辑或数据库。

## 结论

发现 1 项 P1、5 项 P2 代码问题，以及 1 项 P2 架构风险。P1 是此前已指出但尚未闭环的发送控制问题，不是本次新增回归。另记录尚需业务确认或环境验收的架构约束，不将它们全部算成代码缺陷。

保留模块化单体、API/Worker 分进程和 PostgreSQL 权威任务账本是合理候选；本次发现并不构成必须引入 Redis/MQ 的依据。当前不能按生产验收通过处理。

P1 表示发布前必须解决或证明场景不适用；P2 表示需要整改，发布阻断程度取决于真实契约、数据及负载。分级为本项目工程判断。

## 代码发现

### R1 · P1 · 租约丢失和发送截止时间没有约束实际出站

位置：`notifier/worker.py:26`、`:36`；`notifier/production/store.py:298`；`notifier/production/runtime.py:53`。

heartbeat 返回 false 后只记录日志，没有通知正在准备发送的执行路径；传给 transport 的 request 也没有安全截止时间。领取时预留预算不能覆盖领取后的进程暂停、配置读取及其他延迟。旧 Worker 即使无法写回数据库，仍能发起供应商调用。

隔离探针：阻塞 materialize，等待 heartbeat 明确报告租约丢失后再释放，观察到 HTTP send 仍被调用；随后 stale finish 被拒绝。探针使用假的 store 和 HTTP transport，没有连接供应商，也没有复现重复扣款。

建议：定义包含租约和安全截止时间的执行上下文，在准备完成及紧贴出站的位置校验剩余预算；租约失效应阻止尚未发出的请求，约束 DNS/连接/发送预算。暂停恢复、租约丢失、慢配置/DNS 应有回归。不能宣称本地检查能撤销已发送请求或消除任意时刻暂停带来的竞态，远端幂等契约仍是必要条件。

### R2 · P2 · 已丢失快照的旧任务可被核对接口重新入队

位置：`notifier/production/store.py:481`；迁移 `notifier/production/migrations/0002_reconciliation_and_fairness.sql:14`。

v1 的 ambiguous 失败任务可能已按原保留规则清理 snapshot。v2 迁移将它转为待核对，但 `reconcile(not_delivered)` 在 Supplier Key 未过期或不存在时直接设置 pending，没有像普通 replay 那样检查 snapshot。之后 claim 在解密 None 时失败，租约恢复又将 manual 模式任务送回待核对，不能完成真实重发。

隔离方法探针向真实 reconcile 方法提供 `snapshot=None` 的旧任务，实际返回 pending。当前迁移测试本身也构造了无 snapshot 的旧任务，但只测试迁移状态，没有继续测试核对和领取。未在本轮 PostgreSQL 中复现完整路径。

建议：确认送达仍可关闭；确认未送达但缺少快照时，明确终结旧任务并要求新提交，或返回明确的不可恢复结果，不能承诺已恢复投递。补旧数据清理→升级→核对的 PG 集成回归。

### R3 · P2 · Retry-After 的尾部空白或极长整数会变成零等待

位置：`notifier/delivery.py:52`。

没有先处理字段值首尾空格/制表符；`"7200 \t"` 进入日期分支并最终返回 0。极长的纯数字又可能触发 Python 整数转换长度限制，同样返回 0。后者不能安全解释为供应商没有要求等待。

隔离探针通过标准库 HTTPResponse 解析实际响应字节，读取 `Retry-After: 7200 \t` 后得到等待 0，strip 后为 7200；5000 位纯数字也得到 0。没有实际等待两小时或调用供应商。

建议：显式处理 OWS，使用有界的十进制比较/解析；超出支持范围时保守停止自动重试或转处置，不回落成零等待。补数字、日期、OWS、超长数值和畸形输入测试。

### R4 · P2 · 本地凭据故障会打开共享供应商熔断

位置：`notifier/production/runtime.py:58`；`notifier/production/store.py:336`。

凭据文件不可用、operation/账号摘要漂移等发送前错误统一返回 retryable。finish 把所有 retryable 都累计为供应商可用性失败，因此某个 profile 的本地配置错误可以打开 origin 级熔断，拖延同域其他健康 profile；这些错误没有证明供应商不可用。

隔离方法探针从 failures=4 开始，以 `credential_configuration_unavailable` 完成任务，观察到共享 failures=5、open_until=now+60。该探针执行真实 finish 方法并记录 SQL 参数，但不验证 PG 事务/锁。

建议：结果分类至少区分本地配置、目标策略、安全拒绝、供应商网络/服务故障和业务拒绝；重试决定与供应商熔断计数不能共用一个布尔值。配置故障应按 profile 停发/告警，不污染健康账号的供应商可用性统计。

### R5 · P2 · 回执查询依赖当前供应商授权配置

位置：`notifier/production/api.py:167`；`notifier/production/store.py:124`。

重复请求在查入口 key 前先经过当前 Security.prepare 和加密。已接受请求若在响应丢失后遇到 operation 撤销、profile 删除或配置不可用，就不能取回原 ID，而会在查旧任务前失败。调用者可能误将 400 理解为从未接收；当前详情查询需要 ID，也没有按入口 key 查询回执的独立入口。

隔离 API 探针首次提交得到 202；改掉 profile 的允许路径后，以完全相同入口 key 和请求重试，得到 400，且 store.submit 未再次被调用。使用假的 store，不宣称模拟了真实数据库持久化。

建议：分离请求规范化/指纹、原回执查询和新提交授权。已认证的同租户调用者可按 key+匹配指纹取回原回执；仅新任务检查当前投递授权。必须保留不同请求的 409、防并发重复及撤销后禁止新执行的语义，不能以修复回执查询为由绕过出站授权。

### R6 · P2 · 配置允许根路径，但匹配器永远拒绝它

位置：`notifier/production/config.py:152`、`:199`。

配置校验明确允许 `/`，但 operation matcher 将其分成空 segment 后拒绝。因此配置 POST `/` 的 profile 无法接受目标为根路径的请求。

隔离探针验证 `_valid_path_template("/")` 为 true，同一 operation 的 `_operation_allowed(..., "/", "")` 为 false。

建议：统一路径规范化，明确处理根路径；不应为修复它而放松重复斜线、点路径或编码分隔符限制。

## 架构发现

### A1 · P2 · 保留清理与全局准入锁耦合，批量操作实为逐行往返

位置：`notifier/production/store.py:565`、`:578`；锁等待配置 `:55`。

maintain 在整个清理事务中持有 admission 全局锁。仅清理 500 条快照就要执行 500 次 UPDATE 和 500 次 DELETE，另有元数据、审计和容量操作。所有接收、重放、核对都可能等待这把锁；默认 2 秒 lock_timeout 使长清理事务可能表现为全租户 503。

隔离方法探针记录了单批 500 条快照产生的 1000 条逐行 SQL，并验证调用 admission；没有测量真实 DB 延迟或复现线上 503。每条 SQL 的 statement_timeout 不是整个事务的总时间预算。

建议：采用集合式清理、限制每事务时间/工作量，评估缩短配额锁持有范围或使用事务计数维护；在保留数据规模、真实 DB RTT、同时提交/清理/续租下测试锁等待及 p95/p99。不是要求放弃 PG，而是降低其内部耦合。

### 其他必须明确的约束，不作为本轮新增代码缺陷计数

- **租户容量隔离不完整**：`store.py:105` 只限制每租户 pending/in_flight；未决核对和终态保留只有全局条数/字节上限。单租户持续产生未决或大量终态可以耗尽共享保留容量。是否接受，应有租户保留字节/条数预算及运营处理时限；不能把全局背压称为完整的租户隔离。
- **隔离维度未定**：当前 origin 共享限流、冷却及熔断，同 origin tenant 轮转不等于账号配额隔离；多个慢 origin 也可能一起占满所有执行线程。应按真实供应商契约定义受服务器控制的 scope，不能随意以 credential_ref 分桶绕开全局配额。
- **成功语义与外部去重仍是假设**：2xx 接收不等于业务完成；Supplier Key 的唯一性、起算点及留存由外部保证。真实 Adapter、查询对账、跨租户共享供应商账号的 key 命名空间需要接入验收。
- **模块职责**：PostgresStore 同时维护领取、核对、配额、审计、清理和查询，修改容易互相影响。不建议立刻拆微服务；先把结果分类、执行上下文及重试/核对状态决策放进明确的领域模块，保持 SQL 事务原子性和外部 Interface 稳定，避免只机械拆文件。
- **观测/发布证据**：主循环心跳不证明投递线程在前进。尚缺投递耗时/进展、租约丢失/回收、维护落后及锁等待指标；非 owner PG 权限、混合版本滚动升级、HA/PITR、真实告警和代表性压测仍需环境验收。本轮没有确认运行账号权限错误，不能把未经实测的怀疑列为事实。

## 验证记录

- 初次运行 `.venv/bin/python -m unittest discover -s tests -q`：85 项被发现，20 项因沙箱禁止本机 socket bind 报错、26 项 PG 测试跳过；不是业务回归证据。
- 申请沙箱外运行后，同一命令结果：`Ran 85 tests in 4.491s; OK (skipped=26)`，即 59 项通过、26 项跳过。没有设置 `NOTIFY_TEST_DATABASE_URL`。
- Docker 检查：当前用户 Docker socket 不存在，没有可用 daemon，未操作或迁移原数据库，也未声称本轮 PG 集成通过。
- 7 个隔离审查探针全部确认上述行为；其中 Retry-After 探针同时检查两种输入，维护探针验证 SQL 次数，不是吞吐测试。临时脚本在 `/private/tmp/notifier-review-probes-20261024.py`，断言的是现有缺陷行为，不应作为“正确行为回归”直接加入 CI。
- 生产范围 Ruff 命令通过：`.venv/bin/ruff check notifier/production tests/test_production.py tests/test_init_config.py scripts/init_config.py`。
- 扩大到 `.venv/bin/ruff check notifier tests scripts/init_config.py` 有 3 个 import 排序问题，位于旧 demo API、Mock 和共享 Worker；不作为高优先级可靠性问题报告，也未自动改动。

## 整改顺序

1. R1：执行截止时间、租约丢失与出站控制，并明确远端幂等和暂停竞态限制。
2. R2–R5：缺失快照核对、Retry-After、错误分类/熔断、原回执查询，补针对性回归与真实 PG 集成。
3. R6 / A1：根路径一致性、集合式维护及准入延迟验证。
4. 固定真实供应商结果/配额契约及租户容量预算，完成非 owner、滚动升级、备份恢复和告警验收。

本次只新增评审报告和临时探针，没有修复上述问题，也没有重建体验服务。历史 85 项全通过不能覆盖本轮新发现的边界。

## 2026-10-24 整改复核

以下是本轮实现状态，不回写或抹去上面的原始审查证据：

| 发现 | 整改 | 当前结论 |
| --- | --- | --- |
| R1 | Worker 执行控制带有租约失效信号和 claim 后剩余时间；SupplierTransport 在密钥物化前后核验；PinnedConnection 在解析、连接/TLS、每次 socket 写和等待响应头阶段遵从剩余预算 | 已实现并有隔离单元回归；不可排除任意指令点暂停竞态，远端副作用不是 exactly-once |
| R2 | v3 增加缺快照核对 outcome；未送达且快照不存在时标为 failed、保留证据/审计、提示新提交及新 Supplier Key；delivered 可正常关闭 | 实现完成；v1→v4 / PG 领取闭环待隔离 PG 验证 |
| R3 | 解析前剥离 SP/HTAB；超一年纯数字结果使用独立 `cooldown_hold` 与 `paused`，不使用 Infinity 或提前重试；审计恢复步骤 | 实现完成；v4 迁移及 PG hold 事务待验证。无限 hold 需要值班介入，成本已记入 runbook |
| R4 | `retryable` 与 `failure_class` 独立；本地 configuration/authorization/policy 不污染或重置 origin 熔断；供应商网络/服务故障单独累计，业务拒绝不计可用性故障 | 实现完成；PG 事务路径待验证 |
| R5 | 请求规范化及既有 HMAC 指纹兼容，先 lookup tenant+key；不同摘要 409；新任务仍经过现行 profile 授权；授权竞态时复查回执 | 实现完成；真实 DB 并发与撤销集成待验证 |
| R6 | 根路径 `/` 单独做精确匹配 | 实现及单测完成；全套回归待跑 |
| A1 | 维护使用有界 CTE 批量更新/删除并保留 admission 锁、事务配额、FK cascade | SQL 已实现且加入并发回归；本机无隔离 PostgreSQL，未验证语法、并发、锁或延迟，不声称性能收益 |

验证环境复查：当前 `NOTIFY_TEST_DATABASE_URL` 未设置，Docker 命令存在但 daemon socket 不可用，未连接或改动其他数据库。本轮 PG 集成测试不得按通过处理。完整复验结果记于 `docs/verification.md`。

## 2026-10-24 P2 修复复核

R1 出站时限与请求状态分类、R3 非有限时间隔离、R5 撤销后原回执路径已补实现和正确行为测试。低层传输仅将 DNS/连接/TLS 等可证明未发送的网络故障标为可重试；进入 HTTP 写阶段或响应丢失均按可能已发送处理。SQLite 演示没有供应商安全契约，故其发送后超时现明确不自动重放；生产重试由已核验 profile、稳定 Supplier Key 和数据库剩余窗口共同决定。R3 使用独立布尔 hold 与 `paused`，由 migration v4 修复旧非有限值。

本次扩展测试暴露并修复了两个演示路径回归：原演示低层直接重试结果不明请求；行为与生产安全约束冲突。其新测试现在要求不自动重放。**剩余验证阻断**：PostgreSQL v1→v4、API 持久化 JSON、hold 恢复、回执撤权、租约回收及并发写回尚无测试库结果；本机未配置隔离 PostgreSQL，禁止用真实/现用数据库替代。

## 2026-10-24 最新 Code Review 整改

| 问题 | 当前实现与证据 |
| --- | --- |
| 超长 Retry-After hold 绕过 manual 核对 | `cooldown_hold` 继续表示共享 origin 冷却，任务状态另行保持 `reconciliation_required`；runtime 不再用 hold 排除 manual 不确定结果。runbook 仅为有显式重试安全契约且窗口/次数/截止预算完整的任务恢复发送。v5 将历史 manual `paused` 保守转核对并审计。真实本机 HTTP Mock 验证先记副作用、再返回 503 + 超长 Retry-After；解除 hold 后副作用仍为一次。 |
| 完整连接/TLS 尝试超时被判为永久预算耗尽 | `NetworkTimeout` 只在任务整体预算或租约也耗尽时转为本地策略失败；若整体控制仍有效，则本次尝试结束并按“未发送”资格参与后续重试。Worker→PostgreSQL 测试通过实际消耗 socket timeout 的连接和 TLS 模拟，均落为 pending/retryable；发送后超时仍按结果不明处理。 |
| origin hold 阻止过期租约回收 | origin 候选查询在 hold 期间仍选中过期 lease/截止预算任务；锁顺序仍为 origin→job，回收后重新检查 hold，不启动出站。4 个并发 claim 竞争时只回收一次，attempt 关闭且 `in_flight=0`。 |
| replay 上限阻止终态核对 | replay_count 仅在 reconciliation 导致 `pending` 时限制；confirmed delivered、缺快照或 Supplier Key 失效的 confirmed not_delivered 均可事务性保存证据、审计并关闭；测试确认不产生新 attempt。 |
| PostgreSQL 测试失效 | 修复 dict_row 访问；`finish()` 锁内读取数据库权威 key window/deadline/attempt 数据，拒绝陈旧 claim 快照允许不安全重试；并发 maintenance 测试保留 SKIP LOCKED，以有界轮询等待并发提交完成，不再假设单次 claim 必有结果。 |

实现及验证状态：新增迁移 `0005_quarantine_ambiguous_cooldown_jobs.sql`，不改写已发布迁移。隔离 PostgreSQL 17 上 v1→v5、v4→v5、40 项 PostgreSQL 集成测试及全量 **114 项测试均通过，零跳过**；本机 API/Worker 子进程与 Mock 的 `ok`、503 重试、400、429、提交后超时幂等场景通过。Ruff 与 compileall 结果见 `docs/verification.md`。

剩余边界：外部请求已开始后无法由数据库租约撤销；进程在检查与写入间任意暂停仍可能发生竞态。Mock 只能证明测试路径，不证明真实供应商幂等窗口/去重行为；不承诺 exactly-once。迁移 v5 无降级，生产发布仍需排空旧 Worker、备份并验证回滚策略。

## 2026-10-24 v5 Review 后续 P2 整改

| 问题 | 修复结论 |
| --- | --- |
| 跨 attempt 结果不确定性丢失 | v6 新增 `delivery_jobs.outcome_uncertain`；可能发送/可重试供应商响应、租约失效时与 attempt 写回及审计处于同一事务。安全重试继续保留该标记；耗尽或后续本地失败转核对。只有成功响应或核对清除；维护不清理未决任务及其审计、快照、attempt。v6 仅迁移具有 abandoned/reconciliation、ambiguous 错误或可重试 HTTP 状态证据的历史任务，不统一升级全部 failed。 |
| 核对事实与重试命令混同 | API 可选 `retry_after_reconciliation`，返回 `retry_requested`/`retry_scheduled`；旧客户端省略时保留预算内的原重试行为。达到上限时仍以同一事务写入 not_delivered 事实、证据、审计并关闭；不增加 attempt，普通 replay 不可重新打开核对关闭任务。 |
| paused 容量与 Retry-After hold 告警遗漏 | 容量规则纳入 paused，并对复制的全局 gauges 先 max 去重；10 分钟 hold 告警进入默认 oncall route，描述引导先确认供应商冷却并按审计 runbook 操作。 |

验证以 `docs/verification.md` 最新 v5 follow-up 记录为准：隔离 PostgreSQL 17.11 上 v1→v6、v4→v6、完整 **118 项/零跳过**；Ruff、compileall 与 `promtool check rules` / `promtool test rules` 通过。仍未证明真实供应商幂等契约、值班系统实际送达、生产 HA/PITR 或 exactly-once。
