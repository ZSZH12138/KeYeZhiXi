# M6 辅导状态机

## 负责人

陈

## 职责与当前状态

M6 以 S0—S5 状态机和 `SafetyEnvelope` 作为唯一动作安全边界，依据 M8 评分审计
和 M5 诊断/学习状态选择下一教学动作，并生成交给 M2、M7 的查询与反馈任务。
状态机不运行 DINA、BKT、IRT 或 LLM，也不发起网络请求。

当前代码还实现了 M6 私有的 `rules`/`shadow`/`active` 策略运行时、JSON-only
LinUCB 制品、奖励、去标识离线数据、OPE 和 SQLite/PostgreSQL 持久化。默认配置
仍是 `rules`、`rollout_percentage=0.0`、`exploration_rate=0.0`；本阶段没有真实
教学训练、线上 rollout 或 live PostgreSQL migration，不能声称 active 已可生产
启用，也不能声称学习策略优于确定性 baseline。

## 公共接口与私有边界

- 公开输入：M4 `TaskPlan`、M8 `ScoringResultBundle`、M5
  `StateUpdateResult`，以及可选的前版 `SessionStateSnapshot`。
- 公开输出：`TutoringControlResult`。其中 `TaskPlan.course_package_id` 原样进入
  M2 `EvidenceQuery`，`FeedbackGenerationTask` 直接进入 M7。
- `M6TutoringControlService.decide_next_action(...)` 的四输入签名、84 个公共
  schema 和 contract provenance 均未改变。
- `prepare_policy_execution(...) -> PolicyExecutionRef` 是 M0/应用层内部的
  first-writer 冻结入口；直接调用公开方法时会惰性准备同一 binding。
- `CandidateAction`、`TutoringPolicyContext`、manifest、execution、observation、
  reward 和 evaluation 都是 M6 私有 dataclass，不进入 `contracts/`。

所有 action/query/feedback/decision ID 都由 canonical JSON 与 SHA-256 派生；
查询自由文本不拼接概念 ID，教学动作恒为 `must_not_reveal_answer=True`。

## 确定性 baseline 与会话

真实应用链首次调用 M6 时没有历史快照。M6 以 `S1`、`turn_count=0` 建立会话
游标；S0 仍保留完整的合法迁移和测试语义。Repository 已有历史时，即使调用方
继续传入 `None`，M6 也会恢复最新权威快照。

- S1 在教师待复核、活跃误区或前置缺口时进入 S2，否则进入 S3。
- S2 固定进入 S3，S3 固定进入 S4。
- S4 有补救信号时回到 S2；缺少新证据、仍有诊断误区或稳定指标未达标时回到
  S3；只有新证据、纠正率、掌握置信度和提示依赖全部达标时进入 S5。
- S5 为终态，继续迁移返回 `INVALID_STATE_TRANSITION`。

目标依次取诊断优先概念、前置缺口、高优先补救目标和弱概念，稳定去重后只保留
当前诊断支持且具有 `ConceptState` 的概念。无可靠目标时返回
`TUTORING_REFERENCE_MISMATCH`，不会盲目选择首个概念状态。

## SafetyEnvelope、版本与 LinUCB

当前固定版本是：

- baseline `m6-deterministic-v1`
- feature `m6-features-v1`
- action space `m6-action-space-v1`
- reward `m6-reward-v1`
- 默认 active gate `m6-active-gate-v1`

`m6-action-space-v1` 恰好覆盖状态图的 8 条迁移：

```text
m6.transition.s0_to_s1.v1
m6.transition.s1_to_s2.v1
m6.transition.s1_to_s3.v1
m6.transition.s2_to_s3.v1
m6.transition.s3_to_s4.v1
m6.transition.s4_to_s2.v1
m6.transition.s4_to_s3.v1
m6.transition.s4_to_s5.v1
```

`m6-features-v1` 是固定顺序的 23 维有限数值向量：S0—S5 one-hot、五种 task
type one-hot、turn/得分比例/目标数、五个决策信号、最低订正率、最低掌握
置信度、最高提示依赖和学习者证据数。它不包含答案、自由文本或真实身份。

LinUCB 用纯 Python 计算
`theta·x + alpha*sqrt(xᵀA⁻¹x)`，拒绝非有限值、负不确定性、维度或动作版本不符。
epsilon 由 request fingerprint 的 SHA-256 确定性抽样；探索上限为 0.05。教师
待复核、诊断误区、活动误区、前置缺口、单候选等场景不探索。

## rules、shadow 与 active

- `rules`：不读取 manifest、artifact 或 evaluation，公共结果保持 baseline；
  rules execution 的 artifact SHA 为 NULL。
- `shadow`：加载学习制品并记录推荐、分数和不确定性，但 public/chosen action 和
  logging propensity 仍属于 one-hot rules baseline；模型推荐仅写私有
  `shadow_action_id`。
- `active`：只有全部门禁通过时才可采用学习候选，否则返回 rules baseline 并记录
  rejection/fallback reasons。

Active gate 同时要求 approved manifest、artifact SHA 和 feature/action/gate
version 精确匹配、至少两个安全候选、足够 observation support、动态 uncertainty
达标、精确 policy/dataset OPE 已批准、course/class 同时命中配置 allowlist 与
manifest scopes、非补救场景、request/context 一致、命中 rollout 且 kill switch
未开启。任一 artifact、预测、门禁、Repository 或持久化异常都不能产生越界动作。

