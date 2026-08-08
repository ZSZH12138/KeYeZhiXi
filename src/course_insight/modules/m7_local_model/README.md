# M7 DeepSeek 评分与确定性学生反馈

## 负责人

冯

## 职责

在教师量规、M2 课程证据和安全输出契约约束下执行主观评分，并依据 M6
动作生成确定性学生反馈。真实 LLM 调用只用于主观评分且统一使用 DeepSeek API；
密钥只能在调用时从
`DEEPSEEK_API_KEY` 读取，不进入配置对象、提示记录、异常或日志。

M7 现在提供两条明确分离的运行路径：

- 默认路径使用 `PlaceholderRubricAdapter` 和 `EmptyDeepSeekAdapter`，不读密钥、
  不访问网络，架构空示例保持 `status=empty`；
- 显式构造 `DeepSeekClient` 与 `DeepSeekM7Adapter` 后，才允许真实评分请求；
  学生反馈始终由本地模板生成，不读取密钥、不访问网络。客户端仅接受
  `deepseek-v4-flash` 或 `deepseek-v4-pro`；冻结的
  `m7-governed-v1` 策略固定使用 Flash、非思考模式、`temperature=0` 和
  4096 最大输出 token。

## 输入来源

- M8 `RubricScoringTask`；
- M6 `FeedbackGenerationTask`；
- M2 `EvidenceBundle`；
- 内部生成的 `LLMGenerationRequest`，只保留模板版本、证据 ID 和输入校验和。

## 输出

- `RubricScoringResult` 返回 M8，并强制加入
  `teacher_review_required`，不能直接成为无人审核的最终评分；
- `StudentFeedbackPackage` 返回 M0 学生外层，引用只能来自当前
  `EvidenceBundle`，短期只携带来源与定位，不携带证据原文；所有学生可见文字
  必须通过答案泄露检查；
- DeepSeek 的 `LLMGenerationResult` 只在适配器内瞬时解析，不交给仓储；
  M7 将安全提示元数据、调用状态、安全判定和隐私判定合并成一份原子审计。
  最终 `RubricScoringResult` 仍由 M8 的评分流程负责审计。

评分提示使用 `m7-rubric-scoring-json@4.0.0`；反馈模板使用
`m7-deterministic-feedback@1.0.0`。评分输入的题干、学生答案、量规文字和
课程证据全部位于 user JSON，并被 system 规则声明为不可信数据，不能改变角色、
评分规则或输出格式。

学生答案在任何提示消息构造前执行 `m7-outbound-privacy-v1`：邮箱、手机号、
校验通过的中国居民身份证号和带明确标签的学号使用固定占位符脱敏；明确的姓名、
详细地址、健康或家庭自由文本失败关闭。若答案主要由被脱敏标识符构成，系统也会
以 `redaction_meaning_loss` 阻断，转入人工或本地处理。没有默认绕过开关。

## 显式启用

默认应用工厂仍使用占位适配器。需要真实调用时，在运行时显式注入：

```python
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m7_local_model import (
    DeepSeekM7Adapter,
    M7LocalModelService,
)

client = DeepSeekClient(
    model_name="deepseek-v4-flash",
    model_version="runtime-api",
)
adapter = DeepSeekM7Adapter(client)
service = M7LocalModelService(adapter, m7_repository, output_validator)
```

构造函数保持原有三个参数。随后只调用公开入口：

- `score_subjective_answer(rubric_scoring_task, evidence_bundle)`；
- `generate_student_feedback(feedback_generation_task, evidence_bundle)`。

如果没有 `DEEPSEEK_API_KEY`，主观评分在网络请求前以
`MODEL_ADAPTER_UNCONFIGURED` 失败关闭；确定性反馈不受影响。

## 安全与校验

- HTTPS 目标固定为 `https://api.deepseek.com/chat/completions`；
- 使用非流式 JSON Output、有限超时、有限指数退避和响应大小上限；
- v1 执行策略固定使用 V4 Flash、非思考模式和零温度，不接受自定义主机或密钥变量名；
- 评分必须完整且仅覆盖冻结量规分项，正分必须引用学生原文和允许的课程证据，
  单项/总分不能越界；`teacher_review_required` 同时在适配器和 M7 服务边界强制，
  所有评分都进入教师复核；
- 反馈根据 M6 `action_type` 选择固定模板，只引用当前证据包，缺失概念必须属于
  M6 目标；该路径不会消费任何模型生成文本；
- 只对 429、可恢复 5xx、超时、空 JSON content 和资源不足做有限重试；内容
  过滤、截断、畸形/越界 JSON 均失败关闭，不做模型语义修复；
- 所有错误使用稳定、安全错误码，不回传服务端响应正文。

## 调用审计的当前边界

真实评分调用形成一份 `M7ModelAuditRecord`，由 SQLite/PostgreSQL 在一个事务中
幂等写入。记录只包含：

- 请求/调用标识、模型与模板/策略版本、规范化证据 ID；
- 原答案、脱敏后答案、提示输入和验证结果的 SHA-256（适用时）；
- 隐私决定、白名单安全标记、脱敏数量、token、耗时、状态、白名单错误码和
  安全检查时间。

完整提示词、学生答案、匹配到的个人信息、模型响应正文和结构化评分内容均不写入
M7 调用审计。读取接口只接受 `system_admin`；教师与学生均被拒绝。默认保留期为
180 天。部署必须由受信任的调度器至少每天执行一次
`python manage.py purge_m7_model_audits`；该命令使用应用组合根选择 SQLite 或
PostgreSQL 仓储，只输出删除数量，不读取或输出审计内容。底层服务仅保留显式
1—3650 天参数供受控运维集成使用，生产命令固定采用已批准的 180 天。

## 测试

M7 测试全部使用可注入假传输，不消耗真实 API：

```shell
python -m pytest -q \
  tests/unit/test_deepseek_client.py \
  tests/unit/test_m7_deepseek.py
```

## 禁止事项

不得接入 DeepSeek 以外的 LLM、把 DeepSeek 用于学生反馈、加载本地模型权重、
硬编码密钥、默认联网、保存完整模型响应、接受不匹配证据、引用不存在证据、
绕过教师复核、绕过出站隐私治理，或在学生反馈中泄漏答案/证据原文。
