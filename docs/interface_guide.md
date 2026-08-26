# 接口与生产者—消费者指南

> 2026-08-27：知识文件入库、题目自动标注和学生答疑已经采用新的活动发布版本。旧 M3 审批接口仅为兼容保留，不再是生产者入口。新入库对象是 M0/M1/M3/M7 内部接口，因此公共根契约仍为 91 个。

## 使用规则

本项目共有 91 个公开契约，其中 22 个是可独立导出的根契约。
路径 `$` 表示完整契约，`$[]` 表示列表元素，点路径表示组成字段。
服务之间必须传递契约对象，不得用无类型字典代替。

旧 M7 主观评分和 M9 教师解读仍默认关闭，并分别受隐私与质量门限制。知识抽取、题目标注和独立学生答疑只使用精确课程号、班级号下由教师保存的 DeepSeek 密钥；未配置时不出站，学生答疑也不回退到全局密钥。M2 的 PostgreSQL+pgvector、embedding、策略检索和审计
端口已经实现；SQLite 仅用于离线、测试和迁移演练，生产必须使用 PostgreSQL+pgvector。
仓库已提供真实 PostgreSQL+pgvector live 用例；当前机器缺少生产依赖时必须 fail
closed，live 用例会明确 skip，不能把逻辑 `empty` 或 skip 当成生产成功，最终以 CI
`live-m1-m3` job 的实际通过结果为准。

M5 已运行真实 DINA/BKT，M8 已运行真实 2PL IRT、能力估计和自适应选题，M9
已运行本地模型质量检查；这些本地能力仍受数据、质量审核和教师批准门槛约束。

M2 正式业务检索入口是 `retrieve_with_policy`；旧 `retrieve` 仅为已有 lexical 调用
保留的兼容入口。M3 当前生产者是 `KnowledgeIngestionProcessor`：它接收活动文件版本，构建候选 `CourseKnowledgeRelease`，再通过 `knowledge_bundle_from_release` 向 M4–M9 提供兼容的 `KnowledgeBundle`。旧 `build_knowledge_bundle_after_approval` 和审核 wrappers 不再连接教师 Web。

M6 的私有 policy runtime、LinUCB、reward/OPE 和持久化已实现，但默认关闭在
`rules`/零 rollout/零探索状态。它们不增加公共契约；本阶段也没有真实教学训练、
线上 rollout 或 active 生产验证。M4 可选 adapter、M8 自适应选题和 M7 选择性
审核同样默认关闭，不得写成已上线。

## 2026-07-27 运维入口

在保留上述契约边界不变的前提下，当前代码提供以下运维入口：

- 根 `manage.py`：Django 命令总入口；
- `python manage.py sync_roles --check|--dry-run|--apply`：roles 初始化/同步；
- `python manage.py run_ingestion_worker [--once]`：知识文件解析、抽取、题目标注与活动发布 Worker；
- `python manage.py run_outbox_worker [--once]`：M0 outbox Worker；
- `python scripts/migrate_sqlite_to_postgres.py --project-root ...`：显式 SQLite→PostgreSQL 导入。

真实 PostgreSQL 步骤仍需当次环境验证；没有受保护的 live test 数据库时会明确
跳过，不能把跳过写成已通过。

## 22 个根契约

