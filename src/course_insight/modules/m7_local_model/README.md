# M7 DeepSeek 能力与确定性学生反馈

## 负责人

冯

## 职责

在教师量规、M2 课程证据和安全输出契约约束下执行主观评分，并依据 M6
动作生成确定性学生反馈。真实 LLM 统一使用 DeepSeek API，并按用途白名单服务
`subjective_scoring`、`knowledge_extraction`、`question_concept_linking` 和
`student_rag_qa`；测评后的补救反馈仍由本地模板生成。密钥只能在调用时从
`DEEPSEEK_API_KEY` 或 M0 受控密钥存储读取，不进入提示记录、异常或日志。

M7 现在提供两条明确分离的运行路径：

- 默认路径使用 `PlaceholderRubricAdapter` 和 `EmptyDeepSeekAdapter`，不读密钥、
  不访问网络，架构空示例保持 `status=empty`；
- 通过 `build_deepseek_m7_adapter` 显式构造客户端、本地隐私复核器与适配器后，才允许
  真实评分请求；任何复核器缺失、损坏、异常或结果不确定都会在网络前失败关闭；
  学生反馈始终由本地模板生成，不读取密钥、不访问网络。客户端仅接受
  `deepseek-v4-flash` 或 `deepseek-v4-pro`；冻结的
  `m7-governed-v2` 默认使用 Flash、非思考模式、`temperature=0` 和 4096 最大输出
  token；Flash/Pro × 思考/非思考四组合只作为版本化候选，未凭空指定优胜模型。

## 输入来源

- M8 `RubricScoringTask`；
- M6 `FeedbackGenerationTask`；
- M2 `EvidenceBundle`；
- 内部生成的 `LLMGenerationRequest`，只保留模板版本、证据 ID 和输入校验和。

## 输出

- `RubricScoringResult` 返回 M8；默认 `all_review` 强制加入
  `teacher_review_required`。只有身份/SHA 完全匹配且通过课程域校准门的 `selective`
  选择器才能返回空标记，M8 自己的低置信门仍可重新要求复核；当前公共 DeepSeek
  审计尚不暴露 provider fingerprint，因此本 PR 的证据只批准 `shadow`，不自动打开生产免审；
- `StudentFeedbackPackage` 返回 M0 学生外层，引用只能来自当前
  `EvidenceBundle`，短期只携带来源与定位，不携带证据原文；所有学生可见文字
  必须通过答案泄露检查；
- DeepSeek 的 `LLMGenerationResult` 只在适配器内瞬时解析，不交给仓储；
  M7 将安全提示元数据、调用状态、安全判定和隐私判定合并成一份审计记录，
  并以仓储事务幂等写入；这不表示外部 HTTP 请求与数据库写入具有原子性。
  最终 `RubricScoringResult` 仍由 M8 的评分流程负责审计。

评分提示使用 `m7-rubric-scoring-json@5.0.0`；模型必须返回空 `review_flags`，复核路由
只由本地程序决定。反馈模板使用
`m7-deterministic-feedback@1.0.0`。评分输入的题干、学生答案、量规文字和
课程证据全部位于 user JSON，并被 system 规则声明为不可信数据，不能改变角色、
评分规则或输出格式。

学生答案在任何提示消息构造前执行 `m7-outbound-privacy-v2`。边界明确的邮箱、
手机号、校验通过的中国居民身份证号和带明确标签的学号先由确定性层替换为固定
占位符；经过治理的文本随后必须通过本地 `PrivacyReviewer`。推荐组合是
Presidio + `zh_core_web_sm` 的中文实体识别，以及经审批、SHA-256 钉住的轻量
scikit-learn 语义分类器，用于识别自由文本身份、地址、健康和家庭披露。只有所有
启用的复核器都返回 `allow` 才能构造 DeepSeek prompt；`review`、`block`、缺依赖、
模型损坏、置信不足和运行异常均阻断。若脱敏破坏了主要评分语义，也以
`redaction_meaning_loss` 阻断。该机制不再依赖不断扩张的自由文本关键词表，也没有
默认绕过开关。

