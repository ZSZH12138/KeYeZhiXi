# 课业智析

## 项目定位

课业智析是 Python 3.11+、Pydantic v2 的模块化单体。当前交付 84 个数据契约、
10 个公开服务、`AppCoordinator`、Schema 导出和中性空示例。模块之间传递完整
契约对象；`AppCoordinator` 只调用公开服务，不读任何业务表。

架构入口见 [架构说明](docs/architecture.md) 和
[接口指南](docs/interface_guide.md)。契约 Schema、空示例和来源图位于
[contracts/](contracts/)。

### 2026-07-27 运维补充

- Web、部署、进程角色与回滚边界见 [deployment.md](docs/deployment.md)；
- SQLite→PostgreSQL 迁移与源数据限制见 [postgresql_migration.md](docs/postgresql_migration.md)；
- M0 leased outbox Worker 的投递语义与状态文件见 [outbox_worker.md](docs/outbox_worker.md)；
- M6 策略模式、制品、门禁、回滚和 OPE 边界见
  [m6_policy_operations.md](docs/m6_policy_operations.md)。

### 本阶段能力

- 授权课程导入、SHA-256 校验、确定性文本切片；
- 轻量词法证据索引、可定位课程证据；
- M2 RAG/pgvector 目标契约与空索引/检索审计；
- 知识点/先修/误区/题卡/量规/蓝图/Q 矩阵引用校验；
- 幂等任务规划、固定蓝图组卷、客观规则评分和主观评分编排；
- 版本化评分审计、教师复核 v2、个体/班级状态和 S0—S5 辅导；
- M6 私有 `rules`/`shadow`/`active` 运行时、JSON-only LinUCB 制品、奖励/OPE、
  双后端策略持久化和 M0 恢复冻结；默认仍为零 rollout/零探索的 `rules`；
- 证据化学生反馈、教师报告与低证据保护；
- M5 DINA 认知诊断、BKT 知识追踪的契约与空运行；
- M8 IRT、自适应选题和在线标定契约，M9 模型质量/审核边界；
- M7/M9 统一的 DeepSeek API 契约与空适配器；
- M0 配置优先级、真实 Django 学生/教师 Web、两层权限、roles 完整状态同步、
  runtime snapshot 与独立 leased outbox Worker；
- SQLite/PostgreSQL 可切换的 M0、M4—M9 持久化与显式
  SQLite→PostgreSQL 导入器；
- M4 私有意图 adapter 的规则、shadow、active 三阶段运行方式；公开 `TaskPlan`
  契约与八字段业务身份保持不变，运维边界见
  [M4 意图运维说明](docs/m4_intent_operations.md)；
- SQLite 迁移、幂等事件、原子 JSON 快照和 Schema 导出。

## 快速开始：独立 Anaconda 环境

使用自行安装的 Anaconda/Miniconda 创建独立环境，不修改 base。
下面命令不依赖任何主机安装路径：

```shell
conda create --name course-insight-framework python=3.12 -y
conda activate course-insight-framework
python -m pip install --constraint requirements/ci-constraints.txt -e ".[dev]"
python -m pytest -q
```

M0 Web/Worker 使用根 `manage.py`：`python manage.py runserver` 启动开发 Web，
`python manage.py run_outbox_worker` 启动独立 Worker。生产配置与完整验收步骤见
[部署说明](docs/deployment.md)。

导出公开契约 Schema：

```shell
python scripts/export_schemas.py
python -m course_insight.cli export-schemas
```

导出文件统一写入 `contracts/schemas/`；运行数据只进入 `runtime/`。

## 完整目录与目录放置规则

```text
课业智析/
├─ pyproject.toml                 运行依赖与构建配置
├─ README.md                      接手工程师首要入口
├─ config/                        可提交的无密钥示例配置
├─ contracts/
│  ├─ schemas/                    84 份公开契约 Schema
│  ├─ examples/                   1 份中性架构空结果示例
│  └─ contract_provenance.json    契约生产者—消费者来源图
├─ data/raw_course/               本地原始资料占位；真实资料不提交
├─ docs/                           架构与接口说明
├─ progress/                       仅按陈、谢、童、冯记录协作进度
├─ scripts/export_schemas.py       Schema 导出入口
├─ src/course_insight/
│  ├─ contracts/                  Pydantic 契约 Python 包
│  ├─ application/                AppCoordinator 跨模块编排
│  ├─ infrastructure/             配置、SQLite/PostgreSQL、JSON/日志、导入器与 DeepSeek 空适配器
│  ├─ modules/m0_platform/        M0 责任域
│  ├─ modules/m1_course_governance/
│  ├─ modules/m2_evidence_retrieval/
│  ├─ modules/m3_knowledge_bundle/
│  ├─ modules/m4_task_orchestration/
│  ├─ modules/m5_learner_class_state/
│  ├─ modules/m6_tutoring_fsm/
│  ├─ modules/m7_local_model/
│  ├─ modules/m8_assessment_scoring/
│  └─ modules/m9_teacher_analytics/
└─ runtime/                        数据库、索引、快照、日志；不得提交
```

目录放置规则：可导入模型只放 `src/course_insight/contracts/`；生成 Schema 和
示例只放根 `contracts/`；跨模块用例只放 `application/`；模块业务代码放对应
`mX_*`，`service.py` 是公开入口、`repository.py` 是本模块存储 Protocol、
`stubs.py` 是零参数占位依赖；技术细节放 `infrastructure/`；本地授权原始资料
只暂存于 `data/raw_course/`；临时数据库、索引、快照、日志和模型产物只能放
`runtime/`。不得在顶层临时创建新的业务数据或说明文件。

## M0—M9 责任、输入与下游

| 模块 | 负责人 | 职责 | 允许输入/契约来源 | 输出 | 下游 |
|---|---|---|---|---|---|
| [M0](src/course_insight/modules/m0_platform/README.md) | 陈 | 配置、事件、快照、Django 外层 | 环境/相对配置；表单；M8 `LearningEvent` | `ActorContext`、`AsyncJobStatus`、`EventAck` | `AppCoordinator`、M8、M9 |
| [M1](src/course_insight/modules/m1_course_governance/README.md) | 谢 | 授权、来源版本、确定性分块 | 课程 MD、元数据 JSON、授权 CSV | `CoursePackage` | M2、M3 |
| [M2](src/course_insight/modules/m2_evidence_retrieval/README.md) | 谢 | RAG；词法/pgvector 索引与审计 | M1 `CoursePackage`；M6/M8 `EvidenceQuery`；检索策略 | `EvidenceIndexRef`、`EvidenceBundle`、`RetrievalAudit` | M7、M9 |
| [M3](src/course_insight/modules/m3_knowledge_bundle/README.md) | 谢 | 知识、题卡、量规、蓝图、Q 矩阵和标定依据 | M1 `CoursePackage`；教师确认 JSON | `KnowledgeBundle` | M4、M5、M8、M9 |
| [M4](src/course_insight/modules/m4_task_orchestration/README.md) | 陈 | 任务识别、私有可重放意图决策、蓝图选择、引用冻结和工作流编排 | 学生文本；M3 bundle；可选 M5 state | `TaskPlan` | M8、M6 |
| [M5](src/course_insight/modules/m5_learner_class_state/README.md) | 童 | DINA 认知诊断、BKT 知识追踪、个体/班级状态 | M8 观测；M3 Q 矩阵；前版状态 | `StateUpdateResult`、`CognitiveDiagnosisResult`、`KnowledgeTraceSnapshot`、`LearningModelRun` | M6、M9 |
| [M6](src/course_insight/modules/m6_tutoring_fsm/README.md) | 陈 | S0—S5 状态机；安全候选、版本化私有策略与会话幂等 | M4 task；M8 scoring；M5 state；前版 session | `TutoringControlResult`；私有策略记录不进入公共契约 | M2、M7 |
| [M7](src/course_insight/modules/m7_local_model/README.md) | 冯 | 量规评分、学生反馈与 DeepSeek API 边界 | M8 scoring task；M6 feedback task；M2 evidence | `RubricScoringResult`、`StudentFeedbackPackage`、`LLMGenerationResult` | M8、M0 |
| [M8](src/course_insight/modules/m8_assessment_scoring/README.md) | 童 | 组卷、评分、IRT、自适应选题与在线标定 | M4/M3/M5；作答；M7 result；M9 review | `AssessmentPaper`、`ScoringPreparationResult`、`ScoringResultBundle`、`IRTParameterSet`、`CalibrationRunResult`、`AdaptiveSelectionResult` | M0、M2、M5、M6、M9 |
| [M9](src/course_insight/modules/m9_teacher_analytics/README.md) | 冯 | 教师分析、DeepSeek 叙述、模型质量和审核 | M3/M8/M5；标定 result；复核表单 | `TeacherAnalyticsBundle`、`TeacherReviewDecision`、`LLMGenerationResult`、`ModelQualityReport`、`CalibrationReviewDecision` | M0、M8 |

