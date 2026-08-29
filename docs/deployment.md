# 部署说明

> 2026-08-27 当前知识链已经切换为文件入库与原子发布架构。旧 M3 知识包审核只保留历史兼容，不再连接教师页面或新发布流程；详见
> [当前架构](architecture.md) 和 [历史快照](legacy_architecture_snapshot_2026-08-25.md)。

新架构首次运行前执行：

```shell
python manage.py migrate
python manage.py sync_roles --apply
```

本地人工测试建议分别启动：

```shell
python manage.py runserver
python manage.py run_ingestion_worker
python manage.py run_outbox_worker
```

`run_ingestion_worker` 负责知识文件解析、DeepSeek 多知识点抽取、题目标注和活动版本发布；`run_outbox_worker` 只负责既有学习事件投递，两者互不替代。只有核心配置、数据库或日志不可用时 `/health/ready/` 才返回 503。单个 Worker 缺失时返回 HTTP 200 + `degraded`，登录仍可使用，响应中的 `capabilities` 会指出不可用功能。

## 文档边界

本文件描述截至 `2026-08-27` 已实现的 M0 部署面、M1—M3 S1-S6 能力、M5/M8
本地模型能力、M6 私有策略
配置与进程角色。它不把未实测的 PostgreSQL+pgvector 联调写成“已通过”，也不把开发
服务器当成生产 WSGI/ASGI 部署。SQLite 仅用于离线、测试和迁移演练；生产必须使用
PostgreSQL+pgvector。仓库已提供真实 HTTP embedding + PostgreSQL/pgvector + M1—M3
恢复链路的 live 用例；本机没有受保护测试数据库时仍会明确 skip，只有 CI job 通过
后才能记录为联调验收通过。
M5 的 DINA/BKT、M8 的 2PL IRT/能力估计/自适应选择以及 M9 的本地模型质量
已经实现，并仍受数据门槛、shadow 质量审核和教师批准约束。旧 M7 主观评分和 M9
教师解读仍默认关闭，并分别受隐私与质量门限制。知识抽取、题目标注和独立学生答疑使用教师按课程号、班级号保存的密钥；未配置时不出站，也不回退到全局密钥。M2 的 embedding/pgvector 代码、性能验收 CLI 和真实 live 验收用例已经实现；
当前开发机若未提供受保护的 PostgreSQL 测试环境，性能 CLI 会 fail closed，不能
把未执行写成通过。

M6 已实现 rules/shadow/active runtime、artifact 校验、奖励/OPE 与私有持久化，
但默认是 `rules`、零 rollout、零探索。本阶段没有真实教学训练、线上 rollout 或
active 生产验证；部署文档不把这些实现能力写成 active 启用建议，也不声称学习
策略优于 baseline。M4 可选 adapter、M8 自适应选题和 M7 选择性审核同样保持
default-off；发布说明不得把“代码可配置”写成“已生产启用”。

## 配置来源与优先级

平台配置由 `load_platform_settings()` 组装，优先级从低到高如下：

1. `safe_defaults()`
2. `config/app.json`
3. `.env`（路径只有在项目根内才接受）
4. 进程环境变量
5. 代码调用方传入的 `overrides`

推荐部署材料：

- 版本控制中只保留 `config/app.json` 的无密钥配置
- secret 与环境差异放入部署系统环境变量
- 不把真实 `database.url`、`DJANGO_SECRET_KEY` 写入 `app.json`
- `app.json` 只写环境变量名，例如 `database.url_env=DATABASE_URL`
- `.env` 仅用于受控本地部署，必须保持在 Git 忽略范围内

## 关键环境变量

- `COURSE_INSIGHT_ENVIRONMENT`
- `COURSE_INSIGHT_RUNTIME_DIR`
- `COURSE_INSIGHT_CONFIG_DIR`
- `COURSE_INSIGHT_DATABASE__BACKEND`
- `COURSE_INSIGHT_DATABASE__SQLITE_PATH`
- `COURSE_INSIGHT_LOGGING__MODE`
- `COURSE_INSIGHT_WEB__SECURE_COOKIE`
- `COURSE_INSIGHT_M6_POLICY__MODE`
- `COURSE_INSIGHT_M6_POLICY__POLICY_ID`
- `COURSE_INSIGHT_M6_POLICY__EVALUATION_DATASET_IDENTITY`
- `COURSE_INSIGHT_M6_POLICY__ROLLOUT_PERCENTAGE`
- `COURSE_INSIGHT_M6_POLICY__EXPLORATION_RATE`
- `COURSE_INSIGHT_M6_POLICY__GLOBAL_KILL_SWITCH`
- `COURSE_INSIGHT_M6_POLICY__RUNTIME_DIRECTORY`
- `DATABASE_URL`
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `COURSE_INSIGHT_TEST_DATABASE_URL`
- `COURSE_INSIGHT_TEST_DATABASE_NAME`

生产环境必须满足：

- `COURSE_INSIGHT_ENVIRONMENT=production`
- `database.backend=postgresql`
- `database.url` 已通过 `url_env` 间接解析
- `DJANGO_SECRET_KEY` 至少 50 字符、不是 `django-insecure-` 前缀且具有足够字符多样性
- `DJANGO_ALLOWED_HOSTS` 非空且不含 `*`
- `web.secure_cookie=true`
- `logging.mode=stdout`
- 除非另有完整、受审计的 M6 治理批准，保持 `m6_policy.mode=rules`、
  `rollout_percentage=0.0`、`exploration_rate=0.0`

数据库 URL、Django secret、Cookie/Authorization、DeepSeek key、学生原始答案和
完整 prompt 都属于敏感值。配置错误只应暴露字段名与稳定错误码，不应打印值。

默认请求体上限为 `5 GiB + 16 MiB`，用于避免 5 GiB 以内的请求在进入表单前被 Django 拒绝；单个知识文件仍执行独立的 `50 MiB` 业务上限、类型签名和容器校验。生产反向代理的请求体上限也必须不低于应用设置。

