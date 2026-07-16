# M2 课程证据 RAG

## 负责人

谢

## 职责

从 `CoursePackage` 建立可定位证据索引，按 `EvidenceQuery` 执行 RAG
检索，返回带来源的证据与检索审计。当前保留词法检索基线；目标向量
存储是 PostgreSQL/pgvector，其逻辑引用、embedding 版本、检索策略和审计已
由契约固定。当前 pgvector 仅空实现，不安装、不连接、不生成 embedding。

## 输入来源

- M1 `CoursePackage`。
- M6/M8 产生的 `EvidenceQuery`。
- `RetrievalPolicy`：词法、向量或混合检索的版本化策略。
- `EmbeddingModelRef`：embedding 供应商、模型版本和维度的逻辑引用。

## 输出

- `EvidenceIndexRef`：使用 `storage_ref`、`backend` 和 `embedding_model_id`，不暴露主机路径。
- `EvidenceBundle`：供 M7 评分/反馈使用的定位证据。
- `RetrievalAudit`：只记录查询、索引、策略和证据 ID，不记录原始查询文本。

新入口 `initialize_vector_store(...)` 当前返回 `backend=pgvector`、
`status=empty` 的逻辑引用；`empty_retrieval_audit(...)` 返回无证据 ID 的空审计。

## 禁止事项

不得伪造证据、改变查询 ID、跨课程检索、将数据库连接/主机路径放进契约、
在数据库未配置时伪报 `ready`，或让向量实现穿越 M2 边界。
