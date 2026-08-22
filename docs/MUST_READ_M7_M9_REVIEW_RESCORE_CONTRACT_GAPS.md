# M7/M9 教师复核与重评分契约缺口（合并前必读）

状态：**强制阅读；B-05 全链路关闭前长期有效。**

适用范围：当前 M7/M9 修复 PR（下称 PR1）、后续选择性教师审核、`reject` 和
`rescore` 的设计、实现、测试、发布说明及存量数据处理。

本文优先说明“当前代码实际上做了什么”，不以页面文案、没有新增状态版本或 M9
隐藏了分数替代端到端证明。若 PR 描述、进度文件或界面与本文冲突，必须先修正文案，
再由 Application、M0、M8 的负责人冻结并实现跨模块协议。

## 1. 不可误读的结论

1. **延迟入账与模型 rescore 已进入 Application/M0/M8 代码路径，但第 8 节尚未全部关闭。**
   8.1/8.2 的 SQLite 工作流测试覆盖 pending 不入账、reject 后 `awaiting_rescore`、
   绑定原作答的 `local_model_rescore` 以及确认后一次入账。8.3 现有 SQLite→PostgreSQL
   导入可保持 `awaiting_review`；SQLite 还覆盖“评分已保存但尚未停进等待室”和
   “rescore 已完成但提交行仍在 running”的崩溃重放。8.4 提供存量污染盘点（dry-run）
   且无冻结基线时失败关闭，自动 M5 回滚仍禁用。PostgreSQL live 与崩溃恢复演练在
   本机无受保护测试库时 skip，不得写成已通过。
2. `reject` 能保存教师决定、保留被拒绝的原分、避免把它伪装成零分，并在等待室内
   阻止 M5/M6/学生反馈/权威 M9 入账。
3. 恢复路径现有两条：教师完整、守恒的 `override`，以及绑定原
   `AssessmentSubmission` checksum 的模型 `rescore`。rescore 结果必须先回到
   `pending`，不得自动接受。
4. 真正关闭 B-05 仍要求第 8 节全部测试通过，尤其是 SQLite 与 PostgreSQL 状态机
   一致、崩溃恢复和存量盘点。在此之前不要把 B-05 写成 `closed`。
5. 选择性教师审核只决定“哪些评分需要教师确认”；它不能替代上述入账门。即使只有
   低风险抽检样本被教师 `reject`，仍会遇到同一个 B-05 问题。

因此，在 Application、M0 和 M8 的桥接改动合并，并通过本文第 8 节的端到端验收前，
B-05 只能标记为：

> `partially_closed_delayed_posting_and_rescore; section_8_live_and_legacy_open`

在第 8.3/8.4 项通过前，不得使用 `fixed` 或 `closed`。

## 2. 当前真实调用顺序与污染点

### 2.1 首次提交

当前 `AssessmentWorkflow.submit(...)` 在 `scoring_saved` 之后先判断
`requires_teacher_review()` / `has_rejected_score()`：命中则把 submit 停在
`awaiting_review` 或 `awaiting_rescore`，不追加 learning events、不更新 M5、
不调用 M6、不生成学生反馈或权威 M9。学生结果页只看到等待状态，不会把 pending
分数显示为最终成绩。

未命中等待室时，顺序仍是：

1. M8 `prepare_scoring(...)` 拆出客观题审计与 M7 主观评分任务；
2. Application 调 M2 取证，再逐项调用 M7 `score_subjective_answer(...)`；
3. M8 `finalize_scoring(...)` 生成并持久化 `ScoringResultBundle`；
4. Application 追加 learning events，冻结 pre-score baseline，调用 M5、M6、
   M7 学生反馈和 M9 分析，再把 submit 标为 `completed`。

### 2.2 教师复核

当前 `AssessmentWorkflow.review(...)` 的真实顺序是：

