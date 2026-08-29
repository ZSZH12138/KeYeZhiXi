# PostgreSQL 迁移说明

## 结论先行

截至 `2026-08-29`：

- PostgreSQL 仓储、schema migration、SQLite→PostgreSQL 导入器已在代码中实现。
- 本文档不声称这些能力已经在真实 PostgreSQL 环境中完成联调或验收。
- 准确表述只能是“代码已落地、可配置、可阅读；真实通过状态须以当次实测为准”。
- 当前 bundled PostgreSQL schema version 是 `22`，SQLite platform schema version 是 `21`；M4 intent 占用 v10/v11，
  M6 policy 属于 v12，M0 policy freeze 属于 v13，M5/M8 模型运行历史属于 v14，
  M5 学习观测审计身份属于 v15，PostgreSQL M1—M3 S1-S6 与向量 metadata 属于 v16/v17；
  v18—v22 依次加入 M9/M7 模型调用审计、等待队列、重评操作和动态班级名单冻结。

## 已实现的组件

- `src/course_insight/infrastructure/postgresql/migration_runner.py`
  - checksum-locked migration ledger
  - `pg_advisory_xact_lock`
  - `schema_migrations` 校验
- `src/course_insight/infrastructure/postgresql/migrations/*.sql`
  - 当前 bundled PostgreSQL schema version 为 `22`
  - v8 为 `m0_assessment_runs` 增加同一 `paper_id` 只允许一个
    `status <> completed` review 的部分唯一索引（包含 failed）；相同 operation
    可重放，只有完成当前 review 后才允许新的 review operation
  - v9 为 assessment workflow 增加知识包/课程包/证据索引/policy checksum、
    精确 M5 前态冻结字段与 `state_inputs_frozen` 恢复 checkpoint；lease 续租和
    终态写入继续使用 owner/version/未过期租约 CAS
  - v10 新增 `m4_intent_decisions`：以 `request_key` 唯一保存 M4 私有意图决定、
    输入 SHA-256、adapter/policy 元数据、原因码、可选 shadow JSON、UTC 时间和
    payload checksum。该表没有学生原文列；公开 `TaskPlan` 和既有 M4 表不扩字段。
  - v11 扩展该表的状态约束，允许持久化 `unavailable` 与 `failed`，保证适配器
    不可用或抛出异常时仍能形成可恢复、可审计、可重放的拒绝决定。
  - v12/`0012_m6_policy_learning.sql` 新增
    `m6_policy_artifacts`、`m6_policy_executions`、
    `m6_policy_observations`、`m6_policy_rewards` 和
    `m6_policy_evaluations`；这是 M6 policy 表的 schema 版本
  - v13/`0013_m0_policy_freeze.sql` 只为 `m0_assessment_runs` 追加
    `policy_id`、`adapter_id`、`adapter_version`、`artifact_sha256`、
    `feature_schema_version`、`action_space_version`、`gate_policy_version`
    七列和完整性约束
  - v14/`0014_m5_m8_model_runtime.sql` 追加 M5 观测、DINA/BKT 模型、知识追踪，
    以及 M8 IRT 标定、参数、能力、自适应选择和标定审核历史
  - v15/`0015_m5_learning_observation_audit_identity.sql` 为 M5 学习观测追加
    权威评分审计身份和唯一性约束
  - v16/`0016_m1_m2_m3_capabilities.sql` 追加 M1—M3 权威制品、pgvector 文档、
    检索审计和教师复核 CAS 表
  - v17/`0017_vector_index_metadata.sql` 为向量索引追加课程包与 embedding 模型 metadata
  - v18/`0018_m9_model_invocation_audits.sql` 与 v19/`0019_m7_model_invocation_audits.sql`
    分别保存 M9 教师辅助和 M7 语义评分的受控模型调用审计
  - v20/`0020_m0_waiting_rooms.sql` 保存等待评分/复核状态，避免未完成答卷进入画像
  - v21/`0021_m0_rescore_operation.sql` 保存幂等重评操作身份
  - v22/`0022_m0_dynamic_class_roster.sql` 为画像流程冻结动态班级人数、名单校验和与采集时间
  - 已发布的 M4 v10/v11 migration 保持不变；M6 与 M0 freeze 顺延到 v12/v13，
    没有回写或重编号已发布 migration
  - v8 旧行的新字段保持 NULL；首次同 operation 重放时，只有旧 identity、持久化
    TaskPlan 锚点和完整的新依赖形状全部一致才会 CAS 接管。部分填充行、
    `state_inputs_frozen` 旧行，以及 state checkpoint 已越过但缺少
    `state_version` 的行不会被猜测修复
  - v12→v13 历史 submit 行的七个 M6 freeze 字段保持 NULL；只有
    `tutoring_saved|feedback_saved|analytics_saved`、已有保存状态和 frozen
    prior-state 标记、七字段全 NULL 时才允许一次 CAS adoption。部分字段、
    `policy_frozen` 或 review 行不会被猜测修复
