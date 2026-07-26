# M4 私有意图 adapter 运维说明

## 范围与不变项

M4 的唯一公共规划入口仍是
`M4TaskOrchestrationService.create_task_plan(...) -> TaskPlan`。本能力不增加
公开 Pydantic 契约、不改变 84 个 Schema，也不改变 `TaskPlan` 字段、五类任务或
冻结工作流：`qa` 为 `M2 → M7 → M6`，其余四类为
`M8 → M2 → M7 → M5 → M6 → M9`。

私有决策采用批准方案 B。exact request key 包含六个上下文
`course_id`、`class_id`、`learner_id`、`session_id`、`knowledge_bundle_id`、
`course_package_id`，以及规范化 hint 和规范化学生文本的 SHA-256。它不是
`TaskPlan` 的身份：后者仍是八字段
`course_id, class_id, learner_id, session_id, task_type, knowledge_bundle_id,
course_package_id, blueprint_id`。所以不同 hint/文本可产生不同的私有决策；当
八字段相同时，TaskPlan 仍复用其首写权威记录。

已存的 exact-request 决策最优先：它跨进程重启、模式、阈值和 adapter 版本重放，
包括拒绝。不要为已持久化请求重新推断，也不要用新模型结果改写旧决定。

## 决策管线和模式

新请求先规范化文本与 hint，检查合法 hint；然后收集高精度和 legacy 规则。

- `rules`：合法 hint → 无跨规则冲突的单一高精度规则 → 单一 legacy 规则 →
  可恢复拒绝。此模式不加载或导入 scikit-learn。
- `shadow`：最终结果仍完全遵循 `rules`；模型只写 shadow 标签、状态、版本、
  confidence、margin、原因码和是否一致，绝不改变 TaskPlan。
- `active`：合法 hint 和无冲突单一高精度规则仍优先。其余情况可调用模型；只有
  模型为 accepted 且 `confidence >= min_confidence`、`margin >= min_margin` 时
  才能形成 `active_model` 接受。低于任一阈值会 abstain。模型未配置、不可用、
  failed、OOS、低阈值或 abstain 是否回退到唯一 legacy 规则由
  `fallback_to_rules` 控制；非法或不安全输出在 `fail_closed=true` 时立即拒绝，
  不得被 legacy 规则覆盖。

规则没有“先命中即胜”的跨类优先级。高精度与 legacy 命中合并后若出现多个类别，
且存在高精度命中时，`rules`/`shadow` 拒绝并记录
`conflicting_high_precision_rules`；只有纯 legacy 多标签时记录
`conflicting_legacy_rules`。`active` 仅可由通过双阈值的模型解决上述冲突，模型失败
时不得从冲突候选中猜选。空文本、非法 hint、OOS、冲突和未识别请求均以可恢复
`UNSUPPORTED_TASK` 返回；错误与日志不得回显原文。

## 配置与可信 artifact 边界

默认配置是 `mode=rules`、`backend=none`、无 `model_ref` 和模型 pin、
`min_confidence=0.70`、`min_margin=0.10`、
`policy_version=m4-intent-policy-v1`、`fallback_to_rules=true`、
`fail_closed=true`。`rules` 模式必须保持 `backend=none` 且模型引用/身份/checksum
均为空；`shadow`/`active` 必须使用 `backend=sklearn` 并提供完整外部 pin。

`config/app.json` 可放入：

```json
{
  "intent": {
    "mode": "shadow",
    "backend": "sklearn",
    "model_ref": "models/m4_intent/m4-intent-tfidf-logreg/1.0.0",
    "model_id": "m4-intent-tfidf-logreg",
    "model_version": "1.0.0",
    "model_sha256": "<64 lowercase hex>",
    "min_confidence": 0.70,
    "min_margin": 0.10,
    "policy_version": "m4-intent-policy-v1",
    "fallback_to_rules": true,
    "fail_closed": true
  }
}
```

`model_ref` 是相对 `runtime_dir` 的逻辑路径，禁止绝对路径、URL、`..` 和链接
越界；环境变量使用完全相同的
嵌套键：`COURSE_INSIGHT_INTENT__MODE`、
`COURSE_INSIGHT_INTENT__BACKEND`、`COURSE_INSIGHT_INTENT__MODEL_REF`、
`COURSE_INSIGHT_INTENT__MODEL_ID`、`COURSE_INSIGHT_INTENT__MODEL_VERSION`、
`COURSE_INSIGHT_INTENT__MODEL_SHA256`、
`COURSE_INSIGHT_INTENT__MIN_CONFIDENCE`、
`COURSE_INSIGHT_INTENT__MIN_MARGIN`、
`COURSE_INSIGHT_INTENT__POLICY_VERSION`、
`COURSE_INSIGHT_INTENT__FALLBACK_TO_RULES`、
`COURSE_INSIGHT_INTENT__FAIL_CLOSED`。配置覆盖优先级为运行时 overrides、
环境、显式 dotenv、`config/app.json`、安全默认值（按字段合并）。

