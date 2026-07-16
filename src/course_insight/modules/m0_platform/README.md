# M0 平台基础与 Django 外层

## 负责人

陈

## 职责

M0 是系统外层和横切基础：负责配置、事件、快照、健康状态，并规定
Django 页面、会话、鉴权和表单提交的边界。Django 属于 M0，不新增业务模块；
视图只负责将请求转成契约并调用 `AppCoordinator`。当前 Django 仅有架构空作业，
返回 `skipped`，不启动 Web 服务。持久化目标是 PostgreSQL，现有 SQLite
只是可运行基线。

## 输入来源

- 可分发配置与运行时环境变量，不包含密钥值或主机绝对路径。
- Django 会话产生的伪匿名 `ActorContext`。
- 学生表单转换的 `AssessmentSubmission`与教师表单转换的 `TeacherReviewSubmission`。
- M8 产生的 `LearningEvent`。

## 输出

- `EventAck`：事件幂等持久化确认。
- `AsyncJobStatus`：Django 或后台作业的可移植状态；当前前端准备作业为 `skipped`。
- 原子契约快照和不含密钥/主机路径的健康状态。

公开新入口 `prepare_django_frontend(actor_context, requested_at) -> AsyncJobStatus`
只声明并返回架构占位状态；真实 Django view、URL 和模板后续仍放在 M0 边界。

## 禁止事项

不得解释领域分数、跨模块读表、在 view 中实现领域逻辑、将 Django ORM/
请求类型泄漏给 M1—M9、泄漏路径/密钥，或把运行数据库写入项目数据。
