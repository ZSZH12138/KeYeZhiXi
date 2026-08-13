# M5/M8 功能与操作流程

## 一句话说明

M8 负责“出题、评分、复核、标定和选下一题”，M5 负责“根据有效评分判断学生
掌握情况并更新班级状态”。两者之间只传递带版本和校验和的正式契约。

## 完整流程

1. 教师发布知识包、Q 矩阵和测评蓝图。
2. M8 按蓝图的题型、总分、锚点和知识点权重生成并冻结试卷。
3. 学生提交答案；M8 规则评分客观题，并把主观题交给量规评分边界。
4. M8 合并评分并追加审计记录。教师确认或修改时会产生新版本；拒绝结果不会
   进入学习模型。
5. M8 从冻结试卷和最新有效评分生成权威学习观测。
6. M5 使用完整历史运行 DINA 和 BKT，并把模型版本写入学生/班级状态。
7. 当跨学生作答数据达到门槛时，M8 运行 2PL IRT，先生成 shadow 参数。
8. M9 生成质量报告，教师再决定批准、拒绝或暂缓。
9. 只有 approved 参数可用于 EAP 能力估计和自适应选题。
10. 自适应选题先满足知识点配额，再选择信息量最高的合格题，同时排除已作答、
    超难度范围或曝光过高的题。

## 数据门槛

| 功能 | 默认最低数据 | 数据不足时 |
|---|---|---|
| DINA | 200 名学生；每题至少 50 个同时含对错的作答 | `INSUFFICIENT_MODEL_DATA` |
| BKT | 100 名学生；每名学生每个知识点至少 5 次有序作答 | `INSUFFICIENT_MODEL_DATA` |
| 2PL IRT | 200 名学生；至少 10 道题；每题至少 50 个同时含对错的作答 | `INSUFFICIENT_CALIBRATION_DATA` |
| 自适应选题 | approved 参数、有效能力估计、满足策略的候选题 | `ADAPTIVE_POOL_EXHAUSTED` |

这些门槛可以在受控测试中显式调低，但生产默认值不能静默降低。

## 版本与恢复规则

- 试卷、量规、评分审计、模型、状态、IRT 参数、能力估计和选题结果都追加保存。
- 相同身份和相同内容可安全重试；相同身份但内容不同必须报冲突。
- 教师复核必须携带当前审计版本和校验和，防止覆盖别人刚完成的复核。
- M5 只消费每个评分审计的最新有效版本；拒绝版本和旧版本保留历史但不重复学习。
- M5/M8 重启后从 SQLite 或 PostgreSQL 恢复，不依赖进程内缓存。

## 功能启用门槛

```text
正式评分
  -> 最新有效学习观测
  -> DINA/BKT 学生状态
  -> 2PL shadow 标定
  -> M9 质量报告 ready
  -> 教师 approve
  -> approved 参数
  -> 能力估计与自适应选题
```

任一前置条件未满足，后续功能必须停止并返回明确错误，不能使用固定值、普通答对率
或空列表伪装成功。

## 主要验收入口

- 全链路与重启一致性：`tests/e2e/test_m5_m8_full_learning_cycle.py`
- DINA/BKT：`tests/model_validation/test_dina_recovery.py`、
  `tests/model_validation/test_bkt_recovery.py`
- 2PL IRT：`tests/model_validation/test_irt_2pl_recovery.py`
- 自适应效率与约束：`tests/model_validation/test_adaptive_efficiency.py`
- 教师复核到 M5：`tests/integration/test_m8_review_to_m5.py`
- IRT 审批与能力估计：`tests/integration/test_m8_model_approval.py`
