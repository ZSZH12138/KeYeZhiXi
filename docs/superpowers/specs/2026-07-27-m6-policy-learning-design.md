# M6 策略学习与版本化适配器设计

## 状态

- 日期：2026-07-27
- 状态：已实施；默认关闭在 rules/零 rollout/零探索，尚未完成真实教学或线上验证
- 基线：当前 `main` 最新代码

## 目标

在不修改 84 个公共 Pydantic 契约、现有
`M6TutoringControlService.decide_next_action(...)` 签名、契约 provenance 和
M6 原有首写者/重放语义的前提下，为 M6 增加：

- 确定性安全候选动作空间；
- `rules`、`shadow`、`active` 三种运行模式；
- 版本化、可校验、可回滚的策略适配器与制品；
- 受安全包络约束的 LinUCB 与 epsilon 探索；
- 策略执行、观测、奖励和离线评估的私有持久化；
- M0 工作流对实际策略执行身份的内部冻结。

## 已裁决的边界

### M9

M9 现有公开质量报告接口只接收 M8 的 `CalibrationRunResult`。本次不改变
M9 公共接口或 provenance；M6 的 OPE、门禁和批准状态保留为 M6 私有能力，
并在文档中明确“尚未正式接入 M9”。

### M0 / M6 冻结握手

保持 `decide_next_action(...)` 的公开签名不变。M6 新增内部
`prepare_policy_execution(...)`，以现有 `request_fingerprint` 为键执行
first-writer 策略绑定并返回私有 `PolicyExecutionRef`。M0 在
`state_saved -> tutoring_saved` 之间调用该方法并把策略身份冻结到
`AssessmentRun`；重启恢复时必须重新得到完全相同的绑定。

不经过 M0 工作流的 Coordinator/直接调用由
`decide_next_action(...)` 内部惰性执行相同绑定，因此不会出现第二套语义。

最终 migration 策略采用安全追加：M6 五张私有 policy 表仍属于 SQLite/
PostgreSQL schema v10，`0010_m6_policy_learning.sql` 与 SQLite v10 migration
保持不变；M0 freeze 以 SQLite schema v11 和
`0011_m0_policy_freeze.sql` 追加。submit checkpoint 在 `state_saved` 与
`tutoring_saved` 之间增加 `policy_frozen`，恰好冻结 `policy_id`、
`adapter_id`、`adapter_version`、`artifact_sha256`、
`feature_schema_version`、`action_space_version`、`gate_policy_version`。
rules artifact 可为 NULL，learned binding 必须有 lowercase SHA-256。

恢复在 M6 first-writer 已提交但 M0 checkpoint 尚未保存时重新 prepare 并取得
同一 binding；到达或越过 `policy_frozen` 后七字段必须完全一致。从 v10 升级的
历史 submit 仅在七字段全 NULL、checkpoint 为
`tutoring_saved|feedback_saved|analytics_saved`、已有保存状态和 frozen
prior-state 标记时允许一次 CAS adoption。

## 安全不变量

- 公共状态图仍是唯一迁移真相：
  `S0→S1`、`S1→S2|S3`、`S2→S3`、`S3→S4`、
  `S4→S2|S3|S5`、`S5` 终止。
- `SafetyEnvelope` 先根据状态和诊断信号形成合法候选；策略只能在候选内排序，
  不能扩张动作空间。
- 待教师复核、活动误区、前置缺口等补救场景始终禁用探索。
- 任意制品加载、预测、门禁或持久化失败都不允许产生越界动作；运行时回退到
  当前确定性规则策略。
- `rules` 模式不加载可选模型制品，且公共结果必须与增强前逐字段一致。
- 现有 request/input fingerprint 的定义不变；新增独立
  `policy_execution_fingerprint` 记录策略身份。
- 新执行的 `policy_execution_fingerprint` 使用固定顺序的七字段 preimage：
  `input_fingerprint`、`adapter_id`、`adapter_version`、`artifact_sha256`、
  `feature_schema_version`、`action_space_version`、`gate_policy_version`；
  rules 的 artifact 位置显式为 `null`。旧 execution/observation/reward canonical
  JSON 和 checksum 身份保持不变。
- 制品只允许规范 JSON，不反序列化 pickle/joblib，不允许绝对路径或目录穿越。
- 所有概率和模型数值必须有限，维度、动作空间和特征版本必须完全匹配。
- 训练/评估数据不得包含答案、自由文本、真实身份、密钥或主机路径。

## 私有领域模型

新增冻结 dataclass：

- `CandidateAction`
- `TutoringPolicyContext`
- `PolicyPrediction`
- `PolicyDecision`
- `PolicyExecutionRef`
- `PolicyArtifactManifest`
- `PolicyGateResult`
- `PolicyObservation`
- `PolicyOutcome`
- `PolicyRewardRecord`
- `PolicyEvaluationRecord`

它们不进入 `src/course_insight/contracts/`，使用规范 JSON 和 SHA-256 建立身份。

## 决策流水线

1. 校验现有四个公共输入并计算原有 `request_fingerprint`。
2. 命中原有决策重放时原样返回。
3. 解析权威上一轮快照、目标概念、证据水位和 `DecisionSignals`。
4. `SafetyEnvelope` 生成有序候选动作。
5. `FeatureBuilder` 生成固定顺序的 `m6-features-v1` 数值向量。
6. 按 request key 读取或首写 `PolicyExecutionRef`。
7. `rules` 直接选择原规则结果；`shadow` 仅记录候选预测；
   `active` 只有通过全部门禁才可采用候选策略。
