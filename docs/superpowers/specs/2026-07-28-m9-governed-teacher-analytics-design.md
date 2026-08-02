# M9 受控教师解读首版设计与长期边界任务

## 状态

- 决策日期：2026-07-31
- 首版状态：边界已冻结，代码与本地测试完成后可提交
- 当前远端边界：只做本地实现与验证，不 commit、不 push、不创建 PR
- 长期任务：`M9 DeepSeek 教师解读边界校准与扩展`
- 长期任务启动门：全仓测试、跨模块联调及教师标注评估完成之后

## 结论

M9 DeepSeek 首版不是“语句压缩器”，也不是新的评分或决策模块。它是：

> 教师专用、教师主动触发、证据绑定、失败关闭的班级聚合评价解读助手。

模型只能从封闭目录中选择适用的教师核查问题代码并绑定临时事实别名。事实解释、
建议触发依据和问题中文全部由本地确定性模板生成，模型不能输出展示文字。它不能
改变任何正式业务结果。教师先看到程序事实，再按需展开可忽略的辅助层。

## 外部依据

首版边界综合了以下官方资料与研究：

- 教育部教师队伍建设专家指导委员会的教师生成式人工智能应用指引：
  允许学情分析和诊断报告，但要求教师主导、核查内容、去标识化，并禁止把
  AI 结果直接作为最终评价；
  <https://edu.sh.gov.cn/mbjy_xwzx/20251230/3d40abebf1364936b3659ee84be76802.html>
- 国家网信办《生成式人工智能服务管理暂行办法》：要求最小化处理个人信息，
  提高准确性和透明度，并防止歧视；
  <https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm>
- UNESCO 教育与研究生成式 AI 指导及教师 AI 能力框架：强调人的控制、
  教师主体性、隐私、问责、试点与持续评估；
  <https://www.unesco.org/en/articles/guidance-generative-ai-education-and-research>
- 美国教育部 AI 教育报告：教师必须掌握重要教学与形成性评价决定，AI 缺少
  教师所拥有的广泛情境判断；
  <https://www.ed.gov/sites/ed/files/documents/ai-report/ai-report.pdf>
- NIST AI 600-1：生成式 AI 存在混淆、伪逻辑和伪引用风险，应在部署前测试、
  核查来源、记录系统边界并持续监控；
  <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf>
- OWASP LLM Prompt Injection、Misinformation、Excessive Agency 与 Improper
  Output Handling：提示词和 RAG 不能单独消除注入，需最小权限、封闭输出、
  确定性验证和人类批准；
  <https://genai.owasp.org/llmrisk/llm01-prompt-injection/>
- DeepSeek Thinking Mode、JSON Output 和 Context Cache 官方文档：
  V4 默认 thinking、JSON mode 可能空返回、输入输出前缀默认可能写入磁盘缓存；
  <https://api-docs.deepseek.com/guides/thinking_mode/>、
  <https://api-docs.deepseek.com/guides/json_mode/>、
  <https://api-docs.deepseek.com/guides/kv_cache/>
- DeepSeek 隐私政策和开放平台条款：输入会被收集，服务不面向敏感个人数据和
  儿童个人数据，开发者负责下游数据治理，并应披露 AI 输出仅供参考；
  <https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html>、
  <https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>
- 细粒度归因研究说明逐项、局部引用可以降低人工核查负担，但引用存在本身不能
  证明陈述语义正确；
  <https://aclanthology.org/2024.acl-long.182/>、
  <https://aclanthology.org/2024.findings-acl.838/>

这些依据共同支持“教师控制、最少数据、事实绑定、最小权限、严格校验、
持续评估”的初版，而不支持自动评价或个体画像。

## 首版允许范围

### 输入

模型只接收程序生成的匿名定性事实包：

- 班级证据状态与覆盖区间；
- 样本支持数达到首版门槛的概念掌握区间、重点支持区间、置信区间和趋势方向；
- 样本支持数达到门槛的错误模式流行区间；
- 已由规则引擎产生、达到同一群体门槛的建议动作代码和候选状态；
- 本次调用的临时 `fact_ref`、`suggestion_ref` 和内容摘要。

模型不看到真实 ID。程序在本地保存临时别名与真实来源之间的映射，输出通过全部
校验后才恢复来源。

### 输出

模型只能返回：

- 与每项事实完全一致的 `meaning_code` 和 `render_code`；
- 白名单 `question_code` 及与问题类型兼容的临时 `fact_ref`；
- 与每项建议完全一致的动作、状态、`explanation_code` 和事实引用。

程序而非模型写入：

- AI 辅助标签；
- 来源报告身份和 checksum；
- 固定局限说明；
- 正式事实含义标签和解释中文；
- 正式建议内容、动作、状态和依据中文；
- 教师核查问题中文；
- 真实来源引用。

## 首版排除范围

