# M6 策略学习运维指南

## 能力状态与安全边界

当前代码已实现 M6 私有的安全候选生成、`rules`/`shadow`/`active` 运行时、
JSON-only LinUCB 制品校验、奖励关联、去标识离线数据、OPE、SQLite/PostgreSQL
持久化和 M0 策略身份冻结。默认配置仍是 `rules`，`rollout_percentage=0.0`、
`exploration_rate=0.0`；部署不会因这些能力存在而自动启用学习策略。

这些实现已经通过仓库的单元、集成与安全回归，但尚未完成真实教学数据训练、教师
治理批准、线上 rollout 或真实 PostgreSQL live migration 验证。本文不声称
`active` 已具备生产启用条件，也不声称任何学习策略优于确定性 baseline。

M6 的策略制品、执行、观测、奖励和评估均为私有模型，不进入 91 个公共 Pydantic
契约。`M6TutoringControlService.decide_next_action(...)` 仍保留四个公开输入，
公共输出仍是 `TutoringControlResult`。

## 三种模式

| 模式 | 已实现行为 | 默认状态与限制 |
|---|---|---|
| `rules` | 不读取 manifest、artifact 或 evaluation；逐字段保持确定性 baseline，并记录 rules 策略身份 | 默认模式；无 artifact SHA |
| `shadow` | 加载并校验学习制品，记录模型推荐、分数和不确定性；公共动作与 logging propensity 仍属于 rules baseline | 必须配置 `policy_id`；不会因模型推荐改变公共动作 |
| `active` | 只有通过全部门禁时，才可从 `SafetyEnvelope` 已给出的候选中采用 LinUCB 结果 | 必须配置 `policy_id` 与 `evaluation_dataset_identity`；默认零 rollout，因此默认不能采用学习动作 |

任意 manifest、artifact、版本、维度、预测、门禁、Repository 或持久化异常都回退
到 rules。`SafetyEnvelope` 和最终状态机校验始终权威：策略只排序合法候选，不能
新增状态迁移、动作类型或提示模板。教师待复核、诊断误区、活动误区和前置缺口等
补救场景禁止探索。

## 配置字段

既有示例位于 [`config/app.example.json`](../config/app.example.json)，不要另建
第二份应用配置示例。环境变量使用
`COURSE_INSIGHT_M6_POLICY__<UPPER_FIELD_NAME>`；同名环境变量覆盖 `app.json`。

| 字段 | 默认值/约束 | 运维含义 |
|---|---|---|
| `mode` | `rules`；可选 `rules|shadow|active` | 运行模式 |
| `policy_id` | `null` | shadow/active 必填，精确选择 Repository 中的 immutable manifest |
| `evaluation_dataset_identity` | `null` | active 必填，精确选择同一 policy/dataset 的 OPE 记录 |
| `rollout_percentage` | `0.0`，范围 `[0,1]` | 按 request fingerprint 的 SHA-256 确定性分流 |
| `exploration_rate` | `0.0`，范围 `[0,0.05]` | epsilon；还必须不超过 `maximum_exploration_rate` |
| `maximum_exploration_rate` | `0.05`，范围 `[0,0.05]` | 配置层探索上限 |
| `gate_policy_version` | `m6-active-gate-v1` | rules fallback 与 active gate 的冻结版本 |
| `global_kill_switch` | `false` | `true` 时 active gate 一律拒绝 |
| `allowed_course_ids` | `[]` | active 的精确课程 allowlist |
| `allowed_class_ids` | `[]` | active 的精确班级 allowlist |
| `minimum_support` | `1`，至少 1 | active 所需 evaluation observation support |
| `maximum_uncertainty` | `0.0`，有限且非负 | active 允许的预测不确定性上限 |
| `runtime_directory` | `m6_policy` | 解析为 `runtime_dir` 下的目录；不能等于或逃逸 runtime root |

空 allowlist 或零 rollout 会使 active fail closed；默认
`maximum_uncertainty=0.0` 只接受恰好为零的不确定性。把 `mode` 写成 active
并不等于门禁已通过。

## 版本、特征和动作

当前固定版本为：

