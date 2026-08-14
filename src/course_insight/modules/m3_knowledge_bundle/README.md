# M3 知识包

## 负责人

谢

## 职责

验证知识点、先修、误区、题卡、量规、蓝图与 Q 矩阵的引用完整性。
M3 冻结题目和知识版本，为 M5 DINA 认知诊断、M8 IRT 标定与自适应
选题提供不可变的 Q 矩阵/题目依据；它本身不估计学习或题目参数。

## 输入来源

- M1 `CoursePackage`。
- `data/seeds/` 中由教师确认的知识、先修、误区、题目、量规和蓝图 JSON。
- 题目版本与 Q 矩阵关系，侜 M5/M8 后续标定使用。

## 输出

`KnowledgeBundle`，供 M4 任务编排、M5 诊断/追踪、M8 出卷/标定和 M9
分析原样消费。题目、Q 矩阵和参数标定的关联必须使用 `(item_id, item_version)`。

## 持久化与 D3 边界

离线/测试模式下默认使用共享 `SQLiteM1M2M3Repository`；需要文件导出或显式文件后端时，
`FileM3Repository` 将完整、不可变、带 checksum 的知识制品写入
`runtime/artifacts/`；已发布制品必须同时包含 bundle、seed snapshot 和 validation
report，被拒绝的 validation 也要保留对应的 seed snapshot/report。生产模式下，
`PostgresM1M2M3Repository` 使用 0014/0015 migrations 持久化 M3 artifact 和
`m3_teacher_reviews` CAS 复核记录；PostgreSQL 是生产权威后端，SQLite 不是生产后端。
备份、恢复、checksum 校验、清理和回滚必须覆盖所选后端的权威数据，不能只备份
`KnowledgeBundle` JSON 或教师 seed 源文件。

## 禁止事项

不得发布未审核题目、放宽量规守恒、覆盖旧版本、自动生成未经教师
确认的知识，或在 M3 中实现 DINA/BKT/IRT 计算。

## S4 教师复核门

`TeacherReviewWorkflow` 使用不可变记录和 compare-and-swap 版本，状态为
`draft -> submitted -> approved|rejected -> recalled`。记录只含输入 checksum、验证报告
引用、教师 pseudonym、理由、时间和历史，不含课程原文。生产组合根将 M3 绑定到
PostgreSQL CAS 仓储；SQLite CAS 仅用于离线/测试。生产调用旧的无审批发布入口会返回
`M3_REVIEW_REQUIRED`。正式发布必须使用 `build_knowledge_bundle_after_approval`，
并重新捕获 seed snapshot、校验 checksum、验证报告和 bundle 后才提交完整制品。
M1—M3 的真实 PostgreSQL+pgvector live 用例已纳入
`tests/integration/test_postgres_m1_m2_m3_live.py` 和 CI `live-m1-m3` job；本机缺少
受保护测试数据库时仍会明确 skip，不能把 skip 记为 live 验收通过。
M3 service 暴露 `create_teacher_review_draft`、`submit_teacher_review`、
`approve_teacher_review`、`reject_teacher_review` 和 `recall_teacher_review` CAS wrappers；
`AppCoordinator.initialize_course` 可选接收 `teacher_review_id` 与
`teacher_review_version`，提供两者时走审批后发布入口。
现有 M0 教师 Web 的 `TeacherReviewSubmission` 属于 M8/M9 评分复核，不等同于 M3 S4。
`AppCoordinator` 已提供 S4 草稿、提交、批准、拒绝和召回门面；若需要在 Django 中操作 S4，
应调用上述应用门面并复用同一 CAS review id/version，不得绕过 M3 审批门。
