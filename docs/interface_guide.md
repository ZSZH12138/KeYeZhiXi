# 接口与生产者—消费者指南

## 使用规则

本项目共有 84 个公开契约，其中 22 个是可独立导出的根契约。
路径 `$` 表示完整契约，`$[]` 表示列表元素，点路径表示组成字段。
服务之间必须传递契约对象，不得用无类型字典代替。

当前 v2 智能架构仅返回空结果：DeepSeek 不发起 API 请求，pgvector
不建立连接，DINA/BKT/IRT 不运行估计。这些边界仍是 MVP 架构
的正式组成部分，不是新模块。

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
| `ScoringPreparationResult` | `M8.prepare_scoring` | M2、M7、`M8.finalize_scoring` |
| `RubricScoringTask` | `M8.prepare_scoring` | `M7.score_subjective_answer` |
| `RubricScoringResult` | `M7.score_subjective_answer` | `M8.finalize_scoring` |
| `ScoringResultBundle` | `M8.finalize_scoring`、`M8.apply_teacher_review` | M0、M5、M6、M9 |
| `DiagnosisResult` | `M5.update_state` | M6、M8、M9 |
| `LearnerStateSnapshot` | `M5.update_state` | M4、M6、M8、M9 |
| `ClassStateSnapshot` | `M5.update_state` | M9 |
| `StateUpdateResult` | `M5.update_state` | M6、M9 |
| `TutoringControlResult` | `M6.decide_next_action` | M2、M7、M4 后续调度 |
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
| `AssessmentSubmission` | M0 Django 学生表单 | M8 | 仅定义无主机路径的输入格式 |
| `TeacherReviewSubmission` | M0 Django 教师表单 | M9 | 仅定义无主机路径的输入格式 |
| `AsyncJobStatus` | M0 作业边界 | M0 页面/调用方 | Django 作业 `skipped` |
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

`AppCoordinator.run_intelligence_architecture(...) -> ArchitectureScaffoldResult`
用于组织并返回智能架构的空实现结果。它依次请求 M0 Django 外层状态、M2 pgvector
索引引用和检索审计、M7/M9 DeepSeek 空生成、M5 DINA/BKT 空运行、
M8 IRT 空标定与空选题、M9 证据不足质量报告。任何组件产生
非空值，组合契约都必须拒绝。

完整机器可检验映射位于
[`contracts/contract_provenance.json`](../contracts/contract_provenance.json)，
加载与一致性检查位于
[`contracts/provenance.py`](../src/course_insight/contracts/provenance.py)。