- baseline：`m6-deterministic-v1`
- feature schema：`m6-features-v1`
- action space：`m6-action-space-v1`
- reward：`m6-reward-v1`
- active gate：默认 `m6-active-gate-v1`

`m6-features-v1` 是 23 维有限数值向量，顺序为 S0—S5 one-hot、五种 task type
one-hot、turn/得分比例/目标数、五个结构化决策信号、最低订正率、最低掌握
置信度、最高提示依赖和学习者证据数。它不包含答案、自由文本或真实身份。

`m6-action-space-v1` 恰好覆盖当前状态图的 8 条迁移：

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

LinUCB 使用纯 Python 计算
`theta·x + alpha*sqrt(xᵀA⁻¹x)`，拒绝非有限值、负不确定性和维度不匹配。
epsilon 抽样由 request fingerprint 的 SHA-256 确定；可探索且有多个候选时，
胜者 propensity 为 `(1-epsilon)+epsilon/n`，其他候选为 `epsilon/n`。单候选、
rules、fallback 或禁止探索时 logging policy 为 one-hot。

## Manifest、artifact 与 runtime root

应用从 M6 Repository 按 `policy_id` 读取 immutable
`PolicyArtifactManifest`，再从 `m6_policy.runtime_directory` 读取其
`artifact_reference`。manifest 不是课程
`runtime/snapshots/course_runtime_manifest.json` 的字段，两类 manifest 不得混用。

Manifest 精确字段为：

```text
policy_id, adapter_id, adapter_version, algorithm,
state_graph_version, baseline_policy_version,
feature_schema_version, action_space_version, reward_version,
gate_policy_version, training_data_watermark, training_data_checksum,
artifact_sha256, status, created_at, artifact_reference, allowed_scopes
```

`status` 只能是 `draft|shadow|approved|rejected|retired`；
`allowed_scopes` 的 active 检查同时要求 `course:<course_id>` 和
`class:<class_id>`，并且对应 ID 也必须出现在配置 allowlist。

Artifact 必须是无重复键、无 NaN/Infinity、UTF-8、sorted-key compact canonical
JSON，精确包含：

```text
policy_id, adapter_id, adapter_version, feature_schema_version,
action_space_version, dimension, alpha, actions
```

`artifact_reference` 必须是 runtime root 内的相对 `.json` 路径；绝对路径、`..`、
符号链接逃逸、非 JSON、非规范 JSON、SHA-256 不符、缺少任一安全动作参数或
23 维模型不匹配、或 action mapping 未覆盖全部 8 个安全动作都会回退 rules。
Artifact 可以携带额外 action 参数，但 `SafetyEnvelope` 永远不会把额外 action
交给 adapter，因此它们不能被选择。代码不加载 pickle/joblib，也不进行动态执行。

## Promotion 前检查

仓库提供 `python -m scripts.verify_m6_controlled_rollout --evidence <json>` 作为
受控验证入口。证据缺失、OPE 未达到 `sufficient_data`、没有治理批准、模式不是
`rules` 或 rollout 非零都会返回 `status=blocked`；脚本只输出脱敏结果，不会训练、
发布策略，也不会接入 M7/M9。

仓库当前没有训练、manifest 注册或 promotion 的公共 CLI/管理页。制品和私有
Repository 记录必须由另行受审计的治理/发布流程写入；不要用手工 SQL 绕过
dataclass、canonical JSON 和 insert-or-verify 校验。

一次候选 promotion 应按以下顺序执行：

1. 确认 SQLite/PostgreSQL core ledger 已到 v11；v10 的五张 M6 policy 表和
   v11 的 M0 freeze 列都存在。
2. 在部署外完成去标识训练，生成 23 维、覆盖全部 8 个安全动作、
   `algorithm=linucb` 的规范 JSON；计算原始 artifact bytes 的 lowercase SHA-256。
3. 用全新 immutable `policy_id`/adapter version 注册 manifest，并把 artifact 放入
   配置的 runtime root。由于 Repository 是 insert-or-verify，状态、版本或内容
   变化应生成新的 immutable policy identity，而不是覆盖已有记录。
