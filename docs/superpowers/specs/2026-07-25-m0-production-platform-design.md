# M0 生产平台层修订设计

## 状态

- 日期：2026-07-25
- 状态：用户已批准，进入实施
- 决策：采用方案 A——增加非破坏性的应用用例和最小模块自有恢复入口
- 分支：`codex/fix-m0-m4-m6-validation`
- 修改前基线：229 项测试通过，84 个公共 Schema，SQLite schema version 3

## 冲突修复决定

现有 M0、M4、M6 的代码、公共 Pydantic 契约、provenance 和测试彼此闭合。
阻断来自新增 Django 多请求流程，而不是既有实现互相冲突：

```text
开始测评 -> 显示试卷 -> 提交答案 -> 查看结果
                                  -> 教师稍后复核
```

原有 `AppCoordinator.run_assessment_cycle()` 和
`run_teacher_review_cycle()` 都要求调用方在同一次进程调用中提供完整领域对象，无法让
后续 HTTP 请求按稳定标识安全恢复试卷、评分、状态和分析结果。让 View 直接读取
M8/M9 表或把完整对象保存在 Session 都违反原任务约束。

用户已明确批准以下兼容性修复：

1. 保留全部既有公开方法签名和行为；
2. 新增拆分的应用层用例；
3. 在对象所属模块增加最小的保存/读取入口；
4. M0 只保存 Web 流程索引、状态和关联标识，不复制业务对象；
5. 不修改 84 个公共契约类、字段、字段语义或 contract `schema_version`；
6. 不实现 M5 的 DINA/BKT，也不实现 M8 的 IRT/自适应算法。

## 责任与数据所有权

| 数据 | 权威所有者 | M0 可保存的内容 |
|---|---|---|
| `TaskPlan` | M4 | `task_id` |
| `AssessmentPaper`、`ScoringResultBundle`、试卷执行作用域 | M8 | `paper_id`、`attempt_id` |
| `StateUpdateResult` | M5 | 状态结果的稳定查询键 |
| `StudentFeedbackPackage` | M7 | `feedback_id` |
| `TeacherAnalyticsBundle`、`TeacherReviewDecision` | M9 | `report_id`、复核状态 |
| Web 请求进度 | M0 | 伪匿名身份、课程/班级作用域、上述关联标识和状态 |

每个 Repository 只访问本模块前缀的数据表。Django ORM 只管理 M0 Web 的
`User`、`ActorGrant` 和角色同步元数据，不管理 M1—M9 领域表。

## 多请求应用用例

在不改变既有两个一站式用例的前提下，为 `AppCoordinator` 增加以下加法式入口：

```text
start_assessment(...)
  M4.create_task_plan
  -> M8.generate_paper
  -> M0 记录 assessment run

submit_assessment(...)
  M0 读取并校验 run
  -> M4/M8 读取 TaskPlan/Paper
  -> 运行原有评分、状态、辅导、反馈、分析链
  -> M8/M5/M7/M9 分别保存自己的结果
  -> M0 原子推进 run 状态

get_student_assessment(...)
  M0 校验 run 归属
  -> M8/M7 分别读取评分与反馈

get_teacher_review_context(...)
  M0 校验课程/班级作用域
  -> M8/M5/M9 读取评分、状态和分析

review_assessment(...)
  读取上述权威对象
  -> 复用既有教师复核链
  -> 各模块保存新版本
  -> M0 更新流程索引
```

方法之间继续传递现有 Pydantic 对象，不传递 Django、ORM、Session、Psycopg 或
无类型字典。

### 故障恢复和幂等

M0 为每个 `start`、`submit` 和 `review` 操作保存内部幂等键、状态版本、短 lease
和 checkpoint。一次提交的 checkpoint 至少区分：

```text
claimed
-> scoring_saved
-> events_appended
-> state_saved
-> tutoring_saved
-> feedback_saved
-> analytics_saved
-> completed
```

教师复核使用独立 checkpoint 链。推进使用 compare-and-set；重复 POST 返回同一权威
结果；并发请求只能有一个有效 lease。进程在任意保存点退出后，过期 lease 可被后续
请求回收并从最后 checkpoint 重放。

各模块写入采用 insert-or-get 或“同标识同 payload 成功、同标识不同 payload 冲突”
语义。所有现有输出标识均保持确定性。这样即使模块写成功而 M0 尚未推进 checkpoint，
重放也不会制造第二份权威结果。

M8 不能再只用进程内字典保存 `paper_id -> course_id/class_id`。M8 自有 Repository
必须持久化内部 paper execution context；新 Service 实例读取试卷时同时恢复作用域，
保证 `LearningEvent` 永远使用真实课程和班级标识。

M5 必须按 `course_id/class_id/learner_id` 读取最新状态，并按 `attempt_id` 读取完整
`StateUpdateResult`。旧的仅 `(learner_id, state_version)` 唯一语义通过向前 migration
修正；同一伪匿名 actor 可以合法存在于多个课程。M5 的重复处理水位来自 Repository，
不再只依赖进程内集合。

## 课程运行上下文

Web 使用内部 `CourseRuntimeRegistry`。它从 M0 验证过的相对 snapshot 引用加载
现有 `CoursePackage`、`EvidenceIndexRef` 和 `KnowledgeBundle`，并通过 M2 的公开
`build_index()` 恢复进程内检索状态；加载后校验重建的索引标识和 checksum。

