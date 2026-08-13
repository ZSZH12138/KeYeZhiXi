# Outbox Worker 说明

## 当前实现状态

截至 `2026-07-25`，`src/course_insight/modules/m0_platform/outbox_worker.py` 已经提供独立 Worker 实现，且共享代码中已出现公开命令入口：

```shell
python manage.py run_outbox_worker --once
python manage.py run_outbox_worker
```

它不是线程里的“顺手刷队列”，而是面向独立进程的 leased outbox worker：
- claim 一批记录
- 在事务外投递
- 用 compare-and-set ack / fail
- 通过心跳续租
- 在 `runtime/outbox_worker/<worker_id>.status.json` 公开无敏感状态

## 交付语义

当前语义是 at-least-once，不是 exactly-once。

原因来自实现方式：
- sink 先执行 `append_if_absent(event_id, serialized_record)`
- 然后 repository 再执行 `mark_outbox_delivered(...)`
- 如果 sink 写成功但 ack 因数据库错误失败，该记录之后可能再次被领取

因此下游审计文件必须按 `event_id` 幂等。

## 与兼容路径的关系

`M0PlatformService` 仍保留 `M0EventStore` 兼容外观；但组合根 `build_application()` 已能在仓储支持以下方法时自动创建真正的 `OutboxWorker`：
- `claim_outbox_batch`
- `mark_outbox_delivered`
- `mark_outbox_failed`
- `renew_outbox_leases`

SQLite 与 PostgreSQL M0 Repository 都实现了该协议。`initialize()` 与
`append_learning_events()` 只做目录/schema 与数据库事务，不触发文件投递；
Web 请求、`AppConfig.ready()` 和应用初始化都不会“顺手刷队列”。正常投递必须由
独立 Worker 进程驱动。

保留的 `deliver_outbox_records(deliver)` 兼容入口也在事务外执行 callback，并使用
同一 heartbeat 机制续租。若 callback 期间失租，旧 worker 不执行 ack/fail；成功
结果不被旧 owner 确认，异常仍以原异常抛出，记录由 at-least-once 语义恢复。

## 状态文件

Worker 快照字段包括：
- `worker_id`
- `state`
- `last_heartbeat_at`
- `last_success_at`
- `last_error_code`
- `claimed_count`
- `delivered_count`
- `dead_count`

状态值包括：
- `starting`
- `running`
- `idle`
- `stopping`
- `stopped`

这些状态文件用于运维可见性，不是审计日志。

## 重试、失败与死信语义

失败后的行为由 `OutboxSettings` 控制：
- `batch_size`
- `poll_interval_seconds`
- `lease_seconds`
- `max_retries`
- `retry_base_seconds`
- `retry_max_seconds`
- `retry_jitter_ratio`
- `heartbeat_interval_seconds`

失败后：
- `ValueError` / `UnicodeError` 映射为 `OUTBOX_RECORD_INVALID`
- `OSError` 映射为 `OUTBOX_SINK_UNAVAILABLE`
- 其他异常映射为 `OUTBOX_SINK_FAILED`

超过最大重试次数后，记录进入 `dead` 状态，而不是无穷重试。

Worker 停机时先停止领取新记录。若当前 sink append 仍在执行，进程会等待它完成，
同时继续续租该记录，避免协作式停止留下半行或让另一 Worker 提前领取。被强制杀死
或数据库确认失败时，lease 过期后仍按 at-least-once 语义重放。

## app.log 与审计文件的区别

Worker 可能向两个不同目标产生信息：
- `app.log`：记录 `outbox.record_delivered`、`outbox.record_failed`、`outbox.worker_error` 等运行事件
- `runtime/.../audit/learning_events.jsonl`：记录真正的领域审计事件

因此：
- `app.log` 可以轮转、脱敏、输出到 stdout
- 审计 JSONL 必须保持 durable append 和幂等语义

不要把 Worker 的 status 文件、`app.log` 和审计 JSONL 混成同一个“日志系统”概念。

开发环境的 `rotating_file` 模式只允许一个进程独占 writer lock；不要让 Web 与
Worker 同时写同一个 `app.log`。生产环境配置校验强制 `logging.mode=stdout`，
Web 与 Worker 分别写 stdout，再由容器/服务管理平台集中收集和轮转。审计 JSONL
仍由 Worker 的幂等 sink 单独维护。

## 当前文档不能声称的内容

截至 `2026-07-25`，本仓库可以明确写：
- Worker 类已实现
- 组合根可自动构造 Worker 实例
- 公开 `run_outbox_worker` 命令已进入共享代码
- SQLite / PostgreSQL 仓储都实现 leased outbox 协议

但不能写：
- “本次任务已验证生产环境长期运行”
- “Exactly-once 交付”
## 优雅停机与一次执行

- `request_stop()`、`SIGINT` 或 `SIGTERM` 不会放弃正在执行的 sink append；
  Worker 不再 claim 新记录，但等待当前 append 并续租。
- `python manage.py run_outbox_worker --once` 只做一个有界领取/投递周期。
  snapshot 的 `last_error_code` 非空时管理命令返回非零，不打印伪成功。
- 常驻模式正常退出应看到 `outbox worker stopped`；未确认记录留在数据库，lease
  到期后可由下次进程恢复。