| 根契约 | 唯一生产者 | 消费者/字段路径 |
|---|---|---|
| `CoursePackage` | `M1.import_course` | `M2.build_index/build_vector_index(course_package=$)`；M3 旧兼容投影 |
| `EvidenceIndexRef` | `M2.build_index`、`M2.build_vector_index`、`M2.restore_vector_index` | `M2.retrieve_with_policy(evidence_index_ref=$)`；旧 `retrieve` 仅兼容 lexical |
| `EvidenceQuery` | `M8.prepare_scoring`、`M6.decide_next_action` | `M2.retrieve_with_policy(evidence_query=$)` |
| `EvidenceBundle` | `M2.retrieve_with_policy` | `M7.score_subjective_answer`；`M7.generate_student_feedback` |
| `KnowledgeBundle` | M3 `knowledge_bundle_from_release`（当前生产）；旧 service 仅兼容 | M4、M5、M8、M9 |
| `TaskPlan` | `M4.create_task_plan` | `M8.generate_paper`；`M6.decide_next_action` |
| `AssessmentPaper` | `M8.generate_paper` | M0 学生作答外层；`M8.prepare_scoring` |
| `ScoringPreparationResult` | `M8.prepare_scoring` | `M2.retrieve_with_policy($.evidence_queries[])`；`M7.score_subjective_answer($.rubric_scoring_tasks[])`；`M8.finalize_scoring($)` |
| `RubricScoringTask` | `M8.prepare_scoring` | `M7.score_subjective_answer` |
| `RubricScoringResult` | `M7.score_subjective_answer` | `M8.finalize_scoring` |
| `ScoringResultBundle` | `M8.finalize_scoring`、`M8.apply_teacher_review` | M0、M5、M6、M9 |
| `DiagnosisResult` | `M5.update_state` | M6、M8、M9 |
| `LearnerStateSnapshot` | `M5.update_state` | M4、M6、M8、M9 |
| `ClassStateSnapshot` | `M5.update_state` | M9 |
| `StateUpdateResult` | `M5.update_state` | M6、M9 |
| `TutoringControlResult` | `M6.decide_next_action` | `M2.retrieve_with_policy($.evidence_query)`；`M7.generate_student_feedback($.feedback_generation_task)`；应用层后续调度 |
| `FeedbackGenerationTask` | `M6.decide_next_action` | `M7.generate_student_feedback` |
| `StudentFeedbackPackage` | `M7.generate_student_feedback` | M0 Django 学生外层 |
| `TeacherAnalyticsBundle` | `M9.build_teacher_analytics` | M0 Django 教师外层 |
| `TeacherReviewDecision` | `M9.record_teacher_review` | `M8.apply_teacher_review` |
| `EventAck` | `M0.append_learning_events` | `AppCoordinator`/调用方 |
| `ArchitectureScaffoldResult` | `AppCoordinator.run_intelligence_architecture` | 后续各适配器实现者与部署准备流程 |

## v2 组件契约流

