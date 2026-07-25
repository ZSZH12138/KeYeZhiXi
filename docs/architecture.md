# 架构说明

## 架构定位

课业智析保持 M0—M9 十个责任域，不新增 M10。核心是进程内的 Python
模块化单体：模块之间传递 Pydantic 契约对象，不用内部 HTTP，
`AppCoordinator` 只编排公开服务，不直接读任何模块的业务表。

Django 是 M0 的系统外层，负责页面、会话、鉴权和表单入口；它不是
独立业务模块。目标持久化是 PostgreSQL，M2 的目标向量检索后端是
pgvector。LLM 只允许通过 DeepSeek API 边界进入 M7 与 M9。

当前交付已包含 M0 的生产平台面：统一配置与组合根、结构化日志、leased
outbox Worker、真实 Django Web/权限/表单、SQLite 与 PostgreSQL 仓储适配器，
以及 SQLite→PostgreSQL 导入器。真实 Django 由 `manage.py`/WSGI/ASGI 独立启动，
由 `/health/live/` 和 `/health/ready/` 观察。

`prepare_django_frontend()` 只属于 legacy
`run_intelligence_architecture()` 空脚手架。为保持既有公共
`ArchitectureScaffoldResult` 语义，它仍固定返回 `skipped`；这只是“脚手架不启动
Django 作业”，不是“Django 尚未实现”。

“空实现”现在只描述尚未启用的智能算法和外部调用：M2 pgvector 仍为逻辑空边界，
M5 不执行 DINA/BKT 参数估计，M8 不执行 IRT 标定或自适应选择，M7/M9 不调用
DeepSeek。真实 PostgreSQL 适配器已经实现，但若验收环境没有提供受保护的临时
PostgreSQL，相关 live tests 会明确跳过，不能据此声称已完成真实联调。

## M0—M9 能力归属

```text
M0 Django 外层/鉴权/提交契约
           │
           ▼
M1 课程版本与分块 ──► M2 RAG（词法 + pgvector 目标）──┐
           │                                        │
           └──► M3 知识包/Q 矩阵/题目标定依据         ▼
                         │                       M7 DeepSeek
                         ▼                       评分/反馈
                 M4 任务与版本编排                      │
                         │                             ▼
                         ▼                       M8 测评/评分/IRT/
                 M5 DINA 认知诊断 + BKT             自适应选题/在线标定
                         │                             │
                         ▼                             ▼
                 M6 诊断驱动的辅导状态机      M9 DeepSeek 叙述/
                                                       模型质量/教师审核
```

上图表示能力归属和高层协作关系，不是完整的数据契约图或工作流顺序。
M6 直接产出的 `EvidenceQuery` 和 `FeedbackGenerationTask` 分别进入 M2、M7；
M9 直接消费 M3/M5/M8 的契约，与 M6 没有直接数据契约边。

| 模块 | 架构责任 | v2 新边界 | 当前行为 |
|---|---|---|---|
| M0 | 配置、日志、事件/outbox、快照、Django 外层 | `ActorContext`、提交契约、`AsyncJobStatus` | 真实 Web/Worker；legacy scaffold 作业保持 `skipped` |
| M1 | 授权课程、来源版本、确定性分块 | 不增新智能引擎 | 保持现有实现 |
| M2 | 证据索引、RAG 检索与审计 | `EmbeddingModelRef`、`RetrievalPolicy`、`RetrievalAudit` | pgvector 引用为 `empty` |
| M3 | 知识包、题库、量规、蓝图、Q 矩阵 | 为 DINA/IRT 提供版本化标定依据 | 保持教师确认 JSON 输入 |
| M4 | 任务识别、蓝图选择与工作流编排 | 冻结课程包、知识包、蓝图和路由引用 | 确定性路由与持久化幂等 |
| M5 | 学习观测、认知诊断、知识追踪、状态 | DINA 系契约与 BKT 系契约 | 空运行，不估计参数 |
| M6 | S0—S5 教学控制 | 消费 M4 任务、M8 评分、M5 状态和可选前版会话 | 确定性策略、证据门槛与 SQLite 幂等会话 |
| M7 | 主观评分与学生反馈 | DeepSeek 唯一 LLM 适配器 | 返回空生成结果，零网络调用 |
| M8 | 测评、评分、IRT、自适应选题与在线标定 | IRT 参数、能力估计、标定与选题契约 | 空参数集与空选题 |
| M9 | 教师分析、质量门槛与复核 | DeepSeek 教师叙述、`ModelQualityReport` | 空生成，质量为 `insufficient_data` |

## 分层映射

| 路径 | 唯一职责 |
|---|---|
| `src/course_insight/contracts/` | 84 个公开 Pydantic 契约及来源图逻辑 |
| `src/course_insight/application/` | 应用组合根、运行上下文恢复、拆分 Web 用例与既有一站式编排 |
| `src/course_insight/modules/m0_*`—`m9_*` | 十个责任域的服务、仓储边界和可替换实现 |
| `src/course_insight/infrastructure/` | 配置、SQLite/PostgreSQL、JSON/日志、导入器与 DeepSeek 空适配器 |
| `contracts/` | 84 份 Schema、1 份中性空示例与 `contract_provenance.json` |
| `data/raw_course/` | 本地授权原始资料占位；真实资料不进入可分发产物 |
| `runtime/` | 数据库、索引、快照和日志；不进入可分发产物 |

## 运维入口