登录失败限流同时使用 HMAC 后的 `actor+client IP`、纯 `actor` 和纯
`client IP` 三层桶，分别防止换 actor、换 IP 与单组合重试绕过；数据库不保存原始
actor hint 或 IP。成功登录只清理 actor 相关的两个桶，保留共享 IP 聚合历史直至
窗口自然结束。应用只信任 WSGI `REMOTE_ADDR`，不信任客户端提供的
`X-Forwarded-For`；反向代理部署必须在受信任边缘把传给 Django 的
`REMOTE_ADDR` 归一为真实客户端地址，否则限流会按代理出口聚合。Web 的安全 GET
入口拒绝同名 query parameter 重复出现，避免框架默认“取最后一个值”造成作用域歧义。

## 进程角色

### Web

当前代码提供：

- 根 `manage.py`
- Django settings：`course_insight.web_project.settings`
- process-local runtime：`course_insight.modules.m0_platform.django_app.runtime`

不要用 legacy `prepare_django_frontend()` 的 `skipped` 作业状态判断生产 readiness；
它只维持 intelligence scaffold 公共契约。真实 Web readiness 使用
`/health/ready/`。

Web 进程启动前需要：

1. 有效的平台配置
2. 当前核心 schema 与完整 Django migration
3. 已执行 `sync_roles --apply`
4. SQLite/离线模式：校验 `runtime/snapshots/course_runtime_manifest.json`、其中全部
   快照与 policy，以及 `runtime/artifacts/` 下完整 M1/M2/M3 immutable artifacts；
   生产 PostgreSQL 模式：校验 v21（含 0016/0017 与后续 0018–0021）schema、共享仓储中的 M1/M2/M3 制品、审计、review
   和 ready pgvector 引用

建议命令：

```shell
python manage.py migrate
python manage.py sync_roles --apply
python manage.py runserver
```

### Worker

当前代码提供：

```shell
python manage.py run_outbox_worker --once
python manage.py run_outbox_worker
```

它运行 M0 leased outbox Worker，语义为 at-least-once；Worker 状态文件写入：

```text
runtime/outbox_worker/<worker_id>.status.json
```

知识入库使用独立 Worker：

```shell
python manage.py run_ingestion_worker --once
python manage.py run_ingestion_worker
```

它使用 `runtime/ingestion_worker/worker.lock` 保存进程号和心跳。各处理阶段和每批 DeepSeek 调用都会刷新心跳及数据库租约；进程异常结束后，其他 Worker 可以重新认领租约过期的任务。若启动失败，命令会明确报告锁文件中的 PID、心跳和是否过期；不会再用笼统的 `outbox worker could not start safely` 代表知识入库故障。

### Migration

PostgreSQL 核心 schema migration 与 Django migration 是两套：

- Django migration：只覆盖 M0 Web identity / authorization 表
- PostgreSQL core migration：`src/course_insight/infrastructure/postgresql/migration_runner.py`

SQLite 的 `m1_course_packages`、`m2_evidence_indexes`、`m3_knowledge_bundles`
用于离线/测试和迁移演练；SQLite 不是生产权威后端。生产 PostgreSQL core migration
0016 创建 `m1_m2_m3_artifacts`、`m2_vector_indexes`、`m2_vector_documents`、
`m2_retrieval_audits` 和仅供旧发布兼容的 `m3_teacher_reviews`，0017 为向量索引补充
受治理的 package/model metadata；当前 PostgreSQL core schema 为 v21。两者由共享的
`PostgresM1M2M3Repository` 与 pgvector 适配器接收 M1—M3 业务写入。

SQLite→PostgreSQL 数据迁移入口：

```shell
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --dry-run
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --apply
```

`--apply` 通过单一 `PlatformSettings` 加载配置，并要求解析后的
`database.backend=postgresql` 且 `database.url` 可用。配置非法、后端非 PG 或
URL 缺失时，CLI 安全返回 `MIGRATION_CONFIGURATION_INVALID`。

迁移报告字段 `source_file_checksum` 实际表示同一个 SQLite 只读事务中计算的
逻辑快照 checksum，不是数据库文件的物理字节 SHA-256。详见
[PostgreSQL 迁移说明](postgresql_migration.md)。

### Runtime manifest 与 policy

Web 第一次恢复课程上下文时读取
`runtime/snapshots/course_runtime_manifest.json`。文件只能包含
`schema_version` 和 `courses`，每个 course 必须恰好包含以下字段：

```json
{
  "schema_version": 1,
  "courses": [
    {
      "course_id": "course_example",
      "course_package_ref": "snapshots/course-package.json",
      "evidence_index_ref": "snapshots/evidence-index.json",
      "knowledge_bundle_ref": "snapshots/knowledge-bundle.json",
      "state_policy_ref": "policies/state.json",
      "teacher_threshold_policy_ref": "policies/teacher.json"
    }
  ]
}
```

当前 `schema_version=1` 的 manifest 协议要求随 manifest 保留三份
`runtime/snapshots/` 契约快照。SQLite/离线模式下它们与完整
`runtime/artifacts/` 一起用于恢复和 cross-check；生产 PostgreSQL 模式下，M1—M3
制品、审计和旧 review 兼容记录由共享仓储恢复，manifest/snapshot 仅作显式配置的引用和身份
cross-check。部署备份和恢复必须覆盖所选后端的权威数据、manifest 与必要快照；不能
只复制 manifest、单个契约 JSON 或数据库表。当前不把 ready 向量索引的自动发现写成
已完成能力。

所有引用必须是 `runtime/` 内无 `..` 的相对路径。部署启动前必须先校验：

- 校验 `runtime/artifacts/` 中 M1 完整课程导入、M2 完整 lexical snapshot，以及
  旧 S1-S6 兼容路径的 M3 bundle + seed snapshot + validation report；

SQLite/离线启动时默认由共享 `SQLiteM1M2M3Repository` 读取完整制品；显式文件后端或
导出恢复场景才由 `FileM1Repository.get_course_package`、
`FileM2Repository.load_index_artifact`、`FileM3Repository.load_bundle_artifact`
读取 `runtime/artifacts/` 中的 immutable artifacts；
- 校验课程/包/索引身份、package checksum 与发布/ready 状态，并将必需的 snapshots
  仅作身份 cross-check；
- 直接恢复完整 M2 lexical snapshot，不重新 `build_index`；

生产 PostgreSQL 启动时还必须：

