# 接口与生产者—消费者指南

## 使用规则

本项目共有 84 个公开契约，其中 22 个是可独立导出的根契约。
路径 `$` 表示完整契约，`$[]` 表示列表元素，点路径表示组成字段。
服务之间必须传递契约对象，不得用无类型字典代替。

当前 v2 智能算法边界仍返回空结果：DeepSeek 不发起 API 请求，pgvector
不建立连接，DINA/BKT/IRT 不运行估计。这些边界仍是 MVP 架构的正式组成部分，
不是新模块。M0 的 Web、配置、日志、Worker 与数据库适配器不是算法占位，
已经有真实实现。

## 2026-07-25 运维入口

在保留上述契约边界不变的前提下，当前代码提供以下运维入口：

- 根 `manage.py`：Django 命令总入口；
- `python manage.py sync_roles --check|--dry-run|--apply`：roles 初始化/同步；
- `python manage.py run_outbox_worker [--once]`：M0 outbox Worker；
- `python scripts/migrate_sqlite_to_postgres.py --project-root ...`：显式 SQLite→PostgreSQL 导入。

真实 PostgreSQL 步骤仍需当次环境验证；没有受保护的 live test 数据库时会明确
跳过，不能把跳过写成已通过。

## 22 个根契约

| 根契约 | 唯一生产者 | 消费者/字段路径 |
|---|---|---|
| `CoursePackage` | `M1.import_course` | `M2.build_index(course_package=$)`；`M3.build_knowledge_bundle(course_package=$)` |
| `EvidenceIndexRef` | `M2.build_index`、`M2.initialize_vector_store` | `M2.retrieve(evidence_index_ref=$)`；v2 空检索审计 |
| `EvidenceQuery` | `M8.prepare_scoring`、`M6.decide_next_action` | `M2.retrieve(evidence_query=$)` |
| `EvidenceBundle` | `M2.retrieve` | `M7.score_subjective_answer`；`M7.generate_student_feedback` |
| `KnowledgeBundle` | `M3.build_knowledge_bundle` | M4、M5、M8、M9 |
| `TaskPlan` | `M4.create_task_plan` | `M8.generate_paper`；`M6.decide_next_action` |
| `AssessmentPaper` | `M8.generate_paper` | M0 学生作答外层；`M8.prepare_scoring` |
| `ScoringPreparationResult` | `M8.prepare_scoring` | `M2.retrieve($.evidence_queries[])`；`M7.score_subjective_answer($.rubric_scoring_tasks[])`；`M8.finalize_scoring($)` |
| `RubricScoringTask` | `M8.prepare_scoring` | `M7.score_subjective_answer` |
| `RubricScoringResult` | `M7.score_subjective_answer` | `M8.finalize_scoring` |
| `ScoringResultBundle` | `M8.finalize_scoring`、`M8.apply_teacher_review` | M0、M5、M6、M9 |
| `DiagnosisResult` | `M5.update_state` | M6、M8、M9 |
| `LearnerStateSnapshot` | `M5.update_state` | M4、M6、M8、M9 |
| `ClassStateSnapshot` | `M5.update_state` | M9 |
| `StateUpdateResult` | `M5.update_state` | M6、M9 |
| `TutoringControlResult` | `M6.decide_next_action` | `M2.retrieve($.evidence_query)`；`M7.generate_student_feedback($.feedback_generation_task)`；应用层后续调度 |
| `FeedbackGenerationTask` | `M6.decide_next_action` | `M7.generate_student_feedback` |
| `StudentFeedbackPackage` | `M7.generate_student_feedback` | M0 Django 学生外层 |
| `TeacherAnalyticsBundle` | `M9.build_teacher_analytics` | M0 Django 教师外层 |
| `TeacherReviewDecision` | `M9.record_teacher_review` | `M8.apply_teacher_review` |
| `EventAck` | `M0.append_learning_events` | `AppCoordinator`/调用方 |
| `ArchitectureScaffoldResult` | `AppCoordinator.run_intelligence_architecture` | 后续各适配器实现者与部署准备流程 |

## v2 组件契约流

