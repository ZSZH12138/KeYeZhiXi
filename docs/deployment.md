# 部署与运维说明

本文描述当前 MVP 的安装、初始化、进程、配置和数据边界。开发服务器可以用于本地验证；正式环境应使用受支持的 WSGI/ASGI 服务、反向代理和 PostgreSQL + pgvector。

## 1. 环境要求

- Python 3.11–3.13；
- 本地开发可使用 SQLite；
- 生产环境使用 PostgreSQL 16 和 pgvector；
- 使用独立 Conda/venv，不修改系统 Python；
- 知识抽取和答疑需要可访问 DeepSeek API；
- 可选的中文隐私模型需要受治理的 Presidio、spaCy 和本地模型工件。

安装：

```shell
conda create --name course-insight python=3.12 -y
conda activate course-insight
python -m pip install --constraint requirements/ci-constraints.txt -e ".[dev,intent]"
python -m pip check
```

生产部署按实际需要安装 `privacy`、`performance` 或 `ocr` 可选依赖。

## 2. 配置来源

配置优先级从低到高为：

1. 代码安全默认值；
2. `config/app.json`；
3. 项目根 `.env`；
4. 进程环境变量；
5. 调用方显式覆盖。

首次本地运行：

```shell
cp .env.example .env
cp config/app.example.json config/app.json
```

版本控制中只保留 `.env.example` 和 `config/app.example.json`。`.env`、`config/app.json`、数据库 URL、Django secret、DeepSeek key、日志和运行数据均不得提交。

常用变量：

- `COURSE_INSIGHT_ENVIRONMENT`
- `COURSE_INSIGHT_RUNTIME_DIR`
- `COURSE_INSIGHT_CONFIG_DIR`
- `COURSE_INSIGHT_DATABASE__BACKEND`
- `COURSE_INSIGHT_DATABASE__SQLITE_PATH`
- `DATABASE_URL`
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `COURSE_INSIGHT_LOGGING__MODE`
- `COURSE_INSIGHT_M6_POLICY__MODE`

生产环境必须满足：

- `COURSE_INSIGHT_ENVIRONMENT=production`；
- `database.backend=postgresql`，数据库 URL 从环境变量间接加载；
- `DJANGO_SECRET_KEY` 至少 50 个字符，不使用 Django 的不安全默认前缀；
- `DJANGO_ALLOWED_HOSTS` 非空且不含 `*`；
- HTTPS、Secure Cookie 和正确的 CSRF trusted origins 已配置；
- 日志输出到标准输出或受控收集器；
- M6 默认保持 `rules`、零 rollout、零探索，除非已有单独审批。

## 3. 数据库初始化

### 本地 SQLite

```shell
python manage.py migrate
```

默认数据库位于 `runtime/course_insight.db`。同一个 SQLite 文件同时承载 Django 表和模块运行表，以便账户注销使用单事务清除；连接使用 busy timeout 和 WAL 设置减少短暂锁竞争。不要同时用数据库管理器长时间持有写事务。

### 生产 PostgreSQL

生产需要两套迁移：

- Django migration：M0 Web 身份、权限、课程班级和知识入库表；
- core migration：M0 事件/流程以及 M1–M9 模块表和 pgvector。

当前 core schema 版本为 v22；Django M0 迁移包含账户授权回填、外部账户名、删除文件队列、工作区显示名称和删除身份清理。SQLite 到 PostgreSQL 的受控导入见 [postgresql_migration.md](postgresql_migration.md)。

不要把开发 SQLite 数据库作为生产权威数据库，也不要在两个后端之间双写。

## 4. 首个管理员

`config/roles.csv` 只保留管理员引导行。执行：

```shell
python manage.py sync_roles --check
python manage.py sync_roles --apply
python manage.py changepassword pseudonym_system_admin_001
```

`sync_roles` 创建的管理员默认使用不可用密码，因此第三步不可省略。教师和学生必须从管理员 Web 页面创建；CSV 中即使出现旧教师或学生行，也不会创建或复活对应账户。

需要自定义管理员名时，在首次同步前修改 `config/roles.csv` 的 `actor_id`。管理员账户不应与教师或学生共用。

## 5. 进程角色

### Web

```shell
python manage.py runserver
```

开发入口：<http://127.0.0.1:8000/login/>。

### 知识入库 Worker

```shell
python manage.py run_ingestion_worker
```

它处理文件校验、解析、DeepSeek 知识抽取、题目标注、索引构建和活动发布。进度、租约与锁状态写入 `runtime/ingestion_worker/`，崩溃后由租约恢复。

单次检查可用：

```shell
python manage.py run_ingestion_worker --once
```

