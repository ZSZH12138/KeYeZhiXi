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
evidence-id tie-break，权重必须严格归一到 1；策略 `top_k` 会限制查询补充证据数量。
`rerank=true` 只有在应用组合根显式注入确定性的 `ServiceOverrides.reranker` 后才可用，
缺少该端口时正式检索会返回稳定的可恢复错误，不会静默忽略配置；vector/hybrid 必须有已
ready 的 provider 与 vector store。正式
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

## PostgreSQL+pgvector 性能验收

仓库提供可导入核心模块
`course_insight.modules.m2_evidence_retrieval.performance` 和 CLI
`python -m scripts.benchmark_m2_pgvector`。CLI 只读取
`COURSE_INSIGHT_TEST_DATABASE_URL` 与匹配的
`COURSE_INSIGHT_TEST_DATABASE_NAME`，并要求数据库名含有边界分隔的
`test`、`ci` 或 `tmp`。guard 配置缺失、名称不匹配、保留库名或 DSN 格式不安全时输出
`status=blocked`、exit 2；guard 通过后，PostgreSQL 连接、migration、pgvector、benchmark
或 cleanup 运行失败时输出 `status=failed`、exit 1。绝不会把 skip 当通过，也不接受
生产 `DATABASE_URL`。

性能工具依赖受治理的可选 extra：
`python -m pip install -e ".[performance]"`（`numpy>=2,<3`）。缺少或版本不满足的
NumPy 会安全返回 `status=blocked`，不会伪造性能通过。`--batch-size` 默认使用生产
批量写入上限；`chunk_count * query_count * dimension` 也有独立工作量上限。目标示例
`50,000 * 100 * 1,536 = 7.68e9` 在允许范围内，极端组合会在连接数据库前被拒绝。

基准使用现有 migration、pool 和 `PostgresPgVectorStore`，生成由 `seed` 决定的合成
向量/查询（先量化为 NumPy `float32`），建立一个唯一的 staging/ready benchmark index，
通过生产 `add_many` 受限批量写入，记录数据装载与发布的 `build_ms`、重复 exact 查询的
`p50_ms`/`p95_ms`，用分块矩阵计算和独立 Top-K 合并计算 `recall_at_k`，并记录
`relation_bytes`、已有 `index_bytes` 与完整的
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。工具不会创建 HNSW/IVFFlat；
`index_bytes.existing` 是表上已有 schema 索引，
`benchmark_ann_index_bytes` 始终为 `null`，因此不能误读为 ANN 索引大小。

上线前应在接近真实目标规模的 disposable 数据库中显式运行目标命令，替换为实际
chunk 数、embedding 维度和门禁阈值：

```powershell
$targetChunkCount = 50000
$targetDimension = 1536
$targetQueryCount = 100
$targetTopK = 10
$targetMaxP95Ms = 250
$targetMinRecallAtK = 1.0
$targetSeed = 20260815
$targetBatchSize = 1024
python -m scripts.benchmark_m2_pgvector `
  --chunk-count $targetChunkCount `
  --dimension $targetDimension `
  --query-count $targetQueryCount `
  --top-k $targetTopK `
  --max-p95-ms $targetMaxP95Ms `
  --min-recall-at-k $targetMinRecallAtK `
  --seed $targetSeed `
  --batch-size $targetBatchSize
```

只有 JSON 中 `passed=true` 且进程为零退出，才能把该目标规模记录为 exact 搜索
性能通过；必须保存 stdout JSON/log 中的规模（含 `batch_size` 和 `workload_units`）、
构建时间、P50/P95、召回率、关系/索引体积和完整 explain。CI 的 `live-m1-m3` job 另跑
64 chunks、8 维、12 queries 的
有界 smoke；smoke 通过只证明受保护的 live 链路可执行，不替代上面的目标规模验收。
`VectorStore`、`InMemoryVectorStore`、`PgVectorStore` 和
`PostgresPgVectorStore` 都使用受限 `add_many`；每个批次只使用一个 checkout/transaction，
通过一次 cursor `executemany` 传输，校验失败原子回滚。每次运行 cleanup 会在同一事务中
重新校验 `current_database()` 与 guard target，失败时 DELETE 次数为 0；只删除本次生成的
benchmark index 行和向量行，不删除 schema 或其他数据。cleanup 失败会出现在
`failure_reasons` 并使进程非零。
