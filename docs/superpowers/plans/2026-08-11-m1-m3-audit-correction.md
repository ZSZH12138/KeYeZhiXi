# M1-M3 Audit Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 M3 D1 错误码语义，澄清 M1-M3 runtime artifact 持久化边界，并用真实 M1→M2→M3 链路回归证明当前 MVP 的要求功能仍然可恢复、可审计、可消费。

**Architecture:** 保持 runtime immutable artifacts 为 M1-M3 唯一真相，不新增数据库双写或 migration。D1 只在 M3 service 层把已有私有 validation issue 映射为稳定公开 DomainError；D3 通过 README/运维说明和默认 Factory/重启测试统一认知；端到端测试复用公开 Service/Repository，不跨模块读取私有存储。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, immutable runtime artifact store, existing M1/M2/M3 services and contracts.

---

## Scope and non-goals

- 必须完成：D1 错误码修正、D3 文档与运维边界修正、M1→M2→M3 链路回归、artifact 重启恢复回归。
- 保持不变：84 个公共 schema 根、README 权威证据字段、evidence/chunk 身份规则、`.ppt` 拒绝、M3 教师 JSON 输入、M2 pgvector/embedding `empty`。
- 本轮不做：SQLite/PostgreSQL M1-M3 Repository、数据库 migration/双写、真实 pgvector、自动知识抽取、OCR、教师 UI、DINA/BKT/IRT、commit、push、依赖安装。

## Task 1: D1 public error-code projection

**Files:**
- Modify: `src/course_insight/modules/m3_knowledge_bundle/service.py`
- Test: `tests/unit/test_m3_knowledge_bundle_mvp.py`
- Test: `tests/unit/test_domain_error_path_safety.py`

- [ ] **Step 1: Write failing tests for public error categories.**

Add tests that exercise the real `M3KnowledgeBundleService.build_knowledge_bundle()` path and assert:

```python
def test_seed_read_failure_is_not_reported_as_q_matrix_conflict(...):
    # missing required concept seed
    with pytest.raises(DomainError) as raised:
        service.build_knowledge_bundle(package, missing, item, rubric, blueprint)
    assert raised.value.code == "KNOWLEDGE_SEED_READ_FAILED"

def test_seed_schema_failure_is_not_reported_as_q_matrix_conflict(...):
    # invalid required root/schema
    with pytest.raises(DomainError) as raised:
        service.build_knowledge_bundle(package, concept, malformed, rubric, blueprint)
    assert raised.value.code == "KNOWLEDGE_SEED_INVALID"

def test_true_q_matrix_failure_keeps_q_matrix_conflict(...):
    # valid JSON with a Q-matrix mismatch
    with pytest.raises(DomainError) as raised:
        service.build_knowledge_bundle(package, concept, q_matrix_conflict, rubric, blueprint)
    assert raised.value.code == "Q_MATRIX_CONFLICT"
```

Also assert all public error details remain limited to safe report identifiers or stable role/entity/field data; no host path, raw JSON or exception cause may escape.

- [ ] **Step 2: Run the new tests and confirm the expected RED failure.**

Run:

```powershell
python -m pytest tests/unit/test_m3_knowledge_bundle_mvp.py tests/unit/test_domain_error_path_safety.py -q
```

Expected: the new seed read/schema tests fail because the current service maps rejected seed snapshots to `Q_MATRIX_CONFLICT`; the existing Q-matrix test remains green.

- [ ] **Step 3: Implement the smallest deterministic mapping.**

In `service.py`, add a private mapping from the first deterministic validation issue to a public error code. Use this priority and mapping:

```python
_PUBLIC_ISSUE_CODES = {
    "SEED_READ_FAILED": "KNOWLEDGE_SEED_READ_FAILED",
    "SEED_TOO_LARGE": "KNOWLEDGE_SEED_INVALID",
    "SEED_JSON_INVALID": "KNOWLEDGE_SEED_INVALID",
    "SEED_SCHEMA_INVALID": "KNOWLEDGE_SEED_INVALID",
    "CONCEPT_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "ITEM_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "BLUEPRINT_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "PREREQUISITE_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "MISCONCEPTION_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "KNOWLEDGE_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "COURSE_PACKAGE_INVALID": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "COURSE_PACKAGE_MISMATCH": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "Q_MATRIX_CONFLICT": "Q_MATRIX_CONFLICT",
}
```