| 边界 | 生产者 | 消费者 | 当前空结果 |
|---|---|---|---|
| `ActorContext` | M0 Django 鉴权边界 | `AppCoordinator`、所有受授权用例 | 可构造伪匿名上下文 |
| `AssessmentSubmission` | M0 Django 学生表单 | `M8.prepare_scoring` | `answers` 是题目实例 ID 到字符串/布尔/整数/有限浮点答案的映射 |
| `TeacherReviewSubmission` | M0 Django 教师表单 | `M9.record_teacher_review` | 使用 `confirm/override/reject`，并完整携带总分、分项覆盖和教师意见 |
| `AsyncJobStatus` | M0 作业边界 | legacy intelligence scaffold | Django 作业固定 `skipped`；不代表真实 Web 未实现 |
| `EmbeddingModelRef` | M2 检索配置 | M2 索引器 | `empty` |
| `RetrievalPolicy` | M2 检索配置 | M2 检索器 | 允许词法/向量/混合策略 |
| `RetrievalAudit` | M2 | M7/M9 审计与运维 | `empty`，无证据 ID |
| `LLMModelRef` | M7/M9 配置 | DeepSeek 适配器 | provider 固定 `deepseek`，密钥名固定 `DEEPSEEK_API_KEY` |
| `LLMGenerationRequest` | M7 评分/反馈或 M9 叙述 | DeepSeek 适配器 | 只保存输入校验和证据 ID |
| `LLMGenerationResult` | M7/M9 DeepSeek 适配器 | M7/M9 业务服务 | `empty`，内容/引用为空，`not_run` |
| `ModelInvocationAudit` | DeepSeek 适配器 | M9 审计 | `not_run`，token/延迟为 0 |
| `SafetyCheckResult` | M7/M9 安全边界 | DeepSeek 适配器 | `not_run` |
| `LearningObservation`/`LearningObservationBatch` | M8 评分审计转换 | M5 DINA/BKT、M8 IRT | 空 batch 仅携带 learner 和 watermark |
| `CognitiveDiagnosisResult` | M5 DINA 引擎 | M5 状态、M6、M9 | `empty`，无掌握概率 |
| `KnowledgeTraceSnapshot` | M5 BKT 引擎 | M5 状态、M6、M9 | `empty`，无追踪概率 |
| `LearningModelRun` | `M5.run_learning_models` | `ArchitectureScaffoldResult`、M9 质量 | `empty` |
| `IRTItemParameters`/`IRTParameterSet` | M8 标定引擎 | M8 能力估计/选题，M9 审核 | 空参数集 |
| `AbilityEstimate` | M8 IRT 估计 | M8 自适应选题 | `empty`，无 theta/标准误 |
| `CalibrationRunResult` | `M8.calibrate_irt` | M9 质量与审核 | `empty`，不收敛、无指标 |
| `AdaptiveSelectionPolicy` | M8 配置 | M8 选题器 | 空策略不绑定参数集 |
| `AdaptiveSelectionResult` | `M8.select_adaptive_items` | M8 出卷编排 | `empty`，无题目 |
| `ModelQualityReport` | `M9.build_model_quality_report` | M9 教师审核与 M8 发布门槛 | `insufficient_data`，无指标 |
| `CalibrationReviewDecision` | M9 教师审核 | M8 参数版本发布 | 仅契约，当前不自动发布 |

## 编排入口

`M4TaskOrchestrationService.create_task_plan(...) -> TaskPlan` 是唯一任务规划入口。
它支持 `qa`、`diagnostic`、`practice`、`correction` 和
`stage_assessment`，并直接把带类型的 `TaskPlan` 交给 M8/M6：问答工作流固定为
`M2 → M7 → M6`，测评工作流固定为
`M8 → M2 → M7 → M5 → M6 → M9`。多个教师批准蓝图并存时，M4 构造时必须
通过 `blueprint_by_task_type` 显式配置任务类型到 bundle 内蓝图 ID 的映射；
不存在合法确定性选择时返回 `BLUEPRINT_NOT_FOUND`。

M4 幂等身份只由课程、班级、学习者、会话、任务类型、知识包、课程包和蓝图
八项冻结引用组成。SQLite 唯一约束保证重复、并发和进程重启后的调用复用首次
`TaskPlan`。`course_package_id` 从 M3 bundle 经 M4 到 M6 的 M2
`EvidenceQuery` 全程原样透传。