- 通过 v22 schema 和 `PostgresM1M2M3Repository` 校验 M1—M3 制品、审计与旧 review 兼容记录；
- 为 vector/hybrid 检索显式提供并校验 `EvidenceIndexRef`，调用
  `restore_vector_index`；自动发现 ready 向量索引已通过 durable metadata 恢复路径实现，
  但本机未提供可执行的 PostgreSQL+pgvector 测试环境；CI `live-m1-m3` job 会执行
  真实 HTTP embedding、M1 导入、M2 检索审计、旧 M3 兼容发布和重启恢复；
- 用真实 `StatePolicy.from_path()` 和 `TeacherThresholdPolicy.from_path()` 解析两份
  policy，而不只是检查文件存在。

任一文件缺失、过大、越界、字段多余或内容不一致都会使 readiness 返回 503，
错误响应不包含绝对路径。

M6 learned policy 使用独立的私有 manifest/artifact 链，不把字段添加到上述课程
runtime manifest。M6 Repository 按 `m6_policy.policy_id` 读取 immutable
`PolicyArtifactManifest`，再把其中的相对 `.json` `artifact_reference` 限制在
`m6_policy.runtime_directory` 下。默认相对目录 `m6_policy` 会解析为
`<runtime_dir>/m6_policy`。rules 模式在 manifest/artifact/evaluation I/O 前返回；
shadow/active 才会尝试加载，失败时回退 rules。

Artifact 只允许 UTF-8 canonical JSON；拒绝 pickle/joblib、绝对路径、`..`、符号
链接逃逸、重复键、NaN/Infinity、SHA-256 或 23 维 feature/8 动作版本不匹配。
完整字段和 promotion/rollback 见
[M6 策略学习运维指南](m6_policy_operations.md)。

### roles.csv 是完整期望状态

`config/roles.csv` 一行表示一个伪匿名授权关系。`sync_roles` 只在命令运行时读取
CSV；请求期间从 Django `User`、Group/Permission 与 `ActorGrant` 读取两层权限。

- `--check` 只校验 CSV 和 checksum，不读写授权差异；
- `--dry-run` 读取数据库并输出 `create/activate/revoke/unchanged`，不写入；
- `--apply` 在一个事务中同步 Group 权限、用户、grant、membership 和同步元数据；
- 同一文件重复 apply 幂等；
- 文件是该来源管理 grant 的完整期望状态：之前存在但本次省略的 grant 会撤销，
  写入 `is_active=false` 和 `revoked_at`；若用户不再有该角色的有效 grant，
  对应 Group membership 也会删除；
- CSV 中 `is_active=false` 同样表示显式撤销；
- 命令创建的账号初始为 unusable password，密码不写入 CSV。

先比较 `--dry-run`，确认省略撤销符合预期后再 `--apply`。错误 actor、冲突角色、
非法 scope 或缺少 Django permission 时命令 fail closed。

### 日志并发策略

`app.log` 是结构化运行日志；`runtime/.../audit/learning_events.jsonl` 是领域事件
审计；数据库评分审计和教师复核又是另一套追加历史，三者不能互换。

- `logging.mode=rotating_file` 仅用于开发/测试的单进程独占写入。writer lock 会
  拒绝第二个进程打开同一日志文件。本机同时跑 Web 和 Outbox Worker 时，Worker
  使用 `outbox.log`，Web 仍使用 `app.log`。
- 生产部署必须使用 `logging.mode=stdout`，由 systemd、
  容器平台或日志代理采集、轮转和保留各进程 stdout。
- 不要让 Web/Worker 各自使用普通 `RotatingFileHandler` 写同一文件。
- 日志在格式化前递归脱敏，不记录答案、Cookie、Authorization、Session、密钥、
  database URL、prompt、绝对路径或完整异常。

### Test

统一测试入口：

```shell
python -m pytest -q
```

pytest 配置只发现当前 `tests/` 目录，并排除 `runtime/`、历史工作树和临时目录；
运行数据不会被当成测试源码。若需要只检查某个边界，可显式传入 `tests/...` 路径。

真实 PostgreSQL 集成测试依赖环境变量
`COURSE_INSIGHT_TEST_DATABASE_URL` 与
`COURSE_INSIGHT_TEST_DATABASE_NAME`。若任一变量缺失，则
`tests/integration/test_postgres_foundation_live.py` 等 destructive live tests
会显式 `skip`，表示 “real PostgreSQL integration was not run”。
GitHub Actions CI 使用一次性的 PostgreSQL 16 服务并注入这两个变量，
因此 CI 的完整测试会实际执行 M0、M4、M5、M6、M8 相关的 PostgreSQL live tests；
这不替代生产数据库验收。
若确认变量与 DSN 的数据库名不一致，或数据库名属于 `postgres`、
`template0`、`template1`，或没有以 `_`/`-`/边界分隔的 `test`、`ci`、`tmp`
一次性标记，
测试会直接失败，避免对危险库执行 schema destroy / rebuild。

## M6 发布门禁清单

常规生产部署应保持示例的 rules-safe 默认值。任何 shadow/active 变更都必须在
部署变更单中逐项记录：

- [ ] 当前 PostgreSQL core ledger 为 v22；确认 M4 intent 使用 v10/v11，M6 policy 使用 v12，
  M0 freeze 使用 v13，M5/M8 model runtime 使用 v14，观测审计身份使用 v15，
  M1—M3 S1-S6 与向量 metadata 分别使用 v16/v17，M7/M9 模型审计、等待队列、
  重评操作和动态班级名单使用 v18—v22，
  且未改写已应用 migration；
- [ ] 使用全新 immutable `policy_id`；manifest 的 state graph/baseline/feature/
  action/reward/gate version 已治理，artifact 与 manifest 重叠的
  policy/adapter/feature/action 字段及 lowercase SHA-256 完全一致；
- [ ] artifact 位于解析后的 runtime root 内，是 23 维、覆盖全部 8 个安全动作、
  有限数值的 canonical JSON；
- [ ] shadow 证明公共动作仍为 rules baseline，且 observation 的 logging
  propensity、模型分数、不确定性、原因和版本身份完整；
- [ ] reward 只来自真实后续证据；pending/censored/invalid 未伪造成 0；
- [ ] JSONL 使用运行时 HMAC key 去标识，group/session 不跨数据切分；
- [ ] OPE 精确匹配 `policy_id`/`dataset_identity`，状态为 `sufficient_data`、
  `approved=true`，IPS/SNIPS/DM/DR、CI、ESS 和 coverage 完整；
