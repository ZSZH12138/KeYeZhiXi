# M0 Production Hardening Design

## Context

The first M0 production implementation is present but has three correctness gaps:

1. a successful login clears the shared-IP failure bucket;
2. assessment workflow recovery does not renew leases or freeze all executable
   dependencies and the exact pre-state;
3. the legacy outbox delivery facade does not renew a lease while a callback is
   running.

The approved M5/M8 persistence additions remain in scope because the split Web
workflow depends on them. This hardening does not add M5/M8 algorithms, change any
of the 84 public Pydantic schemas, or replace existing public service signatures.

## Decision 1: preserve the shared-IP login bucket

`reset_login_failures` keeps its compatible default behavior for existing internal
callers, but gains an optional `clear_shared_ip` flag. The successful Web login path
clears the actor+IP and actor buckets while preserving the aggregate IP bucket.
The shared IP bucket expires through its configured window and therefore cannot
be erased by logging in with one valid account.

Database failures remain fail-closed and happen before the Django session is
authenticated.

## Decision 2: make workflow recovery deterministic

### Immutable dependency identity

Every new assessment run stores private M0 metadata for the executable inputs:

- knowledge bundle ID, version, course-package ID, and canonical content checksum;
- evidence-index ID, version, and canonical content checksum when the operation
  consumes the index;
- exact byte checksums of the state and teacher policy files when the operation
  consumes those policies.

The start run freezes the knowledge/course identity. Submit and review runs must
match that start identity. An operation replay must match every dependency field
stored on its original row or fail closed as a submission conflict. The workflow
rechecks policy identity immediately before policy-consuming module calls. M5 and
M9 then read the selected policy exactly once and use that same byte sequence for
SHA-256 comparison and strict parsing, so a concurrent file replacement cannot
change the consumed policy after verification.

The course package object itself is not a submit/review input and is not read by
those operations. Its authoritative identity is carried by
`KnowledgeBundle.course_package_id`; the executable retrieval input is frozen
separately as the complete `EvidenceIndexRef` content checksum. This avoids adding
an unused cross-module lookup or changing the public coordinator signatures.

### Exact pre-state

Submit and review gain one private checkpoint between `events_appended` and
`state_saved`: `state_inputs_frozen`.

At that transition M0 records:

- whether the baseline has been frozen;
- exact learner snapshot ID and version, or an explicit frozen absence;
- exact class snapshot ID and version, or an explicit frozen absence.

M5 gains additive repository/service reads scoped by course, class, learner, and
version. Recovery after `state_inputs_frozen` uses only those exact reads and never
calls a latest-state method.

Pre-v9 rows use a narrow compatibility path. A replay may CAS-fill the new fields
only when its old immutable identity still matches, every new field is NULL, and
the current governed knowledge/course identity matches the persisted TaskPlan
anchors. Early checkpoints receive `previous_state_frozen=False`. Checkpoints at
or after `state_saved` are adoptable only with an exact `state_version` and receive
`previous_state_frozen=True`; the impossible legacy `state_inputs_frozen`
checkpoint, partial rows, and conflicting rows fail closed.

### Lease renewal

M0 gains a CAS lease-renewal operation which succeeds only for the same operation,
owner, version, running status, and an unexpired lease. Renewal updates only
`lease_until` and `updated_at`; it does not change the workflow version.

Potentially long module calls run through a small heartbeat executor. The caller
thread renews periodically while the module call executes in a daemon thread. If
renewal fails, the returned value is discarded and the workflow stops with
`WORKFLOW_LEASE_LOST`. Checkpoint advance, completion, and failure also reject an
expired lease, so a stale owner cannot write after expiry.

Module writes remain idempotent and authoritative in their owning repositories.
The heartbeat narrows duplicate-work windows but does not pretend that arbitrary
Python work can be force-cancelled.

## Decision 3: harden the legacy outbox facade

`deliver_outbox_records(deliver)` retains its public signature. SQLite and
PostgreSQL use one shared heartbeat executor around the transaction-free callback.
The existing outbox CAS renewal method extends the lease while the callback runs.

If ownership is lost:

- a successful callback is not acknowledged by the stale worker;
- a failed callback is not marked failed by the stale worker;
- the original callback exception is preserved;
- the record remains recoverable under at-least-once semantics.

The formal `OutboxWorker` remains the production path. This change makes the
compatibility path obey the same ownership rule.

## Persistence and compatibility

- SQLite schema version advances from 8 to 9.
- PostgreSQL gains migration `0009_m0_workflow_recovery_freeze.sql`.
- New workflow columns are nullable where legacy completed rows need to remain
  readable.
- New rows carry explicit dependency and baseline-freeze metadata.
- Wholly legacy rows are adopted lazily by one versioned CAS; there is no blanket
  migration backfill and no guessing for partially populated rows.
- Both adapters keep equivalent CAS, validation, and decoding behavior.
- No public Pydantic contract or provenance edge changes.

## Verification

Each defect is first reproduced by a focused failing test. Verification then covers:

- real Django login behavior and helper-level bucket semantics;
- SQLite/PostgreSQL repository parity for dependency round trips, lease renewal,
  lease expiry, and frozen state references;
- workflow recovery with changed bundle/policy inputs and with a newer concurrent
  learner/class state;
- slow legacy outbox callbacks and simulated lease loss;
- all unit/integration/E2E tests, coverage, Django checks, migrations, role sync,
  schema export, and repository security review.

Live PostgreSQL tests are reported separately and never described as passed when
their required database environment is unavailable.
