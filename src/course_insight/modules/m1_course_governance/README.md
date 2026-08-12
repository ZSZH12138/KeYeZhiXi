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

当前默认 `FileM1Repository` 将完整课程导入制品写入 `runtime/artifacts/`，作为
M1 的权威持久化；它不是 SQLite/PostgreSQL 业务表。完整制品包括 `CoursePackage`、
解析结果、课程 metadata、授权输入和来源 payload，不能只保存一个课程包 JSON。

`m1_course_packages` 仅在 SQLite 中已建，当前 PostgreSQL migration 不创建它；它是
不接收 M1 业务写入的预留表。本 MVP 不为 M1 新增 migration，也不把同一导入双写到 SQL 表和 runtime artifact。备份、恢复、
checksum 校验、清理和回滚必须覆盖 `runtime/artifacts/` 中完整的 M1 artifact
目录及 manifest；恢复前后都要验证身份、版本和 checksum，不能只恢复单个 payload。

## 禁止事项

不得导入未授权来源、静默忽略解析失败、使用不可重现分块、调用网络、
将主机路径写入契约，或读取其他模块业务表。

## S6 ParserRegistry

M1 的默认解析器通过 `ParserRegistry` 绑定 `.md`、`.txt`、`.pdf`、`.docx` 和
`.pptx`。扩展名大小写不敏感；每个 `ParserEntry` 固定 parser id、版本、媒体类型、
能力标签和最大输入字节数，并在调用适配器前完成边界校验。`.ppt` 明确拒绝。
导入快照只保存这些安全元数据，不保存主机路径或解析器原始输出；自定义解析器可通过
同一协议注入，不能绕过 M1 的授权、哈希和字节上限。