### 事件 Outbox Worker

```shell
python manage.py run_outbox_worker
```

它以 at-least-once 语义投递学习事件，不处理知识文件。单次执行：

```shell
python manage.py run_outbox_worker --once
```

详细语义见 [outbox_worker.md](outbox_worker.md)。

## 6. 健康检查

- `/health/live/`：进程存活；
- `/health/ready/`：核心配置、数据库、迁移和运行依赖是否可用。

单个 Worker 未运行时，Web 可以返回 `degraded` 并指出不可用能力；核心数据库、配置或日志不可用时 readiness 返回 503。不要使用兼容脚手架的 `skipped` 字段判断真实 Web 状态。

启动前建议：

```shell
python manage.py check --deploy
python manage.py showmigrations
python manage.py sync_roles --check
```

`check --deploy` 会对开发配置给出合理警告；生产环境不得忽略 secret、HTTPS、Cookie、Host 和 CSRF 相关警告。

## 7. 课程与 DeepSeek 配置

教师为每个课程班级在页面中单独保存 DeepSeek 密钥。密钥加密写入 `runtime/`，不写入课程表、日志或 Git。

安全状态检查：

```shell
python manage.py deepseek_status
```

输出只包含是否配置、模型名和隐私门状态，不输出密钥。

知识抽取、题目标注、学生答疑和教学建议使用该精确课程班级的密钥。缺失时失败关闭，不回退到全局环境变量。旧兼容适配器若使用 `DEEPSEEK_API_KEY`，也必须通过部署系统注入，不能写入文件。

## 8. 账户注销运维

在当前共享 SQLite MVP 中，管理员注销学生时，数据库事务会清除该学生的模块数据、会话、成员关系和用户行。注销教师时，还会删除教师拥有的全部班级、知识文件记录、题库、知识点、模型配置和学习数据。PostgreSQL 部署必须提供并验收等价的班级范围清除适配器；缺失时教师注销会失败关闭，事务不会提交部分删除。

数据库提交后的物理文件清理使用待办队列。正常请求会立即尝试；运维可重试：

```shell
python manage.py cleanup_erasure_files
python manage.py cleanup_erasure_files --limit 100
```

文件目标必须位于允许的运行目录，并以不可变存储键或冻结作答 ID解析。命令不会接受任意用户路径。

成功注销不保留账户名黑名单，旧账户名可以重新用于新账户。新账户会获得新的内部伪匿名 ID，不继承历史数据。

## 9. 备份、恢复和全新实例

需要备份的部署状态包括：

- PostgreSQL 数据库或本地 SQLite 数据库；
- `runtime/uploads/`、课程文件和冻结答卷；
- `runtime/snapshots/` 与 `runtime/artifacts/`；
- 策略、隐私模型和受治理的模型清单；
- 加密 DeepSeek 配置及其部署侧加密材料。

备份必须在停止写入或使用一致性快照时进行，并在恢复后校验 manifest、对象 ID、版本和 checksum。

全新克隆默认没有 `runtime/`，因此没有账户、课程或学习数据。若要把一个本地开发实例恢复到全新状态，应先停止 Web 和两个 Worker，再删除该实例的整个 `runtime/`，然后重新执行迁移和管理员引导。不要只删除数据库而遗留上传文件或密钥；不要在未确认绝对路径时使用递归删除命令。

## 10. 日志与敏感信息

不得记录或提交：

- 密码、Django secret、数据库密码和 DeepSeek key；
- Session/Cookie/Authorization 内容；
- 学生原始作答、完整模型提示词或 provider 原始响应；
- 真实姓名、学号、邮箱、电话或身份映射；
- 本地主机绝对路径。

错误页面只展示稳定错误信息；详细技术上下文写入受控服务器日志。生产日志应设置保留期、访问控制和敏感字段过滤。

## 11. 发布前验证

```shell
python -m pip check
python manage.py check
python -m compileall -q src scripts tests
python -m pytest -q
python scripts/export_schemas.py
git diff --exit-code -- contracts/schemas
git diff --check
```

生产 PostgreSQL/pgvector、真实 embedding、OCR 和隐私模型需要在受保护环境单独验收。测试跳过或本地 SQLite 通过，不能替代这些生产证据。

## 12. 回滚原则

- 代码和配置回滚不能回写或破坏已经冻结的试卷、审计和发布版本；
- 数据迁移回滚前必须有可验证备份；
- M6 learned policy 可通过 kill switch 回退到 rules，不删除既有审计；
- 活动知识发布失败时继续使用上一个已发布版本；
- 账户物理注销不可恢复，必须依靠管理员确认和事前备份处理误操作风险。
