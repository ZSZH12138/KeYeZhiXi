# S1-S6 Production Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 S1-S6 从能力边界升级为可上线、可恢复、可迁移、可替换并且在生产依赖不可用时明确失败的完整链路。

**Architecture:** 以现有 M1/M2/M3 公共服务和 repository protocol 为边界，新增 Parser、Embedding、VectorStore、AuditStore、TeacherReview 和 BackendSelection 私有端口。SQLite 是离线/测试适配器，PostgreSQL+pgvector 是生产适配器；每个实例只选择一个权威后端，artifact export/import 是迁移边界，所有发布动作采用校验后提交和幂等校验。

**Tech Stack:** Python 3.11+, Pydantic v2, stdlib `urllib`, SQLite, psycopg, PostgreSQL+pgvector, pytest, 当前 M1/M2/M3 解析和 artifact 实现。

---

## File map

- Create `src/course_insight/modules/m1_course_governance/parser_protocol.py` for parser ports, registry entries, bounded dispatch and parser metadata.
- Modify `src/course_insight/modules/m1_course_governance/parsers.py` only to expose the current adapters through the protocol without changing normalized block semantics.
- Modify `src/course_insight/modules/m1_course_governance/service.py` to persist parser identity/version in the existing import metadata.
- Create `src/course_insight/application/persistence.py` for authoritative backend selection, readiness and explicit migration boundary.
- Create `src/course_insight/infrastructure/sqlite/m1_m2_m3_repository.py` for one SQLite transaction-backed repository facade.
- Create `src/course_insight/infrastructure/postgresql/m1_m2_m3_repository.py` for the PostgreSQL counterpart using the existing pool protocol.
- Create `src/course_insight/infrastructure/sqlite/migrations_s1_s6.py` and `src/course_insight/infrastructure/postgresql/migrations/0014_m1_m2_m3_capabilities.sql` for versioned S1-S6 schema.
- Create `src/course_insight/modules/m2_evidence_retrieval/embedding.py` for `EmbeddingProvider`, model identity, deterministic test provider and OpenAI-compatible HTTP adapter.
- Create `src/course_insight/modules/m2_evidence_retrieval/vector_store.py` for vector index ports and pgvector implementation.
- Create `src/course_insight/modules/m2_evidence_retrieval/ranking.py` for lexical/vector/hybrid score normalization and stable ordering.
- Modify `src/course_insight/modules/m2_evidence_retrieval/service.py` to use the ports, reject empty configured production paths and expose retrieval results with scores.
- Modify `src/course_insight/contracts/intelligence.py` only with backward-compatible optional audit metadata and validated score records.
- Create `src/course_insight/modules/m2_evidence_retrieval/audit.py` for deterministic audit identity, idempotent persistence and safe metadata redaction.
- Create `src/course_insight/modules/m3_knowledge_bundle/teacher_review.py` for the immutable CAS workflow and approved-publication gate.
- Modify `src/course_insight/modules/m3_knowledge_bundle/service.py` to accept publication only from the approved workflow boundary.
- Add focused tests under `tests/unit`, `tests/integration`, and `tests/e2e`; no generated fixtures or ad-hoc process logs.
- Modify `docs/deployment.md`, the three M1/M2/M3 READMEs, `.env.example`, and `README.md` only after behavior is tested.

## Task 1: S6 parser protocol and metadata

**Files:**
- Create `src/course_insight/modules/m1_course_governance/parser_protocol.py`.
- Modify `src/course_insight/modules/m1_course_governance/parsers.py` and `service.py`.
- Test `tests/unit/test_m1_parser_protocol.py` and update existing M1 parser tests only where the public behavior is unchanged.

- [ ] Write tests proving registry lookup is case-insensitive, `.ppt` is rejected, unsupported media types fail with `COURSE_PARSE_FAILED`, maximum bytes are enforced before parsing, parser version is deterministic, and all five supported adapters produce the current `ParsedSource` ordering.
- [ ] Implement `ParserProtocol.parse(file_name, payload)`, immutable `ParserEntry(extension, media_type, parser_version, capabilities, max_bytes, parser)`, and `ParserRegistry.register/resolve/parse`; reject duplicate extensions and invalid limits during construction.
- [ ] Adapt the existing `parse_source` implementation as the default registered parser set; do not duplicate PDF/DOCX/PPTX parsing logic or change chunk IDs/locators.
- [ ] Add a private `parser_metadata` mapping to the generated import snapshot metadata containing extension, media type and parser version, excluding raw bytes and host paths.
- [ ] Run `pytest tests/unit/test_m1_parser_protocol.py tests/unit/test_m1* -q`; expected: all focused M1 tests pass.

## Task 2: S5 authoritative repositories, schema and migration boundary

**Files:**
- Create the SQLite/PostgreSQL repository facades and migration files listed in the file map.
- Create `src/course_insight/application/persistence.py`.
- Test `tests/unit/test_s1_s6_persistence.py`, `tests/integration/test_s1_s6_sqlite_restore.py`, and `tests/unit/test_postgres_m1_m2_m3_repository.py`.