Presidio/spaCy 与语义分类器是进程内专用检测组件，不是生成式 LLM。可选依赖通过
`pip install -e '.[privacy]'` 安装；中文 spaCy 模型与 sklearn 工件必须由可信管理员
在隔离环境中部署并钉住版本/校验和，应用运行时不得联网下载。数据与工件准入见
`data/m7_privacy/README.md`。结合本项目最低版本，真实隐私运行时只批准 Python
3.11–3.13；使用 Python 3.14 的开发环境只能验证失败关闭和假后端，不得据此宣称
真实中文链路已联调。

## 显式启用

默认应用工厂仍使用占位适配器。教师配置页可以保存密钥，但只有同时存在
`COURSE_INSIGHT_M7_PRIVACY_MODEL_SHA256` / `COURSE_INSIGHT_M7_PRIVACY_MANIFEST_SHA256`
和 `runtime/m7_privacy/privacy-model/` 工件时，工厂才会调用
`build_required_m7_privacy_reviewer` 并装配真实评分适配器。缺工件时保持占位适配器，
不会改用 DenyAll 假装已启用。M9 教师解读在密钥存在时单独配置，不发送学生原文。

```python
from course_insight.modules.m7_local_model import (
    M7LocalModelService,
    build_deepseek_m7_adapter,
    build_required_m7_privacy_reviewer,
)

privacy_reviewer = build_required_m7_privacy_reviewer(
    runtime_dir=runtime_dir,
    model_dir=runtime_dir / "m7_privacy" / "privacy-model",
    expected_presidio_version="2.2.364",
    expected_spacy_version="3.8.13",
    expected_spacy_model_version="3.8.0",
    expected_semantic_model_id="m7-semantic-privacy",
    expected_semantic_model_version="1",
    expected_semantic_model_sha256=approved_model_sha256,
    expected_semantic_manifest_sha256=approved_manifest_sha256,
)
adapter = build_deepseek_m7_adapter(privacy_reviewer=privacy_reviewer)
service = M7LocalModelService(adapter, m7_repository, output_validator)
```

构造函数保持原有三个参数。随后只调用公开入口：

- `score_subjective_answer(rubric_scoring_task, evidence_bundle)`；
- `generate_student_feedback(feedback_generation_task, evidence_bundle)`。

如果没有 `DEEPSEEK_API_KEY`，主观评分在网络请求前以
`MODEL_ADAPTER_UNCONFIGURED` 失败关闭；如果没有可用且通过完整性校验的本地隐私
复核器，则以 `MODEL_INPUT_PRIVACY_BLOCKED` 失败关闭。确定性反馈不受影响。

Presidio 达到阈值的任何实体（包括 `DATE_TIME`、`ORGANIZATION`、`NRP`）都会阻断，
且没有可由调用方配置的忽略列表。这样会保守地误拦一部分课程日期和组织名，但不会用
不断扩张的邻近关键词规则把实体静默改判为安全。未来只有在固定版本的 typed-entity 特征、
语义分类器和独立回归集完成校准后，才能以新策略版本降低误拦；当前语义模型不能覆盖
Presidio 的确定性阻断。全角字符或零宽格式字符若使直接标识符只能在规范化后被识别，
系统不会尝试改写原文，而是以通用隐私标记在网络前阻断。

sklearn 工件启用时，模型文件和 manifest 必须分别由部署配置钉住 SHA-256；manifest
声明的 Python、scikit-learn 与 joblib 版本必须与运行时完全一致，所有校验均在
`joblib` 反序列化前完成。普通 SHA-256 只提供已审批字节的一致性校验，不替代签名、
发布权限或运行目录隔离。

残余审计风险：外部 HTTP 调用与 SQLite/PostgreSQL 写入无法在一个原子事务中提交。
当前实现会在审计写失败时拒绝返回评分，但请求可能已经到达 DeepSeek。部署必须对
审计写失败告警并停用真实适配器；若未来要求“无持久审计绝不出站”，需要公共仓储/
编排契约增加调用前 reservation 与调用后终结状态，不能用一次可用性探测伪装成
原子保证。

## 安全与校验

- HTTPS 目标固定为 `https://api.deepseek.com/chat/completions`；
- 使用非流式 JSON Output、有限超时、有限指数退避和响应大小上限；
- 默认策略使用 V4 Flash、非思考模式和零温度；候选矩阵绑定版本，不接受自定义主机或密钥变量名；
- 评分必须完整且仅覆盖冻结量规分项，正分必须引用学生原文和允许的课程证据，
  单项/总分不能越界；模型无权输出复核标记，默认全部复核，`shadow` 仍全部复核，
  `selective` 只有校准风险上界、样本门和 OOD 门同时通过才允许自动接受；
