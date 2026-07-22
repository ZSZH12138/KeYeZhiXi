# M6 严格契约辅导状态机设计

## 状态

- 日期：2026-07-22
- 状态：设计已批准，书面规范待用户复核
- 实施方案：方案 A，保留现有自定义有限状态机
- 分支：`codex/m6-tutoring-fsm`

## 目标

在不修改 84 个公共契约、其他模块实现、公开服务签名、
`contract_provenance.json` 或 `AppCoordinator` 调用接口的前提下，补齐 M6 的：

- 确定性教学决策；
- 证据支持的目标知识点选择；
- 稳定、版本化的动作与模板引用；
- M6 自有会话历史持久化；
- 跨重试、重启和并发的幂等控制；
- S4 到 S5 的新学习证据门槛；
- 状态、持久化、并发、跨模块与安全测试。

## 审计结论

修改前基线为 Python 3.12.13，`pytest -q` 通过 77 项测试，
`compileall -q src scripts tests` 退出码为 0。公共契约、公开服务、provenance
和 Coordinator 的类型边界可以对齐，没有必须修改公共契约才能继续的阻断冲突。

Coordinator 当前首次调用 M6 时传入空会话历史。M6 可以依据
`TaskPlan.session_id` 从自身 Repository 读取权威快照，所以不需要修改
Coordinator。新会话保持现有真实语义：以 S1、`turn_count=0` 作为首次决策前状态；
S0 仍保留完整合法迁移和测试语义。

## 范围边界

### 允许修改

- `src/course_insight/modules/m6_tutoring_fsm/` 内部实现；
- M6 Repository Protocol 和零依赖内存替身；
- `src/course_insight/infrastructure/sqlite/` 中的 M6 适配器；
- 统一 migration 中仅属于 M6 的新增表和 schema version；
- M6 单元、持久化、并发和跨模块测试；
- M6 README 以及确有必要的直接说明。

### 明确不修改

- `src/course_insight/contracts/` 中的公共类、字段和业务语义；
- M0—M5、M7—M9 的实现；
- `M6TutoringControlService.decide_next_action(...)` 的公开签名；
- `contracts/contract_provenance.json`；
- `src/course_insight/application/coordinator.py`；
- M4 固定工作流和 `TaskPlan.course_package_id` 的权威来源。

## 方案比较与决定

### 采用：快照表与决策表分离

继续使用 `m6_session_states` 保存每轮 `SessionStateSnapshot`，新增 M6 自有
`m6_tutoring_decisions` 保存规范输入身份、证据身份和完整
`TutoringControlResult`。SQLite 使用 `BEGIN IMMEDIATE` 和唯一约束实现
insert-or-get。

优点是快照历史与幂等决策各自职责单一，可以原子检查上一轮、可靠处理 20 路并发，
也不需要改变公共契约。

### 不采用：扩展快照表承载全部决策信息

该方案会把状态历史、输入身份、证据水位和完整输出混在同一张表中，迁移和查询语义
更难审计。

### 不采用：只把指纹放进 JSON payload

JSON 内字段没有数据库唯一约束，无法可靠证明 20 路并发只产生一个权威轮次。

## 内部组件

### `state_machine.py`

只维护唯一状态图并验证候选迁移。合法迁移固定为：

```text
S0 -> S1
S1 -> S2 | S3
S2 -> S3
S3 -> S4
S4 -> S2 | S3 | S5
S5 -> terminal
```

状态图不在其他文件复制。非法状态或迁移统一产生现有
`INVALID_STATE_TRANSITION`。

### `decision_policy.py`

定义冻结的 `M6DecisionPolicy` 和 `DecisionSignals`。v1 默认值为：

```text
policy_version = "m6-deterministic-v1"
weak_mastery_threshold = 0.8
stable_correction_threshold = 0.8
mastery_confidence_threshold = 0.6
maximum_hint_dependency = 0.0
active_misconception_threshold = 0.5
```

这些值只属于 M6 内部策略，不进入公共契约。默认值与当前 M5 输出尺度对齐：活跃/非活跃
误区强度为 0.6/0.2，目标概念置信度为 0.6，待复核提示依赖为 0.5，稳定无提示依赖为
0.0。

决策表为：

| 当前状态 | 条件 | 下一状态 |
|---|---|---|
| S0 | 当前调用完成澄清阶段 | S1 |
| S1 | 待复核、活跃误区或前置缺口 | S2 |
| S1 | 无上述情况 | S3 |
| S2 | 完成最小提示阶段 | S3 |
| S3 | 完成引导阶段 | S4 |
| S4 | 待复核、活跃误区或前置缺口 | S2 |
| S4 | 无新证据，或订正/置信/提示依赖未达标 | S3 |
| S4 | 有新证据且所有稳定条件达标 | S5 |
| S5 | 任意继续推进 | 拒绝 |

