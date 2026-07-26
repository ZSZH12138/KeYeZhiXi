# M4 意图数据格式

本目录只保存人工编写的中性示例，用于验证离线训练与加载链路。不得复制真实学生
文本、课程历史数据库内容或其他个人数据到这里。`example.jsonl` 的内容身份被训练
脚本固定识别为 `sample-only`，即使改名或复制也不能通过生产门禁。

每行必须是一个 UTF-8 JSON 对象，包含且只包含以下八个治理字段；需要固定切分时
可额外包含 `split`：

```json
{
  "example_id": "intent_example_0001",
  "text": "请解释这个通用概念",
  "label": "qa",
  "locale": "zh-CN",
  "paraphrase_group_id": "qa_definition_01",
  "source": "manual",
  "approved": true,
  "notes": "",
  "split": "train"
}
```

- `example_id` 必须唯一；`approved` 必须是布尔值 `true`，未批准样本不会进入训练。
- 标签只允许 `qa`、`diagnostic`、`practice`、`correction`、
  `stage_assessment`、`out_of_scope`。OOS 只用于模型内部；适配器边界返回
  `label=None` 的拒答/弃权状态，绝不进入 `TaskPlan`。
- `locale`、`source`、`example_id`、`paraphrase_group_id` 使用有界安全标识符；
  `notes` 只存非敏感治理说明。
- 文本按 `m4-text-normalization-v1`（Unicode NFKC、首尾/连续空白处理、casefold）
  做重复检测和训练/推理。规范化后重复内容（包括跨标签重复）会被拒绝。
- 同一 `paraphrase_group_id` 不得跨 `train`、`validation`、`test`。若使用显式
  `split`，所有行都必须提供且每个分区都必须覆盖六个训练标签；否则脚本按 group
  和固定随机种子确定性切分。
- JSON 未知字段、重复键、重复 ID、非法标签、空文本、非有限 JSON 常量和超限行
  均会安全失败，错误消息不得回显原始文本。

正式训练必须使用独立治理、人工审核的数据源，并保留其 SHA-256。示例数据及其
指标只能证明管线可运行，不代表生产质量。