1. `AssessmentResults.scored_submit(...)` 允许 `completed` 或等待室中的 submit；
2. M9 `record_teacher_review(...)` 保存 `confirm`、`override` 或 `reject` 决定；
3. M8 `apply_teacher_review(...)` 追加一个新审计版本；`reject` 会得到
   `review_status="rejected_pending_rescore"`，仍保留原分，不会记零；
4. 若 submit 仍在等待室且最新审计仍 pending 或 rejected：只更新等待室 checksum，
   把未入账的 review run `finish` 为 `completed`（只需 scoring checksum），
   不追加 learning events、不更新 M5；reject 会把 submit 改停为 `awaiting_rescore`；
5. 全部 latest audit 接受后，才从冻结 baseline 一次性入账：events、M5、M6、
   学生反馈、M9，再 `finish` 原 submit（completed submit 仍要求 scoring checksum、
   state version、report id 和 feedback id）。

因此，只检查 `state_version` 没有增加，现在可以与等待室状态一起证明 mastery、
class aggregate 和审计水位尚未被 pending/rejected 分数改写。第 8.3/8.4 项通过前，
这仍不能写成 B-05 `closed`。

### 2.3 模型 rescore 的现有边界

以下约束已经部分打通：

- M0 `WorkflowOperation` 含 `rescore`；checkpoint 为
  `pending → claimed → scoring_saved → completed`；completed rescore 只需
  scoring checksum；
- 等待室状态为 `awaiting_review` / `awaiting_rescore`；
- `ScoreAuditRecord.scoring_method` 含 `local_model_rescore`；上一版为
  rejected 时，`validate_audit_history` 允许追加该来源；
- Application `rescore(...)` 绑定原 `AssessmentSubmission` checksum、attempt、
  audit、expected rejected version 和 `rescore_request_id`；相同身份可幂等回放，
  即使 submit 已改停为 `awaiting_review`；
- 新答案或未处于 `awaiting_rescore` 的新请求会被拒绝；rescore 结果回到
  `pending`，不得自动接受。

仍未关闭的是第 8.3 PostgreSQL live / 导入一致性，以及第 8.4 存量污染自动重建。

## 3. B-02 的根因与当前止血边界

B-02 的根因不是少写了几个关键词，而是公共 v1 契约把 `source_id` 和 `locator` 都
定义成自由字符串：上游可以写任意文本，M2 继续传递，M7 原样复制，M0 最终显示。
“字符串与上游完全相同”只能证明引用没有被 M7 篡改，不能证明字符串真的是定位
元数据，也不能排除正文、答案、教师材料、控制字符或个人信息。

PR1 可以且应在 M7 学生可见边界做失败关闭的止血：

- 对 `source_id` 只放行当前仓库的 `source_<数字>`，对 `evidence_id` 只放行
  `evidence_<数字>` / `evidence_chunk_<数字>`；未知前缀和自然语言式 ID 失败关闭；
- 对 locator 使用有界、锚定、无空白的结构白名单；
- 只放行当前 M1 权威生产格式 `paragraph:<正整数>`，以及仓库既有数字型
  `section:1` / `section.1` / `section-1` / `p.1` 兼容格式；
- `page`、`chapter`、`timecode`、`chunk`、`block` 等尚未由公共契约定义的字符串
  即使看起来合理也失败关闭，不能在 M7 正则中先行发明语义；
- 拒绝长自然语言、答案标记、控制字符、电子邮箱、电话号码及其他疑似个人信息；
- 同一安全门同时用于新生成结果和持久化历史读回；
- v1 必填 `EvidenceCitation.quote` 只写受控占位值，不复制
  `EvidenceChunk.text`；安全门只接受这个精确占位值，学生视图不显示它。

这只是消费者侧止血，不是公共 locator 语义的最终解决。

> **明确后续要求：公共契约负责人未来必须定义结构化 locator（typed locator v2）。**

typed locator v2 至少要冻结：