### `target_selector.py`

候选目标按以下顺序稳定合并和去重：

1. `DiagnosisResult.priority_concept_ids`；
2. `ItemDiagnosis.prerequisite_gap_ids`；
3. `RemediationPlan.ordered_targets()` 中 `is_high_priority()` 为真的目标；
4. `LearnerStateSnapshot.weak_concepts(0.8)`。

`FeedbackGenerationTask` 的公共契约要求动作目标必须被当前诊断支持，因此最终候选还必须
同时存在于 `DiagnosisResult.item_diagnoses[].concept_ids`。前置缺口、补救目标或弱知识点若
没有当前诊断支持，不会被强行塞入反馈任务。无可靠目标时使用现有
`TUTORING_REFERENCE_MISMATCH` 安全失败，不再回退到 `concept_states[0]`。

每个最终目标还必须存在对应 `ConceptState`，否则按身份不一致拒绝，不能为缺失状态推造
掌握数据。

### `identity.py`

所有身份使用规范 JSON、UTF-8、排序键和 SHA-256。禁止随机数、当前时间、对象地址、
Python `hash()` 或主机路径。

内部维护两个身份：

- `request_fingerprint`：任务、评分、状态和调用方传入快照的 checksum；用于同一 API
  请求重放。
- `input_fingerprint`：任务、评分、状态和 Repository 解析出的权威上一快照 checksum；
  用于权威决策唯一性。

`decision_id`、`action_id`、`query_id` 和 `feedback_task_id` 从
`input_fingerprint` 派生。同一权威输入始终产生相同 ID。

### `action_factory.py`

动作按目标状态映射：

| 目标状态 | `action_type` | `prompt_template_id` |
|---|---|---|
| S1 | `diagnostic_probe` | `m6.s1.diagnostic_probe.v1` |
| S2 | `minimal_hint` | `m6.s2.minimal_hint.v1` |
| S3 | `guided_question` | `m6.s3.guided_question.v1` |
| S4 | `self_explanation_prompt` | `m6.s4.self_explanation.v1` |
| S5 | `summary_and_transfer` | `m6.s5.summary.v1` |

所有动作的 `must_not_reveal_answer` 恒为 `True`。`reason` 只记录策略版本、状态和
结构化信号类别，不写答案、作答正文、真实身份或完整量规。

每次动作生成一个 `EvidenceQuery`：

- `course_package_id` 从 `TaskPlan` 原样复制；
- `concept_ids` 使用确定性目标；
- `use_case="feedback"`；
- 无权威题卡 ID 时 `item_id=None`；
- 查询文本使用固定的安全说明，请求所选概念的课程规则、辨析和例子；概念 ID 仅放在
  `concept_ids` 字段，不拼接进自由文本；
- 沿用当前边界的 `top_k=3` 与 `min_relevance=0.2`。

`FeedbackGenerationTask` 使用当前任务、学习者、诊断、总分/满分、学习状态快照 ID 和
查询 ID，不复制学生答案或标准答案。

## 会话权威规则

服务在生成决策前依次处理：

1. 先按 `request_fingerprint` 查找既有决策；命中则校验并原样返回。
2. 读取 Repository 中该 `session_id` 的最新权威快照。
3. 调用方和 Repository 都为空：创建 S1、turn 0 的决策前状态。
4. 仅 Repository 有值：使用 Repository 最新快照。
5. 仅调用方有值：校验后将其作为待持久化的上一快照。
6. 两者都有且 checksum 完全一致：继续。
7. 两者都有但 session、turn、state、action 历史或 checksum 不一致：使用现有
   `TUTORING_REFERENCE_MISMATCH` 拒绝，绝不覆盖权威历史。

有效推进创建全新的快照对象，`turn_count` 恰好加一，动作 ID 追加且不重复。服务不修改
调用方传入的对象。

## 新证据身份与 S4 门槛

每条内部决策记录保存一份规范 `EvidenceIdentity`，至少包含：

- `ScoringResultBundle` checksum；
- 最新评分审计的 `(audit_id, audit_version)`；
- `StateUpdateResult.processed_audit_ids`；
- `LearnerStateSnapshot.state_version` 和 checksum。

“有新证据”仅在状态版本上升，或出现未在上一决策中使用的新审计版本/处理水位时成立。
同一状态版本下内容 checksum 改变视为冲突，而不是新证据。没有上一条 M6 决策记录时，
S4 不能证明存在相对新增证据，因此回到 S3。

稳定订正要求所有最终目标同时满足：

- 当前无待教师复核；
- 当前无诊断误区或活跃误区；
- 当前无前置缺口；
- `recent_correction_rate >= 0.8`；
- `mastery_confidence >= 0.6`；
- `hint_dependency <= 0.0`。

## SQLite 持久化

schema version 升级一版，仅新增 M6 自有表：