- 反馈根据 M6 `action_type` 选择固定模板，只引用当前证据包，缺失概念必须属于
  M6 目标；该路径不会消费任何模型生成文本；
- 主观评分路径只对 429、可恢复 5xx、超时、空 JSON content 和资源不足做有限重试；
  内容过滤、截断、畸形/越界 JSON 均失败关闭，不做模型语义修复；
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
  tests/unit/test_m7_deepseek.py \
  tests/unit/test_m7_privacy_reviewer.py \
  tests/unit/test_m7_citation_safety.py
```

部署真实调用前，还必须在安装了钉住版本的 Presidio、spaCy 与
`zh_core_web_sm` 的 Python 3.11–3.13 目标环境运行显式 smoke：

```shell
M7_PRESIDIO_ZH_SMOKE=1 python -m pytest -q \
  tests/integration/test_m7_privacy_runtime_smoke.py
```

该 smoke 未显式启用时会跳过；跳过或在 Python 3.14 只运行假后端单测，都不能作为
真实中文链路跑通的证据。

## 禁止事项

不得接入 DeepSeek 以外的生成式 LLM、把 DeepSeek 用于无证据的测评补救反馈、加载未经审批或
未钉住校验和的本地模型权重、
硬编码密钥、默认联网、保存完整模型响应、接受不匹配证据、引用不存在证据、
绕过本地选择器/教师复核门、绕过出站隐私治理，或在学生反馈中泄漏答案/证据原文。

选择性审核、数据切分、公开数据边界、M9 抽检和 live 评测安全要求见
[`docs/M7_M9_SELECTIVE_REVIEW_EVALUATION.md`](../../../../docs/M7_M9_SELECTIVE_REVIEW_EVALUATION.md)。

## 2026-08-27 新知识链用例

M7 新增三个相互独立的 DeepSeek 用例：

- `knowledge_extraction`：每批正文可以返回零个、一个或多个知识点，必须返回所有被原文支持的候选、chunk ID 和逐字 quote；M7 本地校验引用后计算精确 span。
- `question_concept_linking`：只能从当前活动概念 ID 白名单中返回零到多个题目标签，并把该概念的完整来源并集带入关联。
- `student_rag_qa`：先从活动知识点白名单选择零到五个概念。课程资料完全覆盖时只根据允许的证据 ID 回答；部分或完全不覆盖时，可用教师同一密钥调用 `deepseek-v4-flash` Responses API 的 `web_search`。页面必须区分课程来源和 HTTPS 外部来源，并在无完整课程支撑时警告学生核对事实。

三条用例都复用固定 DeepSeek 出口、严格 JSON、重复键拒绝、响应大小上限和本地来源校验，并只使用教师为精确课程号、班级号保存的密钥。独立学生答疑先经过确定性脱敏和出站复核，但不依赖旧主观评分所需的钉住语义隐私模型；知识抽取不允许模型提供或改写 `source_version_id`，来源身份由本地输入块补齐。

知识抽取提示明确要求输出完整 JSON 数组、列出批内所有可能知识点，并在返回前自检字段、引用和关系。结构或溯源校验失败时，同一批额外重试 5 次，每次携带安全校验代码；仍失败、恰好返回 100 条或响应不完整时才递归拆小文本。普通模式 30 秒只约束一次 DeepSeek HTTP 请求；它不是批次、全部重试或入库任务的总处理时限。

知识文件进入提示前按 6,000 个非空白字符的正文预算尽量装满。PPT/PPTX 的同页碎片先合并成一个 `slide:N` 页面块，默认不再使用 64 个块的提前切批条件。顶层批次默认同时处理 3 个，代码上限为 4；完成顺序可以不同，但合并顺序固定。每个成功批次立即持久化候选知识点和实际证据子块，Worker 异常退出后可跳过已成功且模型/提示指纹未变化的批次。递归拆分产生的子块也随 checkpoint 保存，保证发布后的引用仍能找到精确原文。