生产者—消费者的机器可检验映射见
[contract_provenance.json](contracts/contract_provenance.json)。

## 契约总览（84 个）

每个类都继承 `schema_version: str`，拒绝额外字段，要求带时区时间，并继承
`to_dict() -> dict[str, Any]`、`to_json(indent: int = 2) -> str`、
`to_json_file(path: Path) -> None`、`content_checksum() -> str`、
`from_dict(data: dict[str, Any]) -> Self`、`from_json_file(path: Path) -> Self`。
22 个根契约是：`CoursePackage`、
`EvidenceIndexRef`、`EvidenceQuery`、`EvidenceBundle`、`KnowledgeBundle`、
`TaskPlan`、`AssessmentPaper`、`ScoringPreparationResult`、
`RubricScoringTask`、`RubricScoringResult`、`ScoringResultBundle`、
`DiagnosisResult`、`LearnerStateSnapshot`、`ClassStateSnapshot`、
`StateUpdateResult`、`TutoringControlResult`、`FeedbackGenerationTask`、
`StudentFeedbackPackage`、`TeacherAnalyticsBundle`、`TeacherReviewDecision`、
`EventAck`、`ArchitectureScaffoldResult`。其余是组成类；以下逐一列出字段、类型、公开领域方法和源码。

### 课程契约（[course.py](src/course_insight/contracts/course.py)）

| 类 | 声明字段（不重复列 `schema_version`） | 公开领域方法 |
|---|---|---|
| `SourceAuthorization` | `source_id:str; authorized_by:str; license_note:str; authorized_at:datetime` | `covers(source_id)->bool` |
| `SourceDocument` | `source_id:str; file_name:str; media_type:str; sha256:str; page_count:int\|None; title:str; version:str` | `matches_file(path)->bool` |
| `ContentChunk` | `chunk_id:str; source_id:str; text:str; locator:str; concept_hints:list[str]; sha256:str` | `contains(term)->bool; word_count()->int` |
| `CoursePackage` | `course_package_id:str; course_id:str; package_version:str; source_documents:list[SourceDocument]; content_chunks:list[ContentChunk]; source_authorizations:list[SourceAuthorization]; imported_at:datetime; status:Literal[draft,ready,failed]; checksum:str` | `find_source; find_chunk; list_chunks_by_source; recalculate_checksum` |

### 证据契约（[evidence.py](src/course_insight/contracts/evidence.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `EvidenceIndexRef` | `index_id:str; course_package_id:str; index_version:str; storage_ref:str; backend:Literal[lexical,pgvector]; embedding_model_id:str\|None; source_count:int; chunk_count:int; built_at:datetime; checksum:str; status:Literal[building,ready,empty,failed]` | `assert_ready; matches(course_package)` |
| `EvidenceQuery` | `query_id:str; course_package_id:str; query_text:str; concept_ids:list[str]; item_id:str\|None; use_case:Literal[grading,feedback,qa]; top_k:int; min_relevance:float` | `normalized_text; cache_key` |
| `EvidenceChunk` | `evidence_id:str; source_id:str; chunk_id:str; text:str; locator:str; concept_ids:list[str]; relevance:float; checksum:str` | `citation_label` |
| `EvidenceBundle` | `query_id:str; index_id:str; course_id:str; evidence_chunks:list[EvidenceChunk]; retrieved_at:datetime` | `is_empty; top; citation_ids; contains_source` |

### 知识契约（[knowledge.py](src/course_insight/contracts/knowledge.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `KnowledgeConcept` | `concept_id:str; name:str; chapter_id:str; description:str; aliases:list[str]; status:str` | `matches_name` |
| `PrerequisiteRelation` | `from_concept_id:str; to_concept_id:str; relation_type:Literal[prerequisite,related]; strength:float` | `is_prerequisite` |
| `MisconceptionTag` | `misconception_id:str; name:str; description:str; concept_ids:list[str]; evidence_rules:list[str]` | `applies_to` |
| `RubricCriterion` | `criterion_id:str; description:str; max_score:float; expected_student_evidence:str; course_evidence_ids:list[str]` | `allows(score)` |
| `ReviewPolicy` | `low_confidence_threshold:float; double_score_disagreement_threshold:float; require_evidence_for_positive_score:bool` | `needs_review(confidence,disagreement)` |
| `Rubric` | `rubric_id:str; version:str; total_score:float; criteria:list[RubricCriterion]; review_policy:ReviewPolicy; status:str` | `criterion; criterion_score_sum` |
| `ParameterRule` | `name:str; value_type:str; minimum:float\|None; maximum:float\|None; choices:list[str]; constraints:list[str]` | `accepts(value)` |
| `ItemCard` | `item_id:str; version:str; stem:str; item_type:str; concept_ids:list[str]; misconception_ids:list[str]; difficulty_level:int; cognitive_level:str; parameter_rules:list[ParameterRule]; answer_key:dict[str,Any]; rubric_id:str\|None; source_evidence_ids:list[str]; status:str` | `is_objective; is_approved; max_score` |
| `BlueprintSection` | `section_id:str; name:str; item_count:int; score:float; item_types:list[str]; concept_weights:dict[str,float]; difficulty_range:tuple[int,int]; anchor_item_ids:list[str]` | `accepts(item)` |
| `AssessmentBlueprint` | `blueprint_id:str; version:str; course_id:str; sections:list[BlueprintSection]; total_score:float; duration_minutes:int; status:str` | `section; score_sum` |
| `QMatrixEntry` | `item_id:str; item_version:str; concept_id:str; weight:float` | `is_active` |
| `KnowledgeBundle` | `knowledge_bundle_id:str; course_package_id:str; course_id:str; bundle_version:str; concepts:list[KnowledgeConcept]; prerequisite_relations:list[PrerequisiteRelation]; misconception_tags:list[MisconceptionTag]; items:list[ItemCard]; rubrics:list[Rubric]; blueprints:list[AssessmentBlueprint]; q_matrix:list[QMatrixEntry]; status:Literal[draft,published]; published_at:datetime\|None` | `get_concept; get_item; get_rubric; get_blueprint; approved_items; prerequisite_closure` |

