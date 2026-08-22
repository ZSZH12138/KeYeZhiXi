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

“空实现”现在只描述尚未满足出站条件的外部调用：没有密钥或没有钉住的本地隐私
工件时，M7/M9 不调用 DeepSeek。教师可在页面填写密钥；真实学生评分仍必须通过
`build_required_m7_privacy_reviewer`。M2 的生产
PostgreSQL+pgvector、embedding、策略检索和审计适配器已经实现；SQLite 仅用于
离线/测试/迁移演练。仓库已提供 `tests/integration/test_postgres_m1_m2_m3_live.py`，
覆盖真实 HTTP embedding、M1—M3 PostgreSQL/pgvector 链路和恢复；若验收环境没有提供
受保护的临时 PostgreSQL，相关 live tests 会明确跳过，必须以 CI `live-m1-m3` job
的实际通过结果为准，不能把 skip 写成真实联调通过。

M5 已运行真实 DINA/BKT，M8 已运行真实 2PL IRT、能力估计和自适应选择，M9
已运行本地模型质量检查；这些能力仍受数据门槛、shadow 质量审核和教师批准约束。

M2 正式业务检索入口是 `retrieve_with_policy`；旧 `retrieve` 仅为已有 lexical 调用
保留的兼容入口。M3 生产发布必须经过教师复核 CAS，并使用
`build_knowledge_bundle_after_approval`；无审批的 `build_knowledge_bundle` 不能作为
生产发布入口。

应用层的 `AppCoordinator` 与 `assessment_workflow` 通过
`retrieve_for_application -> retrieve_with_policy` 进入 M2，旧 `retrieve` 不再作为
业务编排入口。完整 vector/hybrid 生产链仍以 CI `live-m1-m3` job 的真实
PostgreSQL+pgvector live 结果为验收条件。

M6 已实现私有、版本化的策略运行时和离线评估代码，但默认仍为
`rules`、零 rollout、零探索。`shadow` 只记录模型建议，`active` 还必须通过
安全候选和多重门禁；本阶段没有执行真实教学训练、线上 rollout 或生产启用，
因此不能把“代码可配置”表述成“学习策略已经上线或优于 baseline”。

## M0—M9 能力归属