| 边界 | 生产者 | 消费者 | 当前行为 |
|---|---|---|---|
| `ActorContext` | M0 Django 鉴权边界 | `AppCoordinator`、所有受授权用例 | 可构造伪匿名上下文 |
| `AssessmentSubmission` | M0 Django 学生表单 | `M8.prepare_scoring` | `answers` 是题目实例 ID 到字符串/布尔/整数/有限浮点答案的映射 |
| `TeacherReviewSubmission` | M0 Django 教师表单 | `M9.record_teacher_review` | 使用 `confirm/override/reject`，并完整携带 `expected_audit_version`、`expected_audit_checksum`、总分、分项覆盖和教师意见；版本与 checksum 必须同时匹配当前评分审计 |
| `AsyncJobStatus` | M0 作业边界 | legacy intelligence scaffold | Django 作业固定 `skipped`；不代表真实 Web 未实现 |
| `EmbeddingModelRef` | M2 检索配置 | M2 索引器 | 生产为 `configured`；`empty` 仅用于离线/兼容场景 |
| `RetrievalPolicy` | M2 检索配置 | M2 检索器 | 允许词法/向量/混合策略 |
| `RetrievalAudit` | M2 | M7/M9 审计与运维 | 正式检索记录 `succeeded`/`failed`；兼容未执行场景才为 `empty` |
| `LLMModelRef` | M7/M9 配置 | DeepSeek 适配器 | provider 固定 `deepseek`，密钥名固定 `DEEPSEEK_API_KEY` |
| `LLMGenerationRequest` | M7 评分/反馈或 M9 叙述 | DeepSeek 适配器 | 只保存输入校验和证据 ID |
| `LLMGenerationResult` | M7/M9 DeepSeek 适配器 | M7/M9 业务服务 | 未装配时 `empty`/`not_run`；真实调用仍不把正文写入公共契约 |
| `ModelInvocationAudit` | DeepSeek 适配器 | M9 审计 | `not_run`，token/延迟为 0 |
| `SafetyCheckResult` | M7/M9 安全边界 | DeepSeek 适配器 | `not_run` |
| `LearningObservation`/`LearningObservationBatch` | M8 评分审计转换 | M5 DINA/BKT、M8 IRT | 最新有效审计生成权威观测；无有效审计时可为空 |
| `CognitiveDiagnosisResult` | M5 DINA 引擎 | M5 状态、M6、M9 | 足量数据返回 DINA 掌握后验；不足时明确失败 |
| `KnowledgeTraceSnapshot` | M5 BKT 引擎 | M5 状态、M6、M9 | 足量有序历史返回 BKT 掌握概率 |
| `LearningModelRun` | `M5.run_learning_models` | `ArchitectureScaffoldResult`、M9 质量 | 绑定完整历史、模型版本和观测水位 |
| `IRTItemParameters`/`IRTParameterSet` | M8 标定引擎 | M8 能力估计/选题，M9 审核 | 2PL shadow 经质量和教师审核后追加 approved 版本 |
| `AbilityEstimate` | M8 IRT 估计 | M8 自适应选题 | approved 参数上的有限 EAP theta/标准误 |
| `CalibrationRunResult` | `M8.calibrate_irt` | M9 质量与审核 | 足量数据返回真实收敛指标和 shadow 参数 |
| `AdaptiveSelectionPolicy` | M8 配置 | M8 选题器 | 绑定参数集、数量、概念配额、难度和曝光上限 |
| `AdaptiveSelectionResult` | `M8.select_adaptive_items` | M8 出卷编排 | 按 Fisher 信息量和约束选择并持久化 |
| `ModelQualityReport` | `M9.build_model_quality_report` | M9 教师审核与 M8 发布门槛 | `ready`、`failed` 或证据不足时 `insufficient_data` |
| `CalibrationReviewDecision` | M9 教师审核 | M8 参数版本发布 | approve/reject/defer 驱动追加式参数状态机 |

M0 的 `TeacherReviewSubmission` 只用于 M8/M9 成绩复核。M3 的旧 `TeacherReviewWorkflow` 已退出当前知识发布链，不能把它重新接到教师文件页。

## 新知识入库内部接口

这些对象不从 `course_insight.contracts` 根导出，不计入 91 个公共契约：

| 内部对象 | 生产者 | 消费者 | 约束 |
|---|---|---|---|
| `KnowledgeExtractionBatch` | M7 batch builder | M7 DeepSeek 抽取 | 每批最多 6,000 个可见字符；可含多个块 |
| `KnowledgeExtractionResult` | M7 抽取器 | 入库处理器 | 返回零到多个候选；100 条触发二分重跑 |
| `KnowledgeEvidenceRef` | M7 本地校验边界 | M3 合并/发布 | 来源版本、块、定位和跨度必须来自输入白名单 |
| `MergedKnowledgeConcept` | M3 概念合并 | 发布处理器、题目标注 | 同义概念合并时来源取并集 |
| `QuestionConceptLinkCandidate` | M7 题目标注 | M3 发布 | concept ID 必须属于当前活动候选集合 |

## 编排入口

