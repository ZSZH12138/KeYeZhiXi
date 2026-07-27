# M6 Policy Learning and Versioned Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to execute this plan task-by-task with review
> checkpoints.

**Goal:** Add safe strategy learning, versioned adapters, private offline
evaluation, and M0 policy freezing without changing public contracts or the
existing `decide_next_action(...)` signature.

**Architecture:** Keep the deterministic FSM and safety envelope authoritative.
Bind each existing request fingerprint to one immutable private policy execution
reference, let rules/shadow/active adapters rank only safe candidates, and persist
the binding plus observation atomically with the authoritative M6 decision.

**Tech Stack:** Python 3.12, frozen dataclasses, Pydantic v2 public inputs,
canonical JSON/SHA-256, pure-Python LinUCB/OPE, SQLite, PostgreSQL/psycopg,
pytest/pytest-cov.

**Global Constraints:** No public schema/provenance/signature changes; no M9
formal integration; no pickle/joblib, raw answer/text/identity data, absolute
artifact paths, network calls, optional ML dependency in rules mode, or changes
to user-owned untracked files.

**Final reconciliation (2026-07-27):** Implementation is complete but remains
disabled by default in `rules` with zero rollout and zero exploration. The
published M4 intent migrations retain schema v10/v11; M6 policy tables are
schema v12 and M0 policy freeze is additive schema v13. The
final regression added rich observations, real logging distributions, raw
reward audit fields, structured provenance validation, and the explicit JSONL
allowlist described in Task 7 below. No real teaching, learned-policy rollout,
or live PostgreSQL migration was performed.

---

### Task 1: Private policy types, safety envelope, and feature schema

**Files**

- Add: `src/course_insight/modules/m6_tutoring_fsm/policy_types.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/safety_envelope.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/features.py`
- Add: `tests/unit/test_m6_policy_domain.py`

**Steps**

1. Write failing validation, immutability, state/candidate matrix, deterministic
   ordering, finite-value, and feature-version tests.
2. Add frozen private dataclasses with canonical serialization and SHA-256
   identities.
3. Implement the complete S0-S5 candidate matrix from the approved design.
4. Implement the fixed-order `m6-features-v1` vector from structured fields only.
5. Run `pytest -q tests/unit/test_m6_policy_domain.py`.

### Task 2: Adapters, LinUCB, exploration, artifacts, and gate

**Files**

- Add: `src/course_insight/modules/m6_tutoring_fsm/policy_adapter.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/linucb.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/policy_artifacts.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/policy_gate.py`
- Add: `tests/unit/test_m6_policy_adapters.py`
- Add: `tests/unit/test_m6_policy_artifacts.py`

**Steps**

1. Write failing tests for deterministic baseline parity, candidate confinement,
   LinUCB math/ties/dimensions, propensity sums, remediation exploration ban,
   path traversal, digest/version mismatch, rollout, scope, kill switch, and
   fail-closed behavior.
2. Define the adapter protocol and deterministic rules adapter wrapping the
   existing `M6DecisionPolicy`.
3. Implement pure-Python LinUCB prediction with strict finite/matrix validation.
4. Implement deterministic SHA-256 epsilon exploration and exact propensities.
5. Implement canonical JSON artifact/manifest loading under a configured root.
6. Implement the approved active gate and structured rejection reasons.
7. Run the two focused test files.

### Task 3: Runtime modes and authoritative M6 decision integration

**Files**

- Add: `src/course_insight/modules/m6_tutoring_fsm/policy_runtime.py`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/repository.py`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/service.py`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/__init__.py`
- Modify: `tests/unit/test_m6_tutoring_fsm.py`
- Add: `tests/unit/test_m6_policy_runtime.py`

**Steps**

1. Add failing tests proving rules parity, shadow non-interference, active gate
   adoption, fallback, identical-request replay, policy execution fingerprint,
   and immutable first-writer binding.
2. Add `prepare_policy_execution(...)` as an additive internal method while
   leaving `decide_next_action(...)` unchanged.
3. Have direct calls lazily prepare the same binding and have all modes reuse it.
4. Extend the private decision record with its policy observation and commit it
   in the existing authoritative decision transaction.
5. Ensure public action construction still uses the current ActionFactory and
   final state-machine validation.
6. Run M6 unit and runtime tests.

### Task 4: SQLite/PostgreSQL v12 persistence and import parity

**Files**

- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m6_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/migrations/0012_m6_policy_learning.sql`
- Modify: `src/course_insight/infrastructure/postgresql/migration_runner.py`
- Modify: `src/course_insight/infrastructure/postgresql/m6_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import_source.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import_destination.py`
- Modify: `tests/integration/test_m6_persistence.py`
- Modify: `tests/unit/test_postgresql_m6_repository_parity.py`
- Modify: `tests/integration/test_postgresql_m4_m6_repository_parity.py`
- Modify: `tests/unit/test_sqlite_to_postgres_import.py`
- Modify: `tests/integration/test_sqlite_to_postgres_migration.py`