4. 先用 `shadow`、零探索运行，核对 rules 公共输出未变化、私有观测完整、日志与
   导出无原始身份/答案/自由文本。
5. 仅从 `observed` 且非 safety-invalid 的 reward 生成 JSONL，按完整 group/session
   和时间边界切分，再运行 OPE。
6. 保存与候选 `policy_id`、canonical JSONL `dataset_identity` 精确匹配的完整
   evaluation。只有 `status=sufficient_data`、`approved=true` 且 IPS/SNIPS/DM/DR
   与四组 confidence interval 完整时，运行时才把它视为可用于 active 的证据。
7. 由授权人员完成外部治理批准后，以 `status=approved` 的 immutable manifest
   发布对应候选；先配置极小非零 rollout、明确 allowlist、有限支持和不确定性
   阈值，重启应用，再只读核对 gate rejection/selection 记录。

即使上述步骤全部完成，也只能说明代码门禁输入齐备；没有真实教学验证和线上
rollout 证据时，仍不能宣称 active 已可生产启用或策略优于 baseline。

## Active gate

一次 active 采用必须同时满足：

- manifest 存在且为 `approved`；
- manifest、artifact、execution 的 SHA-256、feature/action/gate 版本完全一致；
- 当前安全候选至少 2 个；
- evaluation observation count 达到 `minimum_support`；
- 动态预测 uncertainty 不高于 `maximum_uncertainty`；
- 精确 policy/dataset evaluation 可用且已批准；
- course/class 同时命中配置 allowlist 和 manifest scopes；
- 不处于教师复核/误区/前置缺口补救场景；
- request fingerprint 与 context 一致并命中 rollout；
- `global_kill_switch=false`。

任一条件缺失会记录拒绝原因并使用 baseline；门禁不会扩大候选集合。

## Kill switch 与 rollback

紧急停止学习动作时：

1. 把 `COURSE_INSIGHT_M6_POLICY__GLOBAL_KILL_SWITCH=true`，并同时把
   `COURSE_INSIGHT_M6_POLICY__MODE=rules` 作为最保守配置；
2. 正常重启 Web/Worker，使 immutable settings 生效；
3. 对新 request 确认 `decision_source=rules` 或 `fallback`，logging policy 为
   one-hot；核对公共动作仍在安全候选内；
4. 保留 artifact、manifest、evaluation、execution、observation 与 reward
   记录，不删除或改写审计历史。

回滚到旧学习版本时，只能选择先前已验证的 immutable `policy_id` 和与之精确匹配
的 `evaluation_dataset_identity`，重新走 shadow 和 gate；不能覆盖同一 policy
记录。已经提交的决定按原结果重放，M6 first-writer execution 和 M0 frozen identity
不会被回滚配置重写。尚未提交决定的 learned execution 会按其冻结的 policy/
adapter/artifact 身份重新解析 immutable 制品，并复用 first-writer 保存的探索率和
active gate 结论；当前配置切换到 rules 或其他 policy 不会替换它。精确制品缺失、
损坏或校验不一致时仍回退 rules；当前 `global_kill_switch=true` 始终具有更高优先级。

## M0 七字段冻结与恢复

submit checkpoint 在 `state_saved` 与 `tutoring_saved` 之间增加
`policy_frozen`。M0 恰好冻结：

```text
policy_id
adapter_id
adapter_version
artifact_sha256
feature_schema_version
action_space_version
gate_policy_version
```

rules 允许 `artifact_sha256=null`；shadow/active learned binding 必须有合法的
lowercase SHA-256。若 M6 first-writer 已提交但 M0 尚未保存 checkpoint，恢复会
再次 prepare 并取得同一 binding；到达或越过 `policy_frozen` 后，恢复必须逐字段
完全一致，才会调用 `decide_next_action(...)`。

从 v12 升级的历史 submit 行仅在七字段全部为 NULL、checkpoint 为
`tutoring_saved|feedback_saved|analytics_saved`、已有保存状态和 frozen prior-state
标记时允许一次 CAS adoption。部分字段、`policy_frozen` 行或 review operation
不得猜测修复。