- [ ] active 的 course/class 配置 allowlist 与 manifest scopes 都精确覆盖本次
  范围，rollout 非零但受控，探索不超过 0.05，支持度/不确定性阈值有审批依据；
- [ ] kill switch 和回退 rules 的正常重启步骤已演练，旧 execution/M0 freeze
  不会被重写；
- [ ] 明确记录 M9 尚未接入 M6 OPE/approval；当前 M9 公共入口只接 M8
  `CalibrationRunResult`；
- [ ] 明确记录本阶段没有真实教学/线上 rollout/live PostgreSQL migration 证据，
  不宣称 active 已可生产启用或优于 baseline。

## 从零部署与人工验收（20 步）

下面命令从仓库根目录执行。示例主命令使用 Windows PowerShell；Linux/macOS 将
`$env:NAME='value'` 换成 `export NAME='value'`，将 `Copy-Item` 换成 `cp`。
密码和 DSN 不应直接出现在共享终端记录、CI 日志或截图中。

### 1. 创建独立 Python 环境

命令：

```shell
conda create --name course-insight-m0 python=3.11 -y
conda activate course-insight-m0
python --version
```

预期：Python 为 3.11+，且 `where python`/`which python` 指向新环境。失败时检查
Conda 初始化与当前 shell，不要把依赖装入 base。

### 2. 安装并核对依赖

命令：

```shell
python -m pip install --constraint requirements/ci-constraints.txt -e ".[dev,intent]"
python -m pip check
```

预期：安装成功，`pip check` 输出 `No broken requirements found.`。失败时检查
Python 版本、constraint 冲突和网络/私有镜像；不要临时删除版本约束。

### 3. 复制无密钥示例配置

命令：