`model.joblib` 是可信管理员边界：joblib 反序列化可执行代码。只允许受信管理员
发布只读、已验证的 artifact 目录；加载器校验目录/文件类型、大小、manifest 与
模型 SHA-256，并将 manifest 的 model ID/version/checksum 与独立运行配置逐项核对。
相邻 manifest 自报 checksum 不是信任根。绝不接受学生上传、HTTP 参数或其他
非管理员来源的模型路径。

## 离线训练、发布和推进

训练仅离线执行；输出目录必须事先不存在，发布会原子生成 `model.joblib`、
`manifest.json`、`metrics.json`、`label_map.json` 与
`dataset_checksum.txt`。示例数据是中性 sample-only 数据，生成的指标
只说明示例管线可运行，不能作为真实学生数据的效果、上线或门禁证据。

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pip install --constraint requirements/ci-constraints.txt -e '.[intent]'
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' scripts/train_m4_intent.py --input data/m4_intent/example.jsonl --runtime-dir runtime --output-dir runtime/models/m4_intent/m4-intent-sample/1.0.0 --model-id m4-intent-sample --model-version 1.0.0 --seed 17 --min-confidence 0.70 --min-margin 0.10
```

检查 `metrics.json` 的 train/validation/test 五任务 `task_macro_f1`、逐类指标、
OOS recall、`task_coverage`、selective accuracy、固定 6×6 confusion matrix、
组间重叠、`production_gate` 与 `evidence_scope`。生产门禁必须使用受控离线数据与
预先批准的阈值；sample-only artifact 的 `eligible/passed` 始终为 false。
`task_coverage` 只衡量真实五任务样本中达到双阈值并被接收的比例；selective
accuracy 的分母则包含所有被自动接收为五任务的样本，因此把真实 OOS 却被误接收
为任务的样本计为错误，不能通过缩小分母抬高生产门禁结果。

推荐部署序列：

1. `rules → shadow`：安装并校验可信 artifact，配置 `mode=shadow`、
   `backend=sklearn`、`model_ref`、三项外部 pin 和批准阈值，重启；检查 shadow 一致率、拒绝/OOS、
   confidence/margin 覆盖与错误率。
2. `shadow → active`：保留同一已验证 artifact；仅在离线和 shadow 门禁通过后，
   将 `mode=active` 并重启。active 模型接受仍受双阈值限制。
3. `active → rules`：设置 `mode=rules`、`backend=none`，清空 `model_ref`、
   `model_id`、`model_version`、`model_sha256` 并重启。
   已持久化 exact-request 决策仍按原记录重放，这是设计要求，不是回滚失败。

## 持久化、导入与监控

SQLite schema v10 和 PostgreSQL core migration `0010_m4_intent_decisions.sql`
都创建 `m4_intent_decisions`。SQLite→PostgreSQL 导入把它列入固定 allowlist，
以 `request_key` insert-or-get 并回读核验；导入报告仍须按
`docs/postgresql_migration.md` 的逻辑源快照 checksum 和 source-limit 语义解释。

该表和监控只能包含 request key、`input_checksum`、任务标签/状态、决策来源、
adapter id/version、policy version、confidence、margin、原因码、shadow JSON、
payload checksum 与 UTC `created_at`。推荐按模式、来源、状态、原因码、adapter/
policy 版本聚合：决策量、接受率、拒绝率、OOS 率、阈值 abstain 率、
`conflicting_high_precision_rules`、`conflicting_legacy_rules`、shadow agreement、
模型异常和重放率。不得记录、导出、查询展示或告警携带原始学生文本。
原因码属于固定、经审查的协议注册表，不是适配器自由文本；任何未注册原因码都会
在边界映射为固定的 `unsafe_prediction_metadata`，不得持久化或返回原值。

真实 PostgreSQL live test 仍是破坏性受保护操作：仅在
`COURSE_INSIGHT_TEST_DATABASE_URL` 与 `COURSE_INSIGHT_TEST_DATABASE_NAME` 指向
名称含 `test`、`ci` 或 `tmp` 标记的可丢弃库时执行。缺失变量表示跳过且未实测，
绝不是通过；禁止把生产 DSN 放入这些变量。