- [ ] Write SQLite tests that initialize a fresh database, save/load complete M1 course import snapshots, M2 index artifacts, M2 retrieval audits, M3 bundles and teacher review rows, reopen in a new repository instance, and compare canonical checksums and payloads.
- [ ] Write tests for backend selection: `sqlite` creates only SQLite adapters, `postgresql` requires a configured URL environment variable, an unavailable backend fails readiness, and no adapter silently falls back to the other backend.
- [ ] Write migration tests for artifact export/import: validate schema and all checksums before publication, make a conflicting identity fail without partial rows, repeat an identical import idempotently, and rollback when any row fails validation.
- [ ] Add normalized repository protocols for `save/load_course_import`, `save/load_index_artifact`, `save/load_retrieval_audit`, `save/load_bundle`, `create/get/compare_and_swap_teacher_review`, `export_manifest`, and `import_manifest`; keep existing file repositories compatible.
- [ ] Add SQLite tables and PostgreSQL tables for complete payload JSON, checksum, identity, version, status and audit/workflow metadata; add uniqueness constraints on identity+checksum and optimistic version columns.
- [ ] Implement transaction-scoped validate-then-publish in both adapters. PostgreSQL code must use parameterized queries and the existing connection/pool error mapping; never interpolate payloads or credentials into SQL.
- [ ] Run `pytest tests/unit/test_s1_s6_persistence.py tests/integration/test_s1_s6_sqlite_restore.py -q`; run PostgreSQL tests only when a pgvector-enabled URL is present and report them as skipped otherwise.

## Task 3: S1 embedding provider and vector persistence

**Files:**
- Create `src/course_insight/modules/m2_evidence_retrieval/embedding.py` and `vector_store.py`.
- Modify `src/course_insight/modules/m2_evidence_retrieval/service.py`, M2 contracts as needed, and PostgreSQL migration.
- Test `tests/unit/test_embedding_provider.py`, `tests/unit/test_vector_store.py`, and `tests/integration/test_pgvector_index.py`.

- [ ] Write tests for a deterministic test provider, finite/exact-dimension validation, empty input, batch ordering, bounded retry on 429/5xx, timeout mapping, malformed provider JSON, TLS-disabled rejection in production mode, and secret-free error text.
- [ ] Implement `EmbeddingModelIdentity` and `EmbeddingProvider` with `embed_documents` and `embed_query`; the deterministic provider must require an explicit test-only constructor and cannot be selected from production settings.
- [ ] Implement the OpenAI-compatible adapter with stdlib `urllib.request`, explicit connect/read timeout, bounded exponential backoff, `Authorization` only in the request header, response-size limit, and model/dimension validation.
- [ ] Implement vector index build as a two-phase operation: embed and validate every chunk, write staging rows, verify count/checksum/model identity, then atomically mark the index ready. Any failure leaves no ready index.
- [ ] Add pgvector extension/table/index migration with a dimension check and a rebuild operation that creates a new index version before switching the ready pointer; retain lexical artifact behavior for offline mode.
- [ ] Run focused unit tests. If no pgvector database is configured, run the fake-pool contract tests and mark live integration skipped; do not claim live pgvector success.

## Task 4: S3 retrieval strategies and ranking

**Files:**
- Create `src/course_insight/modules/m2_evidence_retrieval/ranking.py`.
- Modify `src/course_insight/modules/m2_evidence_retrieval/service.py` and `src/course_insight/contracts/intelligence.py` only through backward-compatible fields.
- Test `tests/unit/test_retrieval_ranking.py` and `tests/integration/test_retrieval_strategies.py`.

- [ ] Write tests for lexical ranking, vector cosine ranking, min-max normalization, hybrid weighted combination, zero-signal rejection, top-k truncation, rerank determinism and evidence-ID tie-breaking.
- [ ] Implement `RetrievalCandidate(evidence_id, lexical_score, vector_score, final_score)` and pure ranking functions; validate that weights are non-negative, at least one signal is positive, and the configured strategy has its required dependency.
- [ ] Update M2 service dispatch so lexical remains available for file/SQLite mode, vector/hybrid require a configured provider and vector store, and configured production dependencies never return `status='empty'`.
- [ ] Ensure every retrieval result carries stable scores internally and only safe score metadata is exposed to audit; preserve existing evidence IDs and old caller signatures.
- [ ] Run `pytest tests/unit/test_retrieval_ranking.py tests/integration/test_retrieval_strategies.py -q` and the pre-existing M2 suite.

## Task 5: S2 formal retrieval audit

**Files:**
- Modify `src/course_insight/contracts/intelligence.py` with optional audit metadata.
- Create `src/course_insight/modules/m2_evidence_retrieval/audit.py`.
- Modify M2 service/repository adapters and test `tests/unit/test_retrieval_audit.py` plus `tests/integration/test_audit_restore.py`.

