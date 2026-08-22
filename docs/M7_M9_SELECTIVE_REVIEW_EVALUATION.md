# M7/M9 选择性审核与模型评测说明

> PR2 必读。本文覆盖长期任务 1–5、7–8 与 DeepSeek 密钥装配接口（任务 10）。
> 隐私治理任务 6 已在 PR1 完成，本 PR 不复制第二套隐私规则。

## 1. 本 PR 完成什么

1. 为 M7 建立 `DeepSeek-V4-Flash`、`DeepSeek-V4-Pro` 与思考/非思考模式的四组合候选矩阵。
2. 建立版本化评分质量证据格式、离线指标和严格的数据切分校验。
3. 建立 `all_review`、`shadow`、`selective` 三态教师审核选择器。
4. 将模型、模式、提示词、执行策略、特征格式、数据与切分身份绑定到证据；任一变化都会使旧证据失效。
5. 提供只读本地数据准备、受预算约束的 live 评测与选择器拟合工具。
6. 为 M9 增加可重放的低风险评分抽检、解释候选评测与独立技术质量门。
7. 提供固定读取 `DEEPSEEK_API_KEY` 的装配入口；公共接口、命令行参数、日志和证据文件均不接收或保存原始密钥。

本 PR 不在没有证据时选择“最佳模型”，也不把公开数据集成绩解释为本课程生产可用性。当前环境没有
`DEEPSEEK_API_KEY`，因此仓库默认仍是 `all_review`，M9 解释候选仍关闭。

## 2. 不修改的公共边界

- M7 继续输出公共 `RubricScoringResult`。
- M8 继续根据 `review_flags` 生成 `pending` 或 `not_required`。M7 清空复核标记也不能覆盖 M8
  现有的 `low_confidence_threshold`；低于量规阈值的结果仍会进入复核。
- M9 继续输出公共 `TeacherAnalyticsBundle`、`ReviewQueueItem` 与 `ModelQualityReport`。
- 本 PR 不修改公共契约、M8、M5、M6、M0 或 Application。
- PR1 记录的 B-05 延迟入账/rescore 公共编排缺口仍由公共契约负责人处理，本 PR 的选择性审核不绕过该缺口。

## 3. 为什么不能直接相信模型自报 confidence

`confidence` 只是一项候选特征，不是自动放行承诺。选择器必须使用教师复核结果定义
`material_error`，在独立 calibration split 上把候选特征校准为经验错误风险，并在从未参与阈值选择的
test split 上报告风险—覆盖率。不能用同一批样本同时调提示词、拟合阈值并报告最终效果。

选择器采用以下固定流程：

1. 在查看 test 结果前冻结 `material_error` 定义、最大可接受风险、置信水平、最小样本量和分层方案。
2. 使用单调 PAV/isotonic 分段把候选特征映射为错误风险。
3. 对拟自动接受的集合计算单侧风险上界；点估计达标但上界超标时仍送审。
4. 对未见量规 ID/版本、样本不足、长度越界、非有限特征、隐私脱敏、结构异常或依赖漂移执行 hard defer。当前公共评分任务没有显式“题型”字段，因此运行时只能强制量规分层；更细题型仅能在离线数据清单中评估，不能冒充已受运行时门禁保护。
5. 只允许固定身份完全匹配且通过技术发布门的 artifact 进入 `selective`。

## 4. 三种审核模式

| 模式 | 教师队列 | 用途 |
|---|---|---|
| `all_review` | 所有 M7 主观评分进入复核 | 默认、缺证据、证据失效或紧急回退 |
| `shadow` | 仍全部复核，同时记录选择器本来会如何路由 | 收集课程域教师金标并验证反事实风险 |
| `selective` | hard defer 和高风险样本必审；低风险样本自动接受并由 M9 抽检 | 仅在独立课程域证据通过并经负责人批准后启用 |

任何缺文件、校验和不符、版本漂移、解析错误、样本不足或分布外输入都只能回到
`all_review`，不能回退成“按模型 confidence 放行”。选择器不会在线自学习，避免一次错误教师操作或短期分布变化
静默改变生产策略。

## 5. M9 抽检与质量门

M9 对所有 `pending` 评分保持原有强制入队；只对 M8 已判为 `not_required` 的 M7 评分执行稳定、可重放的
分层抽检。抽检身份由版本化策略和评分审计身份确定，相同输入重放得到相同结论，默认抽检率为 0。
抽检条目标记为 `quality_audit_sample`，不能与高风险强制复核混为一谈。

技术质量门读取经过 SHA-256 固定的汇总证据，并核对模型、模式、提示词、策略、数据、split 与指标身份。
公共 `ModelQualityReport.status=ready` 只表示“技术上可以进入 shadow/人工批准流程”，不会自动修改运行模式，
也不会替代教师或负责人批准。