- `src/course_insight/infrastructure/postgresql/sqlite_import.py`
  - 显式、可恢复、批量提交的导入编排
- `src/course_insight/infrastructure/postgresql/sqlite_import_checkpoint.py`
  - 与逻辑源快照、SQLite schema、PostgreSQL migration、目标及批计划绑定的原子 checkpoint
- `src/course_insight/infrastructure/postgresql/sqlite_import_cli.py`
  - `--project-root`、`--dry-run`、`--apply`、`--checkpoint` 命令入口
- `scripts/migrate_sqlite_to_postgres.py`
  - 包装脚本

## 迁移流程

建议流程分两步：

```shell
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --dry-run
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --checkpoint runtime/migration-checkpoint.json --apply
```

语义是：
1. `--dry-run`：先验证 SQLite 源与批处理计划。
2. `--apply`：通过 `--project-root` 加载单一 `PlatformSettings`，要求解析后的
   `database.backend=postgresql` 且 `database.url` 可用；然后执行 PostgreSQL
   schema migration，再分批导入。`--checkpoint` 应指向仅供本次迁移使用的持久文件；
   若省略，CLI 使用 `<report>.checkpoint.json`。

若配置非法、后端不是 PostgreSQL、或解析后 URL 缺失，CLI 会安全输出 `MIGRATION_CONFIGURATION_INVALID` 并返回错误码 `2`。它不再直接硬编码读取 `DATABASE_URL`，而是复用 `config/app.json.database.url_env` 与统一配置加载链。

导入不会删除或改写源 SQLite 文件。每一个目标批次的 insert-or-get 与回读校验
处在同一个 PostgreSQL 事务中；该批任何一行失败，整个批次回滚。报告会在每批
成功后更新 `completed_batches`，重复执行使用相同权威身份并校验 payload，
同 ID 不同内容会失败而不是静默覆盖。

当前导入器要求源 SQLite platform ledger 精确为 1—21 连续版本，并验证 v13
`m0_assessment_runs`、M4 intent、M6 policy、v14 模型历史和 v15 观测审计身份。
它不会把旧源在导入过程中自动升级；先在应用备份和停写边界内运行 SQLite `migrate()` 到 v19，再
执行 dry-run；目标 PostgreSQL 必须迁移到 v21。

### checkpoint 与跨进程恢复

`--apply` 在首批写入前原子创建 checkpoint，并在每一个已回读校验且已提交的
PostgreSQL 批次后，用同目录临时文件、`fsync` 和原子替换推进
`next_batch_index`。进程在任意批次间退出后，使用相同命令和同一 checkpoint
启动新进程即可恢复：

1. 重新读取并验证完整 SQLite 逻辑快照；
2. 严格核对 checkpoint checksum、格式版本、SQLite schema version、
   PostgreSQL migration version、逻辑源快照 checksum、批大小、固定表顺序及
   批次数；
3. 核对由已解析目标的 host / port / database / user 等非密码字段计算的、
   不泄漏 DSN 或凭据的 SHA-256 目标指纹；
4. 对 checkpoint 标记为已提交的每一批执行目标回读校验；
5. 只从第一个未完成批次继续写入；完成后把 checkpoint 原子标记为
   `status=completed`。

checkpoint 只保存摘要、版本和计数，不保存 DSN、源路径、行身份、学生文本或
payload。checkpoint 被编辑、损坏、来自不同逻辑源、不同目标、不同 schema /
migration version 或不同批计划时，导入器会在新的目标写入前用稳定错误码拒绝，
不会猜测恢复。仅轮换同一目标的密码不会改变目标指纹；host、port、database、
user 或 service 绑定变化会安全拒绝旧 checkpoint。运维人员应先确认目标和既有
批次，再为新的迁移显式使用新的 checkpoint 文件，不得手工修改旧 checkpoint
以绕过绑定。

## `source_file_checksum` 的精确定义

报告字段名 `source_file_checksum` 为兼容既有报告结构而保留，但它不是 SQLite
文件物理字节的 SHA-256。实际值是“逻辑源快照 checksum”：

1. 以 SQLite `mode=ro`、`PRAGMA query_only=ON` 打开源库；
2. 在同一个只读事务中校验 migration ledger、表结构、外键和 allowlist 全部行；
3. 对各表按稳定顺序得到的规范化 row fingerprint、表名以及
   `partial_envelope_rows` 做确定性摘要；
4. 事务结束后返回该摘要。

因此 SQLite 的 WAL、空闲页、页布局或文件复制方式变化不会影响该值；任何被导入
的逻辑记录、版本或内容变化都会改变它。运维核对时应将它解释为
`logical_snapshot_checksum`，不要把它与 `Get-FileHash`/`sha256sum` 的物理文件
摘要比较。

## 迁移范围

