# 基于来源文件的增量知识包与可溯源 RAG 重构方案

> 版本：v1.1
> 日期：2026-08-26
> 状态：后续实现的规范性基线
> 历史快照：[`legacy_architecture_snapshot_2026-08-25.md`](legacy_architecture_snapshot_2026-08-25.md)

> 2026-08-26 实施修订：知识文件只有“新增”和“删除”，不提供替换。已有活动知识文件不再重复解析。新增时只解析新文件并按规范化标题合并来源；删除时不调用 LLM，只移除对应来源，来源归零才删除知识点。旧架构仅在独立的 `legacy_architecture_snapshot_2026-08-25.md` 中留档，本文只描述现行流程。

## 1. 结论与目标

本方案不重写课业智析，也不新增 M10。它保留现有 M0–M9 模块化单体、Pydantic 契约、PostgreSQL/pgvector、DINA/BKT、教学状态机、测评与教师分析，只重构从“教师提供资料”到“可供学生和测评使用的知识快照”这一条主链。

目标流程是：

```text
教师配置 DeepSeek API
  -> 一次上传任意数量、混合格式的知识文件和题目文件
  -> 点击“提交”暂存文件
  -> 点击“确定”形成一次变更集
  -> 后台逐文件解析、抽取知识点、建立来源链、索引证据、标注题目
  -> 生成新的不可变课程知识快照
  -> 原子切换活动快照
  -> 学生 RAG 答疑、M5 诊断、M8 测评和 M9 分析消费新快照
```

教师只需要理解四个动作：上传、提交、确定、删除；题目文件额外支持文本编辑。系统内部仍执行严格校验、版本化、审计和失败恢复，但不再要求教师维护知识包审核 ID 或操作审核状态机。

## 2. 必须满足的产品不变量

1. 文件数量是 `0..N`，系统不得假设必须存在固定的 5 个或其他数量的文件。
2. 一个文件失败只影响该文件，不得让其他有效文件失效。
3. 每个活动知识点至少有一条可解析到活动文件版本和具体位置的来源证据。
4. 每次“确定”都产生独立作业和候选快照；相同输入重复执行必须幂等。
5. 发布采用原子活动指针。学生请求只能看到完整的旧快照或完整的新快照，不能看到半成品。
6. 删除知识文件后，新快照的检索、知识点和题目标注不得继续引用该文件；已发布试卷和历史诊断仍保留原快照引用。
7. 新增文件产生全新知识点时，用数据库中的活动题目内容重新标注；只增加同名知识点来源时不重标。删除知识文件时不调用 LLM，只移除已删除知识点的题目链接并刷新保留链接的来源。
8. DeepSeek 输出永远只是候选数据；必须通过 JSON Schema/Pydantic、ID 白名单、来源覆盖和领域约束校验后才能入库。
9. 知识入库 Worker 不可用时，只把入库能力标为降级，不得让认证和普通页面统一返回 503。
10. 教师知识发布不再有 `draft/submitted/approved/rejected/recalled` 审核流程；“确定”就是有权限教师的发布授权边界。
11. DeepSeek 分析输入默认最多包含 6,000 个非空白 Unicode 字符；超限时先按段落组织，单段仍超限时优先在句末切割，确实没有可用句界才按字符硬切。
12. 一个分析块可能包含零个、一个或多个知识点；DeepSeek 必须返回该块中全部可识别知识点的数组，不能只返回“最主要的一个”。
13. 同一知识点从多个文件或多个位置抽取时，来源断言必须做集合并集；规范名、定义或置信度更新不得覆盖既有来源。

## 3. 范围与非目标

### 3.1 本次范围

- 知识文件：Markdown、TXT、PDF、PPT、PPTX；保留现有 DOCX 解析能力，但不作为首屏主推格式。
- 旧 `.ppt`：上传时验证 OLE Compound File 文件头；ingestion Worker 在安装了 Microsoft PowerPoint 的 Windows 主机上关闭宏、只读且隐藏窗口地转换成临时 PPTX，再复用现有 PPTX 解析器。临时文件不入库，引用继续使用原始 `.ppt` 文件名；转换能力不可用或转换失败时只影响该文件。
- 题目文件：固定 UTF-8 TXT 格式，支持选择题、填空题和主观题。
- 教师 API 配置和可用性测试。
- 多文件暂存、批量删除、进度、失败重试、活动快照发布。
- 知识点抽取、稳定身份、来源链和先修关系候选。
- 题目解析和自动 Q 矩阵标注。
- 学生可溯源 RAG 答疑页面。
- M0–M9 下游兼容与迁移。

### 3.2 非目标

- 不引入新的独立向量数据库。
- 首版不整体采用 Microsoft GraphRAG，也不自动生成开放式知识图谱社区摘要。
- 首版不引入 Celery/Redis 作为必需基础设施。
- 不让教师逐条审批每个知识点或每条 Q 链接。
- 不在本次重写 M5 算法、M6 策略学习、M8 IRT 或 M9 成绩复核。
- 不对已经发出的试卷和历史状态做破坏性回写。

## 4. 研究依据与技术取舍

