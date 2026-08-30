# 课业智析（KeYeZhiXi）

[![CI](https://github.com/ZSZH12138/KeYeZhiXi/actions/workflows/ci.yml/badge.svg)](https://github.com/ZSZH12138/KeYeZhiXi/actions/workflows/ci.yml)

课业智析是一个面向学校课程的教学辅助平台。教师可以创建课程与班级、邀请学生、上传课程资料和题库，并查看班级学习情况；学生可以参加测评、订正错题和基于课程资料提问；账户管理员只负责教师与学生账户的创建和物理注销。

当前仓库是可运行的 MVP。仓库不包含预置课程、题库、知识点、学生作答、可登录账户、API 密钥或其他运行数据。

## 主要功能

### 账户管理员

- 创建教师账户和学生账户；
- 物理注销教师或学生账户；
- 注销教师时，同时删除该教师创建的班级、班级成员关系、课程资料、题库、知识点、模型配置和相关学习数据；
- 注销学生时，同时删除其班级关系、作答、画像、反馈和会话数据；
- 已注销的账户名不会被系统记忆，可以重新用于新账户。

管理员不能管理课程、班级或教学内容。

### 教师

- 自定义课程名称和班级名称，创建独立班级；
- 按学生账户名邀请或移出学生，并查看当前人数和完整名单；
- 为每个班级配置独立的 DeepSeek 密钥；
- 上传和删除课程文件、题目文件，确认后由后台任务解析并原子发布；
- 查看知识地图、已做/未做人数、学生掌握情况和教学建议；
- 查看普通复核与建议复核，逐题调整评分。

教师不能创建或注销任何账户。

### 学生

- 只看到本人已加入的课程和班级；
- 参加诊断测评、随心练习、阶段评测和错题订正；
- 查看本人已完成的画像测评、逐题结果和课程来源；
- 针对课程或具体题目发起可溯源答疑。

学生没有账户、课程、班级或成员管理权限。

## 系统模块

项目采用模块化单体架构。模块之间通过 Pydantic 契约和应用层编排协作，不通过内部 HTTP 相互调用。

| 模块 | 功能层职责 |
|---|---|
| M0 平台层 | 登录、权限、账户、课程班级页面、后台任务、配置和事件投递 |
| M1 课程治理 | 校验并解析教师上传的课程文件，保留来源和版本 |
| M2 证据检索 | 从课程资料中检索可引用的原文，支持词法检索和 pgvector |
| M3 知识与题库 | 组织知识点、题目、量规、蓝图、知识关系和发布版本 |
| M4 任务编排 | 把学生操作转换为答疑、诊断、练习、订正或阶段评测任务 |
| M5 学习状态 | 维护学生掌握度、知识追踪和班级统计 |
| M6 辅导控制 | 决定下一步提示、追问、补学或结束动作 |
| M7 DeepSeek 边界 | 执行受约束的知识抽取、题目标注、语义评分、反馈和答疑 |
| M8 测评与评分 | 出卷、保存冻结试卷、评分、IRT 标定和自适应选题 |
| M9 教师分析 | 生成班级报告、复核队列、教学建议和模型质量报告 |

更详细的边界见[架构说明](docs/architecture.md)和[接口指南](docs/interface_guide.md)。

## 技术栈

- Python 3.11+
- Django 5.2
- Pydantic 2
- SQLite（本地开发、离线验证和迁移演练）
- PostgreSQL 16 + pgvector（生产数据与向量检索）
- pytest

## 快速开始

以下命令从全新克隆开始，不依赖仓库作者的本地路径。

### Windows 桌面版

从发布包解压完整的 `KeYeZhiXi` 文件夹后，双击 `KeYeZhiXi.exe`。应用会自动初始化本地数据、启动 Web 平台与两个 Worker，并在平台就绪后打开登录页。首次启动且数据库没有任何账户时，应用会要求创建首个管理员。

运行数据保存在 `%LOCALAPPDATA%\KeYeZhiXi\`，替换程序文件夹不会删除账号和课程数据。关闭“课业智析”状态窗口会一并停止平台与两个 Worker。详细说明见 [Windows 桌面版](docs/windows_desktop.md)。

维护者可在项目根目录执行以下命令构建发布包；PyInstaller 会安装到被 Git 忽略的隔离工具目录，不会写入当前 Python 环境：

```powershell
.\scripts\build_windows_app.ps1 -PythonExecutable python
```

产物位于 `dist\KeYeZhiXi\KeYeZhiXi.exe` 和 `dist\KeYeZhiXi-windows-x64.zip`。

### 源码运行

### 1. 创建环境

```shell
conda create --name course-insight python=3.12 -y
conda activate course-insight
python -m pip install --constraint requirements/ci-constraints.txt -e ".[dev,intent]"
```

### 2. 准备本地配置

复制示例文件：

```shell
cp .env.example .env
cp config/app.example.json config/app.json
```

Windows PowerShell 可使用：

```powershell
Copy-Item .env.example .env
Copy-Item config/app.example.json config/app.json
```

为 `.env` 中的 `DJANGO_SECRET_KEY` 填写随机值。真实密钥、数据库、日志和上传文件都不应提交到 Git。

### 3. 初始化数据库和首个管理员

```shell
python manage.py migrate
python manage.py sync_roles --apply
python manage.py changepassword pseudonym_system_admin_001
```

`sync_roles` 只创建一个不可登录的管理员引导账户；必须执行 `changepassword` 后才能登录。教师和学生只能由该管理员在 Web 控制台创建。

如需使用自己的管理员名称，请在第一次执行 `sync_roles --apply` 前修改 `config/roles.csv` 中的 `actor_id`，并对相同名称执行 `changepassword`。

### 4. 启动平台

分别启动三个进程：

```shell
python manage.py runserver
python manage.py run_ingestion_worker
python manage.py run_outbox_worker
```

访问 <http://127.0.0.1:8000/accounts/login/>。开发服务器只用于本地验证，不是生产 WSGI/ASGI 部署方案。

## 基本使用顺序

1. 管理员登录，创建教师和学生账户。
2. 教师登录，在课程与班级页创建班级。
3. 教师按学生账户名邀请学生。
4. 教师进入班级，配置 DeepSeek、上传课程资料和题目并确认处理。
5. 学生登录，从本人课程列表进入班级，开始测评、订正或答疑。
6. 教师查看班级画像、学生记录和复核任务。

课程和班级页面显示教师填写的名称；URL 和数据库内部仍使用不可变 ID 保证引用、权限和历史记录稳定。

## DeepSeek 与外部调用

知识抽取、题目标注、语义评分、教学建议和答疑使用 DeepSeek。教师在对应班级页面保存密钥，密钥按课程和班级隔离并加密保存在运行目录中。

- 没有班级密钥时，相关功能失败关闭，不回退到其他班级或全局密钥；
- 自动化测试默认不发起真实模型请求；
- 不要把密钥写入源码、示例配置、README、Issue 或日志；
- M7/M9 的可选高风险模型能力还受隐私和质量门限制。

## 数据与部署边界

- `runtime/`：数据库、上传文件、知识发布物、模型配置、日志和 Worker 状态；被 Git 忽略；
- `config/app.json`、`.env`：本地或部署配置；被 Git 忽略；
- `config/*.example.*`、`.env.example`：无密钥模板，可以提交；
- `contracts/`：公开 Pydantic 契约的 JSON Schema 和来源映射；
- `data/raw_course/`：本地授权课程材料占位目录，真实材料不得提交。

SQLite 适合本地 MVP 验证；生产环境必须使用 PostgreSQL + pgvector，并按[部署说明](docs/deployment.md)完成安全配置、备份和真实联调。完整的教师班级级联物理清除当前由共享 SQLite 事务实现；PostgreSQL 部署必须补充并验收对应的受控清除适配器，未配置时注销会失败关闭，不会只删除账户而遗留班级数据。

## 测试与质量检查

```shell
python manage.py check
python -m compileall -q src scripts tests
python -m pytest -q
python scripts/export_schemas.py
git diff --exit-code -- contracts/schemas
```

依赖真实 PostgreSQL、pgvector 或隐私模型工件的测试，在条件缺失时会明确跳过；跳过不等于生产环境已验收。

## 项目结构

```text
.
├── config/       无密钥配置模板和管理员引导角色
├── contracts/    公共契约 Schema 与来源映射
├── data/         受治理的数据格式说明和本地课程材料占位
├── docs/         架构、部署、接口和运维说明
├── scripts/      Schema、迁移、评估和运维工具
├── src/          应用、基础设施、契约和 M0–M9 模块
├── tests/        单元与集成测试
└── manage.py     Django 和 Worker 命令入口
```

## 进一步阅读

- [架构说明](docs/architecture.md)
- [Windows 桌面版](docs/windows_desktop.md)
- [课程、班级与学习流程](docs/course_class_learning_flow.md)
- [部署说明](docs/deployment.md)
- [模块接口指南](docs/interface_guide.md)
- [PostgreSQL 迁移](docs/postgresql_migration.md)
- [Outbox Worker](docs/outbox_worker.md)
- [M4 意图识别运维](docs/m4_intent_operations.md)
- [M6 策略运维](docs/m6_policy_operations.md)

## 维护者

贡献者见 [CONTRIBUTORS.md](CONTRIBUTORS.md)。问题和改进建议请通过本仓库的 GitHub Issues 提交，并避免附带账号、作答、课程原文或 API 密钥等敏感数据。