截至 `2026-07-25`，当前代码中的运维入口包括：

- 根 `manage.py`：统一 Django 命令入口；
- `python manage.py sync_roles --check|--dry-run|--apply`：同步 `roles.csv`；
- `python manage.py run_outbox_worker [--once]`：运行 M0 leased outbox Worker；
- `python scripts/migrate_sqlite_to_postgres.py ...`：显式 SQLite→PostgreSQL 导入；
- `python -m pytest -q`：统一测试入口。

真实 PostgreSQL 集成测试必须同时提供
`COURSE_INSIGHT_TEST_DATABASE_URL` 与 `COURSE_INSIGHT_TEST_DATABASE_NAME`；
后者必须与 DSN 中的库名完全一致，且库名必须带分隔的 `test`、`ci` 或 `tmp`
一次性标记。缺少任一变量时 live tests 明确 `skip`，危险库名则 fail closed。

## M0 多请求应用边界

`ApplicationContainer` 由 `build_application()` 一次组装配置、M0—M9 Service、
所选持久化后端、`AppCoordinator`、`CourseRuntimeRegistry` 与 Worker。Django、
CLI 和 Worker 复用同一组合方式。M1—M3 本里程碑仍从已校验的 runtime snapshots
恢复；SQLite/PostgreSQL 后端切换覆盖本任务实际持久化的 M0、M4—M9。

为支持 HTTP 多请求流程，`AppCoordinator` 在保留既有一站式用例的同时增加
`start_assessment`、`submit_assessment`、`get_student_assessment`、
`get_teacher_review_context` 和 `review_assessment`。M0 只保存关联 ID、
checkpoint、lease 和幂等状态；TaskPlan、状态、反馈、评分与分析仍由 M4/M5/M7/
M8/M9 自有仓储恢复。View 不读领域表，Session 也不保存完整领域契约。

每个操作还冻结知识包、课程包、证据索引与 policy 内容 checksum；进入 M5 前先在
`state_inputs_frozen` checkpoint 保存精确 learner/class 前态身份。恢复只能按该
身份读取，不允许重新读取“最新状态”。潜在长调用在事务外执行，由 M0 CAS
heartbeat 延长 lease；失租 worker 的返回值会被丢弃，且不能再写 checkpoint 或
终态。M5/M9 的冻结 policy 入口只读取文件一次，checksum 比较和严格解析消费同一
份字节，关闭 check-then-use 竞态。

从 v8 升级的 workflow 行采用窄化的惰性接管：只有旧 replay identity 完全一致、
全部新增依赖/恢复字段均为空、且持久化 TaskPlan 的知识包 ID 与课程包 ID 匹配当前
受治理输入时，SQLite/PostgreSQL 才以 CAS 一次性补齐依赖。`state_saved` 之后还
必须已有精确 `state_version`；部分填充行和不可能来自 v8 的
`state_inputs_frozen` 行继续 fail closed。

运行上下文由 `runtime/snapshots/course_runtime_manifest.json` 指向
`CoursePackage`、`EvidenceIndexRef`、`KnowledgeBundle` 和两份 policy。所有引用
必须是 runtime 内相对路径；加载时会重验契约、checksum、重建的词法索引身份以及
`StatePolicy`/`TeacherThresholdPolicy` 内容，任一不一致即 fail closed。

## 日志与投递

应用运行日志与领域审计分离：开发单进程可独占滚动 `app.log`；生产只写 stdout，
交给部署平台采集，Web/Worker 不得多进程共享普通 `RotatingFileHandler`。
`LearningEvent` 与 outbox 在数据库同一事务提交，独立 Worker 在事务外写
`learning_events.jsonl` 后再确认。整体是 at-least-once，并由 sink 以 `event_id`
幂等去重，不是 exactly-once。

## 边界不变量

- 契约对象跨模块原样传递，不降级为临时字典。
- 时间必须带时区，校验和使用规范化 JSON 的 SHA-256。
- 外部身份只通过 M0 的伪匿名 `ActorContext` 进入核心。
- DeepSeek 的密钥只能在运行时由 `DEEPSEEK_API_KEY` 提供，契约、审计和日志不存密钥或完整提示词。
- DINA/BKT/IRT 和在线标定的每次运行都必须绑定数据水位、模型/参数版本与质量报告。
- 新标定参数先以 shadow 版本产生，经 M9 质量门槛与教师审核后才能被 M8 启用。
- 当前智能空实现不读取 DeepSeek 密钥、不访问模型网络、不连接 pgvector，也不
  伪造 DINA/BKT/IRT 或模型质量指标；这不否定 M0 PostgreSQL 平台适配器的存在。
- 量规、试卷、审计和结果总分必须守恒；教师复核追加新版本，不覆盖旧版本。
- 对外不传播主机路径；索引、作业和产物使用逻辑引用或相对路径。

## 扩展顺序

1. 保持 84 个公共契约、既有 Service 签名和模块责任稳定。
2. 在目标环境实测 PostgreSQL、Web/Worker 多进程部署与备份恢复。
3. 在 M2 实现 pgvector 建库、检索与审计，不把向量能力移入 M0。
4. 去标识化作答数据达到质量门槛后，才在 M5 启用 DINA/BKT、在 M8 启用
   IRT shadow 标定和自适应选择。
5. M7/M9 的安全、审计和量规约束完成后，才把 DeepSeek 空适配器替换为真实
   API 适配器。
