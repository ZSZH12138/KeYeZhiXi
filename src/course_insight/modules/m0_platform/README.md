# M0 平台基础与 Django 外层

## 负责人

陈

## 职责

M0 是系统外层和横切基础：负责配置、事件、快照、健康状态，并规定
Django 页面、会话、鉴权和表单提交的边界。Django 属于 M0，不新增业务模块；
视图只负责将请求转成既有 Pydantic 契约并调用 `AppCoordinator`。当前已经有
真实 Django settings、URL、View、Template、models、两层权限、表单和 migration；
根 `manage.py` 提供 Web 与管理命令入口。持久化可以由配置选择 SQLite 或
PostgreSQL；真实 PostgreSQL 是否通过联调，必须以提供受保护测试库后的当次测试
结果为准。

## 输入来源

- 可分发配置与运行时环境变量，不包含密钥值或主机绝对路径。
- Django 会话产生的伪匿名 `ActorContext`。
- 学生表单转换的 `AssessmentSubmission` 与教师表单转换的
  `TeacherReviewSubmission`；前者以题目实例 ID 映射标量答案，后者与 M9 统一使用
  `confirm/override/reject` 并携带完整复核数据。
- M8 产生的 `LearningEvent`。

## 输出

- `EventAck`：事件幂等持久化确认。
- `AsyncJobStatus`：legacy intelligence architecture scaffold 中的 Django
  作业占位状态；为保持既有公共契约固定为 `skipped`。
- 原子契约快照和不含密钥/主机路径的健康状态。

公开新入口 `prepare_django_frontend(actor_context, requested_at) -> AsyncJobStatus`
仍服务于 legacy `run_intelligence_architecture()` 空脚手架，固定返回
`skipped`，不会启动 Web 或 Worker。这个兼容状态不代表 Django 未实现；真实 Web
由根 `manage.py`/WSGI/ASGI 独立启动，就绪性由 `/health/ready/` 检查。

## 当前可运行能力

- `initialize()` 创建运行/配置目录并应用所选后端的核心 migration；它不执行
  JSONL 文件 I/O，也不顺手投递 outbox。
- `append_learning_events()` 在同一事务写入 `m0_learning_events` 与
  `m0_event_outbox`，以 `event_id` 幂等；新 outbox 通过 leased Worker 投递到
  `runtime_dir/audit/learning_events.jsonl`，语义是 at-least-once。
- `save_contract_snapshot()` / `load_contract_snapshot()` 在 `runtime_dir` 内原子保存、
  恢复契约，并拒绝敏感字段及 Windows/POSIX 主机绝对路径。
- `health_check()` 只返回配置、数据库和运行目录的状态，不返回路径、环境变量或密钥。
- `prepare_django_frontend()` 为保持 legacy 公共脚手架契约固定返回
  `status="skipped"`；真实 Django URL/View/Template、权限与健康检查走独立入口。
- M0 只持久化拆分测评流程的关联 ID、checkpoint、lease 和幂等状态；M4/M5/M7/
  M8/M9 各自保存所属领域对象，View 与 M0 都不跨模块读业务表。流程行同时冻结
  知识包/证据索引/policy checksum 与精确 M5 前态引用；长模块调用由 CAS 心跳
  续租，失租调用方不能继续推进、失败或完成流程。M5/M9 对冻结 policy 单次读取，
  checksum 校验和解析使用同一份字节。
- v8→v9 的旧流程行只在 replay identity、TaskPlan 知识包/课程包锚点和全空新增
  字段同时满足时执行一次 CAS 接管；部分填充、缺少精确后状态引用或不可能的
  checkpoint 继续 fail closed。
- 登录失败使用经过 HMAC 的 actor+IP、actor 与 IP 三层桶；成功登录只清理
  actor 相关桶，保留共享 IP 风险历史。

本阶段已经有 `.env` / `app.json` 配置加载、`roles.csv` 校验与同步、
`app.log` 结构化日志、独立 outbox Worker、M0 Web Django migration 与
PostgreSQL 适配器入口。M5 的 DINA/BKT、M8 的 2PL IRT/能力估计/自适应选择
和 M9 本地质量门槛已经实现；知识抽取、题目标注和学生 RAG 可通过教师配置的
DeepSeek 启用，M7 主观评分与 M9 教师解读仍受各自隐私/质量门限制。生产长期运行
与真实 PostgreSQL 仍需在目标环境验收，平台持久化本身不会绕过模型数据门槛。

运维文档入口：

- [deployment.md](../../../../docs/deployment.md)
- [postgresql_migration.md](../../../../docs/postgresql_migration.md)
- [outbox_worker.md](../../../../docs/outbox_worker.md)

专项验证：

```shell
python -m pytest tests/unit/test_m0_platform.py -q
```

## 禁止事项

不得解释领域分数、跨模块读表、在 view 中实现领域逻辑、将 Django ORM/
请求类型泄漏给 M1—M9、泄漏路径/密钥，或把运行数据库写入项目数据。

## 2026-08-27 知识文件与学习运行面

M0 现在提供教师多文件上传、题目 TXT 编辑、批量待删除、确认任务、进度查询和学生课程答疑页面。`run_ingestion_worker` 独立处理知识入库；旧知识包审核页面已经移除。教师密钥按课程号和班级号共享，知识抽取、题目标注和该班学生答疑只使用精确作用域密钥。

入库任务持久化阶段进度、逐文件操作结果、Worker ID 和有限租约；处理阶段及每批模型调用续租。进程崩溃后，租约过期的 `running` 任务可被重新认领并幂等重跑。

知识抽取输出无效时，同一批额外重试 5 次，仍失败才拆小文本；普通模式 30 秒只约束一次 DeepSeek HTTP 请求，不是批次、重试序列或整项任务的总时限。进度只按已完成文件、文本子批和题目推进，等待与重试不虚增百分比。

Readiness 将认证所需核心能力与后台能力分开：核心失败返回 503；outbox 或入库 Worker 缺失返回 HTTP 200 + `degraded`，不会阻断登录。请求体默认允许 `5 GiB + 16 MiB`，单个知识文件仍采用 50 MiB 业务上限、内容签名与 OOXML ZIP 校验、不可猜测存储键和课程级权限校验。

学生起始页只选择测评类型，不再接收自由文本。诊断测评和阶段评测提交后更新画像并进入本人测评历史；随心练习、订正和独立答疑不影响画像。结果页逐题展示原题、选择题选项、答案、知识点、来源文件与原文，并提供签名 POST 答疑入口。
