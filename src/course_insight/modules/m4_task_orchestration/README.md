# M4 任务与测评编排

## 负责人

陈

## 职责

M4 将学生意图、M3 `KnowledgeBundle` 和可选 M5
`LearnerStateSnapshot` 转换为一个经过 Pydantic 正常校验、可持久化重放的
`TaskPlan`。它只决定任务类别、冻结引用和首次责任域参与顺序；不生成题目、
不评分、不更新掌握度，也不执行 M6 的 S0—S5 状态迁移。

## 公开入口

唯一规划入口保持为：

```python
create_task_plan(
    student_text: str,
    task_type_hint: str | None,
    course_id: str,
    class_id: str,
    learner_id: str,
    session_id: str,
    knowledge_bundle: KnowledgeBundle,
    learner_state_snapshot: LearnerStateSnapshot | None,
) -> TaskPlan
```

服务构造函数可通过仅关键字参数 `blueprint_by_task_type` 接收
`task_type -> blueprint_id` 映射。该映射属于 M4 路由配置，不进入公共契约。

## 确定性路由

- 支持 `qa`、`diagnostic`、`practice`、`correction`、
  `stage_assessment`。
- 合法 hint 优先；没有 hint 时使用 `routing.py` 中固定、有序的中英文关键词表。
- 无法识别或 hint 非法时返回 `UNSUPPORTED_TASK`，不调用 LLM 或网络。
- `qa` 不绑定蓝图，工作流固定为 `M2 → M7 → M6`。
- 其余四类任务绑定同课程、`teacher_approved` 的 bundle 内蓝图，工作流固定为
  `M8 → M2 → M7 → M5 → M6 → M9`。
- 多个批准蓝图并存时必须由 `blueprint_by_task_type` 明确选择；不按列表顺序、
  名称、时间或随机值猜测。无合法唯一选择时返回 `BLUEPRINT_NOT_FOUND`。

`workflow` 表示责任域首次参与顺序，不表示一个模块在整个闭环只能调用一次；
`next_module` 永远是该列表首项。

## 引用验证

M4 只接受与请求 `course_id` 一致且状态为 `published` 的知识包，并要求
`knowledge_bundle_id`、`course_package_id` 非空。可选学习者状态必须同时匹配
课程、班级和学习者。对应稳定错误码为 `KNOWLEDGE_BUNDLE_MISMATCH` 和
`LEARNER_STATE_MISMATCH`。

`TaskPlan.knowledge_bundle_id` 和 `TaskPlan.course_package_id` 均原样取自传入的
`KnowledgeBundle`；不得从 ID 字符串、文件名或其他模块业务表推导。

## 幂等与持久化

规范业务身份只包含以下八个字段：

```text
course_id, class_id, learner_id, session_id, task_type,
knowledge_bundle_id, course_package_id, blueprint_id
```

`canonical_idempotency_key` 使用排序 JSON、固定 separators、UTF-8 和 SHA-256。
学生原始措辞、当前时间和随机值不参与身份。`SQLiteM4Repository` 使用现有
`m4_task_plans.idempotency_key` 唯一约束，在一个 `BEGIN IMMEDIATE` 事务中执行
`INSERT ... ON CONFLICT DO NOTHING` 后读取权威行，因此重复、重启和并发请求均
返回首次写入的 `task_id`、`created_at` 和完整契约内容，且不覆盖历史记录。

SQLite migration 仍由应用初始化阶段统一执行；M4 service 不执行 SQL，也不让
`AppCoordinator` 接收 Repository。

## 消费关系与边界

- M8 直接消费 `TaskPlan` 生成试卷。
- M6 直接消费 `TaskPlan`，并将 `TaskPlan.course_package_id` 原样写入给 M2 的
  `EvidenceQuery.course_package_id`。
- M4 只访问自己的 `M4Repository`，不读取其他模块业务表。
- M4 不调用 Django request/ORM、HTTP、外部 SDK、DeepSeek、RAG、embedding、
  DINA、BKT 或 IRT。