- `kind`：例如 `page | section | paragraph | timecode | chunk | block`；
- 每种 kind 对应的强类型值和范围，例如正整数页码、分段章节路径、合法时间区间；
- 可选的版本化 source/chunk 身份和校验和；
- M1/M2 的生产规则、M7 的消费规则、M0 的显示规则；
- v1 字符串到 v2 的单向、可审计迁移；无法无歧义转换的旧值必须失败关闭，不能把
  整段文本包装成 `kind="block"` 来绕过校验；
- JSON Schema、Python 契约和字段级冻结快照。

公共契约负责人还应同时冻结结构化 source/evidence identity；在此之前，M7 不得把
通用“看起来像机器字符串”的正则当成可信来源身份。

typed locator v2 属于公共契约变更，必须由公共契约负责人批准版本和迁移方案；M7
不得单方面把临时正则白名单宣传成全局 locator 契约。

## 4. 关闭 B-05 所需的最小跨模块桥接

建议采用 attempt 级延迟入账，而不是逐题部分入账。只要最新审计中有一个未解决项，
整个 attempt 都保持未入账；这样可以避免后续确认不同题目时重复累计或局部回滚。

### 4.1 Application：在任何消费前设置唯一入账门

在 M8 `finalize_scoring(...)` 成功并持久化后、
`append_learning_events(...)` 之前增加路由：

```text
if scoring.has_rejected_score():
    route = awaiting_rescore
elif scoring.requires_teacher_review():
    route = awaiting_review
else:
    route = ready_to_commit
```

`awaiting_review` 和 `awaiting_rescore` 期间只允许保存：

- M8 评分及其不可变审计历史；
- M9 教师复核队列/教师决定；
- M0 不含分数的工作流状态和通知事件；
- M7/M9 必需的最小化模型调用审计。

明确禁止调用或发布：

- 带分数或 mastery 含义的 learning events；
- M5 `update_state_with_frozen_policy(...)`；
- M6 `prepare_policy_execution(...)`/`decide_next_action(...)`；
- 基于待定分数生成的学生反馈；
- 看似最终的 M9 class/individual analytics。

当前 M9 完整分析需要 M5 state，因此待审阶段应新增一个只从 M8 最新审计生成复核
队列的窄入口，或让教师上下文直接读取 M8 pending 评分；不得为了得到 review queue
而先伪造或更新 M5 状态。

每次 review/rescore 后重新计算整个 attempt 的最新审计集合：

- 任一 latest audit rejected：保持 `awaiting_rescore`；
- 否则任一 latest audit needs review：保持 `awaiting_review`；
- 全部 latest audit 已接受：进入 `ready_to_commit`，从首次 submit 冻结的 pre-score
  learner/class baseline 恢复执行，并且只入账一次。

M5、M6 本身不必为了新提交的延迟入账而改变算法，只要 Application 保证 pending 或
rejected bundle 永远不调用它们。若无法保证这个门，M5 还必须增加独立拒绝 pending
输入的纵深校验，但该校验仍不能替代 Application 编排。

### 4.2 M8：拥有 rescore 的身份、版本与审计历史

M8 必须新增明确的 rescore 命令/服务边界，至少校验：

- attempt、paper、audit、item instance 与被 reject 的最新版本完全一致；
- 调用方提供的 `expected_rejected_version` 仍是 latest version；
- 原始提交、量规、知识包、证据索引、策略和模型身份的 checksum 与冻结值一致；
- 新 criterion scores 守恒且不超过 max score；
- 旧 rejected 版本永不覆盖，新结果只能原子追加为 `version + 1`。

公共评分契约还必须表达模型重评分来源。可由契约负责人新增明确的
`scoring_method="local_model_rescore"`，或新增等价的 provenance 字段；同时把
`validate_audit_history(...)` 从“所有 v2+ 都是 teacher_override”改为基于合法状态
转换的校验。不能把模型重评分伪装为 `teacher_override`。

由 reject 触发的第一份模型重评分建议始终回到 `pending`，并记录
`previous_rejected_version`/`rescore_request_id`。即使未来启用选择性审核，曾被教师
拒绝也是硬风险信号，不应自动接受。

