# M1–M6、M8 阶段补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐非 M7/M9 的阶段功能和验证，并提供深圳大学官网基础风格的 M3 教师知识包审核页面。

**Architecture:** 在现有 Django M0 Web 边界上增加 M3 S4 独立 view/form/template，所有状态变化仍通过 `AppCoordinator` 的 M3 CAS facade。M1 OCR 通过可注入端口扩展 parser registry；M2、M6 和生产联调以现有 CLI/服务为核心，补充可审计的配置、结果和测试，不改变公共 M7/M9 契约。

**Tech Stack:** Python 3.11+、Django 5.2、Pydantic 2、PostgreSQL 16 + pgvector、pytest/pytest-django、Django templates/CSS。

## Global Constraints

- M7 和 M9 相关功能不得实现或修改。
- M0/M8/M9 测评评分复核页面与 M3 S4 知识包审核页面保持边界分离。
- 所有状态写入必须通过现有 CAS service facade，不能由 Web view 直接写仓储。
- 用户输入、上传文件和生产配置必须 fail closed；不记录密钥、原文或完整答案。
- OCR provider、Embedding provider 和真实数据库必须通过依赖注入或受控环境变量提供。
- 先写失败测试，再写最小实现；完成后运行全量测试与覆盖率检查。

---

### Task 1: M3 S4 Web 审核接口和页面

**Files:**
- Modify: `src/course_insight/modules/m0_platform/django_app/urls.py`
- Create: `src/course_insight/modules/m0_platform/django_app/forms/knowledge_review.py`
- Create: `src/course_insight/modules/m0_platform/django_app/views/knowledge_review.py`
- Create: `src/course_insight/modules/m0_platform/django_app/templates/course_insight/teacher/knowledge_review.html`
- Modify: `src/course_insight/modules/m0_platform/django_app/templates/course_insight/base.html`
- Test: `tests/integration/test_django_knowledge_review.py`

**Interfaces:**
- Consumes: `AppCoordinator.create_knowledge_review_draft`, `submit_knowledge_review`, `approve_knowledge_review`, `reject_knowledge_review`, `recall_knowledge_review`.
- Produces: named routes for M3 S4 and a template context containing review status, package identity, validation summary, and action form.

- [ ] **Step 1: Write failing integration tests** for GET rendering, teacher scope rejection, CSRF-protected POST action, stale review version rejection, and successful approval redirect.
- [ ] **Step 2: Run `pytest tests/integration/test_django_knowledge_review.py -q`** and confirm the routes/view are missing.
- [ ] **Step 3: Implement form, view, flow token validation, and URL routes** using existing authz/error mapping patterns and M3 coordinator facades.
- [ ] **Step 4: Implement the restrained SZU-inspired template** using only red accent, white header, simple nav, gray background, title, side navigation, status steps, and border cards.
- [ ] **Step 5: Run the targeted integration tests** and confirm all pass.

### Task 2: M1 PDF parsing and OCR port

**Files:**
- Modify: `src/course_insight/modules/m1_course_governance/parser_protocol.py`
- Modify: `src/course_insight/modules/m1_course_governance/parsers.py`
- Modify: `src/course_insight/modules/m1_course_governance/service.py`
- Modify: `src/course_insight/application/factory.py`
- Create: `tests/unit/test_m1_pdf_ocr.py`
- Modify: `pyproject.toml` only if a pure-Python parser dependency is required.

**Interfaces:**
- Consumes: PDF path, optional injected OCR provider.
- Produces: text-layer extraction, OCR fallback, stable `COURSE_PDF_OCR_REQUIRED` error when fallback is unavailable.

- [ ] **Step 1: Write failing tests** for text-layer PDF, image-only PDF without OCR, image-only PDF with deterministic fake OCR, and empty OCR output.
- [ ] **Step 2: Run the focused tests** and confirm the current registry cannot handle `.pdf`.
- [ ] **Step 3: Implement parser and provider port** without network calls or system-level subprocesses in the default path.
- [ ] **Step 4: Register the parser in the production composition root** and preserve existing `.md`, `.txt`, `.docx`, and `.pptx` behavior.
- [ ] **Step 5: Run focused M1 tests and the existing M1–M3 chain tests**.

### Task 3: M2 target-scale benchmark acceptance

**Files:**
- Modify: `scripts/benchmark_m2_pgvector.py`
- Modify: `src/course_insight/modules/m2_evidence_retrieval/performance.py`
- Create: `tests/unit/test_m2_target_profile.py`
- Modify: `docs/deployment.md` with the exact command and result fields.

**Interfaces:**
- Consumes: disposable database URL/name, target profile arguments, threshold arguments.
- Produces: JSON report with profile id, scale, p50/p95, recall, build time, relation/index bytes, explain, pass/fail/blocked status.

- [ ] **Step 1: Write failing tests** for profile validation, guarded database name, threshold failure, and report schema.
- [ ] **Step 2: Run focused benchmark tests** and confirm the new profile/report fields are absent.
- [ ] **Step 3: Implement the profile and report additions** while keeping exact search as the default and leaving ANN disabled.
- [ ] **Step 4: Run a small representative probe** before any target-size run and record time/memory evidence.
- [ ] **Step 5: Run the configured target profile only if the probe is locally feasible; otherwise save a blocked report with the required server specification.**

### Task 4: M6 controlled verification record

**Files:**
- Create: `scripts/verify_m6_controlled_rollout.py`
- Modify: `docs/m6_policy_operations.md`
- Create: `tests/unit/test_m6_controlled_verification.py`

**Interfaces:**
- Consumes: M6 policy id, dataset identity, rollout and kill-switch configuration.
- Produces: JSON report with `passed`, `blocked`, reason codes, and no synthetic approval.

- [ ] **Step 1: Write failing tests** for missing data blocked, default rules blocked for active, and complete evidence path.
- [ ] **Step 2: Implement the command** using existing M6 repositories and policy gates; do not invoke M7/M9.
- [ ] **Step 3: Add documentation for shadow/OPE/approval/rollback evidence**.
- [ ] **Step 4: Run focused M6 tests and the existing M6 coverage gate**.

### Task 5: Production integration checklist and verification

**Files:**
- Create: `scripts/verify_production_readiness.py`
- Create: `tests/unit/test_production_readiness.py`
- Modify: `docs/deployment.md`

**Interfaces:**
- Consumes: explicit production configuration and disposable test database configuration.
- Produces: structured readiness report covering config, migrations, health, restore identity, cleanup, and secret redaction.

- [ ] **Step 1: Write failing tests** for insecure production config, missing migrations, wrong database guard, and successful disposable readiness.
- [ ] **Step 2: Implement fail-closed checks** reusing existing configuration loader, migration runner, health checks, and repository restore APIs.
- [ ] **Step 3: Run the disposable PostgreSQL/pgvector readiness check** and persist the redacted report.
- [ ] **Step 4: Run full unit, integration, contract, compile, dependency, and coverage checks.**
