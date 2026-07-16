# M5 学习者与班级状态

## 负责人

童

## 职责

从版本化评分审计形成学习观测，执行认知诊断、知识追踪、个体状态更新和
透明班级聚合。认知诊断的规定模型族包含 DINA，知识追踪的规定模型族
包含 BKT。当前 `run_learning_models` 是空实现：产生空 DINA 掌握概率和空
BKT 追踪概率，不运行拟合或伪造参数。

## 输入来源

- M8 `ScoringResultBundle` 与由其审计转换的 `LearningObservationBatch`。
- M3 `KnowledgeBundle` 和 Q 矩阵。
- 前版 `LearnerStateSnapshot`/`ClassStateSnapshot` 与版本化状态策略。
- 当前空架构允许观测列表为空，但 learner 和 watermark 仍必须明确。

## 输出

- `StateUpdateResult`：现有可解释状态更新，供 M6/M9。
- `CognitiveDiagnosisResult`：DINA 系模型的版本化认知诊断输出。
- `KnowledgeTraceSnapshot`：BKT 系模型的版本化知识追踪输出。
- `LearningModelRun`：将 DINA/BKT 两个结果、观测数和水位绑定为原子运行。

## 禁止事项

不得重复消费同一审计版本、覆盖旧状态、混用学习者、在证据不足时伪造
DINA/BKT 结论、在 M5 中做 IRT 选题，或跨模块读表。
