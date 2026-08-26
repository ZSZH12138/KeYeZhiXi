# M8 测评、评分、IRT 与自适应标定

## 负责人

童

## 职责

M8 按蓝图权重组卷，冻结试卷与量规，完成客观/主观评分编排、教师复核和
权威学习观测；同时负责 2PL IRT 标定、能力估计和受约束的自适应选题。

## 输入来源

- M4 `TaskPlan`、M3 `KnowledgeBundle`，以及可选的 M5 学习状态/诊断。
- M0 `AssessmentSubmission` 或已验证的 `student_answers.json`。
- M7 `RubricScoringResult`、M9 `TeacherReviewDecision`。
- M8 权威学习观测、`AdaptiveSelectionPolicy` 和题目曝光快照。

## 输出

- `AssessmentPaper`、`ScoringPreparationResult`、`ScoringResultBundle`。
- `LearningObservationBatch`：只包含最终有效的最新评分版本。
- `CalibrationRunResult` 与追加式 `IRTParameterSet`。
- `AbilityEstimate` 与 `AdaptiveSelectionResult`。

## IRT 启用顺序

1. 至少 200 名学生、10 道题、每题至少 50 个同时含对错的有效作答，才尝试
   2PL 标定；不足时返回 `INSUFFICIENT_CALIBRATION_DATA`。
2. 标定成功先生成 `shadow` 参数，不能直接用于正式能力估计或选题。
3. M9 检查收敛指标、样本覆盖、参数边界和信息量。
4. 只有质量报告为 `ready` 且教师批准，才追加 `approved` 参数版本。
5. 能力估计和自适应选题只接受 `approved` 参数；选题同时遵守概念配额、
   已作答排除、难度范围和曝光上限。

## 数据与恢复规则

- 试卷、冻结量规、评分审计、教师复核、参数版本、能力和选题结果均追加保存。
- 教师修改评分会创建新审计版本；旧版本不覆盖，过期复核会稳定报冲突。
- 服务重启后可恢复原试卷、评分、参数、能力和自适应选题结果。
- SQLite 与 PostgreSQL 使用相同的身份、校验和与状态规则。

## 当前四类出卷规则

- 诊断测评：从教师活动题库随机抽取 20 题；不足 20 题时使用全部题目，并更新画像。
- 随心练习：只使用学生做过的知识点，按题目最弱知识点的 `1 - mastery` 概率选题，不更新画像。
- 阶段评测：按各知识点 `1 - mastery` 权重从低掌握度开始分配共 20 题，题量不足时向其余知识点重分配；教师题库总量不足时使用全部题目，并更新画像。
- 订正：直接使用该生全部错题，不更新画像。

掌握度为 `正确次数 / 作答次数`，上限为 `0.9`；作答次数字段用于区分“从未作答”和“作答过但掌握度为 0”。只有影响画像的试卷在提交后累计次数与正确次数。

## 禁止事项

不得覆盖历史、接受越界评分、让被拒绝评分进入 M5、用答对率冒充 2PL、
未经质量审核和教师批准发布参数、在 M8 调用 DeepSeek，或绕开选题约束。

## 新知识发布版本兼容

新试卷从 M3 活动发布版本投影出的题卡、量规、蓝图和 Q 矩阵生成，并通过 M4 冻结 release UUID。知识文件新增或删除只影响后续新试卷；已发布试卷、评分审计、IRT 参数和成绩复核继续引用原版本，不做破坏性回写。
