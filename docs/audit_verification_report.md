# M5/M8 修复与回归对应表

## 验收范围

本报告覆盖 M5/M8 修复计划 Task 1—12。原契约中的“空实现”不视为完成：只要输入
达到数据门槛，就必须运行真实算法并产生可恢复、可审核的结果。

## 明确缺陷

| 已复现问题 | 修复后的功能行为 | 主要回归测试 |
|---|---|---|
| 学习观测通过题目实例名称猜题目身份 | 从冻结试卷读取真实题目、版本和知识点 | `test_m8_observation_builder.py` |
| 旧试卷可能使用新版量规 | 试卷生成时冻结量规，重启后仍使用原版本 | `test_m8_restart_recovery.py`、`test_m8_append_only_history.py` |
| 不同题目被错误映射到同一知识点 | 每道题按自身 Q 矩阵生成诊断 | `test_m5_diagnosis_mapping.py` |
| 同一学生再次测评会在班级统计中重复计数 | 用该学生最新状态替换旧贡献，再重新聚合 | `test_m5_class_replacement.py` |
| 班级快照身份可能重复 | 每次班级版本生成唯一快照身份 | `test_m5_class_replacement.py` |
| M5 保存中途失败可能留下半套状态 | 学生状态、班级状态和水位在同一事务保存 | `test_m5_atomic_update.py` |
| M8 组卷没有严格执行知识点权重 | 使用最大余数配额和确定性回溯组卷 | `test_m8_weighted_blueprint.py` |
| M8 重启后丢失试卷/量规/评分上下文 | 所有冻结内容持久化并可恢复 | `test_m8_restart_recovery.py` |
| 固定时间或重试时间差会改变业务结果 | 生产使用 UTC 时钟；测试显式注入固定时钟 | `test_m8_restart_recovery.py` |
| 相同身份可能覆盖试卷或评分历史 | 同内容重试复用，不同内容冲突，历史只追加 | `test_m8_append_only_history.py` |
| IRT 参数未经审核即可被使用 | shadow 必须经过 M9 质量门槛和教师批准 | `test_m8_model_approval.py` |
| 自适应选题可能重复、超曝光或忽略配额 | 同时执行已作答排除、曝光、难度和概念配额 | `test_m8_adaptive_selector.py` |
| 教师复核存在过期覆盖和并发竞争 | 在事务内核对审计版本与校验和，只允许一个成功版本 | `test_m8_review_to_m5.py` |
| 被拒绝或旧版评分仍可能进入 M5 | 只消费最新有效版本；拒绝结果不生成学习观测 | `test_m8_review_to_m5.py` |

## 原阶段未实现、现已补齐

| 功能 | 当前实现 | 主要验收测试 |
|---|---|---|
| DINA 认知诊断 | 真实 DINA 参数拟合、掌握后验、连通分量精确/变分推断 | `test_m5_dina.py`、`test_dina_recovery.py` |
| BKT 知识追踪 | 四参数 BKT 拟合、按时间更新掌握概率、重启恢复 | `test_m5_bkt.py`、`test_bkt_recovery.py` |
| 模型驱动学生状态 | DINA/BKT 使用完整治理历史，BKT 结果进入状态 | `test_m8_m5_model_chain.py` |
| 2PL IRT | 真实 2PL 标定、收敛指标和 EAP 能力估计 | `test_m8_irt_2pl.py`、`test_irt_2pl_recovery.py` |
| IRT 质量报告 | 本地检查收敛、覆盖、参数稳定性和信息量 | `test_m9_model_quality.py` |
| 参数审核发布 | shadow/approved/rejected 不可变版本与教师审批状态机 | `test_m8_model_approval.py` |
| 自适应选题 | Fisher 信息量排序与业务约束，结果持久化 | `test_m8_adaptive_selector.py`、`test_adaptive_efficiency.py` |
| 完整学习闭环 | 评分、复核、M5、IRT 审批、能力和选题贯通 | `test_m5_m8_full_learning_cycle.py` |

## 发布判断规则

只有以下检查全部完成，才可判定本轮 M5/M8 修复达到合并门槛：

- 全部自动化测试通过，项目覆盖率不低于 80%。
- 本次新增或重写的 M5/M8 核心文件覆盖率不低于 90%。
- Python 编译检查和依赖完整性检查通过。
- SQLite 迁移通过；真实 PostgreSQL 仅在提供受保护测试库时计为实际通过，环境
  未提供时必须明确记录为跳过。
- Git 差异无空白错误，契约 Schema 与代码一致。

## 本轮最终执行结果

执行时间：2026-08-13。结果如下：

- 全项目测试：`1388 passed, 21 skipped, 0 failed`。其中跳过项是未向该次全量命令
  注入外部服务环境的受控测试，不影响覆盖率计算。
- Task 9—12 定向回归：`38 passed, 0 failed`，覆盖参数审批、能力估计、自适应
  选题、教师复核和完整学习闭环。
- 项目总覆盖率：`88.28%`，高于 80% 门槛。
- 本次重点新增运行文件覆盖率：PostgreSQL BKT 92%、PostgreSQL M8 模型历史
  92%、SQLite BKT 95%、SQLite M8 模型历史 95%、M8 模型生命周期 93%；
  DINA/BKT/IRT/自适应核心算法文件均为 90% 以上。
- 完整学习闭环与 M5/M8 重启一致性：通过。
- Python 编译、`pip check`、契约测试、Schema 重新导出和 Git 空白检查：通过。
- 本地 SQLite v1—v15 迁移链：通过。
- 独立临时 PostgreSQL 16 容器中的真实迁移、SQLite 数据导入、事件仓储和并发
  流程测试：`9 passed, 0 failed, 0 skipped`。事件仓储测试显式使用同一固定 UTC
  时钟完成入队、领取和租约检查，避免测试依赖执行当天的系统时间；生产代码
  的时钟和领取规则没有改动。测试后容器已关闭并自动删除，没有使用或修改
  用户已有数据库。

结论：M5/M8 Task 1—12 的代码和自动化发布门槛已达到；生产发布仍需使用真实
生产规模的去标识化数据完成模型效果监控和容量评估，这不属于本轮代码缺陷。