### 4.3 M0：表达等待态、rescore 操作和可恢复 checkpoint

M0 至少要支持：

- 非租约等待态 `awaiting_review`、`awaiting_rescore`；
- 一个明确的 `rescore` operation，或语义等价且独立幂等的子工作流；
- review 可以读取尚未 completed、但已经 `scoring_saved` 的 submit；
- 等待态不强制要求 state version、feedback id 或 report id；
- 只有权威分数已经一次性入账后，submit attempt 才满足当前意义上的 completed；
- rescore 的 target audit/version、请求 identity、输入 checksum、结果 checksum 和
  checkpoint 被持久化，但 M0 继续保持 payload-free，不保存原始学生答案。

原答案恢复必须先冻结一种安全协议。最小方案是调用方重新提交完全相同的
`AssessmentSubmission`，Application 先与原 submit 的 request checksum 比较，M8 再
比较其 raw-answer checksum；不一致即拒绝。如果产品要求服务端独立重评，则需另建
有保留期、加密、访问控制和删除审计的答案存储，不能把答案塞进 M0 workflow 表或
模型调用审计。

SQLite 与 PostgreSQL 的 M0 repository、建表/迁移、SQLite→PostgreSQL 导入器必须
同时支持新 operation/status/checkpoint。只改 Python `Literal` 会让恢复与重放在不同
后端产生分叉。

## 5. 建议冻结的状态机

### 5.1 Attempt 状态

| 当前状态 | 事件/条件 | 下一状态 | 允许的权威副作用 |
|---|---|---|---|
| `scoring` | M8 评分保存，存在 pending | `awaiting_review` | 保存评分历史和复核队列 |
| `scoring` | M8 评分保存，无 pending/rejected | `ready_to_commit` | 尚无 M5/M6/M9 副作用 |
| `awaiting_review` | 部分 confirm/override 后仍有 pending | `awaiting_review` | 追加决定和审计版本 |
| `awaiting_review` | 任一 reject | `awaiting_rescore` | 保存 reject；不记零、不入账 |
| `awaiting_review` | 所有 latest audit 已接受 | `ready_to_commit` | 尚无 M5/M6/M9 副作用 |
| `awaiting_rescore` | rescore 失败/不可恢复 | `awaiting_rescore` | 保存最小失败审计，允许教师 override |
| `awaiting_rescore` | rescore 结果保存 | `awaiting_review` | 追加模型 rescore 版本，等待复核 |
| `ready_to_commit` | CAS 取得提交权 | `committing` | 开始唯一一次入账 |
| `committing` | events、M5、M6、feedback、M9 全部按 checkpoint 恢复完成 | `completed` | 产生最终引用 |

任何状态都不得把 rejected 分数转换成 `0`。任何包含 rejected latest audit 的 bundle
都不得进入 `ready_to_commit`。

### 5.2 单个审计状态

```text
local_model v1 --selector--> not_required --------------------┐
                \----------> pending --confirm/override------> approved
                                      \--reject--------------> rejected_pending_rescore

rejected_pending_rescore --rescore request--> local_model_rescore v+1 pending
local_model_rescore pending --confirm/override---------------> approved
                            \--reject-------------------------> rejected_pending_rescore
```

`rescore request` 是 workflow 状态，不应改写旧审计记录。每一轮 reject、rescore、确认
都追加不可变版本，并保留完整因果引用。

## 6. 幂等、并发和崩溃恢复

### 6.1 幂等身份

- submit：继续使用稳定 operation id 与完整 request checksum；同 id 不同 payload
  返回冲突。
- review：`submission_id` 唯一，并对 `(audit_id, expected_audit_version)` 做乐观并发
  控制；相同决定重放返回同一结果，不同决定争用同一版本只能一个成功。
- rescore：使用
  `(attempt_id, audit_id, expected_rejected_version, rescore_request_id)` 作为稳定身份；
  `(audit_id, new_audit_version)` 必须唯一。