**Steps**

1. Write failing migration, round-trip, atomicity, restart, duplicate, corruption,
   and concurrent first-writer tests.
2. Add the five M6 policy tables and matching v12 SQLite/PostgreSQL constraints.
3. Implement artifact, execution, observation, reward, and evaluation repository
   operations with parameterized SQL and exact payload validation.
4. Keep decision plus observation atomic and old decision rows readable.
5. Extend the importer allowlist, order, validation, and destination insertions.
6. Run SQLite, fake-PostgreSQL, live-guarded PostgreSQL, and import parity tests.

### Task 5: Reward lifecycle, offline dataset, and OPE

**Files**

- Add: `src/course_insight/modules/m6_tutoring_fsm/rewards.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/offline_dataset.py`
- Add: `src/course_insight/modules/m6_tutoring_fsm/offline_evaluation.py`
- Add: `tests/unit/test_m6_policy_rewards.py`
- Add: `tests/unit/test_m6_offline_evaluation.py`

**Steps**

1. Write failing tests for the v1 reward formula, pending/censored/invalid
   outcomes, idempotency, de-identification, grouped/time splits, IPS/SNIPS/DM/DR,
   bootstrap reproducibility, ESS/coverage, and insufficient-data rejection.
2. Implement immutable reward association without inventing missing outcomes.
3. Implement canonical de-identified JSONL export with an explicit field
   allowlist.
4. Implement pure-Python OPE and deterministic bootstrap seeded from dataset
   identity.
5. Persist evaluation identity, metrics, confidence intervals, slices, and the
   approval verdict as M6-private records.
6. Run both focused test files.

### Task 6: M0 configuration and policy freeze handshake

**Files**

- Modify: `src/course_insight/infrastructure/config/models.py`
- Modify: `src/course_insight/infrastructure/config/loader.py`
- Modify: `src/course_insight/modules/m0_platform/workflow.py`
- Modify: `src/course_insight/modules/m0_platform/repository.py`
- Modify: `src/course_insight/modules/m0_platform/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/workflow_migration.py`
- Modify: `src/course_insight/infrastructure/sqlite/m0_repository.py`
- Keep unchanged: the published M4 v10/0010 and v11/0011 migrations
- Keep: `src/course_insight/infrastructure/postgresql/migrations/0012_m6_policy_learning.sql`
- Add: `src/course_insight/infrastructure/postgresql/migrations/0013_m0_policy_freeze.sql`
- Modify: `src/course_insight/infrastructure/postgresql/m0_repository.py`
- Modify: `src/course_insight/application/assessment_dependencies.py`
- Modify: `src/course_insight/application/assessment_workflow.py`
- Modify: `tests/unit/test_m0_config_loader.py`
- Modify: `tests/unit/test_assessment_dependencies.py`
- Modify: `tests/unit/test_application_web_workflow.py`
- Modify: `tests/unit/test_m0_workflow_hardening.py`
- Modify (runtime/config integration):
  `src/course_insight/modules/m6_tutoring_fsm/policy_types.py`,
  `src/course_insight/modules/m6_tutoring_fsm/policy_runtime.py`,
  `src/course_insight/modules/m6_tutoring_fsm/service.py`,
  `src/course_insight/application/factory.py`,
  `tests/unit/test_m6_policy_domain.py`,
  `tests/unit/test_m6_policy_runtime.py`,
  `tests/unit/test_application_factory.py`

**Steps**

1. Add failing strict-config tests for rules defaults, bounded exploration,
   rollout, allowlist, runtime root, and kill switch.
