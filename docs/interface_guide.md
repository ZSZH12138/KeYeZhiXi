# 模块接口指南

本指南说明 M0–M9 的生产者、消费者和调用边界。字段级 JSON Schema 位于 `contracts/schemas/`，机器可检验的来源映射位于 `contracts/contract_provenance.json`。

## 1. 通用规则

- 跨模块传递 Pydantic 契约对象，不传 Django ORM、Request、Session 或无类型字典；
- 每个模块只访问自身仓储和表前缀；
- 跨模块流程只由应用层或 `AppCoordinator` 编排；
- 公开契约拒绝额外字段，时间必须带时区，版本和 checksum 必须稳定；
- 同一业务身份同一 payload 幂等成功，同一身份不同 payload 明确冲突；
- 运行时私有模型不得为了方便加入公共契约。

## 2. M0 平台接口

M0 生产：

- `ActorContext`：已鉴权的账户、角色和课程班级作用域；
- `EventAck`：学习事件写入结果；
- `AsyncJobStatus`：兼容脚手架的作业状态；
- Django 页面、表单、会话、权限和 Worker 命令。

当前 Web 还包含公共契约之外的内部模型：账户类型、班级工作区、成员关系、课程文件版本、知识入库任务、活动知识发布版本、画像投影和删除文件队列。这些对象只停留在 M0/Application/M1–M3 的内部边界。

账户管理使用外部 `username` 查找用户、内部 `actor_id` 清除模块数据。教师班级管理必须以 `CourseClassWorkspace.owner_teacher` 和活动 `ClassMembership` 为权威范围。

## 3. 核心契约流

| 契约 | 生产者 | 主要消费者 |
|---|---|---|
| `CoursePackage` | M1 | M2、M3 |
| `EvidenceIndexRef` | M2 | M2 检索、应用层恢复 |
| `EvidenceQuery` | M6、M8 | M2 |
| `EvidenceBundle` | M2 | M7、M9 |
| `KnowledgeBundle` | M3 活动发布版本投影 | M4、M5、M8、M9 |
| `TaskPlan` | M4 | M6、M8 |
| `AssessmentPaper` | M8 | M0 学生页面、M8 评分 |
| `ScoringPreparationResult` | M8 | M2、M7、M8 |
| `RubricScoringTask` | M8 | M7 |
| `RubricScoringResult` | M7 | M8 |
| `ScoringResultBundle` | M8 | M0、M5、M6、M9 |
| `DiagnosisResult` | M5 | M6、M8、M9 |
| `LearnerStateSnapshot` | M5 | M4、M6、M8、M9 |
| `ClassStateSnapshot` | M5 | M9 |
| `StateUpdateResult` | M5 | M6、M9 |
| `TutoringControlResult` | M6 | M2、M7、应用层 |
| `FeedbackGenerationTask` | M6 | M7 |
| `StudentFeedbackPackage` | M7 | M0 学生页面 |
| `TeacherAnalyticsBundle` | M9 | M0 教师页面 |
| `TeacherReviewDecision` | M9 | M8 |
| `EventAck` | M0 | 应用层 |
| `ArchitectureScaffoldResult` | `AppCoordinator` 兼容入口 | 离线架构探测 |

公开根契约的精确数量以 `contracts/schemas/` 和 provenance 校验为准，不在叙述文档中复制逐字段清单。

## 4. 知识入库内部流

当前生产知识入口是 `KnowledgeIngestionProcessor`，不是旧知识包审批页：

```text
M0 文件版本/变更集
  -> M1 解析和分块
  -> M7 KnowledgeExtractionBatch/Result
  -> M3 概念合并、题目解析、Q 链接和候选发布
  -> M2 证据索引
  -> M3 CourseKnowledgeRelease 原子发布
  -> KnowledgeBundle 兼容投影
```

重要内部对象：

| 对象 | 约束 |
|---|---|
| `KnowledgeExtractionBatch` | 有界可见文本批次，不携带任意本地路径 |
| `KnowledgeExtractionResult` | 零到多个候选，引用必须来自输入白名单 |
| `KnowledgeEvidenceRef` | 来源版本、块、定位和 quote 全部本地校验 |
| `MergedKnowledgeConcept` | 同义概念合并时保留来源并集 |
| `QuestionConceptLinkCandidate` | 只能引用当前候选概念 ID |
| `CourseKnowledgeRelease` | 不可变发布版本，活动指针原子切换 |

