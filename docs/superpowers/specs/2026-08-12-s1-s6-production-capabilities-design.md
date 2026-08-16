# S1-S6 Production Capabilities Design

**Goal:** 将 S1-S6 从空边界推进为可部署、可观测、可恢复且可迁移的正式能力，同时保持模块边界清晰、生产失败可识别、实现便于替换。

**Architecture:** 采用端口/适配器架构。M1 负责版本化解析输入，M2 通过 `EmbeddingProvider`、索引仓储和检索策略端口提供 lexical/vector/hybrid 检索，S2 将检索输入、策略、模型、索引和 evidence 绑定为不可变审计；M3 继续是教师批准后的发布门禁。M1-M3 的业务对象由配置选择一个权威持久化后端，SQLite 与 PostgreSQL 实现同一 repository protocol，完整 artifact 作为可迁移、可校验的导出/备份格式，不双写。

**Tech Stack:** Python 3.11+, Pydantic v2, existing module/repository protocols, SQLite, PostgreSQL + pgvector, OpenAI-compatible `/v1/embeddings` HTTP, pytest, deterministic local test provider.

---

## 1. Capability decisions

### S1: Embedding and vector storage

- Add a private `EmbeddingProvider` protocol with `model_ref`, `embed_documents`, and `embed_query` operations.
- Implement an OpenAI-compatible HTTP adapter using the Python standard library HTTP client so the core package does not bind to a vendor SDK.
- Provider configuration contains endpoint, environment-key name, model name/version, dimension, timeout, bounded retries, and TLS policy. Secrets never enter contracts, logs, artifacts, or audit payloads.
- Every generated vector is validated for finite values and exact dimension. Provider/model/version/dimension are bound to the index identity and checksum.
- PostgreSQL vector persistence uses pgvector with a versioned migration and an index-rebuild path. Missing extension, unavailable provider, dimension mismatch, timeout exhaustion, or partial batch failure fails the build and never publishes a ready index.
- Deterministic embedding is test-only and cannot be selected by production configuration.

### S2: Retrieval audit

- Extend the existing `RetrievalAudit` contract only with backward-compatible optional metadata needed for formal audit: query checksum, index version, policy version, embedding model identity, retrieved scores, and latency/status metadata.
- Audit records are created for lexical, vector, hybrid, failed, and no-result retrievals. Raw query text, credentials, host paths, and full document contents are excluded.
- Audit identity is deterministic from query semantics, index identity, policy identity, model identity, and request identity. Replays are idempotent; conflicting same-identity audit writes fail closed.
- Audit persistence uses the selected M2 repository and is included in backup/export manifests.

### S3: Retrieval strategies

- Keep lexical ranking as a first-class strategy.
- Add vector ranking over pgvector and a hybrid combiner with explicit normalized scores, weights, top-k, stable tie-breaking, and optional deterministic rerank.
- Policy validation rejects zero signal weight, invalid weight sums, unavailable strategy dependencies, and model/index mismatches.
- SQLite and local tests support lexical and deterministic vector protocol tests; production vector/hybrid requires PostgreSQL + pgvector and a configured provider.

### S4: Teacher confirmation workflow

- Add a private, versioned M3 workflow state machine: `draft -> submitted -> approved|rejected -> recalled`.
- Teacher submission is immutable and stores reviewer pseudonym, action time, input checksum, validation report reference, reason, and expected version. Compare-and-swap prevents stale approval/rejection.
- Only `approved` input can call the existing M3 publication service. No automatic extraction or LLM-generated knowledge becomes authoritative.
- Existing teacher review and M3 publication remain separate bounded contexts; the workflow orchestrates them through public service methods and does not read private repositories across modules.

### S5: Database repositories and migration

- Implement M1/M2/M3 database adapters for SQLite and PostgreSQL behind existing repository protocols.
- Persist complete logical payloads and checksums: M1 import inputs and package; M2 lexical/vector index metadata, documents/postings or vector references, and embedding identity; M3 bundle, seed snapshot, validation report, and workflow records.
- Select exactly one authoritative backend per application instance. Runtime artifact export/import is the migration boundary; migration uses validate-then-publish, idempotency, checksum comparison, and rollback on any failure.
- Add versioned SQLite/PostgreSQL migrations only for the approved S1-S6 tables. Existing M1-M3 runtime artifact behavior remains available as an explicit file backend for offline/export deployments.
- No implicit dual write. Backend health checks fail startup/readiness when the configured authoritative backend is unavailable.

### S6: Parser registry

- Replace the static extension map with a `ParserProtocol` and registry entries containing normalized extension, media type, parser version, capability flags, maximum input bytes, and bounded parse operation.
- Keep MD/TXT/PDF/DOCX/PPTX adapters and the stable `.ppt` rejection. Each adapter emits the existing governed source/chunk semantics: normalized text, locator, stable chunk ID, source checksum, and deterministic ordering.
- Parser errors map to stable M1 error categories. Registry selection is injectable, testable, and independent of the application factory.
- Parser versions and capability metadata are recorded in import metadata so re-import and migration can detect incompatible parser changes.

## 2. Cross-cutting production requirements

- No `empty` success result for configured production S1-S4 paths. Unconfigured optional functionality must fail with a stable recoverable error at startup or operation boundary.
- Every persisted object is canonical, checksummed, versioned, idempotent, and restorable in a fresh process.
- Public contract root count remains stable; additions use backward-compatible optional fields or private persistence models. Existing evidence/chunk IDs and M1-M3 identity rules remain unchanged.
- Secrets, raw query text, raw course payloads, host paths, and provider response bodies are excluded from logs and public errors.
- Feature availability is explicit in settings and health/readiness checks; tests cover enabled, disabled, dependency-unavailable, timeout, version mismatch, retry, replay, and rollback cases.

## 3. Delivery order

1. S6 parser protocol and parser metadata, because S5 and all downstream indexes depend on deterministic parsed output.
2. S5 repository protocol parity, schema migrations, backend selection, export/import and rollback.
3. S1 provider protocol, real HTTP adapter, model identity and pgvector index persistence.
4. S3 lexical/vector/hybrid policy and ranking.
5. S2 complete audit chain and replay/idempotency.
6. S4 teacher workflow and M3 publish integration.
7. Full M1→M2→M3→retrieval→audit→teacher approval→publish→fresh-process restore acceptance.

## 4. Acceptance gates

- Unit tests use real deterministic provider/parser adapters and cover failure semantics.
- PostgreSQL integration tests run against a disposable pgvector-enabled database; they are not claimed as passed without the environment.
- HTTP provider live tests run against a disposable OpenAI-compatible endpoint; secrets are supplied only through environment variables.
- Migration tests verify SQLite/PostgreSQL repository parity, artifact export/import, checksum conflict, rollback, and fresh-process restore.
- Final gate requires full pytest, contract/schema verification, static diff check, and a production configuration test proving no S1-S4 path can silently return `empty`.