- commit：以“attempt + accepted scoring checksum + frozen baseline checksum”为唯一
  入账身份；所有 learning event、M5 processed audit id、feedback id 和 report id 都
  必须可确定性重建或安全读回。

### 6.2 恢复规则

每个外部或跨仓储副作用前先保存 checkpoint/输入引用，副作用后保存输出 checksum：

- scoring 已持久化但 M0 尚未推进：按 checksum 读回，不重新创建另一个 v1；
- review decision 已保存但 M8 尚未追加版本：用 decision identity 查找既有版本，
  没有时再以 expected version CAS 追加；
- rescore 发生进程崩溃：允许模型调用被重试，但所有 invocation 都必须有最小化审计，
  且 M8 的 CAS/唯一约束保证只有一个候选成为权威 `version + 1`；不要声称外部 API
  调用具备 exactly-once；
- M5 已成功但 M0 checkpoint 未推进：按 commit identity/processed audit id 读回同一
  state，不得再次累计；
- 事件已发布但 checkpoint 未推进：event id/ outbox key 必须稳定，重放不得产生第二
  条逻辑事件；
- 任何 checksum、版本或冻结依赖不一致都失败关闭，不能退回“取最新记录继续”。

恢复后也必须重新执行 attempt 级入账门，不能因为 run 曾经到过后置 checkpoint 就
绕过最新 audit 的 pending/rejected 状态。

## 7. 持久化与存量迁移

### 7.1 新数据

- M9 继续持久化完整教师决定身份、expected version、教师意见和时间；重复提交应
  幂等，冲突提交应拒绝。
- M8 保存每个 audit 的全部版本及因果引用；reject 与 rescore 都只追加不覆盖。
- M0 只保存流程身份、状态、引用和 checksum，不保存答案、提示词或模型原始响应。
- M9 rejected/pending-rescore 报告不得包含被污染的个人最近成绩、mastery 派生结论
  或教学建议；它只是状态说明，不是最终分析。
- M9 在私有 `learner_ids` 查询索引中为该报告保留受影响学习者 tombstone；公共
  `individual_reports` 仍为空。按学习者读取“最新报告”必须命中这个
  `pending_rescore` 报告，不能回退到更早且仍含 provisional score 的报告。
- SQLite、PostgreSQL 和 SQLite→PostgreSQL 导入必须对唯一约束、checksum 和读回
  校验保持等价。

### 7.2 已经由旧流程入账的 attempt

迁移不能把旧 completed submit 静默解释成“当时已延迟入账”。上线迁移必须先盘点：

- latest audit 为 pending 的 completed submit；
- latest audit 为 rejected 的 review；
- submit 时已写 M5、后来又出现 review 的 attempt；
- 未合并草稿分支曾生成的 M9 `IndividualReport.recent_score = null` 数据；
- 缺少 pre-score learner/class 基线或依赖 checksum 的 legacy run。

建议给前三类记录增加可查询的 `legacy_score_posted_before_review`/等价迁移标记，并
保留原历史。处理规则为：

1. 有 rejected 最新版本的旧记录继续从学生成绩与 M9 统计隐藏；
2. 没有完整、连续、checksum 可验证的 pre-score baseline，不做自动状态回滚；
3. 若 rejected 之后已有其他 attempt 依赖该 M5 状态，也不做局部删除；转人工重建或
   课程范围离线重放；
4. 若原答案不可恢复，不伪造 rescore；教师完整 override 是唯一恢复路径；
5. 只有在冻结基线完整、后续状态链可证明连续、重放算法/策略版本可用时，才允许由
   专用迁移作业重建，并逐项核对 mastery、class aggregate 和 processed audit ids；
6. `recent_score = null` 只存在于未合并草稿实现，项目确认没有对应生产数据，因此
   PR1 不提供会重写报告和审计校验和的生产迁移。开发者必须隔离并重建这类草稿库；
   repository/import 读到它时失败关闭。若上线盘点发现任何生产记录，必须立即阻断
   发布并另行设计同时保持 M9 模型调用审计 source-report checksum 绑定的迁移。