旧 `TeacherReviewWorkflow` 和 `build_knowledge_bundle_after_approval` 只用于兼容已有迁移与受测接口，不连接当前教师页面。

## 5. 学习任务流

M4 的 `create_task_plan(...)` 是任务规划入口，支持：

- `qa`
- `diagnostic`
- `practice`
- `correction`
- `stage_assessment`

答疑流：

```text
M4 TaskPlan -> M2 检索 -> M7 答疑/反馈 -> M6 辅导控制
```

测评流：

```text
M4 TaskPlan -> M8 冻结试卷 -> M8/M2/M7 评分
            -> M5 状态更新 -> M6 辅导 -> M9 分析
```

任务身份冻结课程、班级、学习者、会话、任务类型、知识发布、课程包和蓝图。恢复必须读取同一版本，不得以当时最新发布或最新画像替换。

## 6. 多请求 Web 用例

| 应用用例 | 作用 | 权威数据 |
|---|---|---|
| `start_assessment` | 创建 TaskPlan、AssessmentPaper 和流程索引 | M4、M8、M0 |
| `submit_assessment` | 恢复试卷并执行评分、状态、辅导、反馈与分析 | M4–M9 |
| `get_student_assessment` | 按本人作用域读取结果 | M8、M7 |
| `get_teacher_review_context` | 按教师课程班级读取复核上下文 | M8、M5、M9 |
| `review_assessment` | 追加教师复核和新评分版本 | M8、M5、M9 |

M0 流程表只保存关联 ID、checkpoint、租约和冻结引用，不复制完整领域对象。长调用使用 CAS lease 和 heartbeat；失去租约的调用方不能继续推进或写终态。

## 7. 评分与复核接口

M8 从冻结试卷生成客观评分或 `RubricScoringTask`。M7 的语义评分只返回受约束 JSON、置信度、理由和允许的证据引用。M8 将结果写为追加式评分审计。

当前教师页面使用逐题改判表单：服务端读取最新审计身份，只接受该题合法分值和可选备注。公共 `TeacherReviewSubmission` 的 `confirm/override/reject` 仍供兼容服务使用，不代表当前页面按钮。

低置信度建议复核按一次作答聚合。教师改判后，M5/M9 和学生反馈按新审计版本幂等更新。

## 8. M6 私有策略边界

公开 `M6TutoringControlService.decide_next_action(...)` 仍接收 `TaskPlan`、`ScoringResultBundle`、`StateUpdateResult` 和可选会话快照，返回 `TutoringControlResult`。

策略制品、执行引用、观测、奖励和 OPE 记录是 M6 私有模型，不进入公共 Schema。默认 `rules` 模式不读取 learned artifact；`shadow` 只记录建议；`active` 还需 manifest、checksum、作用域、支持度、不确定性、OPE、rollout 和 kill switch 全部通过，任何异常回退 rules。

详情见 [m6_policy_operations.md](m6_policy_operations.md)。

## 9. 模型与检索边界

- M2 正式入口为 `retrieve_with_policy`；旧 `retrieve` 只供已受测的 lexical 兼容调用；
- SQLite 使用 lexical 基线；生产 vector/hybrid 使用 PostgreSQL + pgvector；
- M7/M9 provider 固定为 DeepSeek；无密钥或隐私/质量门不满足时失败关闭；
- 模型输出不能直接修改正式分数、状态、建议或权限；
- 公开模型契约只保存校验和、稳定状态和受限审计，不保存 API key、完整 prompt 或 provider 原始响应。

## 10. 运维命令边界

- `python manage.py sync_roles --check|--dry-run|--apply`：管理员引导与权限同步；
- `python manage.py run_ingestion_worker [--once]`：知识入库；
- `python manage.py run_outbox_worker [--once]`：事件投递；
- `python manage.py cleanup_erasure_files [--limit N]`：重试账户注销后的文件清理；
- `python scripts/migrate_sqlite_to_postgres.py ...`：显式 SQLite 到 PostgreSQL 导入；
- `python scripts/export_schemas.py`：生成公开 JSON Schema。

命令行工具必须复用同一配置加载器、路径边界和稳定错误语义，不得接受任意数据库或文件删除目标。
