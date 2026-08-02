# M9 教师分析、受控 DeepSeek 解读与模型质量

## 负责人

冯

## 职责

M9 负责四类结果：

- 用确定性程序构建班级报告、个体报告和教师复核队列；
- 用确定性规则生成有证据门槛的教学建议；
- 记录教师对 M8 评分审计的确认、覆盖或拒绝；
- 可选地使用 DeepSeek 帮助教师理解已经形成的班级聚合事实和既有建议。

DeepSeek 是教师辅助解读器，不是评分器、统计引擎、学生画像器或决策引擎。
正式事实、数字、状态、顺序、建议和教师决定始终归原模块与确定性程序管理。

## 两条严格分离的模型路径

- `generate_teacher_narrative(LLMGenerationRequest)` 是保留的 legacy 空路径。
  默认应用工厂调用它时不读密钥、不联网，固定返回 `empty/not_run`。
- `interpret_teacher_analytics(ActorContext, TeacherAnalyticsBundle)` 是显式启用
  的教师解读路径。只有配置适配器、教师角色、课程与班级双重授权和权威报告
  校验全部通过后，M9 才会调用 DeepSeek。

`M9TeacherAnalyticsService` 的既有三参数构造签名保持不变。真实适配器通过
`configure_teacher_interpreter(...)` 单独、一次性配置。

## 首版冻结边界

### 允许

- M9 用本地确定性模板逐项解释已经计算好的匿名班级聚合事实；
- M9 用本地确定性模板说明现有规则建议为何被触发，但不改变建议；
- DeepSeek 只能从封闭目录中选择少量教师核查问题代码及合法事实引用；
- 教师看到的全部中文解释、问题和建议依据均由本地模板生成。

首版不接受任何模型生成的展示文字。DeepSeek 的作用被收窄为“从既有事实中
编排中性核查角度”，自由措辞或语句压缩留到长期任务用教师标注测试后再讨论。

### 模型输入白名单

- 仅发送样本支持数达到 `minimum_aggregate_size=5` 的班级概念或错误模式；
- 数值先由程序转换成封闭的定性代码，模型不接收原始数字；
- 报告、概念、错误模式和建议只使用本次调用的临时别名；
- 建议只发送固定的 `action_code`、`status_code` 和事实引用。

`5` 是首版保守工程门槛，不是对任何场景都成立的匿名性保证。它被冻结到全仓
测试和教师标注评估完成之后再校准。

### 首版不发送

- `IndividualReport`、`ReviewQueueItem` 和单个学生的任何字段；
- 姓名、学号、稳定学习者 ID、班级 ID、题目 ID或真实证据 ID；
- 分数、比例、阈值、排名、统计字典；
- 学生原始作答、教师自由文本、M7/M8 的 provider 原始响应；
- `TeachingSuggestion.content` 等可被当成指令的自由文本；
- 完整提示词、API key 或思维链。

DeepSeek 当前服务文档说明请求前缀默认可能进入服务端磁盘缓存，因此“本地不保存”
不等于“远端零留存”。首版据此不开放个体学生解读。

### 永久红线

- 不重新评分、计算、改阈值、改置信度、改状态或改排序；
- 不新增、删除、强化或弱化正式建议；
- 不推断能力、人格、动机、心理、健康、家庭或受保护人口属性；
- 不作因果诊断、未来预测、正式评价或行动命令；
- 不写回 M5/M8/M9 业务状态，不自动批准、发布或触发教学行动；
- 不向学生直接发布。

## 输出与校验

模型必须返回封闭 JSON：

- `fact_interpretations`：逐项原样回传事实代码和本地渲染代码；
- `review_questions`：只选择白名单问题代码并引用兼容的已提供事实；
- `suggestion_explanations`：逐项原样回传既有建议及本地解释代码；
- `citation_ids`：完整、同序回传事实别名。

M9 本地执行以下校验：

- 根字段、子字段、类型、数量和顺序完全匹配；
- `source_digest`、事实渲染代码、建议解释代码、动作、状态和引用逐字匹配；
- 模型不能返回 `text`、`explanation` 或任何额外字段；
- 核查问题代码必须在白名单内，且只能引用兼容类型的事实；
- 未知代码、同义改写、诱导式问题和任何模型自由文本均整体拒绝；
- 只接受 `finish_reason=stop`，首版不自动重试。

任一超时、空响应、解析失败、非法引用或封闭代码校验失败都会失败关闭，教师仍可
查看原始程序报告。代码选择仍可能不适合具体课堂情境，因此程序固定添加
“AI 辅助解读，可能出错，不构成评分、正式评价或教学决定”的提示。

## 显式启用

```python
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m9_teacher_analytics import (
    DeepSeekM9NarrativeAdapter,
    M9TeacherAnalyticsService,
)

client = DeepSeekClient(
    model_name="deepseek-v4-flash",
    model_version="runtime-api",
    thinking_enabled=False,
    temperature=0.0,
    max_attempts=1,
    max_tokens=1536,
)
service = M9TeacherAnalyticsService(
    repository,
    statistics_engine,
    suggestion_rule_engine,
)
service.configure_teacher_interpreter(
    DeepSeekM9NarrativeAdapter(client)
)

result = service.interpret_teacher_analytics(
    actor_context,
    authoritative_analytics_bundle,
)
```

没有 `DEEPSEEK_API_KEY` 时，调用会在网络前以
`MODEL_ADAPTER_UNCONFIGURED` 失败关闭。

## M9 自有审计

SQLite/PostgreSQL schema version `10` 新增
`m9_model_invocation_audits`。每次实际尝试由 M9 原子、幂等记录：

- 调用、请求和来源报告身份及 checksum；
- 模型、策略、提示词和输出 schema 版本；
- 输入摘要、provider 状态、校验状态、安全标记；
- token、延迟、稳定错误码；
- 通过校验并实际展示给教师的结构化解读及其 checksum。

不保存 provider 原始响应封装、未通过输出、完整提示词、思维链或 API key。
SQLite→PostgreSQL 导入器包含该表，不会在后端切换时丢失 M9 审计。

## 模型质量当前边界

M8 当前空标定只有零样本和空指标，因此
`build_model_quality_report(...)` 继续返回 `insufficient_data`。
`ready` 只能表示“技术上可送交教师审核”，不等于教师批准或参数发布。
M9 不直接修改 M8 参数集。

## 长期任务冻结门

长期任务名：`M9 DeepSeek 教师解读边界校准与扩展`。

在全仓测试、跨模块联调和教师标注评估完成之前，不扩大当前边界。届时至少比较：

- 无证据陈述率、引用正确率和含义反转率；
- 数字、状态、否定词和建议倾向改变率；
- 因果、诊断、标签和受保护属性推断率；
- 教师拒绝率、修改率、节省时间和对判断偏差的影响；
- 不同群体的反事实一致性；
- 模型版本漂移、失败率、回退率、时延和成本；
- 与完全不使用 DS、只看程序原报告的基线差异。

只有测试证据支持时，才讨论调整最小群体门槛、允许冻结数字片段、扩展跨事实综合，
受约束的语句压缩，或在完成隐私评审后试验教师主动选择的去标识化个体视图。
重新评分、诊断、自动决定、自动发布和修改正式建议保持永久红线。

## 测试

所有模型测试使用假传输，不调用真实 DeepSeek：

```shell
python -m pytest -q tests/unit/test_m9_deepseek.py
```