```text
m6_tutoring_decisions
  decision_id              TEXT PRIMARY KEY
  session_id               TEXT NOT NULL
  turn_count               INTEGER NOT NULL
  previous_turn_count      INTEGER NULL
  request_fingerprint      TEXT NOT NULL UNIQUE
  input_fingerprint        TEXT NOT NULL UNIQUE
  evidence_fingerprint     TEXT NOT NULL
  evidence_identity        TEXT NOT NULL, canonical JSON
  result_payload           TEXT NOT NULL, canonical JSON
  UNIQUE(session_id, turn_count)
  FOREIGN KEY(session_id, turn_count)
    REFERENCES m6_session_states(session_id, turn_count)
```

一次提交在一个 `BEGIN IMMEDIATE` 事务中完成：

1. 再查 request/input fingerprint，命中则读取权威结果；
2. 重新读取最新快照并验证预期上一轮；
3. Repository 为空但调用方提供合法快照时，先保存该上一快照；
4. 插入新快照；
5. 插入决策记录；
6. 读取并校验权威结果；
7. 提交。

任何异常均回滚。相同请求并发时，第一个事务写入，其余事务读取同一结果；不同请求争用
同一上一轮时，只有一个推进，另一方收到陈旧输入错误。

## 跨契约校验

M6 在现有公共契约校验之外补充：

- task、scoring、diagnosis、learner state 的 learner 一致；
- task、learner state、class state 的 course/class 一致；
- scoring attempt 与 diagnosis attempt 一致；
- 最新评分审计的 `<audit_id>:<audit_version>` 身份被 `processed_audit_ids` 覆盖；
- scoring 学习事件中的 course/class/learner 与任务一致；
- 若评分事件声明 `paper_id`，必须等于 bundle 的 `paper_id`；
- snapshot session 必须等于 task session；
- query、feedback task、action 和 session 必须通过
  `TutoringControlResult.assert_query_alignment()`。

校验只读取现有契约字段。不会把跨模块对象降级为临时字典传递，也不会增加跨模块调用。

## 安全和错误处理

- M6 不导入或调用 DeepSeek、网络客户端、M7 SDK、DINA/BKT/IRT 或 pgvector。
- 不读取 `DEEPSEEK_API_KEY` 或任何密钥。
- SQL 全部在 SQLite Repository 中，使用参数化语句。
- 结果和错误不包含主机绝对路径、真实身份、答案或完整提示词。
- 状态非法使用 `INVALID_STATE_TRANSITION`。
- 身份、陈旧快照、版本冲突和无可靠目标使用现有
  `TUTORING_REFERENCE_MISMATCH`，并给出有限、无敏感信息的 details。
- 数据库损坏或 payload 与存储身份不一致视为内部完整性失败，不静默降级。

## 测试设计

### 单元测试

- 穷举 6×6 状态组合：8 条通过，28 条拒绝；
- S5 终止、非法状态、turn 单调、动作不重复；
- S1/S2/S3/S4 的完整决策表；
- S4 到 S5 的新证据、订正、置信和提示依赖门槛；
- 四级目标来源、稳定去重、诊断支持过滤和无目标失败；
- 稳定 ID、模板映射、答案隐藏和安全查询文本。

### 持久化与并发测试

- migration 重复执行；
- 首次写入、读取最新、历史追加和重启恢复；
- 相同输入重试不推进 turn；
- 调用方/Repository 快照冲突拒绝；
- 故障回滚不留下半条快照或决策；
- 20 路并发相同请求只有一个结果 checksum、动作、查询、反馈任务和 turn；
- 不同输入争用同一上一轮只有一个成功推进。

### 跨模块与回归测试

- M4 `TaskPlan` 经 M6 产生 M2 可直接消费的 `EvidenceQuery`；
- M6 反馈任务可直接交给 M7；
- M8/M5 输出可直接交给 M6；
- `AppCoordinator.run_assessment_cycle` 不回归；
- task package ID 原样进入 query；
- 公共契约和 provenance 测试保持通过。
- 现有 M6 相关测试若使用缺字段的 `model_construct` 占位对象，将改为完整合法契约夹具，
  保留并加强原断言，不删除、跳过或弱化测试目的。

### 安全测试

- 动作、reason、query 不包含答案标记、身份或密钥；
- `must_not_reveal_answer=True`；
- M6 源码无网络、DeepSeek、模型或 pgvector 导入；
- 错误不泄漏绝对路径。

## 完成标准

- 聚焦测试、全仓测试和 compileall 全部通过；
- Schema 连续导出两次无额外 diff；
- `git diff --check` 通过；
- 如环境已有 `pytest-cov`，M6 相关覆盖率不低于 90%；否则明确报告未测，且不联网安装；
- 公共契约、其他模块实现、provenance 和 Coordinator 无意外改动；
- 不 push、不 merge，除非用户另行要求。