迁移脚本必须可 dry-run、输出受影响 identity 和数量、可重复执行，并在 SQLite 与
PostgreSQL 分别测试。不要用零分、空 mastery 或删除审计历史来“修复”存量。

## 8. B-05 全链路关闭的验收测试

只有以下测试全部通过，B-05 才能改为 closed：

### 8.1 延迟入账

- M7/M8 产生任一 `pending` 后，断言没有 score-bearing learning event、没有 M5
  state update、没有 M6 决策、没有学生反馈、没有最终 M9 报告；
- 教师仍能看到完整 review queue、证据、review reason 和当前版本；
- 混合客观题、自动接受主观题和 pending 主观题时，整个 attempt 不发生部分入账；
- 多个 pending audit 中只确认一个时仍不入账，全部解决后只入账一次。

### 8.2 Reject 与 rescore

- reject 保存原分和教师决定，但最终成绩不是零，attempt 进入
  `awaiting_rescore`；
- reject 前后 learner mastery、class aggregate、processed audit ids 与干净
  pre-score baseline 完全一致，而不只比较 state version；
- rejected 分数不出现在 M9 score statistics、IndividualReport、教学建议或学生页；
- rescore 必须绑定正确 attempt/audit/item/expected version/输入 checksum，追加
  `version + 1`，保留 rejected 版本；
- rescore 结果先回 pending，未确认前仍不入账；确认后以新权威分数只入账一次；
- 最终 state 与“从同一干净 baseline 只应用最终接受分数”的对照运行完全一致；
- 再次 reject 可以重复该循环，且不会累计旧分。

### 8.3 幂等、并发与恢复

- 同一 submit/review/rescore/commit 重放不增加 audit、event、state、feedback 或
  report 数量；
- 相同 id 不同 payload、过期 expected version 和错误 checksum 均失败关闭；
- 两位教师或两个 rescore worker 并发时只有一个版本胜出，失败方得到明确冲突；
- 在每个 checkpoint 前后模拟崩溃并恢复，最终引用和 checksum 稳定；
- 模拟“模型调用成功但进程在 M8 持久化前崩溃”，允许产生多次调用审计，但只能有
  一个权威新 audit version；
- SQLite、PostgreSQL、SQLite→PostgreSQL 导入后的状态机结果一致。

### 8.4 存量和显示

- legacy polluted attempt 被明确标记且不会被误称为 clean；缺基线时失败关闭；
- 可重建样本恢复后的 mastery、class aggregate、M9 统计和审计水位与干净重放一致；
- 教师和学生页面分别正确显示“待复核”“待重评”“已确认”，且 pending/rejected
  不显示成最终成绩；
- 页面文案不得承诺尚不可达的自动 rescore。

## 9. B-02/公共契约的验收补充

PR1 的 locator 止血至少需要以下参数化测试：

- `paragraph:<正整数>` 与已登记的数字型 `section`/`p.` legacy 格式能通过；
- `page`、`chapter`、`timecode`、`chunk`、`block` 在 typed locator v2 落地前失败；
- 空白、前后空格、超长值、换行/NUL/控制字符失败；
- 普通课程正文、答案标记、邮箱、电话、学号、姓名式文本和未知 ID 前缀失败；
- `quote` 只接受受控占位值，任意课程摘录、个人信息或其他非占位文本都失败；
- M7 新生成和 SQLite/PostgreSQL 历史读回执行同一安全门；
- 学生视图只显示通过治理的 source/locator，不显示 `EvidenceChunk.text` 或 v1 quote
  占位值；
- typed locator v2 上线时同时验证 Python/JSON Schema、producer/consumer 和 v1→v2
  迁移，不能只检查 schema 文件总数。

## 10. PR1 完成与未完成清单

### 10.1 PR1 可宣称完成的范围（须以对应测试通过为前提）