导入器关注的是固定 allowlist 表，而不是任意 schema。代码中可见的核心表包含：
- `m0_event_outbox`
- `m0_assessment_runs`
- `m0_learning_events`
- `m4_task_plans`
- `m4_intent_decisions`
- `m5_state_updates`
- `m5_class_states`
- `m5_learner_states`
- `m6_tutoring_decisions`
- `m6_session_states`
- `m6_policy_artifacts`
- `m6_policy_executions`
- `m6_policy_observations`
- `m6_policy_rewards`
- `m6_policy_evaluations`
- `m7_student_feedback`
- `m8_scoring_results`
- `m8_score_audits`
- `m8_assessment_papers`
- `m9_teacher_reviews`
- `m9_teacher_analytics`

M6 policy 行按各自 canonical payload 与 checksum 验证。observation 的嵌入
`decision_id` 必须与行主键及父 `m6_tutoring_decisions` 一致；execution 使用
request first-writer identity；reward 以 execution + reward version 幂等；
evaluation 以 policy + dataset identity 幂等。v11 M0 七字段随
`m0_assessment_runs` 一并导入并接受同等 all-or-none/operation/checkpoint 校验。

## outbox 与源数据限制

SQLite→PostgreSQL 迁移对 outbox 有一个重要限制：已投递并从 `m0_event_outbox` 删除的记录，不会凭空从更宽泛的事件包中恢复成新的 outbox 待投递项。

因此迁移报告需要区分两类结果：
- 源库中仍保留完整 envelope / outbox 记录的行：可做完整契约校验。
- 已投递且 outbox 行已删除、只剩 5 列历史事件的行：只能报告为 `partial_envelope_rows`，并把 `fully_contract_validated=false`。

对应状态应写成：
- `validated_with_source_limitations`
- `completed_with_source_limitations`

这类记录不能被表述为“完整 Pydantic 验证通过”。

## 与 Django migration 的关系

PostgreSQL core migration 与 Django migration 分离：
- Django migration 负责 `m0_platform_web` 的 `User`、`ActorGrant`、`RoleSyncState`、`LoginFailureBucket`
- PostgreSQL core migration 负责模块仓储表

二者都可能在同一 PostgreSQL 实例上存在，但并不是同一套 migration ledger。

## 测试与未实测声明

仓库中存在：
- `tests/unit/test_postgres_foundation.py`
- `tests/integration/test_postgres_foundation_live.py`
- 多个 PostgreSQL repository / import 集成测试

这些测试覆盖 0010/0011 ledger/DDL、fake-PostgreSQL 仓储语义和导入校验；本阶段
未执行真实 PostgreSQL v10→v11 migration。没有受保护 live 数据库时，不能把
单元/fake parity 结果写成真实 PostgreSQL migration 已通过。

真实 PostgreSQL 集成测试现在同时依赖环境变量 `COURSE_INSIGHT_TEST_DATABASE_URL`
和 `COURSE_INSIGHT_TEST_DATABASE_NAME`。任一变量缺失时，destructive live tests
会显式 `skip`，含义仍然是 “real PostgreSQL integration was not run”。
若确认变量与 DSN 中的数据库名不一致，或目标库名是 `postgres` /
`template0` / `template1`，或库名没有以 `_`/`-`/边界分隔的 `test`、`ci`、
`tmp` 一次性标记，
测试会直接 `fail`，而不是继续对危险库执行删表重建。

可接受示例：`course_insight_test`、`ci_course_insight`、`course-tmp`。
不可接受示例：`postgres`、`course_prod`、`social_prod`、
`latest_production`。禁止把生产 DSN 放进这两个测试变量。

PowerShell 示例（仅对可丢弃库）：

```powershell
$env:COURSE_INSIGHT_TEST_DATABASE_URL='<disposable-test-dsn>'
$env:COURSE_INSIGHT_TEST_DATABASE_NAME='course_insight_test'
python -m pytest tests/integration/test_postgres_m0_repository.py tests/integration/test_postgresql_m4_m6_repository_parity.py tests/integration/test_postgres_m5_m9_repositories.py tests/integration/test_sqlite_to_postgres_migration.py -q
```

测试输出中的 `skipped` 只能记录为“未实测真实 PostgreSQL”，不能记录为通过。

## 回滚与恢复

当前能确认的回滚/恢复能力：
- 批次级失败时，导入器会回滚该批次
- 源 SQLite 文件保持不变，可作为回切时的已知数据基线
- 测试辅助函数可以销毁并重建已知 core schema

当前不能确认或不应宣称的能力：
- 生产环境一键回滚到某个 PostgreSQL migration 版本
- PostgreSQL→SQLite 自动反向迁移
- 已投递 outbox 自动重建

生产迁移前必须单独备份 PostgreSQL 与源 SQLite，并记录应用版本、migration
ledger 和逻辑快照 checksum。回切配置不会把 PostgreSQL 期间的新写入自动复制回
SQLite；如果切换后两边都接受写入，会形成不可自动合并的分叉数据。删除目标
schema、覆盖备份或清理源 SQLite 都是破坏性操作，本仓库不会自动执行。