- [ ] Write tests proving success, no-result, failed, lexical, vector and hybrid retrievals all produce an audit; raw query text, provider credentials, host paths and document text are absent; repeated identical audit writes are idempotent and conflicting writes fail closed.
- [ ] Implement canonical audit identity from query checksum, index identity/version, policy identity/version, model identity, request identity and result IDs; use SHA-256 over canonical JSON and store only safe optional metadata.
- [ ] Add optional query checksum, index version, model identity, score records and latency/status metadata without changing required fields or existing schema root names.
- [ ] Persist the audit through the selected repository in the same logical operation as retrieval completion; failed persistence turns the operation into a safe failure rather than an untracked success.
- [ ] Run focused audit tests and the full retrieval suite.

## Task 6: S4 teacher confirmation and M3 publication gate

**Files:**
- Create `src/course_insight/modules/m3_knowledge_bundle/teacher_review.py`.
- Modify `src/course_insight/modules/m3_knowledge_bundle/service.py` and the S5 repository adapters.
- Test `tests/unit/test_teacher_review_workflow.py` and `tests/integration/test_teacher_approved_publish.py`.

- [ ] Write state-machine tests for `draft -> submitted -> approved|rejected -> recalled`, invalid transitions, immutable submissions, stale expected-version rejection, duplicate replay, reviewer pseudonym validation and safe reason storage.
- [ ] Implement immutable `TeacherReviewSubmission` and CAS transitions with input checksum, validation report reference, reviewer pseudonym, action timestamp, reason and version; do not store raw course content in the review record.
- [ ] Add a publication gate that accepts only an approved review and calls the existing M3 service through its public method; rejected, recalled, stale or missing reviews must not publish.
- [ ] Ensure automatic extraction/LLM output cannot become authoritative without an explicit teacher approval row; preserve the existing M3 bundle/seed/report checksums.
- [ ] Run the workflow and publication tests, then the existing M3 suite.

## Task 7: End-to-end wiring, readiness and operational documentation

**Files:**
- Modify `src/course_insight/application/factory.py`, `runtime_context.py`, `.env.example`, `docs/deployment.md`, the M1/M2/M3 READMEs and `README.md`.
- Test `tests/e2e/test_s1_s6_production_chain.py`, `tests/unit/test_production_no_empty.py`, and all existing tests.

- [ ] Write one deterministic end-to-end test: authorized M1 import → parser metadata → M2 index → lexical/vector/hybrid retrieval → persisted audit → teacher submission/approval → M3 publication → fresh-process restore, including an injected provider failure and a rollback assertion.
- [ ] Wire configuration for backend, provider endpoint/key env/model/version/dimension, vector strategy, parser limits, migration mode and readiness; configuration errors must be stable domain errors and must not expose secrets or absolute paths.
- [ ] Add readiness checks for selected repository, parser registry, provider/model compatibility and vector store; configured production S1-S4 paths must fail readiness rather than return an empty success.
- [ ] Document SQLite offline and PostgreSQL+pgvector production deployment, migration/export/import procedure, rollback behavior, provider replacement contract, required environment variables and live integration prerequisites.
- [ ] Run `.venv\Scripts\python.exe -m pytest -q`, `.venv\Scripts\python.exe -m compileall -q src tests`, a schema-root check, and `git diff --check`.

## Final verification

- [ ] Review the diff for accidental generated files, secrets, raw payloads, absolute paths and unrelated edits.
- [ ] Confirm `git status --short` contains only the intended S1-S6 implementation, tests, documentation and this single plan/spec addition; do not commit or push unless the user explicitly requests it.
- [ ] Record only verified outcomes in `C:\Users\jj\Desktop\智能体\KeYeZhiXi_Xie_MVP_MEMORY.md`, including any environment-dependent live PostgreSQL/provider tests that remain skipped.

## Execution status (2026-08-12)

- S1-S6 implementation and application wiring: complete. Formal application retrieval uses
  `retrieve_for_application -> retrieve_with_policy`; M3 S4 CAS wrappers and approved-publication
  gate are exposed through `AppCoordinator`.
- Vector restart binding: complete. PostgreSQL migration `0015_vector_index_metadata.sql` and
  durable metadata discovery validate package/model/dimension/count/time/checksum bindings without
  re-embedding. The existing `embedding_model_id` column is written and cross-checked as well.
- Verification: `python -m pytest -q` => `1773 passed, 18 skipped`; targeted application/chain
  tests => `42 passed`; targeted M2/pgvector/PostgreSQL tests => `37 passed`; compileall passed;
  `git diff --check` passed.
- The 18 skips are environment-guarded live PostgreSQL/pgvector or other external prerequisite
  tests. No live PostgreSQL+pgvector or external embedding endpoint is claimed as executed.
- No commit, push, destructive reset, or process-log/fixture generation was performed.
- Final readiness wiring and SQLite authoritative repository selection were added after the
  initial execution status; the final full-suite result above includes those changes.