### 任务契约（[tasking.py](src/course_insight/contracts/tasking.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `TaskPlan` | `task_id:str; task_type:Literal[qa,diagnostic,practice,correction,stage_assessment]; course_id:str; class_id:str; learner_id:str; session_id:str; blueprint_id:str\|None; knowledge_bundle_id:str; course_package_id:str; workflow:list[str]; next_module:str; created_at:datetime` | `requires_assessment; next_after; assert_module_allowed` |

### 测评契约（[assessment.py](src/course_insight/contracts/assessment.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `ItemInstance` | `item_instance_id:str; item_id:str; item_version:str; stem:str; parameters:dict[str,Any]; concept_ids:list[str]; rubric_id:str\|None; max_score:float; source_evidence_ids:list[str]` | `instance_checksum; is_subjective` |
| `PaperSection` | `section_id:str; name:str; items:list[ItemInstance]; score:float` | `score_sum` |
| `AssessmentPaper` | `paper_id:str; task_id:str; blueprint_id:str; blueprint_version:str; learner_id:str; sections:list[PaperSection]; generated_at:datetime; immutable_checksum:str` | `all_items; get_item_instance; total_score; freeze` |
| `CriterionScore` | `criterion_id:str; score:float; student_evidence:str; course_evidence_id:str\|None; reason:str` | `has_student_evidence` |
| `RubricScoringTask` | `scoring_task_id:str; attempt_id:str; paper_id:str; item_instance:ItemInstance; student_answer:str; rubric:Rubric; evidence_query_id:str; created_at:datetime` | `max_score; answer_checksum; criterion_ids` |
| `ScoreAuditRecord` | `audit_id:str; audit_version:int; attempt_id:str; item_instance_id:str; criterion_scores:list[CriterionScore]; total_score:float; max_score:float; confidence:float; scoring_method:Literal[rule,local_model,teacher_override]; review_status:str; review_reason:list[str]; created_at:datetime` | `criterion_score_sum; next_version; needs_review` |
| `ScoringPreparationResult` | `attempt_id:str; paper_id:str; learner_id:str; objective_audit_records:list[ScoreAuditRecord]; rubric_scoring_tasks:list[RubricScoringTask]; evidence_queries:list[EvidenceQuery]; raw_answer_checksum:str; prepared_at:datetime` | `pending_task_ids; query_for_task; all_objective_scored` |
| `RubricScoringResult` | `scoring_task_id:str; criterion_scores:list[CriterionScore]; total_score:float; confidence:float; missing_concept_ids:list[str]; review_flags:list[str]; model_name:str; model_version:str; scored_at:datetime` | `criterion_score_sum; requires_review; get_criterion` |
| `RemediationTarget` | `concept_id:str; misconception_id:str\|None; priority:int; recommended_item_ids:list[str]; reason:str` | `is_high_priority` |
| `RemediationPlan` | `plan_id:str; based_on_attempt_id:str; learner_id:str; targets:list[RemediationTarget]; created_at:datetime` | `ordered_targets; target_concept_ids` |
| `ScoringResultBundle` | `attempt_id:str; paper_id:str; learner_id:str; score_audit_records:list[ScoreAuditRecord]; learning_events:list[LearningEvent]; remediation_plan:RemediationPlan; total_score:float; max_score:float; finalized_at:datetime` | `get_audit_record; requires_teacher_review; replace_audit_record; recalculate_total` |

### 状态契约（[state.py](src/course_insight/contracts/state.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `ItemDiagnosis` | `item_instance_id:str; concept_ids:list[str]; misconception_ids:list[str]; error_type:str; confidence:float; evidence_audit_ids:list[str]; prerequisite_gap_ids:list[str]` | `has_misconception` |
| `DiagnosisResult` | `diagnosis_id:str; attempt_id:str; learner_id:str; item_diagnoses:list[ItemDiagnosis]; priority_concept_ids:list[str]; priority_misconception_ids:list[str]; generated_at:datetime` | `diagnosis_for_item; has_prerequisite_gap; top_targets` |
| `MisconceptionStrength` | `misconception_id:str; strength:float; evidence_count:int; last_seen_at:datetime` | `is_active` |
| `ConceptState` | `concept_id:str; mastery_probability:float; mastery_confidence:float; misconceptions:list[MisconceptionStrength]; hint_dependency:float; recent_correction_rate:float; evidence_count:int; updated_at:datetime` | `band; active_misconceptions` |
| `LearnerStateSnapshot` | `snapshot_id:str; course_id:str; class_id:str; learner_id:str; state_version:int; concept_states:list[ConceptState]; overall_mastery:float; evidence_count:int; updated_at:datetime` | `get_concept_state; weak_concepts; next_version` |
| `MasteryDistribution` | `mastered:float; consolidating:float; priority_support:float` | `sum` |
| `ClassConceptStatus` | `concept_id:str; mastery_distribution:MasteryDistribution; mean_mastery_probability:float; mean_confidence:float; mastery_trend_delta:float\|None; trend_comparable:bool; sample_count:int` | `needs_support` |
| `ClassMisconceptionSummary` | `misconception_id:str; affected_count:int; affected_rate_among_assessed:float; evidence_attempts:int` | `is_frequent` |
| `ClassStateSnapshot` | `snapshot_id:str; course_id:str; class_id:str; aggregation_policy_version:str; scope:dict[str,str]; class_size:int; assessed_count:int; coverage_rate:float; concept_status:list[ClassConceptStatus]; misconception_summary:list[ClassMisconceptionSummary]; evidence_status:Literal[sufficient,insufficient]; updated_at:datetime` | `get_concept_status; is_actionable; top_misconceptions` |
| `StateUpdateResult` | `diagnosis_result:DiagnosisResult; learner_state_snapshot:LearnerStateSnapshot; class_state_snapshot:ClassStateSnapshot; processed_audit_ids:list[str]; updated_at:datetime` | `assert_consistent; contains_audit` |

### 辅导契约（[tutoring.py](src/course_insight/contracts/tutoring.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `SessionStateSnapshot` | `session_id:str; current_state:Literal[S0,S1,S2,S3,S4,S5]; turn_count:int; completed_action_ids:list[str]; updated_at:datetime` | `can_transition_to; transition` |
| `TeachingAction` | `action_id:str; state_before:str; action_type:str; target_concept_ids:list[str]; prompt_template_id:str; must_not_reveal_answer:bool; next_state:str; reason:str` | `is_safe_hint` |
| `FeedbackGenerationTask` | `feedback_task_id:str; task_id:str; learner_id:str; teaching_action:TeachingAction; diagnosis_result:DiagnosisResult; score_summary:dict[str,float]; learner_state_snapshot_id:str; evidence_query_id:str; created_at:datetime` | `target_concept_ids; must_hide_answer` |
| `EvidenceCitation` | `evidence_id:str; source_id:str; locator:str; quote:str` | `label` |
| `RubricFeedback` | `criterion_id:str; earned_score:float; max_score:float; message:str; student_evidence:str` | `is_full_score` |
| `StudentFeedbackPackage` | `feedback_id:str; task_id:str; learner_id:str; message:str; rubric_feedback:list[RubricFeedback]; missing_concept_ids:list[str]; evidence_citations:list[EvidenceCitation]; next_practice_item_ids:list[str]; confidence:float; generated_at:datetime` | `has_citations; safe_for_student; citation_ids` |
| `TutoringControlResult` | `teaching_action:TeachingAction; feedback_generation_task:FeedbackGenerationTask; evidence_query:EvidenceQuery; session_state_snapshot:SessionStateSnapshot; created_at:datetime` | `assert_query_alignment; next_state` |