Use a fixed priority list so multiple issues produce deterministic primary error code. Preserve the existing rejected report and `report_id` detail. Keep `_load_seed()` path-safe and map its read/JSON failures to the same public categories.

- [ ] **Step 4: Run the focused tests and the M3 downstream tests.**

Run:

```powershell
python -m pytest tests/unit/test_m3_knowledge_bundle_mvp.py tests/unit/test_domain_error_path_safety.py tests/unit/test_m3_validation.py tests/unit/test_application_factory.py -q
```

Expected: zero failures; existing callers that only assert rejection remain valid, while category-specific tests prove D1 is fixed.

- [ ] **Step 5: Review the diff for public-boundary safety.**

Run `git diff --check` and inspect that no public contract field, schema, provenance endpoint, evidence field, or repository identity changed.

## Task 2: D3 persistence boundary documentation and regression coverage

**Files:**
- Modify: `README.md`
- Modify: `src/course_insight/modules/m1_course_governance/README.md`
- Modify: `src/course_insight/modules/m2_evidence_retrieval/README.md`
- Modify: `src/course_insight/modules/m3_knowledge_bundle/README.md`
- Modify: `docs/deployment.md` if it contains M1-M3 backup/repository claims
- Test: `tests/unit/test_application_factory.py` only where a missing default/restart assertion is identified

- [ ] **Step 1: Add a documentation regression checklist before editing docs.**

Confirm the current default Factory wires `FileM1Repository`, `FileM2Repository`, and `FileM3Repository`, while `_durable_repositories()` covers only the database-backed M0/M4-M9 set. Confirm the SQLite migration table names are scaffolding and have no M1-M3 business writes.

- [ ] **Step 2: Update the docs to one explicit persistence statement.**

Use this exact policy wording, adapted to each document’s context:

```text
M1-M3 的当前权威持久化是 runtime 下的 immutable artifacts，不是 SQLite/PostgreSQL 业务表。
SQLite migration 中的 m1_course_packages、m2_evidence_indexes、m3_knowledge_bundles
目前属于已建但未接入业务写入的预留表；备份、恢复、checksum 校验、清理和回滚必须覆盖
runtime artifact store。M1-M3 本 MVP 不做数据库双写或新增 migration。
```

Document M2’s complete lexical snapshot requirement and M3’s bundle/report/snapshot proof chain, so readers do not assume an index reference or bundle JSON alone is sufficient for recovery.

- [ ] **Step 3: Add only missing regression assertions.**

If existing tests already cover the behavior, keep them unchanged. Otherwise add one focused test that builds the default application and asserts the concrete repository types, and one fresh-process/repository test that restores an artifact without the original seed/source directory. Do not add SQL writes merely to make the table appear populated.

- [ ] **Step 4: Run documentation-adjacent regression tests.**

Run:

```powershell
python -m pytest tests/unit/test_application_factory.py tests/integration/test_m1_file_repository.py tests/integration/test_m2_file_repository.py tests/integration/test_m3_file_repository.py -q
```

Expected: all existing file-repository and factory restart tests pass; no SQLite schema or PostgreSQL migration diff is introduced.

## Task 2A: D3 runtime artifact restore wiring

**Files:**
- Modify: `src/course_insight/application/runtime_context.py`
- Modify: `src/course_insight/application/factory.py`
- Test: `tests/unit/test_application_factory.py`

- [x] Wire the default and override M1/M2/M3 repositories into `CourseRuntimeRegistry`.
- [x] Restore M1 from the complete course-import artifact, M2 from the complete lexical
  artifact, and M3 from bundle + seed snapshot + validation report.
- [x] Remove restore-time M2 rebuilding; fail closed for missing, corrupt, or mismatched
  artifacts, including the M3-to-M1 package checksum binding.
- [x] Keep the current v1 runtime snapshots as required manifest identity locators and
  cross-checks; independent active-artifact selection remains deferred with Phase 9.
- [x] TDD evidence: RED reproduced missing artifact/rebuild/binding failures; GREEN
  passed the D3 focused suite and `git diff --check`.

## Task 3: M1→M2→M3 chain verification

**Files:**
- Test: `tests/integration/test_m1_m2_m3_chain.py` if no equivalent exists; otherwise extend the smallest existing chain test
- Modify: only the minimal service/coordinator file if the chain exposes a real missing connection

- [ ] **Step 1: Write the failing chain test first.**

The test must:

