# M1 课程治理

## 负责人

谢

## 职责

校验教师授权和文件 SHA-256，解析课程文本，以确定性规则分块，并冻结
来源、课程包版本和分块校验和。M1 只负责产生可重现课程输入，不实现
RAG、知识模型或 LLM。

## 输入来源

- 调用方提供的授权 Markdown 课程原文和授权 CSV。
- 调用方提供的 metadata JSON，其课程 ID、来源版本、时间和校验和必须完整。
- 可替换 parser 适配器；边界始终输出同一契约。

## 输出

`CoursePackage`，其中 `SourceDocument`、`SourceAuthorization`和 `ContentChunk`
都带稳定标识、版本与来源定位。契约原样交给 M2 建 RAG 索引和 M3
建知识包。

## 持久化与 D3 边界

离线/导出模式下，`FileM1Repository` 将完整课程导入制品写入
`runtime/artifacts/`；完整制品包括 `CoursePackage`、解析结果、课程 metadata、
授权输入和来源 payload，不能只保存一个课程包 JSON。默认 SQLite 组合根使用共享的
`SQLiteM1M2M3Repository` 保存同一组完整制品；生产环境必须使用共享的
`PostgresM1M2M3Repository`，并运行 `0016_m1_m2_m3_capabilities.sql` 与
`0017_vector_index_metadata.sql`。

`m1_course_packages`、`m2_evidence_indexes`、`m3_knowledge_bundles` 是 SQLite
离线/测试 schema；生产 PostgreSQL 使用 `m1_m2_m3_artifacts` 及 M2/M3 专用表。
M1 不单独维护第二套业务真相，也不把一次导入隐式双写到互不一致的后端。备份、恢复、
checksum 校验、清理和回滚必须覆盖当前选定权威后端；runtime 文件导出则必须覆盖完整
artifact 目录及 manifest，恢复前后都要验证身份、版本和 checksum，不能只恢复单个 payload。

## 禁止事项

不得导入未授权来源、静默忽略解析失败、使用不可重现分块、调用网络、
将主机路径写入契约，或读取其他模块业务表。

## S6 ParserRegistry

M1 的默认解析器通过 `ParserRegistry` 绑定 `.md`、`.txt`、`.pdf`、`.docx` 和
`.pptx`。扩展名大小写不敏感；每个 `ParserEntry` 固定 parser id、版本、媒体类型、
能力标签和最大输入字节数，并在调用适配器前完成边界校验。`.ppt` 明确拒绝。
导入快照只保存这些安全元数据，不保存主机路径或解析器原始输出；自定义解析器可通过
同一协议注入，不能绕过 M1 的授权、哈希和字节上限。

PDF 先读取文本层。扫描 PDF 没有文本层时会明确返回
`COURSE_PDF_OCR_REQUIRED`，不会把空文本当成导入成功；部署方可通过
`ServiceOverrides.ocr_provider` 注入受治理的 OCR 适配器，或启用内置的
Tesseract 适配器。启用内置适配器前安装可选依赖和 Tesseract（含 `chi_sim`、
`eng` 语言数据）：

```powershell
python -m pip install -e ".[ocr]"
$env:COURSE_INSIGHT_OCR__BACKEND = "tesseract"
$env:COURSE_INSIGHT_OCR__EXECUTABLE = "C:\Program Files\Tesseract-OCR\tesseract.exe"
$env:COURSE_INSIGHT_OCR__LANGUAGE = "chi_sim+eng"
```

`COURSE_INSIGHT_OCR__DPI`（默认 200）、`COURSE_INSIGHT_OCR__TIMEOUT_SECONDS`
（默认 30）和 `COURSE_INSIGHT_OCR__MAX_OUTPUT_BYTES` 可按部署资源调整。默认
`backend=disabled`，未配置 OCR 时不会悄悄调用外部进程；OCR 结果仍受页数、
单页和总文本字节上限约束。