M6 在调用方未提供 `SessionStateSnapshot` 时，按 `TaskPlan.session_id` 从自身
Repository 恢复最新权威游标；全新会话从 S1/turn 0 开始。相同请求指纹返回已保存
结果，不重复推进 turn；调用方快照与 Repository 历史冲突时返回
`TUTORING_REFERENCE_MISMATCH`。

`AppCoordinator.run_intelligence_architecture(...) -> ArchitectureScaffoldResult`
保留为 legacy 智能能力脚手架入口。为保持既有
`ArchitectureScaffoldResult.is_empty()` 公共语义，M0 的
`prepare_django_frontend()` 继续返回 `skipped`；M2 pgvector、M7/M9 DeepSeek、
M5 DINA/BKT、M8 IRT/自适应选择和 M9 模型质量也保持空或证据不足状态。
真实 Django Web/health 由独立进程入口提供，不依赖该脚手架，也不把完整领域契约
存入 Session。

## M0 多请求 Web 用例

在不改变既有 `run_assessment_cycle()` 与 `run_teacher_review_cycle()` 签名的
前提下，用户已批准增加以下应用层入口：

| 用例 | 作用 | 权威数据恢复 |
|---|---|---|
| `start_assessment(...)` | 创建 M4 TaskPlan、M8 AssessmentPaper 和 M0 流程索引 | M0 仅保存关联 ID |
| `submit_assessment(...)` | 恢复试卷后执行既有评分、状态、辅导、反馈、分析链 | M4/M5/M7/M8/M9 自有 Repository |
| `get_student_assessment(...)` | 校验 actor/course/class/learner 后读取结果与反馈 | M8 评分、M7 反馈 |
| `get_teacher_review_context(...)` | 校验教师课程/班级作用域并读取复核上下文 | M8/M5/M9 |
| `review_assessment(...)` | 复用既有复核链并追加新审计版本 | M8/M5/M9 |

操作使用稳定幂等键、短 lease、CAS 版本与 checkpoint 恢复。相同操作重放返回
同一权威结果；Repository 采用“同 ID 同 payload 成功、同 ID 不同 payload
冲突”。跨模块仍只传现有 Pydantic 契约，Django Request、ORM、Session、
Psycopg 对象和无类型字典不会进入 M1—M9。

新操作行会冻结完整的知识包/证据索引身份与内容 checksum，以及被消费的两份
policy 字节 checksum。M5 更新前另以 `state_inputs_frozen` checkpoint 固定精确
learner/class 前态；恢复时按完整作用域与版本/快照 ID 读取，绝不以当时的 latest
状态替代。长模块调用通过 M0 lease heartbeat 续租，旧 owner 失租后不能推进或写
终态。M5/M9 的附加冻结入口对 policy 单次读取，并以同一份字节完成 checksum
比较和解析；既有公开方法签名保持不变。

pre-v9 行只允许一次受限兼容接管：旧不可变 identity 必须一致，新增字段必须全部
为 NULL，当前知识包/课程包必须与已持久化 TaskPlan 锚点一致。部分填充、冲突或
缺少必要精确状态引用的行返回稳定冲突并保持原值。

完整机器可检验映射位于
[`contracts/contract_provenance.json`](../contracts/contract_provenance.json)，
加载与一致性检查位于
[`contracts/provenance.py`](../src/course_insight/contracts/provenance.py)。

## 接口运维补充

- 配置优先级与 secret 边界见 [deployment.md](deployment.md)；
- PostgreSQL 迁移 CLI、`partial_envelope_rows` 与源数据限制见
  [postgresql_migration.md](postgresql_migration.md)；
- outbox Worker 的 at-least-once 语义、`app.log` 与审计 JSONL 区别见
  [outbox_worker.md](outbox_worker.md)。

## roles 与 Worker 命令语义

- `python manage.py sync_roles --apply` 把 `roles.csv` 视为该来源管理授权的完整
  期望状态，不是仅追加 patch。
- 之前由该来源管理、现在从 CSV 省略的 grant 会在同一事务中写成
  `is_active=false`、设置 `revoked_at`；用户不再拥有该角色的有效 grant 时，
  对应 Django Group membership 也会移除。
- `--dry-run` 和 `--apply` 都在普通 `revoke=<n>` 摘要中报告省略导致的撤销。
- `python manage.py run_outbox_worker --once` 若最终 snapshot 的
  `last_error_code` 非空，会以命令失败退出，便于自动化发现数据库或 sink 故障。