注册表不是公共契约，不进入 `contracts/`。缺少上下文时 fail closed，健康检查返回
安全的非 ready 状态，不暴露路径或配置值。

## 应用组合根

新增内部 `ApplicationContainer` 和 `build_application()`：

```text
PlatformSettings
  -> M0/M4/M5/M6/M7/M8/M9 backend 选择与 Repository
  -> M0—M9 Service
  -> AppCoordinator
  -> CourseRuntimeRegistry
  -> OutboxWorker
```

M0 构造器增加关键字形式的可选 Repository 注入，同时保留原有三个位置参数的构造
方式。M4/M6 继续使用已有构造注入。Django、CLI 和 Worker 只从该组合根取得对象。

本里程碑不把 M1—M3 扩展成新的通用数据库后端：它们通过已验证的 runtime contract
snapshots 和 `CourseRuntimeRegistry` 恢复，M2 使用现有公开 `build_index()` 恢复
进程内词法索引。Factory 仍组装 M1—M9 Service，但“SQLite/PostgreSQL 切换”只承诺
本任务实际实现适配器的模块。

## 配置

内部配置模型位于 `infrastructure/config/`，优先级固定为：

```text
显式覆盖 > 系统环境变量 > .env > config/app.json > 安全默认值
```

生产环境缺少数据库 URL、Django secret 等必需值时拒绝启动。`app.json` 只允许保存
非敏感配置及敏感值对应的环境变量名。路径必须解析在仓库配置根或 runtime 根内。

`roles.csv` 只用于初始化/同步。解析后写入 M0 Django 授权表，请求期间不扫描 CSV。
同步支持 check、dry-run、apply，保存 checksum，重复执行幂等，并能停用或撤销授权。

## 日志

运行日志与领域审计严格分离：

```text
runtime/<run_id>/logs/app.log
runtime/<run_id>/audit/learning_events.jsonl
数据库评分审计/教师复核历史
```

日志采用一行一个 JSON 对象，使用 `contextvars` 注入关联标识，并在格式化前递归脱敏。
开发模式允许单进程滚动文件；生产模式只输出 stdout，由部署平台收集，避免多进程同时
轮转一个文件。

## Outbox

Repository 增加原子 `claim -> acknowledge/fail -> reclaim` 能力。事件与 outbox 仍在
同一数据库事务写入；Worker 在事务外写 JSONL，然后单独确认投递。

状态机：

```text
pending -> leased -> delivered
                 \-> pending（可重试）
                 \-> dead（超过最大次数）
```

过期 lease 可回收。JSONL sink 以 `event_id` 去重，因此整体语义为 at-least-once
投递、幂等落盘。Worker 作为独立管理命令运行，支持一次执行、轮询、信号优雅退出和
可观测心跳。

`initialize()` 和 `append_learning_events()` 不再触发投递。旧的显式投递方法可作为
兼容包装保留，但 Web 请求、应用初始化和 `AppConfig.ready()` 都不得调用它；正常
投递只有独立 Worker 驱动。

## PostgreSQL

使用 Psycopg 3 与 `psycopg_pool`。迁移文件有不可变编号和 checksum，运行器使用
PostgreSQL advisory lock 串行执行迁移。至少实现并验证 M0、M4、M6 适配器；为支持
获批的多请求流程，同时实现 M5、M7、M8、M9 的最小恢复表和适配器。

SQLite 与 PostgreSQL 必须保持：

- event/outbox 同事务；
- 幂等键与唯一约束一致；
- JSON payload 使用相同 Pydantic 反序列化；
- 乐观版本冲突语义一致；
- Repository 不泄漏连接、游标或 Row。

SQLite→PostgreSQL 工具支持 dry-run、分批和可恢复 checkpoint；每条 JSON payload
都用所属现有 Pydantic 契约重新验证，同时比较 ID、版本、行数和确定性 checksum。
迁移生成安全报告，不删除源 SQLite；任一批次失败回滚该批次；重复执行幂等。

没有可用 PostgreSQL 时，相关集成测试明确 skip，最终报告不得声称实测通过。

## Django

Django 属于 M0，使用自定义 `User(actor_id)` 和 `ActorGrant`。授权为两层：

1. Django Group/Permission；
2. `ActorContext` 的 actor/course/class/learner 作用域。

View 只负责 HTTP 转换、授权、调用应用用例和选择模板。Form 动态读取
`AssessmentPaper`，清洗后构造现有 `AssessmentSubmission`；教师 Form 构造现有
`TeacherReviewSubmission`。所有跨模块对象只通过 Coordinator/Service 取得。

实现登录/退出、学生流程、教师分析与复核、live/ready 健康检查、CSRF、PRG、安全
错误页、登录失败限流、请求体限制和安全 Cookie 配置。

## 数据库版本

公共契约 Schema 数量保持 84。SQLite 数据库版本允许随内部表增加而上升；迁移必须
向前、可重复并保持现有数据。Django migration 与核心 Repository migration 分开。

## 验收不变量

- 原有 229 项测试继续通过；
- 不新增 M10，不引入模块间 HTTP；
- 不改变既有公开 Service 方法的参数和返回值；
- 不删除 provenance 边；新增公开跨模块消费入口时只添加真实、可反射的边；
- View、Coordinator 不直接读数据库；
- 不在事务中执行文件 I/O；
- 不记录答案、Cookie、Authorization、密码、密钥或数据库 URL；
- 不把运行产物写入 `data/`、`contracts/` 或源码目录；
- 未真实运行 PostgreSQL 时明确标记为未实测。