```text
M0 Django 外层/鉴权/提交契约
           │
           ▼
M1 课程版本与分块 ──► M2 RAG（词法 + PostgreSQL/pgvector）──┐
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
M6 直接产出的 `EvidenceQuery` 和 `FeedbackGenerationTask` 分别进入 M2、M7。
M6 policy manifest/execution/observation/reward/evaluation 都留在 M6 私有边界；
M9 直接消费 M3/M5/M8 的公共契约，与 M6 没有直接数据契约边。尤其是当前 M9
公共 `build_model_quality_report(...)` 只接收 M8 `CalibrationRunResult`，M6
OPE/approval 尚未正式接入 M9。

| 模块 | 架构责任 | v2 新边界 | 当前行为 |
|---|---|---|---|
| M0 | 配置、日志、事件/outbox、快照、Django 外层 | `ActorContext`、提交契约、`AsyncJobStatus` | 真实 Web/Worker；legacy scaffold 作业保持 `skipped` |
| M1 | 授权课程、来源版本、确定性分块 | 不增新智能引擎 | 保持现有实现 |
| M2 | 证据索引、RAG 检索与审计 | `EmbeddingModelRef`、`RetrievalPolicy`、`RetrievalAudit` | SQLite lexical 基线；生产 PostgreSQL+pgvector 的 lexical/vector/hybrid 与审计 |
| M3 | 知识包、题库、量规、蓝图、Q 矩阵 | 为 DINA/IRT 提供版本化标定依据与教师复核 CAS | 生产发布必须走 `build_knowledge_bundle_after_approval` |
| M4 | 任务识别、蓝图选择与工作流编排 | 冻结课程包、知识包、蓝图和路由引用 | 确定性路由与持久化幂等 |
| M5 | 学习观测、认知诊断、知识追踪、状态 | DINA 系契约与 BKT 系契约 | 真实模型、完整历史与版本化状态 |
| M6 | S0—S5 教学控制 | 消费 M4 任务、M8 评分、M5 状态和可选前版会话；私有 policy learning 不扩张公共契约 | 确定性 baseline；默认 rules；shadow 不改变公共动作；active 门禁失败回退 rules |
| M7 | 主观评分与学生反馈 | DeepSeek 唯一 LLM 适配器 | 默认占位适配器；密钥+钉住隐私工件才允许出站 |
| M8 | 测评、评分、IRT、自适应选题与在线标定 | IRT 参数、能力估计、标定与选题契约 | 真实 2PL/EAP、审核发布和受约束选题 |
| M9 | 教师分析、质量门槛与复核 | DeepSeek 教师叙述、`ModelQualityReport` | 有密钥时可装配教师解读；IRT 质量报告真实计算 |

## 分层映射

| 路径 | 唯一职责 |
|---|---|
| `src/course_insight/contracts/` | 91 个公开 Pydantic 契约及来源图逻辑 |
| `src/course_insight/application/` | 应用组合根、运行上下文恢复、拆分 Web 用例与既有一站式编排 |
| `src/course_insight/modules/m0_*`—`m9_*` | 十个责任域的服务、仓储边界和可替换实现 |
| `src/course_insight/infrastructure/` | 配置、SQLite/PostgreSQL、JSON/日志、导入器与 DeepSeek 客户端 |
| `contracts/` | 91 份 Schema、1 份中性空示例与 `contract_provenance.json` |
| `data/raw_course/` | 本地授权原始资料占位；真实资料不进入可分发产物 |
| `runtime/` | 数据库、索引、快照、日志和 M6 JSON-only policy artifact；不进入可分发产物 |

## 运维入口

截至 `2026-07-27`，当前代码中的运维入口包括：

- 根 `manage.py`：统一 Django 命令入口；
- `python manage.py sync_roles --check|--dry-run|--apply`：同步 `roles.csv`；
- `python manage.py run_outbox_worker [--once]`：运行 M0 leased outbox Worker；
- `python scripts/migrate_sqlite_to_postgres.py ...`：显式 SQLite→PostgreSQL 导入；
- `python -m pytest -q`：统一测试入口。

M6 没有训练、manifest 注册或 promotion 的公共 CLI/管理页。现有配置、artifact
校验、promotion/rollback/kill-switch 和 OPE 运维边界见
[m6_policy_operations.md](m6_policy_operations.md)。

真实 PostgreSQL 集成测试必须同时提供
`COURSE_INSIGHT_TEST_DATABASE_URL` 与 `COURSE_INSIGHT_TEST_DATABASE_NAME`；
后者必须与 DSN 中的库名完全一致，且库名必须带分隔的 `test`、`ci` 或 `tmp`
一次性标记。缺少任一变量时 live tests 明确 `skip`，危险库名则 fail closed。

## M0 多请求应用边界

`ApplicationContainer` 由 `build_application()` 一次组装配置、M0—M9 Service、
所选持久化后端、`AppCoordinator`、`CourseRuntimeRegistry` 与 Worker。Django、
CLI 和 Worker 复用同一组合方式。SQLite 模式下 M1—M3 从已校验的 runtime
snapshots/artifacts 离线恢复；生产 PostgreSQL 模式下由 0016/0017 migrations 和共享的
`PostgresM1M2M3Repository` 恢复 M1—M3 制品、审计和教师复核记录，并由 M2 显式校验
ready pgvector 引用。SQLite/PostgreSQL 后端切换覆盖本任务实际持久化的全部模块，
但 SQLite 不作为生产后端。

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

submit 在 `state_saved` 与 `tutoring_saved` 之间增加 `policy_frozen`，只保存
M6 私有执行身份的七个字段：`policy_id`、`adapter_id`、`adapter_version`、
`artifact_sha256`、`feature_schema_version`、`action_space_version` 和
`gate_policy_version`。M6 以既有 request fingerprint first-write binding；
崩溃恢复重新 prepare 并逐字段精确比较，不把公共领域 payload 放入 M0 metadata。
M6 自己的私有 execution JSON 另存当次探索率和 active gate 结论，使尚未提交决定
的 learned binding 能在当前 mode/policy 改变后重新解析原 immutable artifact；
该恢复快照不扩展 M0 七字段或公共契约，全局 kill switch 仍可强制 baseline。

从 v8 升级的 workflow 行采用窄化的惰性接管：只有旧 replay identity 完全一致、
全部新增依赖/恢复字段均为空、且持久化 TaskPlan 的知识包 ID 与课程包 ID 匹配当前
受治理输入时，SQLite/PostgreSQL 才以 CAS 一次性补齐依赖。`state_saved` 之后还
必须已有精确 `state_version`；部分填充行和不可能来自 v8 的
`state_inputs_frozen` 行继续 fail closed。

运行上下文由 `runtime/snapshots/course_runtime_manifest.json` 指向
`CoursePackage`、`EvidenceIndexRef`、`KnowledgeBundle` 和两份 policy。SQLite/离线
模式下所有引用必须是 runtime 内相对路径，加载时重验契约、checksum、重建的词法索引
身份以及 policy 内容；生产 PostgreSQL 模式下 M1—M3 权威对象由共享仓储恢复，
manifest/snapshot 只作为显式配置的引用和身份 cross-check。任一不一致即 fail closed。

M6 learned policy 使用另一条私有制品链：Repository 按 `policy_id` 选择 immutable
`PolicyArtifactManifest`，其 `artifact_reference` 再相对于
`m6_policy.runtime_directory` 解析。后者必须位于 `runtime_dir` 之下；只允许
canonical UTF-8 JSON、lowercase SHA-256、有限 23 维 LinUCB 参数，以及完整的
`m6-features-v1`/`m6-action-space-v1` 版本匹配。它不是 course runtime manifest
的一部分。

PostgreSQL core schema 当前为 v21；SQLite bundled platform schema 当前为 v19，SQLite 的
M1—M3 S1-S6 仓储另有独立的 schema version 1。M4 intent 使用已发布的 v10/0010
与 v11/0011；M6 五张私有 policy 表位于 v12/`0012_m6_policy_learning.sql`，M0
七字段 freeze 安全追加在 v13/`0013_m0_policy_freeze.sql`；M5/M8 模型运行历史
追加在 v14/`0014_m5_m8_model_runtime.sql`，学习观测审计身份补强追加在
v15/`0015_m5_learning_observation_audit_identity.sql`；PostgreSQL M1—M3 S1-S6 能力
位于 v16/`0016_m1_m2_m3_capabilities.sql`，向量索引元数据绑定由
v17/`0017_vector_index_metadata.sql` 补充；M9/M7 模型调用审计为 v18/v19，
M0 等待室为 v20/`0020_m0_waiting_rooms.sql`，模型 rescore 操作为
v21/`0021_m0_rescore_operation.sql`。SQLite 对应等待室为 v18、rescore 为 v19。
既有 migration 未被改写或重编号。

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
- DeepSeek 的密钥只能在运行时由 `DEEPSEEK_API_KEY` 或教师写入的 runtime 密钥文件提供，环境变量优先；契约、审计和日志不存密钥或完整提示词。
- 默认主观评分仍全部进入教师复核（`all_review`）。选择性免审没有本校金标前不得作为生产默认。
- 待复核或待重评分数不得写入 M5/M6/学生反馈或权威 M9；reject 后可走绑定原作答 checksum 的 `local_model_rescore`，结果先回到 pending。
- M7 真实评分在密钥之外还必须装配钉住 SHA 的本地隐私复核器；缺工件时保持占位适配器，不得静默 DenyAll 后假装已启用。
- DINA/BKT/IRT 和在线标定的每次运行都必须绑定数据水位、模型/参数版本与质量报告。
- 新标定参数先以 shadow 版本产生，经 M9 质量门槛与教师审核后才能被 M8 启用。
- M6 `m6-features-v1` 与 `m6-action-space-v1` 只允许在确定性
  `SafetyEnvelope` 候选内排序；rules/shadow/fallback 的公共 logging policy 为
  one-hot，学习探索上限为 0.05 且补救场景禁用。
- M6 active 必须同时满足 approved manifest、精确版本/SHA、作用域、至少两个
  候选、支持度、不确定性、离线评估、rollout 和 kill switch 门禁；任一缺失回退
  rules。
- M7/M9 默认不访问模型网络；只有密钥存在且（对 M7 学生评分）隐私门通过时才构造真实客户端。M5/M8/M9 的
  本地模型功能不伪造 DINA/BKT/IRT 或质量指标；M2 生产检索按配置连接 PostgreSQL+pgvector，
  缺少依赖时 fail closed。
- 量规、试卷、审计和结果总分必须守恒；教师复核追加新版本，不覆盖旧版本。
- 对外不传播主机路径；索引、作业和产物使用逻辑引用或相对路径。

## 扩展顺序

1. 保持 91 个公共契约、既有 Service 签名和模块责任稳定。
2. 在目标环境实测 PostgreSQL、Web/Worker 多进程部署与备份恢复。
3. 在目标环境完成 PostgreSQL+pgvector live 建库、检索、审计和恢复验收，不把向量
   能力移入 M0。
4. 在目标环境提供达到治理门槛的去标识化作答数据，验证 M5 DINA/BKT 和 M8
   IRT shadow 标定；只有 M9 质量 ready 且教师批准后才启用自适应选择。在此之前
   不得把自适应选题、M6 active 或选择性审核写成已生产上线。
5. M7/M9 的安全、审计和量规约束完成后，才把 DeepSeek 空适配器替换为真实
   API 适配器。