## Manifest 与 artifact

M6 Repository 按配置 `policy_id` 读取 immutable `PolicyArtifactManifest`。Manifest
冻结 policy/adapter/algorithm/state graph/baseline/feature/action/reward/gate
version、训练水位/checksum、artifact SHA/status/time、相对 artifact 引用和作用域。
状态仅允许 `draft|shadow|approved|rejected|retired`。

Artifact 必须在 `m6_policy.runtime_directory` 下，是无重复键、无 NaN/Infinity、
UTF-8、sorted-key compact canonical JSON；精确包含 policy/adapter/feature/action
身份、`dimension`、`alpha` 和全部 action 的 `theta`/`inverse_covariance`。
绝对路径、`..`、符号链接逃逸、非 JSON、SHA/版本/维度不符全部 fail closed。
代码不加载 pickle/joblib，不动态执行 artifact。

## 持久化、幂等与并发

`m6_session_states` 追加保存每轮快照，`m6_tutoring_decisions` 保存 request/input
fingerprint、证据水位和完整结果。M6 先以 request key insert-or-get immutable
`PolicyExecutionRef`；随后 `PolicyObservation` 与对应 tutoring decision 在同一
权威事务提交。SQLite 使用 `BEGIN IMMEDIATE`；PostgreSQL 使用等价
insert-or-get/约束。相同请求、重启和并发只产生一个 first-writer execution 和一个
权威决定；同 ID 不同 payload、陈旧游标或 observation/decision 身份不一致会 fail
closed。

独立 `save_session_state()` 的首条必须是 turn 0，后续必须连续扩展动作历史并遵守
合法状态迁移。空 Repository 仍可在 `commit_decision()` 同一事务保存调用方提供且
已校验的上一快照，再恰好推进一轮。

## Schema v3、v10 与 v11

M6 不在业务服务内嵌 migration；SQLite 统一由
`infrastructure.sqlite.migrations.migrate()` 执行，PostgreSQL 使用 checksum-locked
core migrations。

- v3：增加 `m6_tutoring_decisions`，保留既有 session state。
- v10/`0010_m6_policy_learning.sql`：增加
  `m6_policy_artifacts`、`m6_policy_executions`、
  `m6_policy_observations`、`m6_policy_rewards`、
  `m6_policy_evaluations`。
- v11/`0011_m0_policy_freeze.sql`：只为 `m0_assessment_runs` 安全追加七个
  policy freeze 字段。

M6 policy 表仍属于 schema v10；0010 和 SQLite v10 migration 保持不变。当前
bundled schema 总版本是 v11，因为 M0 freeze 是后续追加。数据库高于应用支持版本
时拒绝启动；项目不自动破坏性降级，回退旧应用前必须停写并恢复匹配备份。

## M0 七字段冻结与恢复

submit checkpoint 在 `state_saved` 和 `tutoring_saved` 之间增加
`policy_frozen`，恰好保存：

```text
policy_id
adapter_id
adapter_version
artifact_sha256
feature_schema_version
action_space_version
gate_policy_version
```

rules 可省略 artifact SHA；shadow/active learned binding 必须带 lowercase
SHA-256。若 M6 first-writer 已提交但 M0 checkpoint 尚未保存，恢复再次 prepare
并取得同一 binding；到达或越过 `policy_frozen` 后必须七项逐字段一致，才能继续
决定。v10 历史 submit 仅在七字段全 NULL、
`tutoring_saved|feedback_saved|analytics_saved`、已有状态和 frozen prior-state
标记时允许一次 CAS adoption；部分字段、review 或 `policy_frozen` 行不猜测修复。

## Reward、离线数据与 OPE

`m6-reward-v1` 对 observed outcome 计算：

```text
transfer_success - 0.05 * hint_count - 0.10 * loop_count
```

无后续证据时保留 `pending`/`censored`；safety-invalid 记录没有 ordinary scalar
reward，不能进入普通评估。JSONL 使用显式字段 allowlist，并以至少 32 bytes
运行时 key 对 decision/session/group 做 HMAC-SHA256；不导出原始身份或 shadow
action。group/session 不跨 train/evaluation，evaluation 必须位于严格更晚的完整
group 时间边界。

OPE 输出 IPS、SNIPS、DM、DR、确定性 bootstrap CI、ESS、action/support coverage
与 state/group slices。缺失或过低 propensity、数据少、覆盖不足或 ESS 过低时
返回 `insufficient_data`，不得批准 active。OPE 依赖离线假设，不是因果证明。

## M9 边界

M6 OPE/approval/gate 尚未正式接入 M9。当前公共
`M9TeacherAnalyticsService.build_model_quality_report(...)` 只接收 M8
`CalibrationRunResult`；不得把 M6 私有 `approved=true` 表述成 M9 审核或监控。

详细配置、promotion、rollback、kill switch、JSONL/OPE 和部署检查见
[`docs/m6_policy_operations.md`](../../../../docs/m6_policy_operations.md)。

## 禁止事项

不得非法跳转、扩张安全动作、覆盖历史、泄漏标准答案/自由文本/真实身份、跳过
证据查询、把 HMAC key 或数据库 secret 写入制品、加载 pickle/joblib、使用绝对
artifact 路径、调用 DeepSeek/其他模型、手工 SQL 覆盖 immutable policy 记录，
或越权读写其他模块的数据表。