### 教师分析契约（[analytics.py](src/course_insight/contracts/analytics.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `ClassReport` | `class_id:str; coverage_rate:float; concept_summaries:list[ClassConceptStatus]; misconception_summaries:list[ClassMisconceptionSummary]; score_statistics:dict[str,float]; evidence_status:str` | `is_actionable` |
| `IndividualReport` | `learner_id:str; overall_mastery:float; weak_concept_ids:list[str]; active_misconception_ids:list[str]; recent_score:float; review_required_count:int` | `needs_follow_up` |
| `ReviewQueueItem` | `audit_id:str; audit_version:int; learner_id:str; item_instance_id:str; recommended_score:float; confidence:float; review_reasons:list[str]` | `priority_key` |
| `TeachingSuggestion` | `suggestion_id:str; action_type:str; concept_ids:list[str]; content:str; trigger_metrics:dict[str,float]; affected_count:int; affected_rate:float; coverage_rate:float; confidence:float; evidence_ids:list[str]; status:str` | `is_actionable` |
| `CriterionOverride` | `criterion_id:str; previous_score:float; new_score:float; reason:str` | `delta` |
| `TeacherAnalyticsBundle` | `report_id:str; class_report:ClassReport; individual_reports:list[IndividualReport]; review_queue:list[ReviewQueueItem]; teaching_suggestions:list[TeachingSuggestion]; generated_at:datetime` | `open_review_count; actionable_suggestions; report_for_learner` |
| `TeacherReviewDecision` | `decision_id:str; audit_id:str; expected_audit_version:int; decision:Literal[confirm,override,reject]; final_total_score:float; criterion_overrides:list[CriterionOverride]; teacher_comment:str; reviewer_id:str; reviewed_at:datetime` | `is_override; override_score_sum; assert_matches` |

### 事件契约（[events.py](src/course_insight/contracts/events.py)）

| 类 | 声明字段 | 公开领域方法 |
|---|---|---|
| `LearningEvent` | `event_id:str; event_type:str; course_id:str; class_id:str; learner_id:str; attempt_id:str\|None; payload:dict[str,Any]; occurred_at:datetime` | `idempotency_key` |
| `EventAck` | `accepted_event_ids:list[str]; duplicate_event_ids:list[str]; failed_event_ids:list[str]; persisted_at:datetime` | `all_succeeded; accepted_count` |

### M0 平台外层契约（[platform.py](src/course_insight/contracts/platform.py)）

| 类 | 声明字段 | 公开领域规则/用途 |
|---|---|---|
| `ActorContext` | `actor_id:str; role:student\|teacher\|course_admin\|system_admin; course_ids:list[str]; class_ids:list[str]; issued_at:datetime` | 鉴权范围不可重复；只用伪匿名身份 |
| `AssessmentSubmission` | `submission_id:str; attempt_id:str; paper_id:str; learner_id:str; answers:dict[str,str\|bool\|int\|float]; submitted_at:datetime` | Django 学生表单到 M8 的无路径输入；答案键为题目实例 ID |
| `TeacherReviewSubmission` | `submission_id:str; audit_id:str; expected_audit_version:int; reviewer_id:str; decision:confirm\|override\|reject; final_total_score:float; criterion_overrides:list[CriterionOverride]; teacher_comment:str; submitted_at:datetime` | Django 教师表单到 M9 的无路径输入；M9 转成同词汇的 `TeacherReviewDecision` |
| `AsyncJobStatus` | `job_id:str; job_type:django_frontend\|vector_index\|llm_generation\|learning_model\|calibration; status:queued\|running\|succeeded\|failed\|skipped; progress:float; result_ref/error_code; created_at/finished_at` | 终态必须有完成时间；legacy Django scaffold 作业保持 `skipped` |

### M2/M7/M9 智能边界契约（[intelligence.py](src/course_insight/contracts/intelligence.py)）

| 类 | 声明字段 | 公开领域规则/用途 |
|---|---|---|
| `EmbeddingModelRef` | `provider:str; model_name:str; model_version:str; dimension:int\|None; status:empty\|configured` | configured 才允许维度；供 M2 索引版本绑定 |
| `RetrievalPolicy` | `policy_id:str; strategy:lexical\|vector\|hybrid; top_k:int; lexical_weight/vector_weight:float; rerank:bool` | 至少启用一种检索信号 |
| `RetrievalAudit` | `audit_id:str; query_id:str; index_id:str; policy_id:str; retrieved_evidence_ids:list[str]; status:empty\|succeeded\|failed; created_at:datetime` | 证据 ID 唯一；空审计不能带证据 |
| `LLMModelRef` | `provider:deepseek; model_name:str; model_version:str; api_key_env:DEEPSEEK_API_KEY; status:empty\|configured` | 将 LLM 供应商和密钥名固定为可检验契约 |
| `LLMGenerationRequest` | `request_id:str; use_case:rubric_scoring\|student_feedback\|teacher_narrative; model_ref; prompt_template_id/version; evidence_ids; input_checksum; created_at` | 证据 ID 不可重复；不保存完整提示词 |
| `LLMGenerationResult` | `request_id:str; provider:deepseek; status:empty\|succeeded\|failed\|blocked; content; structured_output; citation_ids; finish_reason; generated_at` | 空实现必须是空内容/引用且 `not_run` |
| `ModelInvocationAudit` | `invocation_id/request_id; provider:deepseek; model_name; status:not_run\|succeeded\|failed\|blocked; input_tokens/output_tokens/latency_ms; error_code; created_at` | 只保留调用元数据，不保留密钥/提示词 |
| `SafetyCheckResult` | `request_id:str; status:not_run\|passed\|blocked; flags:list[str]; checked_at:datetime` | M7/M9 的安全门槛输出 |
| `ArchitectureScaffoldResult` | `status:empty; web_job_status; vector_index; retrieval_audit; scoring_generation; teacher_generation; learning_model_run; calibration_result; adaptive_selection_result; model_quality_report; completed_at` | `is_empty()`；任一组件不是预期空状态即拒绝 |

### M5/M8/M9 学习模型契约（[learning_models.py](src/course_insight/contracts/learning_models.py)）