2. Add private M0 run fields for the exact seven frozen components:
   `policy_id`, `adapter_id`, `adapter_version`, `artifact_sha256`,
   `feature_schema_version`, `action_space_version`, and
   `gate_policy_version`.
3. Preserve the published M4 v10/v11 migrations; keep M6 policy tables in
   PostgreSQL/SQLite v12 and persist M0 freeze additively in v13.
4. Add `policy_frozen` between `state_saved` and `tutoring_saved`. Before the
   M6 workflow call, prepare and freeze the policy execution; on recovery
   require an exact seven-field match.
5. Keep non-M0 direct calls compatible through M6 lazy preparation.
6. Allow only all-null migrated-v12 submit rows at
   `tutoring_saved|feedback_saved|analytics_saved`, with saved-state/frozen
   prior-state markers, to adopt once through CAS; reject partial, review, or
   `policy_frozen` adoption.
7. Run focused config, dependency, workflow, and recovery tests.

### Task 7: Cross-mode, concurrency, security, and compatibility regression

**Files**

- Add: `tests/integration/test_m6_policy_modes.py`
- Add: `tests/integration/test_m6_policy_concurrency.py`
- Modify: `tests/integration/test_m6_cross_module.py`
- Modify: `tests/e2e/test_m0_golden_path.py`
- Modify: `tests/contract/test_provenance_endpoints.py`

**Steps**

1. Add rules/shadow/active golden fixtures and assert rules public output is
   byte-for-byte stable.
2. Exercise same-request and competing-request concurrency across policy binding
   and decision commit.
3. Exercise restart/recovery with frozen M0 policy identity.
4. Verify tampered artifact, NaN/Inf, path escape, secret-like fields, low support,
   low ESS, and kill switch never produce an active unsafe action.
5. Assert public schemas, service signature, provenance, and M4→M6→M2/M7 flow are
   unchanged.
6. Bind every new execution fingerprint to the fixed ordered preimage
   `input_fingerprint`, `adapter_id`, `adapter_version`, `artifact_sha256`,
   `feature_schema_version`, `action_space_version`, `gate_policy_version`;
   preserve legacy execution/observation/reward canonical identities.
7. Persist rich observations with decision/input/request/execution identity,
   context and candidate checksums, baseline/chosen/shadow actions, full
   logging action probabilities, model scores, uncertainty, reason codes,
   complete policy identity, logging policy, and aware timestamp. Require the
   embedded decision ID to match the observation row and parent decision.
8. Preserve raw outcome components and pending/censored/observed/invalid reward
   status; reject path-like, non-structured free-text, email-like, and
   secret-like outcome provenance. Safety-invalid rewards must not enter
   ordinary scalar evaluation.
9. Export only the approved canonical JSONL fields:
   `decision_id`, `context`, `candidate_actions`, `chosen_action`,
   `propensity`, `reward`, `reward_status`, `policy_version`,
   `feature_schema_version`, `action_space_version`, `anonymous_group_key`,
   `occurred_at`, `session_id`, `event_time`, `state`,
   `target_propensities`, `direct_estimates`. HMAC decision/session/group
   identity and exclude raw identity plus shadow action.

### Task 8: Documentation and complete verification

**Files**

- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/interface_guide.md` (the repository's actual interface document)
- Modify: `docs/deployment.md` (the repository's actual deployment checklist)
- Modify: `docs/postgresql_migration.md`
- Add: `docs/m6_policy_operations.md`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/README.md`
- Modify: `config/app.example.json` and `.env.example`; do not create another
  application configuration example.

**Steps**

1. Document modes, safety envelope, feature/action/reward/gate versions,
   artifact promotion and rollback, kill switch, reward/OPE/JSONL limits, M6
   policy schema v12 plus additive M0 freeze v13, and the explicit M9
   non-integration.
2. Run focused M6/M0 tests, then `pytest -q`.
3. Run coverage and require at least 80% overall and at least 90% for new M6
   policy code where the existing coverage setup supports file reporting.
4. Run `python -m compileall -q src scripts tests` and `git diff --check`.
5. Inspect the complete diff for public-contract drift, secrets, unsafe
   deserialization, absolute path leakage, SQL interpolation, and unrelated
   changes.