| 依据 | 可复用结论 | 本项目取舍 |
|---|---|---|
| [NeurIPS 2020 RAG](https://proceedings.neurips.cc/paper_files/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html) | 用外部非参数知识支撑生成，使知识可更新并能提供依据 | 保留 M2 检索，M7 只基于检索证据回答，不让模型凭记忆补答 |
| [W3C PROV-O](https://www.w3.org/TR/prov-o/) | `Entity/Activity/Agent`、`wasDerivedFrom`、`wasRevisionOf` 可表达来源和版本 | 不引入 RDF 服务；在关系表中实现等价的文件—块—知识断言—作业来源链 |
| [GraphRAG 数据流](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) | 文档到 TextUnit，再由知识项反向引用 TextUnit，可保留 breadcrumbs | 采用“概念断言必须引用 chunk”的模式，不采用完整 GraphRAG；其仓库已说明主要处于[维护模式](https://github.com/microsoft/graphrag) |
| [LlamaIndex ingestion pipeline](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/ingestion/pipeline.py) | 内容哈希、父文档引用、去重、upsert/delete 是成熟入库模式 | 在现有 M1/M2 仓库内实现，不新增 LlamaIndex 运行时依赖 |
| [LlamaIndex SentenceSplitter](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/sentence.py) 及其[测试](https://github.com/run-llama/llama_index/blob/main/llama-index-core/tests/text_splitter/test_sentence_splitter.py) | 按段落、句子、正则片段、字符逐级降级，能减少悬空半句并覆盖中文和连续长文本 | 自行实现小型确定性切块器：段落优先、句末优先、字符兜底，保留本项目稳定 locator，不新增框架依赖 |
| [Late Chunking](https://arxiv.org/abs/2409.04701) | 独立短块可能丢失长距离上下文；分块仍需携带文档级语境 | 每个分析块保留文件名、标题路径、相邻定位和原始 chunk IDs；首版不更换现有 embedding 模型 |
| [Unstructured PPTX 分区器](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pptx.py) | PPTX 可保留页码、表格和备注等元数据 | 继续使用现有轻量解析器；仅将 Unstructured 作为后续可替换 ParserRegistry 适配器，不污染当前权威环境 |
| [Unstructured API 格式表](https://github.com/Unstructured-IO/unstructured-api/blob/main/README.md) | `.txt/.md/.ppt/.pptx/.pdf` 均可经分区流水线处理 | 用于验证格式策略；旧 `.ppt` 仍通过隔离转换器处理 |
| [pgvector](https://github.com/pgvector/pgvector) | 向量可与关系数据一起 upsert、update、delete，保留 PostgreSQL 事务能力 | 继续使用现有 M2 PostgreSQL/pgvector，按来源版本和快照过滤/删除 |
| [Celery task 指南](https://github.com/celery/celery/blob/main/docs/userguide/tasks.rst) | 可重试任务必须幂等，任务状态可暴露 `PROGRESS` | 复用 M0 现有 lease/heartbeat/CAS 实现同等语义；暂不引入 Celery/Redis |
| [DeepSeek JSON 输出](https://api-docs.deepseek.com/guides/json_mode) 与 [JSON Schema](https://api-docs.deepseek.com/api/create-response/) | 可请求结构化输出，但 JSON 模式仍可能空返回或截断 | 严格 Schema、Pydantic 二次校验、有限重试和失败隔离；空响应不得写库 |
| [LLM 题目知识概念标注研究](https://arxiv.org/abs/2403.17281) | LLM 可用于题目概念多标签标注，尤其适合标签数据不足场景 | LLM 生成候选链接，但链接必须属于活动概念集并带证据、置信度和算法版本 |
| [Q 矩阵完备性研究](https://scholarship.libraries.rutgers.edu/esploro/outputs/journalArticle/How-to-Build-a-Complete-Q-Matrix/991031758407804646) | 不完整 Q 矩阵会降低认知分类可靠性 | 发布前运行覆盖率、孤立概念、无标签题目和完备性门禁；低质量链接不直接驱动高风险诊断 |
| [Q 矩阵构建与验证案例](https://eric.ed.gov/?id=EJ994673) | Q 矩阵需要多类证据并应接受经验数据校准 | 首版用语义证据自动标注，后续由 M8 作答数据提供经验校准，不增加教师审批 UI |
| [RAGAS 论文](https://aclanthology.org/2024.eacl-demo.16.pdf) | RAG 要分别评估检索相关性、上下文覆盖、忠实度和答案质量 | 建立离线金标集和人工抽检，不只检查接口是否返回 200 |
| [OWASP 文件上传指南](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) | 扩展名、内容类型、签名、文件名、大小、权限和存储位置都要防护 | 上传文件先隔离、重命名、限额和内容探测，永不按原文件名拼接路径 |
| [Django 文件管理](https://docs.djangoproject.com/en/5.2/topics/files/) | 文件存储应由 Storage 抽象管理，不应依赖最终磁盘文件名 | M0 用 Django Storage 保存原始版本，领域表只存不可猜测对象键与校验和 |

结论：研究和仓库主要提供设计模式，而不是要求替换技术栈。当前项目已有足够的解析、检索、领域校验和后台任务骨架，新增大型框架会扩大依赖和运行环境风险，收益不足。

## 5. 新架构总览

```text
┌──────────────────────────── M0 教师 UI ────────────────────────────┐
│ API 配置 │ 多文件暂存 │ 题目编辑 │ 批量删除 │ 进度/错误/质量摘要 │
└────────────────────────────────┬───────────────────────────────────┘
                                 │ 确定：KnowledgeChangeSet
                                 ▼
┌───────────────────────── M0 Ingestion Job ─────────────────────────┐
│ lease + heartbeat + CAS + step checkpoint + retry + progress      │
└───────┬─────────────────────┬────────────────────┬─────────────────┘
        │                     │                    │
        ▼                     ▼                    ▼
  M1 确定性解析          M7 受控 DeepSeek       M3 领域校验/组装
  file -> chunks         知识抽取/题目标注       概念注册表/Q 矩阵
        │                     ▲                    │
        └──────────┬──────────┘                    │
                   ▼                               │
             M2 staging 索引 ◄─────────────────────┘
             lexical + pgvector                    │
                   └──────────────┬────────────────┘
                                  ▼
                    M3 CourseKnowledgeRelease
                  （M1/M2/M3 引用 + checksum）
                                  │ 原子切换 active_release_id
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
       M4/M7 学生答疑        M5/M6 学习支持       M8/M9 测评分析
```

### 5.1 来源级增量计算，完整不可变发布

课程已有活动发布版本时，系统先从数据库恢复知识点、来源原文、题目和题目标注，不重新读取活动源文件。知识文件变更只允许新增和删除：新增文件才进入解析与 DeepSeek 抽取；删除文件只在内存候选集中移除对应 `source_version_id`。新增与删除可以在同一作业中组合执行。

```text
course_id
+ source_version_sha256
+ parser_version
+ extraction_policy_version
+ model_id/model_version
+ embedding_model_ref
```

新增候选按规范化知识点标题与活动知识点合并，来源做集合并集。删除后仍有其他来源的知识点保留，来源归零的知识点不进入新版本。无论物理计算量多小，最终仍写入一个包含完整知识点、来源、题目和链接的新 `CourseKnowledgeRelease` 并原子切换活动指针，历史版本不原地修改。

若本次操作全部是题目文件新增、编辑或删除，则复用活动知识点和未变化题目，只读取变化的题目文件。知识文件新增若只补充已有知识点来源，旧题链接本地刷新来源即可；若出现全新知识点，才使用数据库中的题目内容重新调用 DeepSeek 多标签标注。知识文件删除不调用知识抽取或题目标注模型。

### 5.2 知识抽取分析块

M1 的证据 chunk 继续尽量贴合原文件段落和定位；在调用 M7 前，由确定性 `ExtractionBatchBuilder` 把相邻证据 chunk 组织成分析块：

1. PPT/PPTX 先把同一页内的标题、正文段落和表格单元格按原顺序合并成一个页面块，页面定位统一为 `slide:N`；只有整页超过上限时才继续按句子切分；
2. 全文不超过 6,000 个非空白 Unicode 字符时，可作为一个分析批次；
3. 超过阈值时，按原块顺序尽量装满，加入下一块会超限才结束当前批次；默认不再因为达到 64 个短块而提前切批，仍保留可显式启用的 4,096 块安全上限；
4. 单个段落或页面超过 6,000 字符时，优先在中文或英文句末标点处切割；
5. 单个句子仍超过阈值时，最后才按字符数硬切；
6. 每个子块保存原 `chunk_id`、`source_id`、locator 和原文字符区间；
7. 不用重叠文本制造重复来源；需要上下文时传入只读标题路径和前后定位摘要。

6,000 是首版默认值，作为 M7 extraction policy 的版本化配置，而不是散落在代码中的魔法数字。任何配置值必须在 `1,000..20,000` 之间，改变配置会改变提取指纹并触发重新分析。

知识抽取默认采用 3 路有界并发，部署代码强制限制在 1–4 路。调度器只维持不超过并发数的在途请求，不会一次性把全部批次提交到线程池；任一批次完成后才派发下一批。不同批次可以乱序完成，但候选知识点始终按原批次顺序合并，因此发布结果不受网络完成顺序影响。

每个成功批次立即保存独立 checkpoint，内容包括批次指纹、模型/提示版本指纹、候选知识点和实际证据子块；重启后只复用指纹完全一致且重新通过契约校验的成功批次。失败批次保存安全错误码、异常类型和批次序号，失败后停止派发新批次；运行中的其他请求完成后退出。服务端结构化日志记录作业/课程/班级上下文和安全异常类型，不记录课件正文、模型响应或 API 密钥。

## 6. 教师端最小交互

### 6.1 页面结构

教师从课程页进入“知识与题目”页面，不再输入 `review_id`。页面只包含：

1. API 状态：未配置、验证中、可用、不可用；
2. 知识文件区：拖放/选择多个文件、已上传文件列表、每文件状态与删除勾选；
3. 题目文件区：上传 TXT、查看校验结果、打开文本编辑器、删除勾选；
4. 底部操作：`提交`、`确定`；
5. 作业区：总进度、当前步骤、每文件结果和错误原因；
6. 最近活动快照：版本、文件数、知识点数、题目数、低置信链接数。

### 6.2 三个动作的精确定义

- `提交`：把浏览器选择的文件保存为 staged 版本，只做安全检查、内容哈希和基础格式校验，不改变学生可见数据。
- `删除`：对一个或多个文件建立 staged delete 标记；此时仍不改变活动快照。
- `确定`：冻结本次新增、题目编辑和删除为 `KnowledgeChangeSet`，创建后台作业。按钮必须二次确认，确认文案展示将新增、编辑和删除的文件数量。

如果没有可用 API，允许教师暂存和删除暂存项，但禁止启动需要 DeepSeek 的“确定”，并明确显示配置入口；这不会影响登录、浏览现有课程或学生使用旧活动快照。

### 6.3 进度显示

总进度按持久化步骤计算，不伪造基于时间的百分比：

```text
已排队 -> 校验文件 -> 解析文本 -> 抽取知识点 -> 建立候选索引
       -> 解析/重标题目 -> 校验知识包 -> 发布快照 -> 完成
```

每文件显示 `等待 / 处理中 / 成功 / 失败 / 已删除`。页面可轮询状态 API；后续可加 SSE，但轮询是首版可靠基线。

## 7. 发布与失败语义

### 7.1 批次状态机

```text
staged
  -> queued
  -> validating
  -> parsing
  -> extracting
  -> indexing
  -> relinking_questions
  -> validating_release
  -> publishing
  -> succeeded | partial_success | failed | cancelled
```

状态单向推进；恢复时根据 checkpoint 重入。任何重试必须以同一 `change_set_id` 和步骤指纹幂等执行。

### 7.2 文件级隔离

| 场景 | 发布结果 |
|---|---|
| 新文件成功 | 新文件进入候选快照 |
| 新文件失败 | 新文件不进入候选快照，其他成功项可发布 |
| 显式删除文件 | 新快照排除该文件并重建相关知识点、索引和题目标注 |
| 所有新增均失败且没有有效删除 | 不发布空变更，活动快照保持不变，批次为 `failed` |
| 部分成功 | 发布成功部分，批次为 `partial_success`；失败项保持可见，教师修正/重新提交后再次“确定” |

### 7.3 原子发布

M3 新增 `CourseKnowledgeRelease`，只保存同一候选版本下的公共引用与校验和：

```text
release_id
course_id
release_version
course_package_ref + checksum
evidence_index_ref + checksum
knowledge_bundle_ref + checksum
question_catalog_ref + checksum
created_by / created_at
change_set_id
quality_summary
status = staging | active | superseded | failed
```

发布事务只做两件事：校验所有引用均为 `ready`，然后 CAS 更新课程的 `active_release_id`。大型解析、LLM 和 embedding 不在发布事务中执行。

## 8. 核心数据模型

### 8.1 文件与版本（M1）

```text
SourceFile
  source_file_id           稳定文件身份
  course_id
  kind                     knowledge | question
  display_name
  active_version_id?
  lifecycle                active | pending_delete | deleted

SourceFileVersion
  source_version_id
  source_file_id
  object_key               Django Storage 对象键
  original_filename
  extension / detected_mime
  sha256 / byte_size
  parser_id / parser_version
  uploaded_by / uploaded_at
  predecessor_version_id?  仅供历史数据兼容，现行新增/删除流程不建立替换关系
  state                    staged | parsed | failed | retired
```

每次上传都按新增文件处理，重名文件不会覆盖活动文件。若教师要更新内容，应先删除旧文件，再新增新文件；系统不提供替换操作。

### 8.2 来源块（M1/M2）

每个 chunk 必须包含：

```text
chunk_id
source_version_id
content_sha256
text
locator_type              page | slide | heading | line_range
locator_payload           JSON，必须可渲染
title_path
ordinal
```

展示定位示例：

- `计算机网络基础.pdf · 第 12 页`
- `TCP拥塞控制.pptx · 第 8 张幻灯片 · “慢启动”`
- `课程说明.md · 3.2 滑动窗口`
- `术语表.txt · 第 41–48 行`

### 8.3 概念注册表与来源断言（M3）

```text
ConceptRegistry
  concept_id               首次创建后稳定
  course_id
  canonical_name
  aliases
  status                   active | retired | merged
  merged_into_concept_id?

ConceptAssertion
  assertion_id
  concept_id
  release_id
  source_version_id
  chunk_id
  evidence_span_start/end
  extracted_name / definition
  relation_type            defines | explains | example | prerequisite
  confidence
  extraction_run_id
  schema_version
```

概念不是从文件名或名称哈希直接生成。首次出现时创建稳定 `concept_id`；后续运行通过规范名、别名、词法/向量候选和受控合并规则复用身份。名称更正不会自动产生全新概念。

每个活动概念至少有一个活动 `ConceptAssertion`。删除某个来源时只删除该来源支持；若仍有其他来源，概念继续活动；若支持归零，则在新 release 中退休该概念。

合并概念时按以下不可变键去重并做来源并集：

```text
(source_version_id, chunk_id, evidence_span_start, evidence_span_end, relation_type)
```

聚合概念的 `source_count` 和来源卡片由断言集合实时推导，不允许存成会被后一次抽取覆盖的单值字段。两个文件都支持同一概念时，学生和教师必须能看到两个文件的来源。

### 8.4 题目与 Q 链接（M3）

```text
QuestionFileVersion
QuestionVersion
  question_id              文件内稳定 ID
  type                     choice | fill_blank | subjective
  stem / options / answer / explanation / rubric?
  source_version_id
  source_line_range

QuestionConceptLink
  release_id
  question_id / question_version
  concept_id
  evidence_chunk_ids
  confidence
  linker_policy_version
  status                   usable | needs_attention | rejected
```

M5/M8 只消费 `usable` 链接。`needs_attention` 会在教师质量摘要中显示，但不引入逐条审批流程。

## 9. 系统解析流水线

### 9.1 阶段 A：上传安全与格式识别（M0/M1）

对每个文件独立执行：

1. 生成不可猜测的 `object_key`，原文件名只作显示元数据；
2. 检查单文件大小、单批文件数、教师与课程配额；
3. 同时检查扩展名、浏览器 Content-Type、服务端探测 MIME/魔数；
4. 对 PDF 检查头、页数上限、加密状态和解压后资源上限；
5. 对 PPTX 检查 ZIP/Office Open XML 结构、压缩比、条目数、路径穿越和宏/嵌入对象策略；
6. 对 TXT/MD 检查编码并规范为 UTF-8，原字节仍保留；
7. 文件保存到不可直接执行、不可由 Web 服务器解释的存储区；
8. 失败只记录该文件的安全错误码，不把异常堆栈返回给教师。

首版建议默认限额：单文件 50 MiB、单批 50 个文件、解压后 250 MiB、PDF 500 页、PPTX 500 张幻灯片。配置项放在 M0 settings，测试环境可下调；具体数值应根据实际课程数据再校准。

### 9.2 阶段 B：确定性解析（M1）

继续由 M1 `ParserRegistry` 选择解析器：

- Markdown：保留标题层级与行号；
- TXT：保留行号和段落；
- PDF：优先提取文本层并保留页码；无文本层时才走可选 OCR；
- PPTX：保留幻灯片号、标题、正文、表格文本和备注；
- DOCX：保留当前兼容能力；
- PPT：隔离临时目录中转换成 PPTX，再由 PPTX 解析器处理。

M1 不提取“知识点”，只产出确定、可复现、可定位的文档与 chunks。任何解析器不得直接调用 DeepSeek。

解析后的长文本统一经过 M1 长段落切割器：先保留原段落；仅当段落超过 6,000 个非空白字符时，才依次尝试句末标点、较弱分隔符和字符硬切。切割 locator 在原 locator 后追加 `segment:<n>;chars:<start>-<end>`，所以任何子块都能回到原文件和原段落，而不是只知道“这是第几个模型输入块”。

### 9.3 阶段 C：候选知识点抽取（M7）

M7 新增 `KnowledgeExtractionAdapter`，输入是课程标识、受控 chunk 窗口和允许引用的 `chunk_id` 列表；输出遵循固定 JSON Schema：

```json
{
  "concepts": [
    {
      "name": "拥塞窗口",
      "description": "发送方用于限制未确认在途数据量的状态变量。",
      "aliases": ["cwnd"],
      "evidence": [
        {
          "chunk_id": "chk_...",
          "quote": "发送方使用拥塞窗口限制未确认的在途数据量",
          "relation_type": "definition"
        }
      ]
    }
  ],
  "citation_ids": ["chk_..."]
}
```

系统提示词必须明确写入：

```text
当前输入可能包含零个、一个或多个知识点。
请检查完整输入，返回所有能够由原文证据直接支持的知识点。
不要只返回最主要知识点，不要合并语义不同的知识点。
如果没有可识别知识点，返回空 concepts 数组。
每个知识点至少引用一个允许的 chunk_id，并逐字复制一段存在于该 chunk 的 quote。
```

字符区间不交给模型估算。M7 校验 quote 确实存在于指定 chunk 后，在本地计算零基、左闭右开的 `span_start/span_end`，再交给后续合并与溯源流程。

`concepts` 数组不设置“只取前 N 个”的业务截断。为防止异常输出导致资源耗尽，Schema 允许每分析块最多 100 个候选。格式或溯源校验失败时，同一分析块以安全校验代码修正提示并额外重试 5 次；仍失败才二分。达到 100 个候选或 DeepSeek 明确报告响应不完整时也会二分，而不是静默丢弃知识点。二分最多递归 8 层；每个成功子批立即累计已处理字符，重试中的批次只更新活动状态，最小文本完成 5 次重试后仍失败才保留原始错误。

本地校验器必须拒绝以下结果：

- 引用了输入白名单之外的 chunk；
- span 越界或与源文本不匹配；
- 没有来源证据的概念；
- 重复键、未知枚举、非有限数值、过长字段；
- 先修关系自环或生成明显环路；
- JSON 空响应、截断、Schema 不匹配。

空响应和临时网络错误最多有限重试；重试次数与退避写入配置。普通模式 30 秒是每一次 DeepSeek HTTP 请求各自的等待上限，不是批次或任务总时限。永久性文件错误不重试，避免高频失败循环。

### 9.4 阶段 D：概念规范化与候选图（M3）

M3 按顺序执行：

1. 名称规范化和别名展开；
2. 在同课程活动注册表中用精确别名、词法相似度和向量相似度产生合并候选；
3. 对高置信且无冲突的候选复用既有 `concept_id`；
4. 对不确定候选保留独立身份，禁止自动破坏性合并；
5. 汇总所有活动来源断言形成概念定义和先修边；
6. 检查无来源概念、环路、同名冲突和最低覆盖率；
7. 产生候选 `KnowledgeBundle`，继续复用 M3 现有 schema 与领域校验。

聚合定义必须保留断言清单，不能只存一段模型摘要后丢失原始证据。

第 5 步执行的是不可变来源并集：新抽取结果只增加或确认来源断言；只有文件被教师明确删除时，才从候选 release 排除该文件的旧断言。概念展示可以选择一条主定义，但来源列表必须包含所有去重后的文件与位置。

### 9.5 阶段 E：候选证据索引（M2）

M2 为 `release_id` 创建 staging 分区：

- 词法与向量记录都包含 `course_id/release_id/source_version_id/chunk_id`；
- embedding 缓存按 chunk 内容和 embedding 模型指纹复用；
- staging 索引只能供本批题目标注和内部校验使用，学生检索只过滤 `active_release_id`；
- 删除通过构建新 release 排除旧来源完成，不在旧 release 上原地清空；
- 旧 release 按保留策略回收前仍可支撑历史审计和回滚。

### 9.6 阶段 F：题目解析和重标（M3/M2/M7）

题目标注本身已经是多标签：DeepSeek可从当前概念 ID 白名单返回零到多个知识点。只有新增文件产生了活动版本中不存在的新知识点 ID 时，才对数据库中的全部活动题目重新标注；题目源文件不重新读取。只增加同名知识点来源时，保留题目标签并本地更新其证据。删除知识文件时不调用 DeepSeek，直接删除指向已消失知识点的链接并刷新保留链接的来源。

流程：

1. M3 确定性解析题目 TXT；
2. M2 用题干、选项、答案和解析检索候选概念及其来源证据；
3. M7 在候选概念白名单内做多标签选择；
4. M3 校验链接、置信度、证据和 Q 矩阵约束；
5. 无有效链接的题目标记 `needs_attention`，不用于 DINA/受约束选题，但仍可在教师文件列表中编辑修复。

### 9.7 阶段 G：发布校验（M3）

发布前最少检查：

- 所有活动知识点均有活动文件来源；
- 所有引用的 chunk、文件版本、概念、题目均存在于同一候选 release；
- 不存在引用 retired 概念的 `usable` Q 链接；
- 题目 ID 在课程内唯一，题型和答案结构合法；
- Q 矩阵不存在全零可用题目行；
- 关键概念覆盖率、孤立概念数和低置信链接数进入质量摘要；
- M1 包、M2 索引、M3 知识包和题目目录 checksum 均匹配。

失败时不切换活动指针。对“部分文件失败但候选整体合法”的批次允许 `partial_success` 发布。

## 10. 题目 TXT 规范

文件编码固定为 UTF-8。一个文件可含任意数量题目；每题以 `[QUESTION]` 和 `[/QUESTION]` 包围。首版不允许任意 YAML/Markdown 混合语法，减少教师看不见的解析歧义。

### 10.1 选择题

```text
[QUESTION]
id: net-choice-001
type: choice
stem: TCP 慢启动阶段，拥塞窗口通常如何增长？
option.A: 每个 RTT 约增加 1 MSS
option.B: 每个 RTT 近似翻倍
option.C: 始终保持不变
option.D: 直接降为 0
answer: B
explanation: 每收到一组确认，窗口增加，按 RTT 观察呈近似指数增长。
[/QUESTION]
```

### 10.2 填空题

```text
[QUESTION]
id: net-fill-001
type: fill_blank
stem: TCP 用于估算网络可承载发送量的发送方状态变量称为 ____。
answer: 拥塞窗口|cwnd
explanation: 两种写法均接受。
[/QUESTION]
```

### 10.3 主观题

```text
[QUESTION]
id: net-subjective-001
type: subjective
stem: 比较流量控制与拥塞控制的目标和作用范围。
answer: 流量控制保护接收端；拥塞控制保护网络路径并调节发送速率。
rubric: 应分别说明控制对象、反馈来源和典型机制，共 10 分。
explanation: 可结合接收窗口与拥塞窗口说明。
[/QUESTION]
```

解析器按题隔离错误。某题缺少字段时只把该题标记为失败；同文件其他合法题仍可进入候选目录。教师编辑保存时创建新的题目文件版本，只有再次“确定”才发布。

## 11. 删除与一致性规则

### 11.1 批量删除知识文件

删除不是直接执行 SQL 级联，而是一次变更集：

1. 教师勾选多个活动文件并点击删除；
2. 文件进入 `pending_delete` 并立即从普通教师列表隐藏，等待教师统一确认处理；
3. 候选 release 排除这些 `source_version_id`；
4. M1 从候选包排除对应 chunks；
5. M2 候选索引不包含对应证据；
6. M3 重建概念断言：仍有其他来源的概念保留，来源归零的概念退休；
7. 本地删除指向来源归零知识点的题目链接，并以保留知识点的最新来源刷新其余链接；不调用 DeepSeek；
8. 发布后 `pending_delete` 转为最终 `deleted`，不再进入后续确认任务；学生 RAG 只查新 release。

### 11.2 删除题目文件

候选题目目录排除该文件中的题目及 Q 链接，M8 后续不再选取这些题；已经生成或提交的试卷继续引用其冻结 `question_version`，不被追溯删除。

### 11.3 历史保留

- 活动视图使用新 release，满足用户所说的“删除对应知识点和题目标注”；
- 历史 release、已发布试卷和学习状态使用旧引用，满足审计和可复现；
- 普通教师 UI 不提供回滚按钮，以保持简单；系统管理员可用受控命令 CAS 回切上一个完整 release。

## 12. 学生可溯源 RAG 答疑

### 12.1 页面与交互

新增学生“课程答疑”页面：

- 输入一个课程相关问题；
- 显示检索和生成进度；
- 输出答案正文；
- 每个事实段落带 `[1] [2]` 来源编号；
- 来源卡片显示文件名、页/幻灯片/标题/行号和短摘录；
- 点击来源可打开只读预览并定位到相应位置；
- 可同时显示多个匹配知识点；每个知识点最多取两块原文，总计最多八块，相关例题总计最多两道；
- 课程资料完全覆盖时只根据课程证据回答；部分覆盖时结合课程证据与 DeepSeek 联网检索；完全没有匹配知识点时允许联网回答，但必须醒目警告“无课程知识点支撑，建议核对事实”；
- 联网失败时可以使用模型已有知识给出一般性回答，但必须使用更强的“无课程依据且联网失败”警告，不得伪造来源。

### 12.2 模块链

```text
M0 StudentQARequest
  -> M4 创建 qa TaskPlan 并冻结 active release
  -> M7 从活动知识点白名单选择 0..5 个概念并判断 full/partial/none
  -> M2 按多个概念加载活动原文与最多两道例题
  -> full：M7 只允许使用课程 evidence_id 白名单
  -> partial/none：同一教师密钥通过 deepseek-v4-flash Responses API 执行 web_search
  -> M7 本地校验课程引用、HTTPS 外部来源、覆盖状态和警告
  -> M6 可选生成下一步学习动作
  -> M0 渲染答案与来源卡片
```

这是一种新的“受证据约束答疑”能力，不等同于旧 M7 的确定性补救反馈。旧的“DeepSeek 不生成学生补救反馈”约束继续有效；本方案只为明确的 `student_rag_qa` 任务开放 M7 DeepSeek 适配器。

### 12.3 安全和审计

- 教师配置的密钥由 M0 runtime 密钥存储按课程号、班级号管理，不写数据库业务表、日志或契约；知识抽取、题目标注和学生答疑不回退到全局环境变量；
- 学生问题先经过确定性脱敏和出站复核；该独立答疑链不依赖旧主观评分所需的钉住语义隐私模型；
- 发给模型的上下文只含最少必要 chunks、伪匿名用户标识和课程上下文；
- 模型返回的每个 citation ID 必须来自本次检索结果；
- 不满足引用约束时退化为抽取式证据摘要或“证据不足”；
- 审计保存模型/模板/策略版本、输入输出校验和、证据 ID、延迟和结果状态，不保存密钥；
- 首版不提供持久化自由聊天记忆，页面会话只保留当前浏览器会话所需内容。

## 13. M0 后台作业与就绪性

### 13.1 独立知识入库 Worker

新增命令：

```text
python manage.py run_ingestion_worker
python manage.py run_ingestion_worker --once
```

它与 `run_outbox_worker` 分离：

- outbox Worker 继续负责学习事件投递；
- ingestion Worker 负责耗时文件解析、LLM、embedding 和候选发布；
- 两者使用独立 PID/lease 名称、日志文件和健康状态；
- 重复启动返回“已有活动 Worker，PID/lease/最后心跳为 …”，不得只返回通用 `could not start safely`；
- 每个步骤写 checkpoint，进程重启后继续而非从头重复扣费。

首版复用现有数据库 lease、heartbeat、CAS 和 Worker 运行模式，不引入 Celery。只有当并发课程、水平扩容或跨机器调度成为真实瓶颈时，再实现同一 JobService 接口的 Celery 适配器。

### 13.2 健康检查分层

`/health/ready/` 返回组件矩阵，但 HTTP 状态按请求能力判定：

```json
{
  "status": "degraded",
  "capabilities": {
    "web_auth": "ready",
    "course_runtime": "ready",
    "student_read": "ready",
    "learning_outbox": "ready",
    "knowledge_ingestion": "not_ready"
  }
}
```

- Web、数据库和基础运行时不可用：整体 not ready；
- 仅 ingestion Worker 未心跳：整体 degraded，登录仍可用，教师“确定”按钮禁用或作业保持 queued；
- 仅 DeepSeek 配置无效：该课程的抽取/答疑能力 degraded，其他课程和认证不受影响；
- readiness 中不得泄露密钥、DSN、内部路径或异常堆栈。

## 14. 公共契约与接口

### 14.1 建议新增契约

```text
SourceFileRecord
SourceFileVersionRecord
KnowledgeChangeSet
KnowledgeChangeOperation
IngestionBatchStatus
IngestionFileStatus
KnowledgeExtractionRequest/Result
ConceptAssertionRecord
CourseKnowledgeReleaseRef
QuestionFileRecord
QuestionParseResult
QuestionConceptLinkRecord
StudentQARequest
StudentQAAnswer
StudentQACitation
```

已有 `CoursePackage`、`EvidenceIndexRef`、`KnowledgeBundle`、`ItemCard`、`TaskPlan` 等契约优先以新增可选字段或明确的 v2 契约扩展，不原地改变已发布字段含义。

### 14.2 建议 Web 路由

```text
GET    /teacher/courses/<course_id>/knowledge/
POST   /teacher/courses/<course_id>/knowledge/files/stage/
POST   /teacher/courses/<course_id>/knowledge/files/delete-stage/
POST   /teacher/courses/<course_id>/questions/files/stage/
GET    /teacher/courses/<course_id>/questions/files/<source_file_id>/edit/
POST   /teacher/courses/<course_id>/questions/files/<source_file_id>/edit/
POST   /teacher/courses/<course_id>/knowledge/confirm/
GET    /teacher/courses/<course_id>/knowledge/jobs/<job_id>/

GET    /student/courses/<course_id>/qa/
POST   /student/courses/<course_id>/qa/
GET    /student/courses/<course_id>/sources/<source_version_id>/
```

所有写路由要求 CSRF、RBAC、课程范围校验和幂等键。文件 ID、job ID 和 source version 必须再次绑定 `course_id`，防止越权引用。

### 14.3 错误格式

统一错误 envelope：

```json
{
  "success": false,
  "error": {
    "code": "M1_PPTX_CONTAINER_INVALID",
    "message": "该文件不是有效的 PowerPoint 演示文稿，请重新导出为 .pptx。",
    "recoverable": true,
    "file_id": "src_..."
  }
}
```

建议稳定错误码：

- `M0_INGESTION_WORKER_UNAVAILABLE`
- `M0_INGESTION_JOB_ALREADY_RUNNING`
- `M0_DEEPSEEK_CONFIGURATION_REQUIRED`
- `M1_FILE_TYPE_UNSUPPORTED`
- `M1_FILE_SIGNATURE_MISMATCH`
- `M1_PPTX_CONTAINER_INVALID`
- `LEGACY_PPT_CONVERSION_UNAVAILABLE`
- `LEGACY_PPT_CONVERSION_FAILED`
- `M1_PDF_ENCRYPTED`
- `M1_PARSE_FAILED`
- `M7_EXTRACTION_EMPTY_RESPONSE`
- `M7_EXTRACTION_SCHEMA_INVALID`
- `M7_CITATION_OUT_OF_SCOPE`
- `M3_CONCEPT_WITHOUT_SOURCE`
- `M3_QUESTION_FORMAT_INVALID`
- `M3_Q_MATRIX_QUALITY_GATE_FAILED`
- `M2_STAGING_INDEX_NOT_READY`
- `M3_RELEASE_PUBLISH_CONFLICT`

## 15. M0–M9 变更矩阵

“覆盖”表示新实现替代旧活动实现；“删除”表示从活动代码、路由或服务装配中移除，但 Git 历史和历史快照仍保留。

### M0 平台与运行时

- **保留**：Django、登录与 RBAC、`ActorContext`、运行时容器、AppCoordinator、学习事件 outbox、lease/heartbeat/CAS、日志和健康端点。
- **修改**：readiness 改为按能力分层；认证不再被非关键 ingestion/DeepSeek 状态统一阻断；Worker 重复启动和日志锁错误变成可操作提示。
- **覆盖**：教师首页的“知识包审核”入口改为按课程进入“知识与题目”；知识包页改成多文件暂存、确定、批量删除和作业进度。
- **删除**：`teacher-knowledge-review-lookup`、带 `review_id` 的知识审核页面、相关表单和 flow token；不得删除 M8/M9 成绩复核路由。
- **补充**：入库 JobService/Repository、`run_ingestion_worker`、文件存储、作业状态 API、来源预览、学生答疑页面。

### M1 课程治理与来源解析

- **保留**：ParserRegistry、授权/SHA 校验、确定性解析、稳定 chunk、source locator、PDF OCR 可选边界。
- **修改**：从“导入预期文件集合”改成逐 `SourceFileVersion` 解析；每文件独立结果；CoursePackage 由活动文件集合动态组装。
- **覆盖**：固定 5 文件/seed 目录式生产入口由文件生命周期和 change set 入口替代。
- **删除**：缺任意预期文件就让整个课程导入失败的假设。
- **补充**：Django Storage 对象引用、MIME/魔数/容器校验、文件版本链、PPT 转换适配器、按内容指纹缓存。

### M2 证据检索

- **保留**：`retrieve_with_policy`、lexical/vector/hybrid、pgvector、检索审计、Evidence ID 与来源定位。
- **修改**：所有索引记录增加 `release_id/source_version_id`；学生检索强制过滤活动 release；内部可查询 staging release。
- **覆盖**：只按全局课程索引恢复的方式改成 release 分区与活动指针恢复。
- **删除**：无法按来源文件或快照隔离的索引写入路径。
- **补充**：来源级 upsert/delete、embedding 指纹缓存、候选索引 ready 状态、引用展示元数据。

### M3 知识包、题库与 Q 矩阵

- **保留**：`KnowledgeBundle` 教育语义、概念/先修/误区、题目、量规、蓝图、Q 矩阵验证和题目选择能力。
- **修改**：知识包由活动文件断言和题目文件构造；概念 ID 稳定；发布单位升级为 `CourseKnowledgeRelease`；Q 链接有证据、置信度和版本。
- **覆盖**：`build_knowledge_bundle_after_approval` 生产路径由 `build_candidate_release -> validate_release -> publish_release` 取代。
- **删除**：知识发布用 `TeacherReviewWorkflow`、审核状态机、审核 ID、教师 seed 作为产品入口。旧审核表迁移期只读，不得继续参与运行时。
- **补充**：ConceptRegistry、ConceptAssertion、题目 TXT 解析器、QuestionConceptLink、概念退休/合并、质量摘要和 release 仓库。

### M4 任务编排

- **保留**：任务识别、版本冻结、持久化幂等和既有五类学生任务。
- **修改**：TaskPlan 冻结 `release_id`，由 release 解析 M1/M2/M3 引用；`qa` 明确调用新的学生 RAG 答疑入口。
- **覆盖**：无明确活动 release 的知识任务不得再从 manifest 拼接“最新”引用。
- **删除**：无。
- **补充**：`student_rag_qa` 子类型、证据不足状态、答疑超时/降级结果。

### M5 学生/班级状态

- **保留**：DINA/BKT、误区、追加式状态、final-valid 观测门禁和回放一致性。
- **修改**：每次状态更新记录 Q 矩阵/release 身份；只消费 `usable` 链接；遇到退休概念时保留历史状态但不向新活动诊断继续写入。
- **覆盖**：无。
- **删除**：对“概念集合永不变化”的隐含假设。
- **补充**：概念 merged 映射读取、release 兼容检查和无可用 Q 链接降级。

### M6 教学状态机

- **保留**：S0–S5、确定性规则、shadow/active policy、kill switch、无外部网络调用。
- **修改**：可消费 `StudentQAAnswer` 的证据充分度和概念引用，决定是否给出下一步练习/澄清。
- **覆盖**：无。
- **删除**：无。
- **补充**：证据不足时的确定性教学动作，不在 M6 内新增 LLM。

### M7 DeepSeek 与确定性反馈

- **保留**：DeepSeek 客户端边界、隐私门、引用安全、结构校验、审计、主观评分和原有确定性学生补救反馈。
- **修改**：把“真实 LLM 只用于主观评分”改为显式用途白名单：`subjective_scoring`、`knowledge_extraction`、`question_concept_linking`、`student_rag_qa`。每种用途使用独立 schema、模板、限额和审计。
- **覆盖**：旧文档中“不得把 DeepSeek 用于任何学生反馈”的宽泛表述，收窄为“不得把 DeepSeek 用于无证据的补救反馈”；受证据约束的答疑是新能力。
- **删除**：任何让原始模型文本直接穿透到 M1/M3/M0 的路径。
- **补充**：KnowledgeExtractionAdapter、QuestionConceptLinker、StudentRAGAnswerAdapter、JSON Schema、引用覆盖校验和有限重试。

### M8 测评与评分

- **保留**：出卷、评分、试卷/答卷快照、IRT、在线标定、主观题复核和历史审计。
- **修改**：出卷从活动 release 的题目目录/Q 矩阵读取；试卷继续冻结 `question_version/concept_id/release_id`；低质量链接不参加认知诊断约束。
- **覆盖**：静态知识包就是永久题库的读取方式。
- **删除**：无。
- **补充**：题目删除后的历史解析、Q 链接经验校准输入和 release 兼容门。

### M9 教师分析

- **保留**：班级/学生分析、教学建议、模型质量、成绩复核和受控 DeepSeek 教师解读。
- **修改**：分析结果显示所用 release；活动概念变化不改写历史报表。
- **覆盖**：无。
- **删除**：任何知识包“审批”含义的教师分析入口；成绩复核保留。
- **补充**：入库质量面板，包括失败文件、无来源/退休概念、题目解析失败、无标签题目、低置信 Q 链接、检索与答疑质量趋势。

## 16. 数据库与迁移方案

### 16.1 迁移原则

- 只新增迁移，不修改已经发布的 migration 文件；
- 先双读验证、再切活动指针、最后删除旧运行时代码；
- 迁移前保留旧架构快照和数据库备份；这不等同于保留旧产品流程；
- SQLite 继续用于测试/离线，但 PostgreSQL 是生产权威；
- 任一步校验失败时，旧 release 继续服务，不发布半迁移数据。

### 16.2 建议表归属

| 模块 | 新增或扩展表 |
|---|---|
| M0 | `m0_ingestion_jobs`、`m0_ingestion_job_files`、`m0_ingestion_checkpoints` |
| M1 | `m1_source_files`、`m1_source_versions`、`m1_source_chunks` |
| M2 | 在索引元数据增加 `release_id/source_version_id`，或新增窄映射表；不复制第二套向量库 |
| M3 | `m3_concept_registry`、`m3_concept_assertions`、`m3_question_files`、`m3_question_versions`、`m3_question_concept_links`、`m3_course_knowledge_releases` |

所有跨模块关联在数据库中保存 opaque ID；领域服务仍通过公共契约交换。共享的 PostgreSQL M1/M2/M3 仓库可以在发布事务中验证三类制品并 CAS 活动 release，但业务构造逻辑仍归各模块服务。

### 16.3 旧数据导入

1. 从当前 runtime manifest 恢复已校验的 `CoursePackage/EvidenceIndexRef/KnowledgeBundle`；
2. 为已有原始文件生成 `SourceFile` 和 `SourceFileVersion`；无法定位原文件的旧制品标为 `legacy_generated`，只用于迁移，不伪造文件来源；
3. 将旧 bundle 概念和题目导入 registry/catalog；有现成 source locator 的建立断言，无来源的列入迁移报告；
4. 构建 `legacy-release-v1` 并做与旧 runtime 的 checksum/数量对账；
5. 切换课程 `active_release_id`；
6. 新 UI 上线后移除知识审核路由、服务装配和菜单；
7. `m3_teacher_reviews` 暂时只读，确认无需审计后再另做删除迁移。

不应为了迁移通过而为无来源概念编造证据。无来源旧概念可暂时只服务已冻结历史任务，新活动 release 必须由真实来源重新解析。

### 16.4 回滚

- 代码发布回滚：新表是追加式，不阻止旧二进制读取旧数据；
- 数据发布回滚：系统管理员 CAS 把 `active_release_id` 指回上一个 `active/superseded` 且校验完整的 release；
- 作业回滚：未发布 staging 制品可标记 failed 后按保留期清理；
- 不使用“恢复知识审核流程”作为回滚方法。

## 17. 测试与验收门槛

### 17.1 单元测试

- 每种文件签名、容器、编码、大小和路径安全校验；
- M1 每种 parser 的 locator 稳定性；
- 内容指纹、幂等键和 cache hit/miss；
- DeepSeek 空响应、截断、未知 ID、span 越界和 Schema 错误；
- 概念复用、重名、别名、合并、退休、先修环；
- 三类题目语法、逐题错误隔离和唯一 ID；
- Q 链接白名单、置信度和质量门；
- release checksum 与 CAS 冲突；
- 引用 ID 到文件位置的完整链。

### 17.2 集成测试

至少覆盖：

1. 只上传 1 个 Markdown 并发布；
2. 同批上传 MD/PDF/PPT/PPTX/TXT；
3. 上传任意 N 个文件，不存在固定 5 文件假设；
4. 其中 1 个伪造 PPTX 或损坏 PPT 转换失败，其他文件发布为 `partial_success`；
5. 相同文件重复确定不产生重复 chunks/概念/向量；
6. 一次删除多个知识文件，索引、断言、概念和 Q 链接同步变化；
7. 删除一种来源但概念仍有另一来源时概念保留；
8. 删除最后来源后概念退休且活动题目标注移除；
9. 新增全新知识点后，使用数据库中的活动题目内容完成重标，不重新读取题目文件；
10. 只新增已有知识点的来源时，保留题目标签并在本地刷新来源；
11. 删除题目文件后新试卷不再选取，旧试卷仍可查看/评分；
12. ingestion Worker 中途退出，重启后从 checkpoint 恢复；
13. 重复启动 Worker 返回清楚错误，Web 登录仍成功；
14. API 未配置只阻止解析/答疑，不返回全站 503；
15. PostgreSQL/pgvector staging 与 active release 隔离。

### 17.3 E2E 人工验收

教师：

- 配置 API -> 拖入混合文件 -> 提交 -> 确定 -> 看进度 -> 查看知识点来源；
- 上传含三类题目的 TXT -> 查看成功/失败题目 -> 编辑 -> 再确定；
- 勾选多个文件 -> 删除 -> 确定 -> 验证知识点和题目标注变化；
- 上传一个真实旧版 PPT，确认引用保留原文件名和 slide 定位；再故意上传损坏 PPTX/伪造 PPT，确认只影响该文件且错误可理解。

学生：

- 对每种知识文件至少问一个可回答问题，答案引用正确文件和具体位置；
- 问一个知识包没有的问题，必须得到证据不足答复；
- 删除来源后重复提问，不得再引用已删除文件；
- 完成练习、订正和测评，确认 M4–M9 原有功能仍可工作。

### 17.4 RAG/Q 矩阵质量验收

建立一套不调用真实生产数据的课程金标集，至少含：

- 30 个问题及其相关 chunk IDs；
- 20 个应回答问题、10 个应拒答问题；
- 30 道题目及人工确认的概念标签；
- 一组概念新增、删除、同义词和多来源案例。

最低建议门槛（首次基线测量后可调整）：

- source locator 可解析率：100%；
- 活动概念有来源覆盖率：100%；
- 引用 ID 合法率：100%；
- 已删除来源被活动 RAG 检索到：0；
- 应拒答问题的无依据强答率：0；
- 检索 Recall@5：不低于 0.90；
- 人工核验的答案忠实率：不低于 0.90；
- 题目概念 micro-F1：不低于 0.85；
- 可用于 M5/M8 的题目 Q 链接覆盖率：不低于项目计划书规定的课程验收阈值。

自动指标只作回归信号，最终仍抽样人工核查引用是否真的支持答案。

### 17.5 回归要求

- 全套现有单元、集成和 E2E 测试必须通过；
- 新代码总体覆盖率不低于 80%，关键删除/发布/权限路径要求分支覆盖；
- M5 DINA/BKT、M6 FSM、M8 出卷评分/IRT、M9 成绩复核分别增加 release 兼容回归；
- 真实 DeepSeek 调用不进入默认自动测试，使用假传输覆盖协议、重试和校验；
- PostgreSQL live test 继续使用带 `test/ci/tmp` 标记的专用数据库并 fail closed。

## 18. 分阶段实施顺序

### 阶段 0：冻结历史与契约设计

- 完成本历史快照和本方案；
- 列出现有知识审核路由、服务、契约和测试引用；
- 定义新契约与数据库迁移，不改 UI 行为。

完成条件：契约能表达任意文件、逐文件失败、活动 release、来源链和题目标注。

### 阶段 1：M0/M1 文件生命周期与作业骨架

- 新表、Storage、安全校验、暂存/删除/确定 API；
- `run_ingestion_worker`、checkpoint、进度和分层 readiness；
- 先用假 extractor 贯通空候选发布。

完成条件：混合文件可逐个解析；损坏文件不拖垮整批；Worker 可恢复。

### 阶段 2：M7 抽取与 M3 来源化知识点

- DeepSeek Schema、提取适配器和测试假传输；
- ConceptRegistry/Assertion、概念规范化和质量门；
- M2 staging 索引和 release 发布。

完成条件：每个活动概念可点击回到原文件位置；删除来源后无孤儿。

### 阶段 3：题目文件与动态 Q 矩阵

- 固定 TXT parser/编辑器；
- M2 候选检索、M7 多标签链接、M3 验证；
- M5/M8 消费活动/冻结 Q 矩阵。

完成条件：三类题目可用；只有新增全新知识点时才重标数据库中的活动题目，删除与同名来源合并仅做本地链接刷新；历史试卷稳定。

### 阶段 4：学生 RAG 答疑

- 学生页面、M4 路由、M2 检索、M7 引用约束生成；
- 来源预览和证据不足降级；
- RAG 金标评测。

完成条件：所有显示引用都能打开到文件位置，无证据问题不强答。

### 阶段 5：覆盖旧知识审核实现

- 教师首页和课程导航切换到新页面；
- 移除 M3 知识审核路由、表单、flow token、coordinator wrappers 和活动服务装配；
- 保留 M8/M9 成绩复核；
- 迁移现有课程并切活动 release。

完成条件：产品代码不再要求知识审核 ID；旧 URL 返回明确的迁移说明或 410，而不是神秘 404；没有活动代码依赖审核状态机。

### 阶段 6：全模块验收与文档更新

- 执行 M0–M9 自动回归和五天人工验收数据集；
- 更新 `docs/architecture.md`、部署、Worker、接口指南和各模块 README；
- 清理旧 runtime seed 入口和只为旧 UI 存在的代码；
- 复核权威 Conda 环境，不增加未使用的大型依赖。

完成条件：新文档与运行行为一致，演示可从空课程完整走通。

## 19. 关键风险与控制

| 风险 | 控制 |
|---|---|
| LLM 编造知识点或来源 | chunk 白名单、span 精确校验、概念必须有断言、发布门禁 |
| 多文件部分成功造成认知混乱 | 页面明确失败/保留旧版；release 质量摘要；原子发布 |
| 删除破坏历史试卷 | 活动 release 与冻结试卷引用分离，禁止历史级联删除 |
| 每次全量重标题目成本升高 | 首版保证正确；缓存 unchanged 题目输入，后续再引入影响集优化 |
| OCR/旧 PPT 扩大依赖 | PowerPoint 转换器只在 ingestion Worker 内注入、按文件降级，不作为 Web 启动依赖 |
| Worker 重复执行和 LLM 重复扣费 | change set/步骤指纹、checkpoint、CAS lease、结果缓存 |
| 文件上传攻击 | 隔离存储、随机对象键、签名/容器检查、限额、解压保护、RBAC |
| API 不可用导致全站不可用 | 能力级 readiness，旧 release 继续服务 |
| 自动 Q 矩阵质量不足 | 置信度/证据、`needs_attention`、完备性门、M8 经验数据后续校准 |
| 大型框架破坏单一环境 | 不把 GraphRAG/Celery/Unstructured 设为首版必需依赖，先复用现有边界 |

## 20. 最终架构决策

1. **M0–M9 保持不变，不新增模块。**
2. **以 `CourseKnowledgeRelease` 作为课程知识的一致性和发布单位。**
3. **教师“确定”取代知识包审批；M8/M9 成绩复核不受影响。**
4. **M1 做确定性解析，M7 做受控智能抽取/标注/答疑，M3 做教育领域校验与发布，M2 做证据索引。**
5. **已有活动版本时按来源增量计算：新增只解析新文件，删除只移除来源；发布物仍是完整不可变快照。**
6. **删除通过新 release 排除来源并本地更新概念和 Q 链接，不调用 LLM，也不直接破坏历史数据。**
7. **学生答案优先引用活动 release 的具体文件位置；课程依据不足时允许联网或模型常识降级，但必须区分来源并显示事实核对警告。**
8. **知识入库使用独立 M0 Worker，复用现有可靠性模式，不复用学习事件 outbox 的业务语义。**
9. **首版不引入 GraphRAG、Celery、独立向量库或完整 Unstructured 依赖。**
10. **新方案覆盖旧知识审核实现；旧实现只存在于 Git 历史、数据库迁移期只读数据和历史快照中。**

## 21. 参考资料与仓库

### 论文与标准

- Lewis et al., [Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks](https://proceedings.neurips.cc/paper_files/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html), NeurIPS 2020.
- W3C, [PROV-O: The PROV Ontology](https://www.w3.org/TR/prov-o/).
- Es et al., [RAGAS: Automated Evaluation of Retrieval Augmented Generation](https://aclanthology.org/2024.eacl-demo.16.pdf), EACL 2024 Demo.
- Li et al., [Automate Knowledge Concept Tagging on Math Questions with LLMs](https://arxiv.org/abs/2403.17281), 2024.
- Li et al., [Knowledge Tagging System on Math Questions via LLMs with Flexible Demonstration Retriever](https://arxiv.org/abs/2406.13885), 2024.
- Köhn & Chiu, [How to Build a Complete Q-Matrix for a Cognitively Diagnostic Test](https://doi.org/10.1007/s00357-018-9255-0), Journal of Classification, 2018.
- Li & Suen, [Constructing and Validating a Q-Matrix for Cognitive Diagnostic Analyses of a Reading Test](https://eric.ed.gov/?id=EJ994673), Educational Assessment, 2013.
- Jang, [Demystifying a Q-Matrix for Making Diagnostic Inferences about L2 Reading Skills](https://eric.ed.gov/?id=EJ866998), Language Assessment Quarterly, 2009.

### 官方文档与 GitHub 仓库

- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode) 与 [Responses API JSON Schema](https://api-docs.deepseek.com/api/create-response/).
- [pgvector](https://github.com/pgvector/pgvector).
- [LlamaIndex](https://github.com/run-llama/llama_index) 及其 [ingestion pipeline](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/ingestion/pipeline.py).
- [Unstructured](https://github.com/Unstructured-IO/unstructured)、[PPTX parser](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pptx.py) 与 [Unstructured API](https://github.com/Unstructured-IO/unstructured-api).
- [Microsoft GraphRAG](https://github.com/microsoft/graphrag) 与 [default dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md).
- [Celery task guide](https://github.com/celery/celery/blob/main/docs/userguide/tasks.rst).
- [Django file management](https://docs.djangoproject.com/en/5.2/topics/files/).
- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html).