| 类 | 声明字段 | 公开领域规则/用途 |
|---|---|---|
| `LearningObservation` | `observation_id; learner/course/class/attempt/item ID; item_version; concept_ids; score/max_score; source_audit_id/version; occurred_at` | 分数不越界，概念不重复，观测可回溯到审计版本 |
| `LearningObservationBatch` | `batch_id:str; learner_id:str; observations:list[LearningObservation]; watermark:str; created_at:datetime` | 观测 ID 唯一且学习者一致；空列表是合法架构输入 |
| `CognitiveDiagnosisResult` | `run_id; learner_id; model_type:DINA\|DINO\|GDINA\|NCDM; model_version; concept_mastery; observation_count; status; generated_at` | 概率为 [0,1]；`empty` 时掌握字典必须为空 |
| `KnowledgeTraceSnapshot` | `trace_id; learner_id; model_type:BKT\|BKT_FORGETTING\|DKT; model_version; concept_probabilities; watermark/count; status; updated_at` | 概率为 [0,1]；`empty` 时追踪字典必须为空 |
| `LearningModelRun` | `run_id; diagnosis; knowledge_trace; observation_count; status; created_at` | DINA/BKT 学习者、观测数和空状态必须对齐 |
| `IRTItemParameters` | `item_id/version; discrimination; difficulty; guessing; sample_size` | 题目版本不可变；区分度为正，猜测率在 [0,1) |
| `IRTParameterSet` | `parameter_set_id; model_type:1PL\|2PL\|3PL; version; item_parameters; sample_size; status:empty\|shadow\|approved\|rejected; created_at` | 题目版本唯一；空集不带参数/样本 |
| `AbilityEstimate` | `estimate_id; learner_id; parameter_set_id; theta; standard_error; status; estimated_at` | 仅 `estimated` 状态可同时携带 theta 和标准误 |
| `CalibrationRunResult` | `run_id; parameter_set; converged; metrics; status:empty\|shadow\|failed; generated_at` | 空标定不得声称收敛或产生指标 |
| `AdaptiveSelectionPolicy` | `policy_id; version; parameter_set_id; max_items; concept_quotas; status:empty\|configured` | 配额非负；空策略不绑定参数集 |
| `AdaptiveSelectionResult` | `selection_id; policy_id; learner_id; item_ids; ability_estimate; status; selected_at` | 题目 ID 唯一；空选题无题目或能力估计 |
| `ModelQualityReport` | `report_id; subject_ref; metrics; observation_count; status:insufficient_data\|ready\|failed; generated_at` | 证据不足时禁止伪造质量指标 |
| `CalibrationReviewDecision` | `decision_id; calibration_run_id; reviewer_id; decision:approve\|reject\|defer; target_parameter_version; reason; reviewed_at` | M9 教师对 M8 shadow 标定的审核契约 |

`DomainError` 不是 84 个 Pydantic 类之一；它固定包含 `code`、`module`、
`message`、`details`、`recoverable`，公开 `to_dict()`、`with_detail()` 和稳定
字符串表示。来源图辅助模型位于
[provenance.py](src/course_insight/contracts/provenance.py)。

## 公开服务与 AppCoordinator 接口

所有参数由调用方以关键字传递。表中的“来源 → 消费者”说明契约边界；所列错误
是服务直接产生的稳定代码，嵌套契约仍可能抛出自身 `DomainError`。

### M0PlatformService

源码：[service.py](src/course_insight/modules/m0_platform/service.py)。构造：
`M0PlatformService(database_path: Path, runtime_dir: Path, config_dir: Path)`。

- `initialize() -> None`：读取本地 config/runtime，初始化迁移；供应用启动使用；
  错误 `DATABASE_UNAVAILABLE`。
- `prepare_django_frontend(actor_context: ActorContext, requested_at: datetime)
  -> AsyncJobStatus`：为保持既有 `ArchitectureScaffoldResult` 公共语义，作为
  legacy intelligence scaffold 固定返回 `skipped`；它不启动 Web 或 Worker。
  真实 Django 已由独立 URL/View/Template、WSGI/ASGI 与 health 入口提供。
- `append_learning_events(events: list[LearningEvent]) -> EventAck`：事件来自 M8
  scoring；事件与 outbox 同事务持久化，确认供 `AppCoordinator`；文件投递只由
  独立 Worker 完成；错误 `EVENT_PERSIST_FAILED`。
- `save_contract_snapshot(obj: ContractModel, path: Path) -> Path`：任意上游契约
  写入 runtime 原子快照；错误 `CONFIG_INVALID`。
- `load_contract_snapshot(model_type: type[T], path: Path) -> T`：恢复同类型契约；
  错误 `CONFIG_INVALID`。
- `health_check() -> dict[str,str]`：供应用健康观察；错误
  `DATABASE_UNAVAILABLE`。

### M1CourseGovernanceService

源码：[service.py](src/course_insight/modules/m1_course_governance/service.py)。构造：
`M1CourseGovernanceService(parser_registry: Any, hash_tool: Any, repository: M1Repository)`。

- `import_course(raw_course_files: list[Path], course_metadata_path: Path,
  source_authorization_path: Path|None, output_dir: Path) -> CoursePackage`：原始输入
  来自教师授权 MD/JSON/CSV；输出原样给 M2/M3；错误 `COURSE_PARSE_FAILED`、
  `UNAUTHORIZED_SOURCE`、`SOURCE_HASH_MISMATCH`。

### M2EvidenceRetrievalService

源码：[service.py](src/course_insight/modules/m2_evidence_retrieval/service.py)。构造：
`M2EvidenceRetrievalService(index_dir: Path, tokenizer_or_embedding_adapter: Any,
repository: M2Repository)`。

- `build_index(course_package: CoursePackage) -> EvidenceIndexRef`：输入来自 M1；
  索引引用供后续 `retrieve`；错误 `INDEX_NOT_READY`。
- `initialize_vector_store(course_package_id: str, requested_at: datetime)
  -> EvidenceIndexRef`：声明 `backend=pgvector` 的逻辑索引；当前不连接
  PostgreSQL/pgvector，返回 `empty`。
- `empty_retrieval_audit(index_ref: EvidenceIndexRef, requested_at: datetime)
  -> RetrievalAudit`：为未执行的 RAG 检索产生无证据 ID 的空审计。
- `retrieve(evidence_query: EvidenceQuery, evidence_index_ref: EvidenceIndexRef)
  -> EvidenceBundle`：查询来自 M6/M8，索引来自本服务；证据给 M7；错误
  `INDEX_NOT_READY` 及契约引用不匹配错误。

### M3KnowledgeBundleService

源码：[service.py](src/course_insight/modules/m3_knowledge_bundle/service.py)。构造：
`M3KnowledgeBundleService(repository: M3Repository, schema_validator: Any)`。

- `build_knowledge_bundle(course_package: CoursePackage, concept_seed_path: Path,
  item_seed_path: Path, rubric_seed_path: Path, blueprint_seed_path: Path,
  prerequisite_seed_path: Path|None=None, misconception_seed_path: Path|None=None)
  -> KnowledgeBundle`：课程包来自 M1、种子来自教师确认 JSON；输出给
  M4/M5/M8/M9；错误 `Q_MATRIX_CONFLICT`。

### M4TaskOrchestrationService

源码：[service.py](src/course_insight/modules/m4_task_orchestration/service.py)。构造：
`M4TaskOrchestrationService(repository: M4Repository,
idempotency_key_factory: IdempotencyKeyFactory,
*, blueprint_by_task_type: Mapping[str, str]|None=None)`。

- `create_task_plan(student_text: str, task_type_hint: str|None, course_id: str,
  class_id: str, learner_id: str, session_id: str,
  knowledge_bundle: KnowledgeBundle,
  learner_state_snapshot: LearnerStateSnapshot|None) -> TaskPlan`：文本来自学生，
  bundle 来自 M3、状态来自 M5；输出给 M8/M6；错误 `UNSUPPORTED_TASK`、
  `KNOWLEDGE_BUNDLE_MISMATCH`、`LEARNER_STATE_MISMATCH`、
  `BLUEPRINT_NOT_FOUND`。

### M5StateService

源码：[service.py](src/course_insight/modules/m5_learner_class_state/service.py)。构造：
`M5StateService(repository: M5Repository, state_update_policy: Any,
class_aggregation_policy: Any)`。

- `update_state(scoring_result_bundle: ScoringResultBundle,
  knowledge_bundle: KnowledgeBundle,
  previous_learner_state_snapshot: LearnerStateSnapshot|None,
  previous_class_state_snapshot: ClassStateSnapshot|None,
  state_policy_path: Path) -> StateUpdateResult`：scoring 来自 M8、bundle 来自 M3、
  前版状态来自本模块；输出给 M6/M9；错误 `INSUFFICIENT_EVIDENCE`、
  `STALE_STATE_VERSION`、`STATE_POLICY_INVALID`、`STATE_REFERENCE_MISMATCH`。