M9 自由叙事仍是 default-off candidate。无教师标注时评测必须返回 `insufficient_data`；生产教师报告继续使用
现有封闭代码、结构化事实和本地模板。

## 6. 数据与许可边界

- CESA（1,800 个、5 道科学短答题）和 ASAP-ZH（942 个、3 道题）可用于中文外部机制测试。
- SciEntsBank/SemEval 可用于 unseen-answer、unseen-question、unseen-domain 的英文分布外测试。
- 适配器只读取用户已下载的本地包，要求调用方提供 SHA-256，不自动联网下载，也不把第三方全文提交仓库。
- 在数据包内许可经人工确认前，manifest 使用 `license=NOASSERTION`；“公开下载”不等于允许再分发。
- 外部数据缺少本项目完整多维量规、课程证据引用和本校教师复核标签，因此最多支持机制验证和 shadow，
  不能单独批准生产 `selective`。

课程域数据必须按题目、量规版本以及适用的学生/班级组切分，防止同一题或同一模板跨 train、calibration、
test 泄漏。持久化汇总只保留 case id、版本、指标、计数和校验和，不保存学生答案、提示词或供应商正文。

## 7. 评测指标

评分质量至少报告：

- normalized MAE、normalized RMSE、QWK、exact agreement、adjacent agreement；
- Brier score 与 ECE（只把置信度视为待校准特征）；
- coverage、selective risk、risk-coverage curve、material-error review recall；
- 最差题目/分层表现、样本数，以及 bootstrap 或二项置信区间；
- JSON 空响应、结构失败、证据错误、延迟、token 与显式价格快照。

不能只报告宏平均或单次调用。四组合候选必须使用相同数据、split、提示词版本与重复次数，成本比较必须绑定
评测时的价格快照，不能把会变化的网页价格写成永久常量。

## 8. live 运行安全

live 入口必须同时要求显式的联网确认、第三方处理确认、最大 case 数和预算上限。密钥只从
`DEEPSEEK_API_KEY` 环境读取；不得提供 `--api-key`，不得把密钥放进 model ref、异常、审计、stdout 或结果文件。
预算检查应在每次请求前执行，达到上限立即停止。

DeepSeek 当前官方文档列出 `deepseek-v4-flash` 与 `deepseek-v4-pro`，两者均支持 JSON Output 和思考/非思考
模式。思考模式忽略 temperature 等采样参数；JSON Output 偶发空内容，因此空响应必须计为失败而不是自动重试到
成功后再只报告成功样本。

live 汇总必须保存响应中的 provider model 与 `system_fingerprint`，以便发现候选别名后的后端变化；不保存正文。
现有公共 DeepSeek 审计仍只记录配置别名和内部 `runtime-api`，无法自动证明生产后端未漂移。因此在公共基础设施
暴露并校验 provider identity 之前，证据只足以批准 `shadow`，不能仅凭本 PR 自动打开生产 `selective`。

## 9. 发布顺序

1. 代码与 synthetic/toy 回归通过，默认保持 `all_review`。
2. 外部数据集运行通过，只允许进入 `shadow`。
3. 使用课程域教师金标重新冻结定义、分层、阈值和 split。
4. 独立 test 达标，并在 shadow 期通过 M9 抽检验证风险上界、最差分层和教师 override/reject 率。
5. 负责人显式批准 artifact SHA 与策略版本后才可启用 `selective`。
6. 模型、模式、提示词、量规、隐私策略、特征 schema 或数据漂移时立即退回 `all_review` 并重新评测。

## 10. 方法与数据依据

- DeepSeek 官方模型与价格：<https://api-docs.deepseek.com/quick_start/pricing/>
- DeepSeek 思考模式：<https://api-docs.deepseek.com/guides/thinking_mode/>
- DeepSeek JSON Output：<https://api-docs.deepseek.com/guides/json_mode/>
- CESA / ASAP-ZH：<https://aclanthology.org/2020.aacl-main.37/>
- SciEntsBank / SemEval：<https://www.nist.gov/publications/semeval-2013-task-7-joint-student-response-analysis-and-8th-recognizing-textual>
- SelectiveNet 风险—覆盖率：<https://proceedings.mlr.press/v97/geifman19a.html>
- Learning to Defer：<https://proceedings.mlr.press/v119/mozannar20b.html>
- NIST AI RMF Measure：<https://airc.nist.gov/airmf-resources/airmf/5-sec-core/>
- ETS 自动评分部署评估：<https://www.ets.org/research/policy_research_reports/publications/report/2020/kbxs.html>