```powershell
if (-not (Test-Path -LiteralPath config/app.json)) { Copy-Item -LiteralPath config/app.example.json -Destination config/app.json }
if (-not (Test-Path -LiteralPath config/roles.csv)) { Copy-Item -LiteralPath config/roles.example.csv -Destination config/roles.csv }
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

预期：三个目标文件存在；Git 仍不会跟踪真实 `.env`。失败时检查当前目录和文件
权限。已有文件必须先备份并人工合并，不能被示例覆盖。不要把示例伪匿名 ID
替换为真实姓名、学号、邮箱或电话。

### 4. 设置生产配置与 secrets

在 secret manager 或当前受控进程环境中设置值；以下只展示变量名：

```powershell
$env:COURSE_INSIGHT_ENVIRONMENT='production'
$env:COURSE_INSIGHT_DATABASE__BACKEND='postgresql'
$env:COURSE_INSIGHT_LOGGING__MODE='stdout'
$env:COURSE_INSIGHT_WEB__SECURE_COOKIE='true'
$env:DATABASE_URL='<postgresql-dsn-from-secret-manager>'
$env:DJANGO_SECRET_KEY='<at-least-50-random-characters>'
$env:DJANGO_ALLOWED_HOSTS='insight.example.edu'
$env:DJANGO_CSRF_TRUSTED_ORIGINS='https://insight.example.edu'
```

预期：`load_platform_settings()` 能构造 production settings，且错误输出不回显
secret。失败时检查 `url_env`、DSN 语法、secret 长度、allowed hosts、secure
cookie 和 stdout 模式。不要把实际值写回 `config/app.json`。

### 5. 备份 SQLite 并创建 PostgreSQL 应用库

在切换前停止旧写入，复制现有 SQLite 数据库和 `-wal`/`-shm`（若存在），保留离线
模式的 `runtime/artifacts/` 作为迁移输入，再由 DBA 创建专用 PostgreSQL 应用库与
最小权限账号。生产备份必须同时覆盖 PostgreSQL 0016/0017 中的 M1—M3 制品、向量索引/文档、
检索审计和教师复核记录；不能只备份 SQLite 文件或只复制 manifest。示例：

```shell
createdb --host 127.0.0.1 --username course_insight_owner course_insight_app
```

预期：目标是空的专用库，应用账号可连接但不是 PostgreSQL 超级用户；SQLite
备份可只读打开。失败时检查 DNS/TLS、账号权限和备份一致性。此处的应用库绝不能
用于 destructive live tests。

### 6. 执行核心 migration 或显式导入

全新空库只应用 core migration：

```shell
python -c "from pathlib import Path; from course_insight.infrastructure.config import load_platform_settings; from course_insight.application.factory import build_application; s=load_platform_settings(project_root=Path.cwd()); c=build_application(s); c.m0_service.initialize(); print('core migrations ready'); c.close()"
```

从 SQLite 迁移则必须先 dry-run，再 apply：

```shell
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --dry-run
python scripts/migrate_sqlite_to_postgres.py --project-root . --source runtime/course_insight.db --report runtime/migration-report.json --apply
```

预期：PostgreSQL core schema version 为 22；v10/v11 属于 M4 intent，v12 新增 M6 私有
policy 表，v13 为 `m0_assessment_runs` 追加策略冻结字段，v14 新增 M5/M8 模型
运行历史，v15 为学习观测追加权威评分审计身份，v16/v17 分别追加 M1—M3 S1-S6
能力与向量 metadata，v18—v22 追加模型调用审计、等待队列、重评操作和动态班级名单冻结。
报告为 `validated`/`completed`，或在已投递旧事件
存在时明确为 `*_with_source_limitations`。失败时检查 `error_code`、migration
checksum、源 schema version、`partial_envelope_rows` 和每表 digest。不要用
`Get-FileHash` 比较报告的 `source_file_checksum`，它是逻辑快照 checksum。

v8 workflow 行升级后新增字段保持 NULL。应用仅在相同 operation 首次重放、
持久化 TaskPlan 的知识包/课程包锚点匹配且新增字段全部为空时做一次 CAS 接管；
部分填充或缺少必要精确状态引用的行会 fail closed，运维人员不得直接手工填值。

v12→v13 的历史 submit 行同样只允许窄化 adoption：M6 七字段必须全 NULL，
checkpoint 只能是 `tutoring_saved|feedback_saved|analytics_saved`，并且已有保存
状态和 frozen prior-state 标记。部分策略身份、`policy_frozen` 行或 review
operation 不得手工补齐。

### 7. 检查并执行 Django migration

命令：

```shell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python manage.py migrate
```

预期：`check` 无错误，`makemigrations` 输出 `No changes detected`，随后只应用
Django auth/session 与 M0 Web identity/authorization migrations。失败时检查
production 配置、数据库权限和 migration 冲突；不要让 Django migration 管理
M1—M9 领域表。

### 8. 校验、预览并应用完整 roles 状态

命令：

```shell
python manage.py sync_roles --check
python manage.py sync_roles --dry-run
python manage.py sync_roles --apply
python manage.py sync_roles --dry-run
```

预期：check 显示 `valid=true`；首次 dry-run 给出差异；apply 原子完成；第二次
dry-run 的 create/activate/revoke 均为 0。失败时检查伪匿名 actor、角色/scope
形状和 migration。特别核对 `revoke=<n>`：从 CSV 省略旧 managed grant 会撤销它，
不是保留幽灵权限。

### 9. 为验收账号设置密码

`sync_roles` 创建的账号默认没有可用密码。通过 Django Password Hasher 的交互命令
分别设置，不把密码写进 CSV 或命令行参数：

```shell
python manage.py changepassword pseudonym_student_001
python manage.py changepassword pseudonym_teacher_001
```

预期：两次提示 `Password changed successfully`。失败时检查 actor 是否由
`sync_roles --apply` 创建，以及 username 是否等于 actor_id。

### 10. 准备并验证 course runtime manifest 与两份状态/教师 policy

SQLite/离线验收时，将课程初始化产生并由 M0 保存的 `CoursePackage`、
`EvidenceIndexRef`、`KnowledgeBundle` 快照放入 `runtime/snapshots/`；同时保留
M1/M2/M3 File repository 写入的完整 `runtime/artifacts/`。快照不能替代 artifact：
M2 要有完整 lexical snapshot，M3 要有 bundle、seed snapshot 和 validation report。
生产 PostgreSQL 验收则校验 v22 migration、共享仓储中的 M1—M3 制品、审计、review
和 ready pgvector 引用，不把 SQLite 文件当成生产数据源。把真实可解析的
`StatePolicy` 与 `TeacherThresholdPolicy` 放入 `runtime/policies/`，再写上述
manifest。不要手工伪造 checksum。验证命令：

```shell
python -c "import django; django.setup(); from course_insight.modules.m0_platform.django_app import runtime; r=runtime.get_web_runtime(); print(tuple(r.courses)); runtime.close_application_container()"
```

预期：输出配置的 course ID。失败时检查相对引用、快照契约、package/index
checksum、published/ready 状态，以及两份 policy 的必需字段和数值范围。

另行核对 M6 默认关闭状态：

```shell
python -c "from pathlib import Path; from course_insight.infrastructure.config import load_platform_settings; s=load_platform_settings(project_root=Path.cwd()); p=s.m6_policy; print(p.mode, p.rollout_percentage, p.exploration_rate, p.global_kill_switch, p.runtime_directory)"
```

常规部署预期前三项为 `rules 0.0 0.0`，kill switch 为 `False`，最后一项是
`runtime_dir` 下的 `m6_policy` 绝对解析结果。该绝对路径只用于本机人工核对，不得
复制到共享日志、manifest 或制品。若本次仅部署 baseline，不要注册或加载 learned
manifest。

### 11. 配置日志采集

生产环境启动 stdout 日志收集器（systemd journal、容器 logging driver 或同等
平台能力），并核对应用模式：

```shell
python -c "from pathlib import Path; from course_insight.infrastructure.config import load_platform_settings; s=load_platform_settings(project_root=Path.cwd()); print(s.logging.mode)"
```

预期：输出 `stdout`。失败时检查环境变量优先级。开发 `rotating_file` 下同一文件
只能一个进程独占；本机 Web 写 `app.log`，Worker 写 `outbox.log`。若出现
`LOG_SINK_IN_USE`，不要移除 writer lock 后强行多进程写同一文件。

### 12. 验证一次投递并启动常驻 Worker

先做有界检查：

```shell
python manage.py run_outbox_worker --once
```

预期：无错误时输出 `outbox worker stopped`；snapshot 有 `last_error_code` 时命令
必须非零退出。随后在独立服务/终端启动：

```shell
python manage.py run_outbox_worker
```

预期：`runtime/outbox_worker/*.status.json` 的状态为 `running` 或 `idle`，heartbeat
持续更新。失败时检查数据库、migration、审计目录权限和 stdout 日志。不要在
View、`AppConfig.ready()` 或 Web worker hook 中启动它。

### 13. 执行部署检查并启动 Web

命令：

```shell
python manage.py check --deploy
```

在真实生产中由受管 WSGI/ASGI 服务器和 HTTPS 入口加载
`course_insight.web_project.wsgi/asgi`。仅做本机 HTTP 人工验收时，在独立终端
显式改成开发配置后运行：

```powershell
$env:COURSE_INSIGHT_ENVIRONMENT='development'
$env:COURSE_INSIGHT_WEB__SECURE_COOKIE='false'
python manage.py runserver 127.0.0.1:8000
```

预期：production 配置下没有 system check error；任何 warning 都需结合反向代理、
TLS 与部署拓扑逐项处理。开发配置运行 `check --deploy` 出现 secure-cookie/HSTS
类 warning 是预期诊断，不代表生产配置合格。`runserver` 仅用于人工验收。

### 14. 验证 live 与 ready

在另一终端执行：

```shell
curl.exe -i http://127.0.0.1:8000/health/live/
curl.exe -i http://127.0.0.1:8000/health/ready/
```

预期：live 为 200 `{"status":"live"}`；配置、数据库、migration、runtime、logging
和 Worker heartbeat 全部正常时 ready 为 200 且 `status=ready`。503 时按响应中的
安全组件名定位，不会得到 DSN、主机、绝对路径、SQL 或异常堆栈。

### 15. 验证学生流程

浏览器访问 `/accounts/login/`，以 student 伪匿名账号登录，进入 `/student/`，
选择获授权课程/班级，开始测评、提交并查看结果和反馈。

预期：页面只显示该 actor 的范围；表单字段来自冻结 `AssessmentPaper`，POST 走
CSRF 与 PRG，结果来自 M8、反馈来自 M7 的公开入口。失败时检查 Django permission、
唯一有效 `ActorGrant`、manifest 与表单错误；不要通过 Session 或直接查表补数据。

### 16. 验证教师分析与复核

退出 student，再以 teacher 登录 `/teacher/`，打开授权班级、复核上下文，对待复核
audit 执行 confirm/override/reject。

预期：需要 `view_class_analytics`、`view_student_report`，提交复核还需要
`review_score`；override 追加 audit v2、新 LearningEvent 与刷新后的 analytics，
不会覆盖 v1。失败时检查精确班级 grant、audit/version、分项和总分守恒。

### 17. 验证越权、CSRF 与错误映射

用 student 请求其他 learner/课程，或用 teacher 请求未授权班级；再提交缺失/错误
CSRF token、旧 audit version 和未知 paper。

预期：未登录跳转登录；无权限为 403；不存在为 404；版本冲突为 409；无效表单为
400。错误页不显示路径、SQL、堆栈或答案。若越权返回 200，立即停止验收并检查
Django Group/Permission 与 `authorize_scope()` 的 exact grant。

### 18. 验证 Outbox 与日志脱敏

学生提交和教师复核后，等待 Worker 投递，再检查：

```powershell
Get-Content -LiteralPath runtime/audit/learning_events.jsonl
Get-ChildItem -LiteralPath runtime/outbox_worker -Filter *.status.json
```

生产模式从日志平台查询同一 request/attempt/worker 关联 ID；开发单进程才检查
`runtime/logs/app.log`。预期：审计 JSONL 每个 `event_id` 只出现一次，Worker
delivered_count 增加；运行日志不含密码、Cookie、Authorization、Session、DSN、
学生原始答案或 prompt。若 sink 已写但 ack 失败，重启后应去重再确认。

### 19. 验证 PostgreSQL、live tests 与重启恢复

先在生产应用库用只读 SQL 核对 migration ledger、0016/0017 的 M1—M3/pgvector/review
表、关键计数和追加版本；SQLite/离线模式才核对 `runtime/artifacts/` 中每个完整制品
的 manifest、身份、版本和 payload checksum。正常停止并重启 Web/Worker 后，重新
恢复已有课程、索引和知识包，再访问结果与复核上下文。destructive live tests 必须
使用另一个可丢弃数据库：

```powershell
$env:COURSE_INSIGHT_TEST_DATABASE_URL='<dsn-ending-in-course_insight_test>'
$env:COURSE_INSIGHT_TEST_DATABASE_NAME='course_insight_test'
python -m pytest tests/integration/test_postgres_m0_repository.py tests/integration/test_postgresql_m4_m6_repository_parity.py tests/integration/test_postgres_m5_m9_repositories.py tests/integration/test_sqlite_to_postgres_migration.py tests/unit/test_postgres_s1_s6_migration.py tests/unit/test_postgres_m1_m2_m3_repository.py -q
```

预期：重启后稳定 ID、评分、状态、反馈与分析可恢复，且 M1—M3 制品、审计、review
和 ready 向量引用可校验；live tests 必须实际运行而不是 skip。任一变量缺失会 skip，
验收报告必须明确“未执行真实 PostgreSQL+pgvector 联调”；名称不匹配、保留库或无
分隔 marker 会 fail。绝不把生产应用库 URL 复用为测试 URL。

### 20. 演练回切 SQLite

先停止 Web/Worker，确认没有写入，再成对恢复迁移前 SQLite 备份和同一备份点的
`runtime/artifacts/`（以及 manifest 所引用的 snapshots），然后切换：

```powershell
$env:COURSE_INSIGHT_DATABASE__BACKEND='sqlite'
$env:COURSE_INSIGHT_DATABASE__SQLITE_PATH='runtime/course_insight.db'
$env:COURSE_INSIGHT_ENVIRONMENT='development'
$env:COURSE_INSIGHT_LOGGING__MODE='rotating_file'
python manage.py check
python manage.py run_outbox_worker --once
```

预期：SQLite 历史 core migration 仍可前向到 version 13，M1—M3 S1-S6 独立 ledger
为 version 1，应用可读取切换前的 SQLite 基线。失败时检查备份、文件权限和 schema
ledger。仓库没有 PostgreSQL→SQLite 自动
反向迁移；切到 PostgreSQL 后产生的新数据不会出现在旧 SQLite。只有在明确接受
该数据水位差异、或另行完成受审计的数据回迁后，才能把回切用于生产。

## Web / Worker / migration / test / SQLite 回滚

### Web 回滚

Web 回滚是部署层回滚：

- 回到上一版应用包
- 保留当前数据库与完整 runtime 目录，尤其不能丢失 M1/M2/M3 artifacts
- 若上一版不兼容新的 Django migration，需要先确认 schema 兼容性

### Worker 回滚

Worker 回滚通常是停止独立 Worker 进程。由于 outbox 是 at-least-once：

- 已投递并确认的记录不会回到队列
- 已 claim 但未 ack 的记录会在 lease 过期后被重新领取
- 协作式停止会等待正在执行的 append，并继续续租；强制杀进程只应作为最后手段

### M6 policy 回滚

最保守的回滚是设置 `m6_policy.mode=rules`、rollout/exploration 为 0，并设置
`global_kill_switch=true` 后正常重启。不要删除或覆盖 manifest、artifact、
evaluation、execution、observation、reward 或 M0 freeze 字段。已提交决定继续
按原结果重放；尚未提交决定的旧 learned execution 会按冻结身份、探索率和 gate
结论重新解析原 immutable artifact，不能被新的 mode/policy 替换。精确制品缺失或
校验不一致时回退 rules；`global_kill_switch=true` 始终覆盖冻结批准。

若回到旧 learned policy，只能选先前验证过的 immutable `policy_id` 和精确
`evaluation_dataset_identity`，重新从 shadow/gate 开始；仓库没有覆盖同一
policy 记录或热更新 settings 的安全入口。

### Migration 回滚

当前明确存在的是：

- PostgreSQL 测试 schema 销毁/重建辅助函数 `destroy_schema_for_tests()` 与 `rebuild_schema_for_tests()`
- SQLite→PostgreSQL 单批失败的事务回滚和原 SQLite 保留
- SQLite/离线回滚可恢复经过 checksum 验证的完整 `runtime/artifacts/` 版本；生产
  PostgreSQL 回滚必须恢复经过校验的 0016/0017 数据库备份或 manifest，不是 migration 降级，
  也不能只恢复 SQLite 预留表

这些不是生产 schema 回退工具。上线前必须由数据库平台创建可恢复备份并记录
core/Django migration ledger。仓库不支持生产一键降级 PostgreSQL migration，
不得在生产调用测试用 destroy/rebuild。

### Test 回滚

测试失败后的回滚依赖各仓储事务和测试隔离，不是独立部署功能。

### SQLite 回滚

当前能确认的 SQLite 回滚只有“配置回切”：

- 把 `database.backend` 改回 `sqlite`
- 指向已有的 SQLite `runtime/...db`
- 重新初始化本地进程

仓库中没有 PostgreSQL→SQLite 自动数据回迁工具，因此不要写“支持自动回滚到
SQLite 数据集”。回切不包含 PostgreSQL 期间新增的数据；两个后端同时重新接受
写入会形成分叉，不能自动合并。

## 日志、审计与敏感信息

- 应用日志：`app.log` 或 stdout
- 审计日志：`runtime/.../audit/learning_events.jsonl`
- Worker 状态：`runtime/outbox_worker/*.status.json`

`app.log` 与审计文件是两个不同面：

- `app.log` 是运行观测
- 审计 JSONL 是领域事件凭据

部署时不应把真实课程原始文件、数据库文件、runtime 快照、日志或审计 JSONL 打进可分发产物。

配置文件、PostgreSQL/SQLite 数据、`runtime/artifacts/`、runtime snapshots、日志、
审计、用户密码与 secret manager 变更必须分别备份/回滚；其中 M1/M2/M3 必须分别
覆盖完整课程导入、完整 lexical snapshot、bundle + seed snapshot + validation
report。删除数据库、覆盖备份、清理源 SQLite 或删除单个 artifact payload、确认
outbox 投递和外部日志保留策略都可能不可逆；执行前必须解析精确目标并由部署负责
人确认。

## S1-S6 上线补充（当前权威）

### 生产后端与配置

生产必须选择 `database.backend=postgresql`，并运行 PostgreSQL migration 至 v21：
`0016_m1_m2_m3_capabilities.sql` 创建完整 M1/M2/M3 制品表、pgvector 索引/文档表、
检索审计表和教师复核 CAS 表，`0017_vector_index_metadata.sql` 补充向量索引的
课程包/embedding 模型 metadata；M1/M2/M3 在应用组合根共用一个
`PostgresM1M2M3Repository`，不会在 SQLite 与 PostgreSQL 之间隐式 fallback。

向量检索启用时还必须设置：

- `COURSE_INSIGHT_EMBEDDING__BACKEND=openai_compatible`
- `COURSE_INSIGHT_EMBEDDING__ENDPOINT`（生产必须 HTTPS）
- `COURSE_INSIGHT_EMBEDDING__API_KEY_ENV` 与对应 secret manager 环境变量
- `COURSE_INSIGHT_EMBEDDING__MODEL_NAME`
- `COURSE_INSIGHT_EMBEDDING__MODEL_VERSION`
- `COURSE_INSIGHT_EMBEDDING__DIMENSION`

应用检索策略由同一份受治理配置选择，不能在业务调用点临时拼接：

- `COURSE_INSIGHT_RETRIEVAL__POLICY_ID`
- `COURSE_INSIGHT_RETRIEVAL__STRATEGY=lexical|vector|hybrid`
- `COURSE_INSIGHT_RETRIEVAL__TOP_K`
- `COURSE_INSIGHT_RETRIEVAL__LEXICAL_WEIGHT`
- `COURSE_INSIGHT_RETRIEVAL__VECTOR_WEIGHT`
- `COURSE_INSIGHT_RETRIEVAL__RERANK`

`AppCoordinator` 和 `assessment_workflow` 将这一版本化策略传入
`retrieve_for_application -> retrieve_with_policy`；因此审计中的 `policy_id` 与实际
业务策略一致。vector/hybrid 缺少 ready pgvector、embedding 或审计仓储时必须 readiness
失败，不能降级成伪造的 `empty` 成功。

密钥只从环境变量读取，不写入 settings、审计、数据库 payload 或日志。embedding 模型
身份和维度属于索引身份；替换模型必须新建 index version，完成 staging 校验后发布。

### 正式链路与恢复

M1 导入保存 parser id/version 和完整输入快照；M2 使用 lexical、vector 或 hybrid
策略，正式业务入口是 `retrieve_with_policy`，对成功/无结果写入脱敏审计；旧
`retrieve` 仅兼容已有 lexical 调用。M3 当前由 `KnowledgeIngestionProcessor` 构建完整候选版本并在事务中原子发布，不再等待知识包复核 CAS。重启时恢复活动 `CourseKnowledgeRelease`、文件版本和检索引用；旧 M1/M2/M3 制品及 review 表只作兼容读取。向量恢复调用
`restore_vector_index`，不会重新请求 embedding。自动发现 ready 向量索引已通过 durable
metadata 恢复路径实现。当前 `AppCoordinator` 和 `assessment_workflow` 已通过
`retrieve_for_application -> retrieve_with_policy` 进入正式 M2 边界；旧 `retrieve` 仅作
兼容。完整 vector/hybrid 生产链须以 CI `live-m1-m3` job 的实际结果作为生产验收依据。现有 M0 教师 Web 的 `TeacherReviewSubmission` 只用于 M8/M9 成绩复核，与知识发布无关。

### 向量性能验收与 ANN 索引

`0016` 默认保留精确余弦搜索，保证结果召回率；它不会在未知 embedding 维度时盲目
创建全表 ANN 索引。上线前必须用目标课程规模定义 chunk 数、P95 检索耗时和召回率，
先运行 `EXPLAIN (ANALYZE, BUFFERS)` 与重复请求基准，再决定是否为每个 ready index
创建按维度绑定的表达式索引。例如配置维度为 1536 时：

```sql
CREATE INDEX CONCURRENTLY m2_vec_hnsw_<safe_suffix>
ON m2_vector_documents
USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE index_id = '<index_id>' AND index_version = '<index_version>';
```

索引名和 `WHERE` 值必须由受治理的 index identity 生成，不能把用户输入直接拼接进 SQL。
M2 PostgreSQL adapter 的查询也使用同一维度表达式，使该 partial index 可被规划器使用；
创建后必须在目标规模上同时记录 P50/P95、索引构建时间、索引大小和召回率。没有这些
实测数据时只能标记“功能可用、性能未验收”，不能把精确搜索或 ANN 参数写成生产 SLA。

仓库提供 `python -m scripts.benchmark_m2_pgvector` 作为可执行的 exact-search
性能门禁。它只读取 `COURSE_INSIGHT_TEST_DATABASE_URL` 和匹配的
`COURSE_INSIGHT_TEST_DATABASE_NAME`，只允许名称含边界分隔 `test`、`ci` 或 `tmp`
的 disposable 数据库。guard 配置缺失、名称不匹配、保留库名或 DSN 格式不安全时输出
`status=blocked`、exit 2；guard 通过后，连接、migration、pgvector、benchmark 或
cleanup 运行失败时输出 `status=failed`、exit 1，不会 skip，也不会读取生产
`DATABASE_URL`。CLI 使用现有 migration、pool 和
`PostgresPgVectorStore`，每次生成唯一安全 benchmark identity，finally 只删除该
identity 的向量行和索引行。

性能 CLI 使用可选依赖 `performance`：

```shell
python -m pip install -e ".[performance]"  # numpy>=2,<3
```

缺少或版本不满足的 NumPy 会输出 `status=blocked` 并以非零退出。合成向量和 query
先量化为 NumPy `float32`；ground truth 使用分块矩阵乘法与 Top-K 合并，避免按
`chunk * query * dimension` 做 Python 标量循环。写入沿用生产 `add_many` 批量能力，
默认/上限为受治理的 bounded batch；`chunk_count * query_count * dimension` 另有工作量
上限。`50,000 * 100 * 1,536 = 7.68e9` 的目标示例允许执行，超过组合上限的极端参数在
连接数据库前 fail closed。

目标规模验收必须在接近生产数据规模的 disposable clone 中显式执行，不能使用
smoke 默认值代替。PowerShell 示例（数值必须按目标课程和已批准 SLA 替换）：

```powershell
$targetChunkCount = 50000
$targetDimension = 1536
$targetQueryCount = 100
$targetTopK = 10
$targetMaxP95Ms = 250
$targetMinRecallAtK = 1.0
$targetSeed = 20260815
$targetBatchSize = 1024
python -m scripts.benchmark_m2_pgvector `
  --chunk-count $targetChunkCount `
  --dimension $targetDimension `
  --query-count $targetQueryCount `
  --top-k $targetTopK `
  --max-p95-ms $targetMaxP95Ms `
  --min-recall-at-k $targetMinRecallAtK `
  --seed $targetSeed `
  --batch-size $targetBatchSize
```

stdout JSON 是默认输出，不写持久化过程报告。结果必须原样保存到 CI artifact/log，
至少检查 `scale`（含 `batch_size`、`workload_units`）、`build_ms`、`p50_ms`、`p95_ms`、`recall_at_k`、
`relation_bytes`、`index_bytes` 和完整 `explain`；只有 `passed=true` 且进程退出码为
0 才能记录为目标规模性能通过。`build_ms` 表示 staging 数据装载与发布时间，不是
ANN 构建时间；工具不自动创建 HNSW/IVFFlat，`index_bytes.existing` 只报告表上已有
索引，`benchmark_ann_index_bytes` 为 `null`。如果 exact 门禁不达标，应根据 JSON
证据另行评估已批准的按维度 ANN 方案，不得把失败隐藏。

CI `live-m1-m3` job 在真实 `pgvector/pgvector` service 上额外运行一个有界 smoke：

```bash
python -m scripts.benchmark_m2_pgvector \
  --chunk-count 64 --dimension 8 --query-count 12 --top-k 5 \
  --max-p95-ms 1000 --min-recall-at-k 1.0 --seed 20260815
```

该 smoke 失败、退出非零或 guard/pgvector 不可用都会使 job 失败；smoke 只验证
工具和 live PostgreSQL 链路，不替代目标规模生产验收。`VectorStore`、
`InMemoryVectorStore`、`PgVectorStore` 和 `PostgresPgVectorStore` 的每个 bounded
`add_many` 批次只使用一个 checkout/transaction，并通过一次 cursor `executemany`
传输，校验失败原子回滚。cleanup 使用同一事务中的参数化 DELETE，并先重新校验
`current_database()` 与 guard target；校验失败时不执行 DELETE。它会保留主失败分类、
写入 `failure_reasons` 并使进程非零。CI smoke 的 JSON 只接收 stdout，
stderr 保留在 Actions 日志，不与 JSON 合并。

迁移使用 `export_manifest` / `import_manifest`：目标端会先验证每条记录、每个 payload
checksum 和 manifest checksum，再在一个事务中发布；同身份同内容幂等，冲突或任一失败
整体回滚。SQLite 适配器用于离线/测试/迁移演练，不应与生产 PostgreSQL 双写。

### Readiness 与验收

生产缺失 provider、pgvector、审计仓储或教师复核 CAS 能力时必须 fail closed；不会返回
“成功的空索引/空审计”。上线前至少执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q src tests
```

真实 PostgreSQL+pgvector 还需设置专用、可销毁的
`COURSE_INSIGHT_TEST_DATABASE_URL` 和匹配的 `COURSE_INSIGHT_TEST_DATABASE_NAME`。
若环境未提供它们，live 用例只能 skip，验收报告必须明确“未执行真实数据库联调”。

本阶段还提供两个只读检查入口：

```powershell
.venv\Scripts\python.exe -m scripts.verify_m6_controlled_rollout --evidence evidence.json
.venv\Scripts\python.exe -m scripts.check_production_readiness --settings production-facts.json
```

前者在证据缺失或门禁不完整时返回 `blocked`，保持 M6 rules/零 rollout；后者检查
生产环境、PostgreSQL+pgvector、迁移、主机和安全 cookie 等事实。两个命令都不修改
数据库、不发布策略，也不实现 M7/M9。

OCR 适配器、M5 DINA/BKT 真实数据、M6 shadow/OPE/批准记录和 M8 IRT 标定数据
属于目标环境证据，不能用本地单元测试或合成数据替代。缺少这些条件时，应保留
`COURSE_PDF_OCR_REQUIRED`、`INSUFFICIENT_MODEL_DATA` 或 `blocked` 状态，不能写成生产通过。