8. 状态机再次验证最终状态，沿用原 ActionFactory 构造公共结果。
9. 在一个仓储事务内提交会话快照、原决策记录和策略观测。

候选的稳定顺序与当前规则决策保持一致。并列分数保持
`SafetyEnvelope` 提供的稳定候选顺序；禁止时间、进程随机数、Python `hash()`
或对象地址参与决策。

## 特征、模型与探索

`m6-features-v1` 只使用已有结构化输入，包括：

- 当前状态 one-hot；
- 任务类型 one-hot；
- turn、得分比例、目标数量；
- 教师复核、诊断误区、活动误区、前置缺口、新证据标志；
- 最低订正率、最低掌握置信度、最高提示依赖；
- 学习者证据数量。

LinUCB 使用纯 Python 实现，制品保存每个动作的 `theta`、逆协方差矩阵、
维度和 `alpha`。得分为
`theta·x + alpha*sqrt(xᵀA⁻¹x)`。模型层输出确定性胜者，
epsilon 包装器用 request identity 的 SHA-256 确定性抽样并记录真实 propensity：

- 胜者：`(1-epsilon) + epsilon / n`
- 其他候选：`epsilon / n`
- 单候选：`1.0`

## 版本与门禁

制品 manifest 至少冻结：

- `policy_id`
- `adapter_id` / `adapter_version`
- `artifact_sha256`
- `feature_schema_version`
- `action_space_version`
- `gate_policy_version`
- 状态：`draft|shadow|approved|rejected|retired`
- 相对 artifact 引用和允许作用域

active 门禁要求 approved、校验和/版本匹配、候选数至少 2、支持度和不确定性
达标、离线评估批准、manifest 与配置的 course/class 双重作用域允许、rollout
命中且 kill switch 未开启。运行时使用 evaluation 的真实 observation count，
并要求精确 policy/dataset 记录包含完整 IPS/SNIPS/DM/DR 与对应 confidence
interval，缺失时 fail closed。

## 最终观测与执行审计

新 `PolicyObservation` 保存 decision/input/request/execution identity、
context/candidate-set checksum、baseline/chosen action、完整 logging action
distribution、model scores、动态 uncertainty、decision source/reason codes、
shadow action、完整 policy/adapter/artifact/feature/action/gate identity、
logging policy ID 和带时区时间。嵌入的 `decision_id` 必须同时匹配 observation
行主键与父 tutoring decision。

学习 epsilon 记录真实全动作分布；rules、shadow public action、fallback、禁止探索
和单候选均记录 one-hot logging policy。shadow action 不是 public/chosen action。

## 奖励与离线评估

`m6-reward-v1`：

`transfer_success - 0.05 * hint_count - 0.10 * loop_count`

没有后续证据时使用 `pending`/`censored`，禁止把缺失证据伪造为 0 奖励。
最终 reward 还保留 transfer success、独立订正、自我解释、额外 hint/turn/loop、
教师复核升级、safety flag、结构化 outcome event IDs/watermark 和带时区观察时间；
safety-invalid 记录不能进入 ordinary scalar-reward evaluation。event ID/watermark
拒绝路径、自由文本、邮箱和常见 secret-like 前缀。

离线导出使用去标识 canonical JSONL，并按 session/group/time 切分。显式字段
allowlist 为 `decision_id`、`context`、`candidate_actions`、`chosen_action`、
`propensity`、`reward`、`reward_status`、`policy_version`、
`feature_schema_version`、`action_space_version`、`anonymous_group_key`、
`occurred_at`、`session_id`、`event_time`、`state`、
`target_propensities`、`direct_estimates`。decision/session/group 使用运行时
HMAC-SHA256，不导出原始身份或 shadow action。

OPE 输出 IPS、
SNIPS、DM、DR、bootstrap CI、ESS、动作覆盖率及按状态/群组切片；没有可信
propensity 或覆盖不足时返回 `insufficient_data`，不得批准 active。

## 持久化

SQLite/PostgreSQL schema v10 新增 M6 私有表：

- `m6_policy_artifacts`
- `m6_policy_executions`
- `m6_policy_observations`
- `m6_policy_rewards`
- `m6_policy_evaluations`

旧 M6 决策仍可读取。新策略执行绑定、决策观测和原 M6 决策遵守幂等唯一约束；
SQLite→PostgreSQL 导入器同步加入这些表。当前 bundled schema 总版本为 v11；
M0 assessment run 的七个冻结字段由追加的 v11/0011 提供，不保存公共领域
payload。

## 验收

- rules 模式黄金回归逐字段一致；
- shadow 不改变公共动作，active 只能选择安全候选；
- 相同请求、并发、重启、回放得到同一策略绑定和同一公共结果；
- 制品篡改、版本不符、NaN/Inf、路径越界、kill switch 全部安全失败；
- 奖励缺失、低覆盖、低 ESS 明确阻止批准；
- SQLite/PostgreSQL schema 和仓储语义一致；
- 公共契约、公开签名和 provenance 无变化；
- 聚焦测试、全仓测试、覆盖率、compileall 和 `git diff --check` 通过。

## 验证与未验证边界

最终回归为 905 passed、13 个需要外部 PostgreSQL 的测试 skipped；coverage 总计
86%。这些结果验证了本地/假 PostgreSQL 语义与安全回归，不等于真实 PostgreSQL
live migration 已通过。本阶段没有训练、批准、部署或 rollout 学习策略，没有真实
教学数据结论，也不声称学习策略优于 baseline 或 active 已可生产启用。
