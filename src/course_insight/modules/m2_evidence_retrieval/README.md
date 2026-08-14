# M2 课程证据 RAG

## 负责人

谢

## 职责

从 `CoursePackage` 建立可定位证据索引，按 `EvidenceQuery` 执行 RAG
检索，返回带来源的证据与检索审计。SQLite 提供离线/测试 lexical 基线；生产
PostgreSQL+pgvector 已支持 embedding、lexical/vector/hybrid 检索、两阶段索引发布
和审计。仓库已提供真实 HTTP embedding + PostgreSQL/pgvector + M1—M3 恢复链路的
live 用例；本机缺少受保护测试数据库时仍会明确 skip，只有 CI live job 实际通过后
才能记录为生产验收通过。

## 输入来源

- M1 `CoursePackage`。
- M6/M8 产生的 `EvidenceQuery`。
- `RetrievalPolicy`：词法、向量或混合检索的版本化策略。
- `EmbeddingModelRef`：embedding 供应商、模型版本和维度的逻辑引用。

## 输出

- `EvidenceIndexRef`：使用 `storage_ref`、`backend` 和 `embedding_model_id`，不暴露主机路径。
- `EvidenceBundle`：供 M7 评分/反馈使用的定位证据。
- `RetrievalAudit`：只记录查询、索引、策略和证据 ID，不记录原始查询文本。

`build_vector_index(...)` 与 `restore_vector_index(...)` 是生产向量索引的构建/恢复入口。
`retrieve_with_policy(...)` 是正式业务检索入口，支持 lexical、vector、hybrid 并写入
脱敏审计。`initialize_vector_store(...)` 与 `empty_retrieval_audit(...)` 仅保留给
离线/兼容或未执行场景；SQLite 可以返回逻辑 `empty`，生产缺少 provider、pgvector
或审计仓储时必须 fail closed。应用层通过
`retrieve_for_application(...) -> retrieve_with_policy(...)` 进入该边界；旧
`retrieve(...)` 仅兼容已有 lexical 调用，新业务不得用它替代
`retrieve_with_policy(...)`。

## 持久化与 D3 边界

离线/测试模式下默认使用共享 `SQLiteM1M2M3Repository`；需要文件导出或显式文件后端时，
`FileM2Repository` 将完整、不可变、带 checksum 的 lexical 制品写入
`runtime/artifacts/`，制品必须包含 `EvidenceIndexRef` 和完整 lexical snapshot，即
`documents` 与 `postings`。生产模式下，`PostgresM1M2M3Repository` 使用 0014/0015 migrations
持久化 M2 制品、`m2_vector_indexes`、`m2_vector_documents` 和
`m2_retrieval_audits`；PostgreSQL+pgvector 是生产权威后端，SQLite 不是生产后端。
备份、恢复、checksum 校验、清理和回滚必须覆盖所选后端的权威数据。

## 禁止事项

不得伪造证据、改变查询 ID、跨课程检索、将数据库连接/主机路径放进契约、
在数据库未配置时伪报 `ready`，或让向量实现穿越 M2 边界。

## S1-S3、S2 当前实现

`EmbeddingProvider` 是可替换端口；生产适配器使用 OpenAI-compatible
`/v1/embeddings`，校验批次顺序、模型名、有限数值和精确维度，并执行有上限的重试。
deterministic provider 仅允许测试构造器。向量索引按 staging -> ready 两阶段发布，
写入前校验每个 chunk，ready 版本不可覆盖；pgvector migration 同时约束索引维度与
向量维度。

`RetrievalPolicy.strategy` 支持 lexical、vector、hybrid。hybrid 使用固定权重和稳定
evidence-id tie-break；vector/hybrid 必须有已 ready 的 provider 与 vector store。正式
检索通过 `retrieve_with_policy`，三种策略统一执行必选证据、最低相关度和补充 `top_k`
规则；成功、无结果和已取得合法索引身份的失败都会写入脱敏审计（查询 checksum、索引/
策略/模型身份、分数和单调时钟耗时），持久化失败会使成功调用失败，但不会覆盖原始失败
业务错误。重新进程可用 `restore_index`
或在提供并校验 `EvidenceIndexRef` 后调用 `restore_vector_index`，不重建已有向量；
自动发现 ready 向量索引已通过 durable metadata 恢复路径实现。真实 PostgreSQL+pgvector
联调用例位于 `tests/integration/test_postgres_m1_m2_m3_live.py`，由 CI 的
`live-m1-m3` job 执行；本机没有测试数据库时不把 skip 记为通过。

应用组合根通过 `COURSE_INSIGHT_RETRIEVAL__POLICY_ID`、`__STRATEGY`、`__TOP_K`、
`__LEXICAL_WEIGHT`、`__VECTOR_WEIGHT` 和 `__RERANK` 注入同一个受治理策略；
`retrieve_for_application(...)` 会把该策略原样传给 M2 和审计，不再把业务入口写死为 lexical。