`M4TaskOrchestrationService.create_task_plan(...) -> TaskPlan` 是唯一任务规划入口。
它支持 `qa`、`diagnostic`、`practice`、`correction` 和
`stage_assessment`，并直接把带类型的 `TaskPlan` 交给 M8/M6：问答工作流固定为
`App.retrieve_for_application → M2.retrieve_with_policy → M7 → M6`，测评工作流固定为
`M8 → App.retrieve_for_application → M2.retrieve_with_policy → M7 → M5 → M6 → M9`。
多个教师批准蓝图并存时，M4 构造时必须
通过 `blueprint_by_task_type` 显式配置任务类型到 bundle 内蓝图 ID 的映射；
不存在合法确定性选择时返回 `BLUEPRINT_NOT_FOUND`。

应用层现在通过 `retrieve_for_application(...)` 委托到
`M2.retrieve_with_policy(...)`，并由该入口统一策略校验和审计；旧 `retrieve` 仅为已有
lexical 调用保留兼容。完整 vector/hybrid 生产链仍需 CI `live-m1-m3` job 的真实
PostgreSQL+pgvector 结果验收，不能把模块级代码可用写成 live 通过。

M4 幂等身份只由课程、班级、学习者、会话、任务类型、知识包、课程包和蓝图
八项冻结引用组成。SQLite 唯一约束保证重复、并发和进程重启后的调用复用首次
`TaskPlan`。`course_package_id` 从 M3 bundle 经 M4 到 M6 的 M2
`EvidenceQuery` 全程原样透传。

M6 在调用方未提供 `SessionStateSnapshot` 时，按 `TaskPlan.session_id` 从自身
Repository 恢复最新权威游标；全新会话从 S1/turn 0 开始。相同请求指纹返回已保存
结果，不重复推进 turn；调用方快照与 Repository 历史冲突时返回
`TUTORING_REFERENCE_MISMATCH`。

### M6 私有策略边界

公开 `M6TutoringControlService.decide_next_action(...)` 仍精确接收
`TaskPlan`、`ScoringResultBundle`、`StateUpdateResult` 和可选
`SessionStateSnapshot` 四个输入，返回 `TutoringControlResult`。新增的
`prepare_policy_execution(...) -> PolicyExecutionRef` 是应用层/M0 内部入口，
不进入 91 个 schema 或 contract provenance；直接调用公开方法时会惰性执行相同
first-writer binding。

| 私有值/接口 | 生产者 | 消费者 | 不变量 |
|---|---|---|---|
| `CandidateAction` | `SafetyEnvelope` | rules/LinUCB adapter、最终状态机 | 仅 `m6-action-space-v1` 的 8 条公共状态迁移 |
| `TutoringPolicyContext` / 23 维 feature | M6 service / `FeatureBuilder` | policy adapter | `m6-features-v1`；仅结构化有限值，不含答案/自由文本/真实身份 |
| `PolicyArtifactManifest` | 受治理的私有发布流程 | M6 Repository/loader/gate | immutable；canonical JSON artifact、SHA/版本/作用域精确匹配 |
| `PolicyExecutionRef` | M6 first-writer prepare | M0 freeze、M6 replay | request/input identity 与 policy/adapter/artifact/feature/action/gate version 绑定 |
| `PolicyObservation` | M6 runtime | M6 Repository、offline dataset | public chosen action、真实 logging propensity、模型分数/不确定性和审计身份 |
| `PolicyRewardRecord` | M6 reward association | M6 Repository、offline dataset | `m6-reward-v1`；pending/censored/invalid 不伪造 scalar reward |
| `PolicyEvaluationRecord` | M6 OPE | Repository 与 active gate | IPS/SNIPS/DM/DR、CI、ESS、coverage、slices；insufficient 不得 approved |

`rules` 不做 manifest/artifact/evaluation I/O；`shadow` 的 public/chosen action 与
propensity 仍属于 rules，模型建议只写 `shadow_action_id`；`active` 也只能从
`SafetyEnvelope` 候选中选择，并要求 approved manifest、精确 SHA/版本、
course/class 双重作用域、支持度、不确定性、OPE、rollout 和 kill switch 全部通过。
任一异常回退 rules。