M0 仍恰好只保存上述七个身份字段。为保证 M6 在“execution 已 first-write、决定尚未
提交”时可确定性恢复，M6 私有 execution JSON 还保存当次 `exploration_rate` 和
active gate 的允许结论/原因；这些字段不进入 M0、公共契约或 execution fingerprint
的既定七项 preimage。

## Reward、JSONL 与 OPE

`m6-reward-v1` 只对 `observed` outcome 计算：

```text
transfer_success - 0.05 * hint_count - 0.10 * loop_count
```

没有后续证据时必须使用 `pending` 或 `censored`，不能伪造零奖励；带 safety flag
的记录为 `invalid`，不能进入普通 scalar-reward evaluation。outcome event ID 和
watermark 只接受无路径、无邮箱、非 secret-like 的结构化审计标识。普通
`observed` 记录即使没有额外审计元数据，也必须保留 `transfer_success`、
`additional_hint_count` 和 `loop_count` 三个原始公式分量。

Canonical JSONL 的字段 allowlist 为：

```text
decision_id, context, candidate_actions, chosen_action, propensity,
reward, reward_status, policy_version, feature_schema_version,
action_space_version, anonymous_group_key, occurred_at, session_id,
event_time, state, target_propensities, direct_estimates
```

decision/session/group 使用至少 32 bytes secret key 的 HMAC-SHA256；不导出原始
身份或 shadow action。密钥只由受控作业在运行时提供，不写入配置示例、数据集或
日志。切分要求 group 和 session 不跨 train/evaluation，且 evaluation 在严格更晚
的完整 group 时间边界。

OPE 实现 IPS、SNIPS、DM、DR、按 canonical dataset identity 确定性 bootstrap
confidence interval、ESS、action/support coverage，以及 state/group slices。
默认阈值是 20 行、ESS 10、action coverage 0.8、support coverage 0.95、最小 logging
propensity 0.01、200 次 bootstrap、95% interval，并要求 DR 下界至少 0。
当前 fail-closed 实现还要求 required state/action pairs 的 action coverage 和
support coverage 都实际达到 1.0；配置的 0.8/0.95 是附加下限，不会放宽这一要求。
缺失/过低 propensity、覆盖不足、ESS 过低或数据太少时返回
`insufficient_data`，不得批准 active。

OPE 是依赖 logging propensity、direct estimate 和数据覆盖假设的离线估计，不是
线上因果证明；通过门禁也不等于策略真实优于 baseline。

## Schema 与 PostgreSQL 验证边界

- v12/`0012_m6_policy_learning.sql`：新增
  `m6_policy_artifacts`、`m6_policy_executions`、
  `m6_policy_observations`、`m6_policy_rewards`、
  `m6_policy_evaluations`。
- v13/`0013_m0_policy_freeze.sql`：只为 `m0_assessment_runs` 安全追加七个 freeze
  字段与完整性约束。
- v14/v15 继续追加 M5/M8 模型运行历史与学习观测审计身份，不修改 M6 表。
- PostgreSQL v16/v17 继续追加 M1—M3 S1-S6 与向量 metadata，不修改 M6 表；
  SQLite platform ledger 仍为 v15，M1—M3 SQLite 仓储使用独立 version 1 ledger。
- 已发布的 M4 v10/v11 与 M6/M0 v12/v13 保持不变。
- 当前 bundled PostgreSQL schema version 为 17、SQLite platform schema version 为 15，SQLite→PostgreSQL
  allowlist 同时包含 M4 intent、M6 policy、M0 freeze 和 M5/M8 新历史表。

仓库的 live PostgreSQL tests 需要受保护的临时数据库。本阶段没有执行真实教学或
线上 rollout；live PostgreSQL migration 只能以当次未跳过的 CI/验收结果记录为通过。

## M9 非集成边界

M6 的 OPE、approval 和 gate 是 M6 私有能力，尚未正式接入 M9。当前公共
`M9TeacherAnalyticsService.build_model_quality_report(...)` 只接收 M8
`CalibrationRunResult`；91 个公共 schema 和 contract provenance 都未增加 M6
policy evaluation 边。不能把 M6 的私有 `approved=true` 写成 M9 审核，也不能
声称 M9 已批准或监控 M6 策略。