- `run_learning_models(observation_batch: LearningObservationBatch)
  -> LearningModelRun`：为同一学习者返回对齐的空 DINA 认知诊断与空 BKT
  知识追踪；不做概率估计。

### M6TutoringControlService

源码：[service.py](src/course_insight/modules/m6_tutoring_fsm/service.py)。构造：
`M6TutoringControlService(state_machine_definition: Any,
repository: M6Repository, policy_runtime: PolicyRuntime|None = None)`。

- `prepare_policy_execution(task_plan: TaskPlan,
  scoring_result_bundle: ScoringResultBundle,
  state_update_result: StateUpdateResult,
  previous_session_state_snapshot: SessionStateSnapshot|None)
  -> PolicyExecutionRef`：应用层内部 first-writer 冻结入口；返回 M6 私有模型，
  不进入公共 schema/provenance。直接调用 `decide_next_action(...)` 时仍会惰性执行
  相同绑定。

- `decide_next_action(task_plan: TaskPlan,
  scoring_result_bundle: ScoringResultBundle,
  state_update_result: StateUpdateResult,
  previous_session_state_snapshot: SessionStateSnapshot|None)
  -> TutoringControlResult`：输入来自 M4/M8/M5/本模块；查询给 M2、反馈任务给
  M7。Repository 会恢复最新会话，以 canonical SHA-256 指纹完成重放、重启与
  并发下的 insert-or-get。默认 rules 不读取学习制品；shadow 不改变公共动作；
  active 只有通过安全候选、版本、作用域、支持度、不确定性、OPE、rollout 和
  kill-switch 门禁才可采用候选。任一失败回退 rules。错误
  `TUTORING_REFERENCE_MISMATCH`、`TUTORING_POLICY_INTEGRITY_ERROR`、
  `INVALID_STATE_TRANSITION`。

### M7LocalModelService

源码：[service.py](src/course_insight/modules/m7_local_model/service.py)。构造：
`M7LocalModelService(local_model_adapter: Any,
prompt_repository: M7Repository, output_validator: Any)`。
类名保留以维持已有公开接口；LLM 方向只允许 DeepSeek API，不再扩展
为其他供应商或本地权重推理。

- `invoke_deepseek(request: LLMGenerationRequest) -> LLMGenerationResult`：
  调用 DeepSeek 空适配器；当前不读取 `DEEPSEEK_API_KEY`、不访问网络，
  合法 M7 用例固定返回空内容和 `not_run`；教师叙述等越界用例返回
  `LLM_USE_CASE_INVALID`。
- `score_subjective_answer(rubric_scoring_task: RubricScoringTask,
  evidence_bundle: EvidenceBundle) -> RubricScoringResult`：任务来自 M8、证据来自
  M2；结果回 M8；错误 `EVIDENCE_REQUIRED`、`INVALID_MODEL_JSON`。
- `generate_student_feedback(feedback_generation_task: FeedbackGenerationTask,
  evidence_bundle: EvidenceBundle) -> StudentFeedbackPackage`：任务来自 M6、证据
  来自 M2；结果给学生；错误 `EVIDENCE_REQUIRED`、`INVALID_MODEL_JSON`。

### M8AssessmentService

源码：[service.py](src/course_insight/modules/m8_assessment_scoring/service.py)。构造：
`M8AssessmentService(repository: M8Repository, rule_scorer: Any,
parameter_item_generator: Any)`。

- `generate_paper(task_plan: TaskPlan, knowledge_bundle: KnowledgeBundle,
  learner_state_snapshot: LearnerStateSnapshot|None,
  diagnosis_result: DiagnosisResult|None) -> AssessmentPaper`：输入来自
  M4/M3/M5；试卷给学生和 prepare；错误 `BLUEPRINT_UNSATISFIABLE`。
- `prepare_scoring(assessment_paper: AssessmentPaper,
  raw_answer_path: Path|AssessmentSubmission,
  knowledge_bundle: KnowledgeBundle) -> ScoringPreparationResult`：试卷来自本模块、
  答案来自系统边界、bundle 来自 M3；任务/查询给 M7/M2；错误
  `ANSWER_FORMAT_INVALID`。
- `finalize_scoring(scoring_preparation_result: ScoringPreparationResult,
  rubric_scoring_results: list[RubricScoringResult]) -> ScoringResultBundle`：准备来自
  本模块、分项来自 M7；输出给 M0/M5/M6/M9；错误
  `SCORING_TASK_RESULT_MISMATCH`、`RUBRIC_RESULT_INVALID`。
- `apply_teacher_review(current_scoring_result_bundle: ScoringResultBundle,
  teacher_review_decision: TeacherReviewDecision) -> ScoringResultBundle`：当前审计
  来自本模块、决定来自 M9；新版本给 M0/M5/M9；错误
  `REVIEW_VERSION_CONFLICT`、`REVIEW_TOTAL_MISMATCH` 及复核契约错误。
- `calibrate_irt(observation_batch: LearningObservationBatch,
  requested_at: datetime) -> CalibrationRunResult`：当前产生空 2PL 参数集，
  不估计、不声称收敛、不伪造质量指标。
- `select_adaptive_items(policy: AdaptiveSelectionPolicy,
  ability_estimate: AbilityEstimate, requested_at: datetime)
  -> AdaptiveSelectionResult`：当前不计算 IRT 信息量，返回空题目列表。

### M9TeacherAnalyticsService

源码：[service.py](src/course_insight/modules/m9_teacher_analytics/service.py)。构造：
`M9TeacherAnalyticsService(repository: M9Repository, statistics_engine: Any,
suggestion_rule_engine: Any)`。

- `build_teacher_analytics(knowledge_bundle: KnowledgeBundle,
  scoring_result_bundle: ScoringResultBundle,
  state_update_result: StateUpdateResult,
  teacher_threshold_policy_path: Path) -> TeacherAnalyticsBundle`：输入来自
  M3/M8/M5/策略边界；输出给教师；错误 `REPORT_SCOPE_INVALID`。
- `generate_teacher_narrative(request: LLMGenerationRequest)
  -> LLMGenerationResult`：仅接受 DeepSeek `teacher_narrative` 用例；当前空实现
  不访问网络且不生成叙述；其他用例返回 `LLM_USE_CASE_INVALID`。
- `build_model_quality_report(calibration_result: CalibrationRunResult,
  requested_at: datetime) -> ModelQualityReport`：为 M8 空标定返回
  `insufficient_data`，不伪造指标；后续作为参数发布门槛。
- `record_teacher_review(raw_review_path: Path|TeacherReviewSubmission,
  current_scoring_result_bundle: ScoringResultBundle) -> TeacherReviewDecision`：原始
  JSON 来自教师、当前审计来自 M8；决定回 M8；错误 `REPORT_SCOPE_INVALID` 和
  复核契约错误。

### AppCoordinator

源码：[coordinator.py](src/course_insight/application/coordinator.py)。构造接收
`m0_service`—`m9_service` 十个对应公开服务，不接收仓储：

- `start_assessment(...)`：为一次 Web 流程创建 M4 `TaskPlan`、M8
  `AssessmentPaper` 与 M0 的无 payload 流程索引。
- `submit_assessment(...)`：按稳定标识恢复试卷，执行既有评分、状态、辅导、
  反馈与教师分析链；客观题全量流程允许主观任务列表为空。
- `get_student_assessment(...)`：在 M0 校验 actor/course/class/learner 归属后，
  从 M8/M7 的公开入口取得权威评分与反馈。