M6 的 OPE/approval 尚未正式接入 M9。当前公共
`M9TeacherAnalyticsService.build_model_quality_report(...)` 只接收 M8
`CalibrationRunResult`；M9 不生产或消费上述 M6 私有值，也不能把 M6 私有
`approved` 解读成 M9 审核。

`AppCoordinator.run_intelligence_architecture(...) -> ArchitectureScaffoldResult`
保留为 legacy 智能能力脚手架入口。为保持既有
`ArchitectureScaffoldResult.is_empty()` 公共语义，M0 的
`prepare_django_frontend()` 继续返回 `skipped`；该 legacy 脚手架不执行 M2 正式
`retrieve_with_policy`；M7/M9 DeepSeek 默认空结果，没有作答数据的 M5/M8 探测
保持证据不足。正式测评流程使用真实
DINA/BKT、2PL IRT、模型质量和自适应选择。
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

学生起始页不再接收自由文本，而是把用户选择的测评类型映射为服务端固定意图后复用上述契约。`AssessmentProjectionReceipt` 记录会改变画像的诊断测评和阶段评测，学生只能查看自己的试卷 ID、得分、错题数和逐题反馈；随心练习、订正和独立答疑不写入画像统计。

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

submit 在 `state_saved` 后先调用 M6 `prepare_policy_execution(...)`，并在新增
`policy_frozen` checkpoint 恰好冻结 `policy_id`、`adapter_id`、
`adapter_version`、`artifact_sha256`、`feature_schema_version`、
`action_space_version`、`gate_policy_version` 七项。rules artifact 可为 NULL；
learned binding 必须为 lowercase SHA-256。崩溃在 M6 first-write 与 M0 checkpoint
之间时，恢复取得同一 binding；`policy_frozen` 或更晚恢复必须七项完全一致。

pre-v9 行只允许一次受限兼容接管：旧不可变 identity 必须一致，新增字段必须全部
为 NULL，当前知识包/课程包必须与已持久化 TaskPlan 锚点一致。部分填充、冲突或
缺少必要精确状态引用的行返回稳定冲突并保持原值。

v10→v11 的 policy freeze 兼容同样窄化：只有 submit、七字段全 NULL、
checkpoint 为 `tutoring_saved|feedback_saved|analytics_saved`、已有保存状态与
frozen prior-state 标记的旧行才可一次 CAS adoption；`policy_frozen`、review 或
部分填充行不会被猜测修复。

完整机器可检验映射位于
[`contracts/contract_provenance.json`](../contracts/contract_provenance.json)，
加载与一致性检查位于
[`contracts/provenance.py`](../src/course_insight/contracts/provenance.py)。

## 接口运维补充

- 配置优先级与 secret 边界见 [deployment.md](deployment.md)；
- PostgreSQL 迁移 CLI、`partial_envelope_rows` 与源数据限制见
  [postgresql_migration.md](postgresql_migration.md)；
- outbox Worker 的 at-least-once 语义、`app.log` 与审计 JSONL 区别见
  [outbox_worker.md](outbox_worker.md)；
- M6 配置、artifact、promotion/rollback、kill switch、reward/JSONL/OPE 细节见
  [m6_policy_operations.md](m6_policy_operations.md)。

## roles 与 Worker 命令语义

- `python manage.py sync_roles --apply` 把 `roles.csv` 视为该来源管理授权的完整
  期望状态，不是仅追加 patch。
- 之前由该来源管理、现在从 CSV 省略的 grant 会在同一事务中写成
  `is_active=false`、设置 `revoked_at`；用户不再拥有该角色的有效 grant 时，
  对应 Django Group membership 也会移除。
- `--dry-run` 和 `--apply` 都在普通 `revoke=<n>` 摘要中报告省略导致的撤销。
- `python manage.py run_outbox_worker --once` 若最终 snapshot 的
  `last_error_code` 非空，会以命令失败退出，便于自动化发现数据库或 sink 故障。