1. Import an anonymous multi-format/text fixture through M1.
2. Build and persist the M2 lexical index from that exact `CoursePackage`.
3. Build and persist the M3 bundle from teacher-confirmed seeds referring to deterministic `evidence_{chunk_id}` IDs.
4. Assert M1 package ID/version/checksum, M2 index package binding, and M3 bundle package binding all match.
5. Create fresh M1/M2/M3 repositories/services, remove the original seed/source inputs, restore the artifacts, and retrieve a known evidence chunk.
6. Feed the restored M3 bundle to the existing downstream compatibility path without M3 reading M2 private storage.

- [ ] **Step 2: Run the chain test and verify RED for any missing link.**

Run:

```powershell
python -m pytest tests/integration/test_m1_m2_m3_chain.py -q
```

If it passes immediately, treat the test as a regression proof and do not add speculative production code. If it fails, classify the failure as an actual missing connection before modifying code.

- [ ] **Step 3: Fix only the missing connection with TDD.**

Keep all changes within the existing public service/repository boundaries. Do not make M3 call M2, do not pass temporary dictionaries across modules, and do not introduce a second persistence authority.

- [ ] **Step 4: Run the complete M1-M3 and contract gate.**

Run:

```powershell
python -m pytest tests/unit/test_m1_course_governance_mvp.py tests/unit/test_m1_parsers.py tests/unit/test_m2_evidence_retrieval_mvp.py tests/unit/test_m3_knowledge_bundle_mvp.py tests/unit/test_m3_validation.py tests/contract -q
```

Expected: all tests pass, public schema count remains 84, and `git diff --check` is clean.

## Task 4: Final two-stage review and evidence pack

**Files:**
- Modify: `KeYeZhiXi_Xie_MVP_MEMORY.md` only to append verified implementation results
- Modify: this plan to mark completed steps

- [ ] **Step 1: Run spec-compliance review.**

Check every audit item D1/D3 and every user-approved boundary against code, tests, and docs. Record any gap before claiming completion.

- [ ] **Step 2: Run code-quality review.**

Inspect error mapping determinism, exception safety, path safety, test isolation, artifact authority, and diff scope. Resolve all Critical/Important findings before final verification.

- [ ] **Step 3: Run final verification.**

Run:

```powershell
python -m pytest -q
git diff --check
rg -n "m1_course_packages|m2_evidence_indexes|m3_knowledge_bundles" src/course_insight/application src/course_insight/infrastructure README.md docs
```

Expected: full test output is recorded with exit code, no diff whitespace errors, and search results clearly show database tables as pre-created/unwired rather than business writes.

- [ ] **Step 4: Append evidence to memory.**

Append the actual test counts, changed files, unresolved deferred capabilities, and any environment limitation to `C:\Users\jj\Desktop\智能体\KeYeZhiXi_Xie_MVP_MEMORY.md`. Do not create another process log unless a test failure requires a durable incident record.

## Self-review checklist

- [x] D1 seed read, JSON/schema, entity/reference/binding and true Q-matrix cases each have an explicit behavior/test.
- [x] D3 docs state the same runtime-artifact authority everywhere touched.
- [x] M1→M2→M3 uses the same package identity and stable evidence identity across build and restore.
- [x] No public evidence field names, schema root count, migration, or deferred capability was changed.
- [x] No task uses a placeholder instruction; every implementation step names files, behavior, command, and expected evidence.

## Execution status (2026-08-12)

- D1 implementation: complete; Luna Max specification review APPROVED and quality review APPROVED.
- D3 documentation and restore wiring: complete; Luna Max specification review APPROVED after the
  explicit v1 snapshot-locator boundary was documented, and quality review APPROVED.
- Chain regression: complete; `tests/integration/test_m1_m2_m3_chain.py` passes and proves
  M1→M2→M3 identity/checksum continuity plus artifact-only data recovery after source/seed cleanup.
- Final full-suite verification and memory evidence append: complete (`1674 passed, 17 skipped`,
  schema roots `84`, `git diff --check` clean).

## Final verification update (2026-08-12)

- [x] D1/D3 and S1-S6 requirements are implemented and wired through the application boundary.
- [x] Production capability readiness is fail-closed for backend, parser, embedding, vector,
  audit, and teacher-review ports.
- [x] Final full suite: `1773 passed, 18 skipped`; `compileall -q src tests` and
  `git diff --check` passed. The skipped tests remain environment-gated live PostgreSQL/pgvector
  or external-resource checks; no live integration is claimed.