- `get_teacher_review_context(...)`：校验教师课程/班级作用域，并从 M8/M5/M9
  的公开入口恢复复核上下文。
- `review_assessment(...)`：复用既有教师复核链，追加评分审计版本和新的学习事件，
  并按 checkpoint 恢复失败后的重放。
- `initialize_course(*, raw_course_files, course_metadata_path,
  source_authorization_path, output_dir, concept_seed_path, item_seed_path,
  rubric_seed_path, blueprint_seed_path, prerequisite_seed_path,
  misconception_seed_path) -> dict[str,ContractModel]`，返回课程包、索引与知识包。
- `run_assessment_cycle(*, index_ref, knowledge_bundle, student_text,
  task_type_hint, course_id, class_id, learner_id, session_id,
  raw_answer_path: Path|AssessmentSubmission,
  state_policy_path, teacher_threshold_policy_path) -> dict[str,ContractModel]`，返回
  测评、评分、状态、辅导与分析对象。
- `run_teacher_review_cycle(*, knowledge_bundle, scoring_result_bundle,
  state_update_result, raw_review_path: Path|TeacherReviewSubmission, state_policy_path,
  teacher_threshold_policy_path) -> dict[str,ContractModel]`，返回复核后的评分、状态与分析对象。
- `run_intelligence_architecture(*, actor_context: ActorContext,
  course_package_id: str, learner_id: str, requested_at: datetime)
  -> ArchitectureScaffoldResult`，保留为 legacy 智能能力脚手架；M0 的作业字段
  为兼容契约保持 `skipped`，M2 pgvector/RAG、M5 DINA/BKT、M7/M9 DeepSeek、
  M8 IRT/自适应在线标定仍保持空结果或证据不足。真实 Django 不由该入口启动。
- `export_run_manifest(*, objects: list[ContractModel], output_path: Path) -> Path`，
  仅导出 ID、checksum、时间，不导出学生答案。

拆分 Web 用例使用 M0 的稳定 operation ID、短 lease、CAS 版本与 checkpoint；
权威领域对象仍由 M4/M5/M7/M8/M9 各自持久化。既有一站式方法签名保留。
编排器产生的流程错误使用稳定 `DomainError`；其他错误原样来自对应服务。

每个拆分操作还冻结知识包、课程包、证据索引和 policy checksum；M5 更新前以
`state_inputs_frozen` 固定精确 learner/class 前态。长模块调用使用 CAS heartbeat
续租；M5/M9 对 policy 只读取一次，并对同一份内存字节完成 checksum 校验和严格
解析，避免校验后文件被替换。M6 在 `state_saved` 与 `tutoring_saved` 之间增加
`policy_frozen`，冻结 `policy_id`、adapter ID/version、artifact SHA-256、
feature/action/gate version；恢复逐字段精确比较。v9 依赖字段与 v11 M0 freeze
字段的历史 workflow 接管都受限于全 NULL、合法 checkpoint、已保存状态和 CAS，
部分迁移状态继续 fail closed。
失租 owner 的结果会被丢弃，且不能继续推进或写终态。登录限流使用
actor+IP、actor 与 IP 三个 HMAC 桶，成功登录保留共享 IP 历史。

## 三类系统边界原始 JSON

边界 JSON 必须 UTF-8、拒绝额外字段、时间固定为带偏移 ISO 8601，身份只用
`pseudonym_*`。下列结构是格式说明，值均为中性占位，不绑定具体课程内容。

### 课程元数据 `course_metadata.json`

```json
{
  "course_id": "course_example",
  "course_name": "示例课程",
  "course_package_id": "course_package_example",
  "imported_at": "2000-01-01T00:00:00+00:00",
  "package_version": "1.0.0",
  "source_sha256": "64-character-lowercase-sha256"
}
```

`source_sha256` 必须等于授权 Markdown 原始字节的 SHA-256；`course_id` 必须与
蓝图一致。

### 学生答案 `student_answers.json`

```json
{
  "attempt_id": "attempt_example",
  "paper_id": "paper_example",
  "learner_id": "pseudonym_example",
  "answers": [
    {"item_instance_id": "item_instance_example_1", "answer": "answer_example"},
    {"item_instance_id": "item_instance_example_2", "answer": false},
    {"item_instance_id": "item_instance_example_3", "answer": "response_example"}
  ]
}
```

`answers` 必须恰好覆盖冻结试卷、实例 ID 唯一；主观答案必须是可见文本。

### 教师复核 `teacher_review.json`

```json
{
  "decision_id": "decision_example",
  "audit_id": "audit_example",
  "expected_audit_version": 1,
  "decision": "override",
  "final_total_score": 5.0,
  "criterion_overrides": [
    {"criterion_id": "criterion_example_1", "previous_score": 0.0, "new_score": 2.0, "reason": "reviewed"},
    {"criterion_id": "criterion_example_2", "previous_score": 0.0, "new_score": 3.0, "reason": "reviewed"}
  ],
  "teacher_comment": "review_comment_example",
  "reviewer_id": "pseudonym_example",
  "reviewed_at": "2000-01-01T00:00:00+00:00"
}
```

`override` 必须完整覆盖当前量规分项，旧分和版本必须与当前 v1 一致，新分之和
等于 `final_total_score` 且不超过题目上限。

## PostgreSQL 目标、SQLite 基线、JSON 与 runtime 边界

目标部署使用 PostgreSQL；M2 未来的向量扩展仍归 M2/pgvector。仓库已经包含
Psycopg 3 连接池、checksum-locked core migrations、M0/M4—M9 PostgreSQL
Repository，以及显式 SQLite→PostgreSQL 导入 CLI。SQLite 仍是完整可运行基线；
两个后端必须保持模块前缀、幂等身份、追加式版本与现有 Pydantic 契约一致。

本文档不声称真实 PostgreSQL 联调已在当前机器跑通。live tests 必须同时提供
`COURSE_INSIGHT_TEST_DATABASE_URL` 和与 DSN 库名完全一致的
`COURSE_INSIGHT_TEST_DATABASE_NAME`；库名还必须带分隔的 `test`、`ci` 或 `tmp`
标记。缺少变量会明确 `skip`，保留库或危险命名会 fail closed。

| 表 | 归属 | 主身份/版本 |
|---|---|---|
| `schema_migrations` | infrastructure | migration version |
| `m0_learning_events` | M0 | event_id、idempotency_key |
| `m0_event_outbox` | M0 | event_id、lease/version；事件同事务提交，Worker 在事务外投递 |
| `m0_assessment_runs` | M0 | operation_id、checkpoint、CAS version；只存流程关联元数据 |
| `m1_course_packages` | M1 | course_package_id + package_version |
| `m2_evidence_indexes` | M2 | index_id + index_version |
| `m3_knowledge_bundles` | M3 | knowledge_bundle_id + bundle_version |
| `m4_task_plans` | M4 | task_id、idempotency_key |
| `m4_intent_decisions` | M4 私有审计 | request_key、输入 checksum、决策/影子元数据；无原始学生文本 |
| `m5_learner_states` | M5 | course_id + class_id + learner_id + state_version |
| `m5_class_states` | M5 | course_id + class_id + state_version |
| `m5_state_updates` | M5 | attempt_id + state_version |
| `m6_session_states` | M6 | session_id + turn_count |
| `m6_tutoring_decisions` | M6 | request/input fingerprint、session_id + turn_count |
| `m6_policy_artifacts` | M6 private | policy_id、artifact SHA-256；immutable manifest |
| `m6_policy_executions` | M6 private | request fingerprint、policy execution fingerprint |
| `m6_policy_observations` | M6 private | decision/request/execution identity；logging propensity |
| `m6_policy_rewards` | M6 private | execution fingerprint + `m6-reward-v1` |
| `m6_policy_evaluations` | M6 private | policy_id + canonical JSONL dataset identity |
| `m7_student_feedback` | M7 | feedback_id |
| `m8_assessment_papers` | M8 | paper_id，并持久化 course/class 执行作用域 |
| `m8_score_audits` | M8 | audit_id + audit_version；只能追加 |
| `m8_scoring_results` | M8 | attempt_id + 审计版本集合 |
| `m9_teacher_reviews` | M9 | decision_id、audit_id + expected version |
| `m9_teacher_analytics` | M9 | course_id + class_id + report_id |