- **B-01 的代码级出站治理引擎**：边界明确的标识符只做确定性脱敏，自由文本不再
  依赖不断增长的关键词表，而是交给本地 Presidio/spaCy 实体识别与校验和钉住的
  轻量语义分类器；缺依赖、缺工件、置信不足、异常和非 allow 结果均在 prompt/网络
  前失败关闭，审计不保存原文或命中 span，并有零 transport 回归测试。
- **B-02 的 M7 消费者侧止血**：对学生可见 source/locator 做结构白名单和失败关闭，
  禁止复制课程原文；新生成与历史读回一致。
- **B-03 的公共 v1 形状恢复**：`EvidenceCitation.quote: str` 恢复为必填并只使用安全
  占位值；`IndividualReport.recent_score: float` 恢复为非 nullable；拒绝评分时省略
  IndividualReport，而不是把字段改成 null；补字段级 schema 冻结测试、M7 旧 quote
  形状兼容读回，并对未合并 nullable 草稿库失败关闭和隔离。
- **B-05 的 M9 侧防护**：教师 reject 决定可持久化、被拒绝分数不记零且不进入 M9
  分数统计/最近成绩；M9 不把已污染的 M5 state 再包装成权威个人报告或教学建议；
  私有 learner-scope tombstone 阻止最新报告查询回退暴露旧分数。
- **边界披露**：代码注释、测试名、PR 描述、README/进度和界面不再声称 reject 已经
  清除提交阶段的 M5 影响，也不声称自动 rescore 已可用。

### 10.2 PR1 明确未完成、不得顺带宣称完成的范围

- **B-05 全链路**：Application 的审核前入账门、M0 等待态、M8 模型 rescore 版本、
  最终一次性 commit、崩溃恢复和存量污染重建；
- **typed locator v2**：PR1 白名单只是止血，公共契约负责人仍需定义并发布结构化
  locator v2；
- **B-01 的生产放行**：PR1 不提交训练数据或模型二进制；目标环境仍须部署经审批、
  版本/SHA 钉住且通过本课程域误报/漏报评测的中文模型工件。没有该工件时引擎会
  正确失败关闭，不能把“代码已实现”误写成“真实学生答案已获准出站”；
- **选择性审核的生产放量**：离线评测、shadow、硬风险门、抽检和回滚阈值属于后续
  发布工作；选择器无论多准确都不得绕过 B-05 入账门；
- **legacy 自动净化**：没有完整冻结基线与连续依赖证明的旧 attempt 不能自动回滚；
- **公共契约新版本审批**：M8 rescore provenance/status 及 locator v2 的版本升级需由
  公共契约负责人批准，不能在 M7/M9 私有实现中静默改变。

## 11. 合并与发布门

PR1 合并说明必须逐字表达以下事实：

> 本变更完成 M7/M9 安全止血、延迟入账等待室，以及绑定原作答的模型 rescore
> 协议。B-05 第 8.3/8.4 节（PostgreSQL live/导入一致性、存量污染自动重建）
> 仍未在本环境关闭。选择性审核保持 `all_review`；真实学生 DeepSeek 评分还要
> 有钉住的本地隐私工件。

在此之前，任何“选择性审核已安全上线”或“真实学生答案已获准出站”的产品文案
都不成立。

真实模型、选择性自动接受或 rescore 生产启用还必须满足：

1. 本文状态机由 Application、M0、M8、M7、M9 及公共契约负责人共同冻结；
2. SQLite/PostgreSQL 迁移和恢复演练通过；
3. 第 8 节测试提供 mastery、class aggregate、processed audit ids 的直接证据；
4. B-01 出站治理独立通过；
5. 监控能区分 pending、rejected、rescore、commit 和 legacy contaminated；
6. 回滚不会把等待中的分数误当成 completed 或重新入账。

在此之前，任何“教师可 reject 后自动重评”“选择性审核已安全上线”或“被拒绝评分
对学习状态无影响”的产品文案都不成立。
