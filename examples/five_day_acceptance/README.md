# 方案 6：五天验收测试数据包

这套数据服务于 `course_id=course_network`、`class_id=class_01`，覆盖需要人工或
外部输入的 M0-M9 功能。内部 outbox、数据库重放、迁移锁、跨模块对象传输等不需
人工造数的路径，继续由项目自动测试覆盖。

## 使用顺序

1. 账号：查看 `accounts/account_roles.csv`，密码只从已忽略的
   `runtime/five_day_acceptance/credentials.txt` 读取。
2. 入库：在教师“知识与题目”页面一次上传 `courseware/` 的五个合法文件并点击
   “确定处理”，测试混合格式解析、进度、来源定位和原子发布；`governance/` 仅供旧批处理兼容测试。
3. 题目：上传 `../new_architecture_manual_test/questions/questions_valid.txt`，确认系统自动
   关联当前结构化知识点；`seeds/` 不再作为当前教师知识流程的输入。
4. 学生：用 `scenarios/intent_examples.jsonl` 和
   `scenarios/manual_student_answers.json` 驱动六类意图与三档表现。
5. 模型：`model_data/learning_observations.jsonl` 有 200 名伪名学生；
   `bkt_sequences.jsonl` 为每个概念提供 120 名学生、每人 6 次时序作答。
6. 复核与安全：使用 privacy、teacher_review 和 tutoring_state_paths 三份场景集。
7. 每完成一项，在 `expected/coverage_matrix.csv` 记录实测结果，并用
   `manifest.json` 核对文件是否被意外修改。
8. 数据包自检：在项目根目录运行
   `python -m scripts.verify_five_day_acceptance --pack examples/five_day_acceptance`；
   结果写入 `verification_report.json`。

## 文件说明

- `courseware/`：MD、TXT、DOCX、PDF、PPTX 五种合法课件；最终二进制由专用
  artifact builder 生成并经过渲染检查。
- `invalid_fixtures/`：旧 PPT、空文本、非法 UTF-8、损坏 OOXML/PDF、无文本层
  扫描 PDF 和加密 PDF。加密样本口令为 `CourseInsight2026!`，仅用于确认系统拒绝导入。
- `official_sources/`：RFC Editor 原始文本及来源/许可清单，不改写 RFC 原文。
- `seeds/`：旧 S1-S6/M3 算法兼容数据，含 8 概念、28 题、2 量规、3 蓝图和 Q 矩阵；不用于当前知识管理页面。
- `scenarios/`：意图、检索、主观作答、隐私、教学状态机、教师复核样本。
- `model_data/`：DINA、BKT、2PL 和 M9 聚合所需的确定性合成数据。
- `expected/`：阈值、预期结果和 M0-M9 覆盖矩阵。
- 数据索引工作簿位于 `outputs/.../five_day_acceptance_data_index.xlsx`，用于现场勾选
  Coverage、查看账号/题库/学生规模和预期结果。

所有身份均为测试伪名。隐私场景中的邮箱、电话和姓名是专门构造的虚构测试值，
不得替换为真实学生信息。
