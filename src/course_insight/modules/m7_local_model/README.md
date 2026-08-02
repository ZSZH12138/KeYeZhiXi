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
  `EvidenceBundle`，且必须通过答案泄露检查；
- DeepSeek 的 `LLMGenerationResult` 只在适配器内瞬时解析，不交给仓储；
  M7 只记录安全提示元数据、`ModelInvocationAudit` 和
  `SafetyCheckResult`。最终 `RubricScoringResult` 由 M8 的评分流程负责审计。

评分提示使用 `m7-rubric-scoring-json@3.0.0`；反馈模板使用
`m7-deterministic-feedback@1.0.0`。评分输入的题干、学生答案、量规文字和
课程证据全部位于 user JSON，并被 system 规则声明为不可信数据，不能改变角色、
评分规则或输出格式。

当前版本按负责人冯于 2026-08-02 的决定，暂不提供学生答案个人信息检测、阻断
或自动脱敏。显式启用真实适配器后，`student_answer` 会原样进入 DeepSeek 请求；
是否增加出站治理机制，待与项目负责人讨论后再确定。

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
  单项/总分不能越界，所有评分都进入教师复核；
- 反馈根据 M6 `action_type` 选择固定模板，只引用当前证据包，缺失概念必须属于
  M6 目标；该路径不会消费任何模型生成文本；
- 只对 429、可恢复 5xx、超时、空 JSON content 和资源不足做有限重试；内容
  过滤、截断、畸形/越界 JSON 均失败关闭，不做模型语义修复；
- 所有错误使用稳定、安全错误码，不回传服务端响应正文。

## 调用审计的当前边界

真实评分调用只形成三份可持久化记录：

- 安全提示记录：模板/策略版本、输入校验和和证据 ID；
- `ModelInvocationAudit`：模型、token、耗时、状态和错误码；
- `SafetyCheckResult`：输出安全检查状态与有限标记。

完整提示词、学生答案、模型响应正文和验证后的评分结果均不写入 M7 调用审计。
SQLite/PostgreSQL 目前只持久化最终 `StudentFeedbackPackage`，审计相关方法是
显式 no-op；在 M1—M3 详细实现和整体数据保留政策确定前不新增审计表。

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
绕过教师复核，或在学生反馈中泄漏答案。真实部署前必须单独确认学生答案的
个人信息出站规则。