以下内容不进入模型：

- 个体报告、复核队列和单个学生状态；
- 学生作答、教师评论及其他自由文本；
- 姓名、学号、稳定学习者 ID、班级 ID、题目 ID 和真实证据 ID；
- 原始分数、比例、阈值、排名和统计字典；
- 现有建议正文；
- M7/M8 的 provider 原始响应、完整提示词或思维链。

首版 `minimum_aggregate_size=5` 是保守工程门槛，不是普遍适用的匿名性声明。
未满足门槛时在联网前返回 `REPORT_SCOPE_INVALID`。

## 永久红线

- 不重新评分、计算或改写数字；
- 不改阈值、置信度、状态、排序和优先级；
- 不增删、强化或弱化正式建议；
- 不推断能力、人格、动机、态度、心理、健康、家庭或受保护属性；
- 不作因果诊断、未来预测、正式评价或行动命令；
- 不批准、发布或写回 M5/M8/M9 业务状态；
- 不自动触发教学动作，不向学生直接发布。

`ModelQualityReport.status="ready"` 只表示技术上可送交教师审核，不等于教师批准，
更不等于 M8 参数发布。

## 执行和校验

- 模型固定为 `deepseek-v4-flash`；
- 显式 `thinking_enabled=False`；
- `temperature=0`；
- 非流式 JSON Output；
- `max_attempts=1`，不自动重试；
- `max_tokens=1536`；
- 策略 `m9-teacher-interpretation-v2`；
- 提示词 `m9-teacher-interpretation-json@3.0.0`；
- 输出 schema `m9_teacher_interpretation_v2`。

本地校验要求：

- 所有对象字段、类型、列表数量和顺序精确；
- `source_digest`、渲染代码、建议解释代码、动作、状态和引用精确；
- 每项事实和建议一一对应，不能遗漏、增加或重排；
- 问题代码必须在封闭白名单中，只能引用兼容类型的输入事实；
- 任意 `text`、`explanation`、未知代码和额外字段均整体拒绝；
- 所有展示中文来自仓库内确定性模板，provider 自由文本不能进入结果或审计；
- provider 只能以 `finish_reason=stop` 成功。

任何失败均不做模型修复或第二次请求，直接回退到程序报告。

## 权限与权威来源

既有构造签名保持：

```python
M9TeacherAnalyticsService(repository, statistics_engine, suggestion_rule_engine)
```

显式调用顺序：

1. 使用 `configure_teacher_interpreter(adapter)` 一次性配置；
2. `ActorContext.role` 必须是 `teacher`；
3. 报告 `course_id` 与 `class_id` 必须同时在教师授权作用域内；
4. M9 repository 必须在该课程与班级作用域中存在相同 `report_id` 和 checksum
   的权威报告；
5. 只有以上检查全部通过才构建提示并联网。

## 审计与持久化

SQLite/PostgreSQL schema version `10` 新增 M9 自有表
`m9_model_invocation_audits`。一条实际调用只形成一条原子记录，成功和失败使用同一
结构。记录包含版本、状态、哈希、token、延迟、错误分类，以及通过校验后实际展示
给教师的结构化解读。未通过输出不保存。

不保存 API key、完整提示词、provider 原始响应封装或思维链。SQLite→PostgreSQL
导入顺序先报告后审计，以保持外键和后端切换完整性。

## 长期任务：M9 DeepSeek 教师解读边界校准与扩展

### 冻结期

全仓测试、全员模块联调和教师标注评估完成前：

- 不调整最小群体门槛；
- 不开放个体学生解读；
- 不发送数字和自由文本；
- 不增加开放问答或多轮对话；
- 不允许模型生成、压缩或改写展示文字；
- 不让模型筛选、重排或生成正式建议。

### 启动后的评估集

使用去标识化、教师标注的固定测试集和红队集，至少测量：

- 无证据陈述率与引用正确率；
- 数字、状态、否定和建议倾向改变率，目标为零；
- 因果、人格、诊断和受保护属性推断率，目标为零；
- 含义反转率、非法引用率、校验拒绝率和回退率；
- 教师拒绝率、修改率、核查时间和节省时间；
- AI 标签对教师判断偏差的影响；
- 不同群体的反事实一致性；
- 模型、提示词或策略版本变化后的漂移；
- 与只看程序原报告、不调用 DS 的基线比较。

### 可能讨论但不预先承诺

测试证据支持且完成隐私评审后，可以逐项讨论：

- 调整最小群体门槛；
- 允许程序提供、模型只能逐字复制的冻结数字片段；
- 更丰富但仍逐句引用的跨事实综合；
- 经教师标注测试证明不改变倾向的受约束语句压缩；
- 教师主动选择、短时、去标识化的个体视图；
- 经过脱敏和注入隔离的有限评价摘录。

永久红线不因长期任务而放宽。