M7 的持久表保存现有学生反馈契约，不保存 DeepSeek 密钥、完整提示词或本地模型
权重。每个仓储只访问本模块前缀。教师确认的 JSON 是只读输入；`runtime/`
保存数据库、JSON 快照、索引、日志和运行清单。运行产物不得回写 `data/` 或
`contracts/`。

## 智能算法空边界

`AppCoordinator.run_intelligence_architecture` 保留为智能能力脚手架入口。
M0 Web 已是真实基础设施，其他智能算法仍不需要 pgvector、DeepSeek 密钥或
模型训练数据：

1. M0 为保持 legacy `ArchitectureScaffoldResult.is_empty()` 语义返回 Django
   作业 `skipped`；真实 Web 由独立部署入口启动并通过 health 检查。
2. M2 返回 `backend=pgvector`、`status=empty` 的逻辑索引和空 RAG 审计。
3. M7 返回 DeepSeek 评分空结果，M9 返回 DeepSeek 教师叙述空结果。
4. M5 返回 DINA 认知诊断和 BKT 知识追踪空运行。
5. M8 返回 IRT 空标定与空自适应选题。
6. M9 为空标定返回 `insufficient_data` 质量报告。
7. 编排器将上述值组合为 `ArchitectureScaffoldResult(status="empty")`。

该入口不伪造真实模型运行。M0 已实现的 Web/Worker/PostgreSQL 能力不应被写成
算法空实现。

## 新工程师入口与推荐顺序

- 陈：先读 [M0 service](src/course_insight/modules/m0_platform/service.py) 的 Django 外层契约与
  [SQLite migrations](src/course_insight/infrastructure/sqlite/migrations.py)，再读
  [M4 service](src/course_insight/modules/m4_task_orchestration/service.py)、
  [M6 state machine](src/course_insight/modules/m6_tutoring_fsm/state_machine.py)、
  [M6 service](src/course_insight/modules/m6_tutoring_fsm/service.py) 和
  [M6 SQLite repository](src/course_insight/infrastructure/sqlite/m6_repository.py)，
  最后读 [AppCoordinator](src/course_insight/application/coordinator.py)。
- 谢：按 M1 `service.py` → M2 `service.py` → M3 `service.py` 阅读，先理解
  `CoursePackage` 和证据定位，再处理 RAG/pgvector 引用与 Q 矩阵标定依据。
- 童：先读 M8 `paper_generator.py`/`rule_scorer.py`/`service.py` 的审计身份和
  IRT/在线标定边界，再读 M5 `update_policy.py`、`aggregation.py` 与 DINA/BKT 空运行。
- 冯：先读 M7 DeepSeek 空适配器及量规/证据约束，再读 M9
  `reports.py`/`suggestions.py`、DeepSeek 叙述与模型质量门槛。

共同修改契约前先读 `src/course_insight/contracts/` 中的 Python 契约、
`contracts/schemas/` 和 `contracts/contract_provenance.json`，并保持生产者—消费者
边界一致。

## 当前能力状态与后续落点

| 模块 | 当前可运行行为 | 真实实现与启用条件 |
|---|---|---|
| M0 | 配置、日志、SQLite/PostgreSQL、真实 Django/权限/表单、流程恢复、leased Worker | 在目标环境完成生产容量、备份与真实 PostgreSQL 验收 |
| M1 | 仅固定本地文本解析与段落切分 | 在 parser registry 后增加可替换解析器 |
| M2 | 词法匹配基线；pgvector 逻辑索引/审计为 `empty` | 实现 embedding 适配器、pgvector 迁移/重建与检索评估 |
| M3 | 只接受教师确认 JSON，不自动抽取知识 | schema validator 后的教师审核工作流 |
| M4 | 五类规则识别、私有 SHA-256 决策重放、显式蓝图映射和 SQLite 原子复用；可选 adapter 默认关闭 | 经离线与 shadow 门禁后启用可信 adapter，但保持 84 个公开契约、`TaskPlan` 和八字段业务身份 |
| M5 | 现有可解释更新；DINA/BKT 契约返回空概率 | 数据质量门槛后在 M5 实现可版本化 DINA/BKT 引擎 |
| M6 | 8 条安全迁移、确定性 baseline、rules/shadow/active、纯 Python LinUCB、版本化制品、奖励/OPE、双后端持久化和 M0 七字段冻结；默认 rules/零 rollout/零探索 | 先完成真实教学数据治理、shadow 观察、OPE 审核和受控 rollout；当前不声称 active 可生产启用或优于 baseline |
| M7 | `PlaceholderRubricAdapter` 未配置时抛出 `MODEL_ADAPTER_UNCONFIGURED`；DeepSeek 适配器返回 `empty` | 安全、超时、限流和输出校验完成后在 M7 启用 DeepSeek API |
| M8 | 固定 anchor/规则评分；IRT 标定与自适应选题为 `empty` | 足量数据下实现 IRT shadow 标定，经 M9 质量/教师审核后启用 |
| M9 | 阈值统计/规则建议；DeepSeek 叙述 `empty`；质量 `insufficient_data` | 实现模型指标与标定审核；在 M9 启用 DeepSeek 教师叙述 |

M6 私有 OPE/approval 尚未正式接入 M9；当前 M9 公共质量入口只接收 M8
`CalibrationRunResult`。公共 84 个 schema、`decide_next_action(...)` 四输入签名
和 contract provenance 均未为 M6 policy learning 改动。

后续实现必须保留 84 个契约、10 个服务和 `AppCoordinator` 的责任边界，
不得把密钥、日志、真实运行数据或主机路径写入可分发项目文件。

## 禁止事项与安全边界

- 禁止跨模块读表、改变权威契约名或把契约转成临时字典跨模块传递。
- 禁止核心内部 HTTP、路由装饰器、网络客户端和绕过 `AppCoordinator` 的编排。
- 禁止真实姓名、学号、邮箱、电话、身份映射、密钥和真实 `.env`。
- 禁止 DeepSeek 以外的 LLM、本地模型权重、硬编码 `DEEPSEEK_API_KEY`，以及未经教师审核的高风险自动评分。
- 智能空实现禁止访问模型网络、连接 pgvector、读取 DeepSeek 密钥或伪造
  DINA/BKT/IRT/模型质量指标；M0 仅按显式配置连接 SQLite/PostgreSQL。
- 禁止把数据库、日志、索引、快照、模型文件或真实课程资料写入受管数据目录。
- 所有路径、JSON/CSV、时间、引用、分数和版本都必须在系统边界校验；错误只
  返回稳定代码和相对/安全信息。

复用现有 Pydantic 契约，补齐鉴权、限流、错误映射和审计，且不能让
M1—M9 依赖 HTTP/ORM/SDK 类型。
