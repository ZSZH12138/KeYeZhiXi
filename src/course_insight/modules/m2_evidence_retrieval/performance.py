"""Bounded PostgreSQL+pgvector performance evidence for M2.

The benchmark deliberately measures the existing exact-search path exposed by
``PostgresPgVectorStore``.  It creates no HNSW/IVFFlat index: the result is
evidence for deciding whether exact search is sufficient, not an ANN rollout.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import json
import math
import os
import re
import secrets
import time
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
    PostgresPgVectorStore,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    M2_VECTOR_DOCUMENTS_TABLE,
    MAX_VECTOR_BATCH_SIZE,
    VectorDocument,
    build_pgvector_exact_search_sql,
)


SCHEMA_VERSION = "m2_pgvector_benchmark/v1"
TEST_DATABASE_URL_ENV = "COURSE_INSIGHT_TEST_DATABASE_URL"
TEST_DATABASE_NAME_ENV = "COURSE_INSIGHT_TEST_DATABASE_NAME"
DEFAULT_CHUNK_COUNT = 128
DEFAULT_DIMENSION = 8
DEFAULT_QUERY_COUNT = 20
DEFAULT_TOP_K = 5
DEFAULT_MAX_P95_MS = 1000.0
DEFAULT_MIN_RECALL_AT_K = 1.0
DEFAULT_SEED = 20260815
DEFAULT_BATCH_SIZE = MAX_VECTOR_BATCH_SIZE


@dataclass(frozen=True, slots=True)
class BenchmarkProfile:
    """Named acceptance scale; profiles never change the search algorithm."""

    profile_id: str
    chunk_count: int
    dimension: int
    query_count: int
    top_k: int
    max_p95_ms: float
    min_recall_at_k: float
    batch_size: int = DEFAULT_BATCH_SIZE
    ann_enabled: bool = False


TARGET_SCALE_PROFILE = BenchmarkProfile(
    profile_id="m2-target-scale-v1",
    chunk_count=50_000,
    dimension=1_536,
    query_count=100,
    top_k=10,
    max_p95_ms=DEFAULT_MAX_P95_MS,
    min_recall_at_k=1.0,
)

# These bounds keep accidental local/CI runs finite while leaving room for a
# realistic target-scale run in a disposable PostgreSQL database.
MAX_CHUNK_COUNT = 100_000
MAX_DIMENSION = 4_096
MAX_QUERY_COUNT = 500
MAX_TOP_K = 100
MAX_SEED = 2**63 - 1
MAX_BATCH_SIZE = MAX_VECTOR_BATCH_SIZE
# 50,000 * 100 * 1,536 = 7.68 billion scalar products.  This bound admits
# that governed target while rejecting the Cartesian extremes implied by the
# individual field limits.
MAX_WORK_UNITS = 10_000_000_000
_MAX_ID_NONCE = 1_000_000_000

_DISPOSABLE_NAME = re.compile(
    r"(?:^|[_-])(?:test|ci|tmp)(?:$|[_-])",
    re.IGNORECASE,
)
_RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
_BENCHMARK_ID = re.compile(r"^m2_benchmark_[0-9]+_[0-9]+$")
_BENCHMARK_VERSION = re.compile(r"^v[0-9]+$")
_DOCUMENT_TEXT_CHECKSUM = "1" * 64
_PUBLISH_CHECKSUM = "0" * 64


class BenchmarkError(RuntimeError):
    """Base class for safe benchmark failures."""


class BenchmarkConfigurationError(BenchmarkError, ValueError):
    """The benchmark was not allowed to start with the supplied configuration."""


class BenchmarkExecutionError(BenchmarkError):
    """The database benchmark could not produce a trustworthy result."""


class BenchmarkCleanupError(BenchmarkExecutionError):
    """The benchmark cleanup failed and may also carry the primary failure."""

    def __init__(
        self,
        *,
        primary_error: BaseException | None,
        report: dict[str, object] | None = None,
    ) -> None:
        self.primary_error = primary_error
        self.report = report
        reasons: list[str] = []
        if primary_error is not None:
            reasons.append(_safe_failure_reason(primary_error))
        reasons.append("cleanup_failed")
        self.failure_reasons = tuple(reasons)
        super().__init__("benchmark cleanup failed")


@dataclass(frozen=True, slots=True)
class DisposableDatabaseTarget:
    """A DSN/name pair that is proven safe before a benchmark can run."""

    dsn: str = field(repr=False)
    database_name: str

    def __post_init__(self) -> None:
        _validate_disposable_target(self.dsn, self.database_name)


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Validated benchmark inputs and governed defaults."""

    chunk_count: int = DEFAULT_CHUNK_COUNT
    dimension: int = DEFAULT_DIMENSION
    query_count: int = DEFAULT_QUERY_COUNT
    top_k: int = DEFAULT_TOP_K
    max_p95_ms: float = DEFAULT_MAX_P95_MS
    min_recall_at_k: float = DEFAULT_MIN_RECALL_AT_K
    seed: int = DEFAULT_SEED
    batch_size: int = DEFAULT_BATCH_SIZE
    profile_id: str = "m2-bounded-v1"
    ann_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id
            or self.profile_id != self.profile_id.strip()
            or any(character in "\\/:?*<>|\"" for character in self.profile_id)
        ):
            raise ValueError("profile_id must be safe text")
        if self.ann_enabled is not False:
            raise ValueError("ANN activation is not part of the M2 acceptance benchmark")
        _validate_bounded_int(
            self.chunk_count,
            field="chunk_count",
            minimum=1,
            maximum=MAX_CHUNK_COUNT,
        )
        _validate_bounded_int(
            self.dimension,
            field="dimension",
            minimum=1,
            maximum=MAX_DIMENSION,
        )
        _validate_bounded_int(
            self.query_count,
            field="query_count",
            minimum=1,
            maximum=MAX_QUERY_COUNT,
        )
        _validate_bounded_int(
            self.top_k,
            field="top_k",
            minimum=1,
            maximum=MAX_TOP_K,
        )
        if self.top_k > self.chunk_count:
            raise ValueError("top_k must not exceed chunk_count")
        max_p95_ms = _validated_number(self.max_p95_ms, "max_p95_ms")
        min_recall_at_k = _validated_number(
            self.min_recall_at_k,
            "min_recall_at_k",
        )
        if max_p95_ms <= 0.0:
            raise ValueError("max_p95_ms must be positive")
        if not 0.0 <= min_recall_at_k <= 1.0:
            raise ValueError("min_recall_at_k must be between 0 and 1")
        _validate_bounded_int(
            self.seed,
            field="seed",
            minimum=0,
            maximum=MAX_SEED,
        )
        _validate_bounded_int(
            self.batch_size,
            field="batch_size",
            minimum=1,
            maximum=MAX_BATCH_SIZE,
        )
        workload_units = self.chunk_count * self.query_count * self.dimension
        if workload_units > MAX_WORK_UNITS:
            raise ValueError(
                "benchmark workload chunk_count*query_count*dimension "
                f"must not exceed {MAX_WORK_UNITS}"
            )
        object.__setattr__(self, "max_p95_ms", max_p95_ms)
        object.__setattr__(self, "min_recall_at_k", min_recall_at_k)


@dataclass(frozen=True, slots=True)
class BenchmarkIdentity:
    """Generated identity for the one index owned by one benchmark run."""

    index_id: str
    index_version: str

    def __post_init__(self) -> None:
        if _BENCHMARK_ID.fullmatch(self.index_id) is None:
            raise BenchmarkConfigurationError("benchmark index identity is invalid")
        if _BENCHMARK_VERSION.fullmatch(self.index_version) is None:
            raise BenchmarkConfigurationError("benchmark index version is invalid")


class _Cursor(Protocol):
    def fetchone(self) -> object | None:
        """Return one row."""

    def fetchall(self) -> list[object]:
        """Return all rows."""


class _Connection(Protocol):
    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        """Execute a parameterized SQL statement."""

    def transaction(self) -> AbstractContextManager[object]:
        """Open a transaction."""


class _Pool(Protocol):
    def connection(self) -> AbstractContextManager[_Connection]:
        """Checkout one PostgreSQL connection."""


class _VectorStore(Protocol):
    def assert_available(self) -> None:
        """Require pgvector before mutating the disposable database."""

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        """Start the staging index."""

    def add_many(
        self,
        index_id: str,
        index_version: str,
        documents: Sequence[VectorDocument],
    ) -> None:
        """Insert one bounded batch through the production adapter."""

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
    ) -> None:
        """Publish the complete staging index."""

    def search(
        self,
        index_id: str,
        index_version: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
    ) -> tuple[Any, ...]:
        """Execute the production exact-search query."""


def percentile_ms(values: Sequence[float], percentile: float) -> float:
    """Return an inclusive, linearly interpolated percentile in milliseconds."""

    if not values:
        raise ValueError("percentile values must be non-empty")
    percentile_value = _validated_number(percentile, "percentile")
    if not 0.0 <= percentile_value <= 100.0:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("percentile values must be finite")
    position = (len(ordered) - 1) * percentile_value / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def recall_at_k(
    retrieved_ids: Sequence[str],
    expected_ids: Sequence[str],
    k: int,
) -> float:
    """Compute distinct-hit recall against an independently ranked top-k list."""

    _validate_bounded_int(k, field="k", minimum=1, maximum=MAX_TOP_K)
    expected = tuple(expected_ids[:k])
    retrieved = tuple(retrieved_ids[:k])
    if not expected:
        raise ValueError("expected_ids must contain at least one item")
    return sum(expected_id in retrieved for expected_id in expected) / len(expected)


def evaluate_thresholds(
    p95_ms: float | None,
    recall_value: float | None,
    config: BenchmarkConfig,
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate both gates without converting missing measurements into passes."""

    reasons: list[str] = []
    if p95_ms is None:
        reasons.append("p95_ms is missing")
    elif not math.isfinite(float(p95_ms)) or p95_ms > config.max_p95_ms:
        reasons.append("p95_ms exceeds max_p95_ms")
    if recall_value is None:
        reasons.append("recall_at_k is missing")
    elif not math.isfinite(float(recall_value)) or recall_value < config.min_recall_at_k:
        reasons.append("recall_at_k is below min_recall_at_k")
    return not reasons, tuple(reasons)


def require_numpy() -> Any:
    """Load the governed numerical dependency or fail closed with instructions."""

    try:
        import numpy
        version = str(getattr(numpy, "__version__", ""))
        major = int(version.split(".", 1)[0])
        if major < 2 or major >= 3:
            raise ValueError("unsupported numpy version")
        return numpy
    except Exception:
        raise BenchmarkConfigurationError(
            "numpy>=2,<3 is required for the benchmark; install the performance extra"
        ) from None


def generate_vector_batches(
    config: BenchmarkConfig,
    *,
    batch_size: int | None = None,
) -> Iterator[tuple[int, Any]]:
    """Yield deterministic float32 vector matrices in bounded batches."""

    numpy = require_numpy()
    effective_batch_size = config.batch_size if batch_size is None else batch_size
    _validate_bounded_int(
        effective_batch_size,
        field="batch_size",
        minimum=1,
        maximum=MAX_BATCH_SIZE,
    )
    generator = numpy.random.Generator(numpy.random.PCG64(config.seed))
    for start in range(0, config.chunk_count, effective_batch_size):
        count = min(effective_batch_size, config.chunk_count - start)
        batch = (
            generator.random(
                (count, config.dimension),
                dtype=numpy.float32,
            )
            * numpy.float32(2.0)
            - numpy.float32(1.0)
        ).astype(numpy.float32, copy=False)
        zero_rows = numpy.all(batch == numpy.float32(0.0), axis=1)
        if bool(numpy.any(zero_rows)):
            batch[zero_rows, 0] = numpy.float32(1.0)
        yield start, batch


def _generate_query_array(config: BenchmarkConfig) -> Any:
    numpy = require_numpy()
    query_seed = (config.seed + 1) % (MAX_SEED + 1)
    generator = numpy.random.Generator(numpy.random.PCG64(query_seed))
    queries = (
        generator.random(
            (config.query_count, config.dimension),
            dtype=numpy.float32,
        )
        * numpy.float32(2.0)
        - numpy.float32(1.0)
    ).astype(numpy.float32, copy=False)
    zero_rows = numpy.all(queries == numpy.float32(0.0), axis=1)
    if bool(numpy.any(zero_rows)):
        queries[zero_rows, 0] = numpy.float32(1.0)
    return queries


def generate_vectors(config: BenchmarkConfig) -> Iterator[tuple[str, tuple[float, ...]]]:
    """Yield deterministic dense vectors quantized to PostgreSQL float4 values."""

    for start, batch in generate_vector_batches(config):
        for offset, row in enumerate(batch):
            yield (
                f"m2-benchmark-evidence-{start + offset:010d}",
                tuple(float(value) for value in row),
            )


def generate_queries(config: BenchmarkConfig) -> tuple[tuple[float, ...], ...]:
    """Return deterministic float32-quantized query vectors."""

    return tuple(
        tuple(float(value) for value in row)
        for row in _generate_query_array(config)
    )


def exact_top_k(
    vectors: Iterable[tuple[str, Sequence[float]]],
    query_vector: Sequence[float],
    k: int,
) -> tuple[str, ...]:
    """Rank all supplied vectors in Python as the independent exact ground truth."""

    _validate_bounded_int(k, field="k", minimum=1, maximum=MAX_TOP_K)
    scored = [
        (cosine_score(vector, query_vector), evidence_id)
        for evidence_id, vector in vectors
    ]
    if not scored:
        raise ValueError("vectors must be non-empty")
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(evidence_id for _, evidence_id in scored[:k])


def cosine_score(left: Sequence[float], right: Sequence[float]) -> float:
    """Calculate the exact cosine score used by the independent ground truth."""

    if len(left) != len(right) or not left:
        raise ValueError("vector dimensions must match and be non-empty")
    if any(
        type(value) not in {int, float} or not math.isfinite(float(value))
        for value in (*left, *right)
    ):
        raise ValueError("vectors must contain finite numbers")
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(
        -1.0,
        min(
            1.0,
            sum(float(a) * float(b) for a, b in zip(left, right))
            / (left_norm * right_norm),
        ),
    )


def make_benchmark_identity(seed: int) -> BenchmarkIdentity:
    """Generate a SQL-safe, non-user-controlled benchmark index identity."""

    _validate_bounded_int(seed, field="seed", minimum=0, maximum=MAX_SEED)
    nonce = secrets.randbelow(_MAX_ID_NONCE)
    return BenchmarkIdentity(f"m2_benchmark_{seed}_{nonce}", "v1")


def require_disposable_test_database_url(
    environ: Mapping[str, str] | None = None,
) -> DisposableDatabaseTarget:
    """Return an explicitly confirmed disposable target or fail closed.

    Unlike pytest's live-test helper this function never skips: the CLI must
    return a non-zero result when the guard is absent or unsafe.
    """

    values = os.environ if environ is None else environ
    dsn = values.get(TEST_DATABASE_URL_ENV)
    confirmed_name = values.get(TEST_DATABASE_NAME_ENV)
    if not isinstance(dsn, str) or not isinstance(confirmed_name, str):
        raise BenchmarkConfigurationError(
            f"{TEST_DATABASE_URL_ENV} and {TEST_DATABASE_NAME_ENV} must be set"
        )
    return DisposableDatabaseTarget(dsn, confirmed_name)


def build_search_sql(dimension: int, *, explain: bool = False) -> str:
    """Build the bounded exact query through the shared adapter SQL builder."""

    _validate_bounded_int(
        dimension,
        field="dimension",
        minimum=1,
        maximum=MAX_DIMENSION,
    )
    return build_pgvector_exact_search_sql(dimension, explain=explain)


def explain_search(
    pool: _Pool,
    identity: BenchmarkIdentity,
    *,
    dimension: int,
    query_vector: Sequence[float],
    top_k: int,
) -> dict[str, object]:
    """Run one parameterized ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``."""

    _validate_query_vector(query_vector, dimension)
    _validate_bounded_int(top_k, field="top_k", minimum=1, maximum=MAX_TOP_K)
    vector = _vector_literal(query_vector)
    with pool.connection() as connection:
        row = connection.execute(
            build_search_sql(dimension, explain=True),
            (vector, identity.index_id, identity.index_version, vector, top_k),
        ).fetchone()
    payload = _plan_payload(row)
    return {"summary": _plan_summary(payload), "plan": _json_ready(payload)}


def collect_storage_bytes(pool: _Pool) -> tuple[dict[str, object], dict[str, object]]:
    """Return accurately named heap/table/total and existing index sizes."""

    with pool.connection() as connection:
        relation_row = connection.execute(
            """
            SELECT pg_relation_size(%s::regclass) AS heap_bytes,
                   pg_table_size(%s::regclass) AS table_bytes,
                   pg_total_relation_size(%s::regclass)
                       AS total_relation_bytes
            """,
            (
                M2_VECTOR_DOCUMENTS_TABLE,
                M2_VECTOR_DOCUMENTS_TABLE,
                M2_VECTOR_DOCUMENTS_TABLE,
            ),
        ).fetchone()
        index_rows = connection.execute(
            """
            SELECT indexrelid::regclass::text AS index_name,
                   pg_relation_size(indexrelid) AS bytes
            FROM pg_index
            WHERE indrelid = %s::regclass
            ORDER BY indexrelid::regclass::text
            """,
            (M2_VECTOR_DOCUMENTS_TABLE,),
        ).fetchall()
    if not isinstance(relation_row, Mapping):
        raise BenchmarkExecutionError("PostgreSQL relation size query returned no row")
    relation_bytes = {
        "heap_bytes": _required_int(relation_row, "heap_bytes"),
        "table_bytes": _required_int(relation_row, "table_bytes"),
        "total_relation_bytes": _required_int(
            relation_row,
            "total_relation_bytes",
        ),
    }
    existing_indexes = []
    for row in index_rows:
        if not isinstance(row, Mapping):
            raise BenchmarkExecutionError("PostgreSQL index size query returned an invalid row")
        existing_indexes.append(
            {
                "name": str(row.get("index_name")),
                "bytes": _required_int(row, "bytes"),
            }
        )
    index_bytes = {
        "existing": existing_indexes,
        "benchmark_ann_index_created": False,
        "benchmark_ann_index_name": None,
        "benchmark_ann_index_bytes": None,
    }
    return relation_bytes, index_bytes


def read_environment(
    pool: _Pool,
    target: DisposableDatabaseTarget,
) -> dict[str, object]:
    """Read non-secret server/extension identity for the benchmark report."""

    if type(target) is not DisposableDatabaseTarget:
        raise BenchmarkConfigurationError(
            "read_environment requires a DisposableDatabaseTarget"
        )

    with pool.connection() as connection:
        row = connection.execute(
            """
            SELECT current_database() AS database_name,
                   current_setting('server_version') AS postgres_version,
                   (
                       SELECT extversion
                       FROM pg_extension
                       WHERE extname = 'vector'
                   ) AS pgvector_version
            """
        ).fetchone()
    if not isinstance(row, Mapping):
        raise BenchmarkExecutionError("PostgreSQL environment query returned no row")
    if str(row.get("database_name")) != target.database_name:
        raise BenchmarkConfigurationError(
            "the connected PostgreSQL database does not match the guarded database"
        )
    return {
        "database_guard": "passed",
        "database_name": target.database_name,
        "backend": "postgresql+pgvector",
        "postgres_version": str(row.get("postgres_version")),
        "pgvector_version": (
            None
            if row.get("pgvector_version") is None
            else str(row.get("pgvector_version"))
        ),
        "search_mode": "exact",
        "ann_index_created": False,
    }


def cleanup_benchmark_index(
    pool: _Pool,
    target: DisposableDatabaseTarget,
    identity: BenchmarkIdentity,
) -> None:
    """Delete this run only after revalidating the connected database."""

    if type(target) is not DisposableDatabaseTarget:
        raise BenchmarkConfigurationError(
            "cleanup requires a DisposableDatabaseTarget"
        )

    with pool.connection() as connection:
        with connection.transaction():
            row = connection.execute(
                "SELECT current_database() AS database_name"
            ).fetchone()
            if not isinstance(row, Mapping) or str(row.get("database_name")) != target.database_name:
                raise BenchmarkConfigurationError(
                    "the connected PostgreSQL database does not match the guarded database"
                )
            connection.execute(
                f"DELETE FROM {M2_VECTOR_DOCUMENTS_TABLE} "
                "WHERE index_id = %s AND index_version = %s",
                (identity.index_id, identity.index_version),
            )
            connection.execute(
                "DELETE FROM m2_vector_indexes "
                "WHERE index_id = %s AND index_version = %s",
                (identity.index_id, identity.index_version),
            )


def build_report(
    *,
    config: BenchmarkConfig,
    status: str,
    environment: Mapping[str, object],
    identity: BenchmarkIdentity,
    build_ms: float | None,
    p50_ms: float | None,
    p95_ms: float | None,
    recall_at_k_value: float | None,
    relation_bytes: Mapping[str, object],
    index_bytes: Mapping[str, object],
    explain: Mapping[str, object] | None,
    failure_reasons: Sequence[str],
) -> dict[str, object]:
    """Create the stable JSON object used by local runs and CI logs."""

    environment_value = dict(environment)
    environment_value.setdefault("benchmark_index_id", identity.index_id)
    environment_value.setdefault("benchmark_index_version", identity.index_version)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "environment": _json_ready(environment_value),
        "scale": {
            "profile_id": config.profile_id,
            "chunk_count": config.chunk_count,
            "dimension": config.dimension,
            "query_count": config.query_count,
            "top_k": config.top_k,
            "seed": config.seed,
            "batch_size": config.batch_size,
            "workload_units": config.chunk_count * config.query_count * config.dimension,
            "ann_enabled": config.ann_enabled,
        },
        "build_ms": _finite_or_none(build_ms),
        "p50_ms": _finite_or_none(p50_ms),
        "p95_ms": _finite_or_none(p95_ms),
        "recall_at_k": _finite_or_none(recall_at_k_value),
        "relation_bytes": _json_ready(dict(relation_bytes)),
        "index_bytes": _json_ready(dict(index_bytes)),
        "explain": None if explain is None else _json_ready(dict(explain)),
        "thresholds": {
            "max_p95_ms": config.max_p95_ms,
            "min_recall_at_k": config.min_recall_at_k,
        },
        "passed": status == "passed" and not failure_reasons,
        "failure_reasons": [str(reason) for reason in failure_reasons],
    }


def serialize_report(report: Mapping[str, object]) -> str:
    """Serialize one report without non-standard NaN/Infinity values."""

    return json.dumps(
        _json_ready(dict(report)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class M2PgVectorBenchmark:
    """Run one bounded benchmark against an existing PostgreSQL pool."""

    def __init__(
        self,
        pool: _Pool,
        config: BenchmarkConfig,
        *,
        target: DisposableDatabaseTarget,
        identity: BenchmarkIdentity | None = None,
        store_factory: Callable[[object], _VectorStore] = PostgresPgVectorStore,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if type(target) is not DisposableDatabaseTarget:
            raise BenchmarkConfigurationError(
                "M2PgVectorBenchmark requires a DisposableDatabaseTarget"
            )
        self._pool = pool
        self._config = config
        self._target = target
        self.identity = identity or make_benchmark_identity(config.seed)
        self._store_factory = store_factory
        self._clock_ns = clock_ns

    def run(self) -> dict[str, object]:
        identity = self.identity
        relation_bytes: dict[str, object] = {
            "heap_bytes": None,
            "table_bytes": None,
            "total_relation_bytes": None,
        }
        index_bytes: dict[str, object] = {
            "existing": [],
            "benchmark_ann_index_created": False,
            "benchmark_ann_index_name": None,
            "benchmark_ann_index_bytes": None,
        }
        primary_error: BaseException | None = None
        cleanup_armed = False
        report: dict[str, object] | None = None
        try:
            require_numpy()
            environment = read_environment(self._pool, self._target)
            store = self._store_factory(self._pool)
            store.assert_available()
            queries = generate_queries(self._config)
            ground_truth = _exact_ground_truth(self._config, queries)

            build_started = self._clock_ns()
            cleanup_armed = True
            store.begin(
                identity.index_id,
                identity.index_version,
                dimension=self._config.dimension,
            )
            for start, vector_batch in generate_vector_batches(self._config):
                documents = tuple(
                    VectorDocument(
                        evidence_id=f"m2-benchmark-evidence-{start + offset:010d}",
                        chunk_id=f"m2-benchmark-chunk-{start + offset:010d}",
                        vector=tuple(float(value) for value in row),
                        text_checksum=_DOCUMENT_TEXT_CHECKSUM,
                    )
                    for offset, row in enumerate(vector_batch)
                )
                store.add_many(
                    identity.index_id,
                    identity.index_version,
                    documents,
                )
            store.publish(
                identity.index_id,
                identity.index_version,
                expected_count=self._config.chunk_count,
                checksum=_PUBLISH_CHECKSUM,
            )
            build_ms = (self._clock_ns() - build_started) / 1_000_000.0

            latencies: list[float] = []
            query_recalls: list[float] = []
            for query_vector, expected_ids in zip(queries, ground_truth):
                query_started = self._clock_ns()
                matches = store.search(
                    identity.index_id,
                    identity.index_version,
                    query_vector,
                    top_k=self._config.top_k,
                )
                latencies.append(
                    (self._clock_ns() - query_started) / 1_000_000.0
                )
                query_recalls.append(
                    recall_at_k(
                        [str(match.evidence_id) for match in matches],
                        expected_ids,
                        self._config.top_k,
                    )
                )
            p50_ms = percentile_ms(latencies, 50.0)
            p95_ms = percentile_ms(latencies, 95.0)
            recall_value = sum(query_recalls) / len(query_recalls)
            passed, reasons = evaluate_thresholds(p95_ms, recall_value, self._config)
            explain = explain_search(
                self._pool,
                identity,
                dimension=self._config.dimension,
                query_vector=queries[0],
                top_k=self._config.top_k,
            )
            relation_bytes, index_bytes = collect_storage_bytes(self._pool)
            report = build_report(
                config=self._config,
                status="passed" if passed else "failed",
                environment=environment,
                identity=identity,
                build_ms=build_ms,
                p50_ms=p50_ms,
                p95_ms=p95_ms,
                recall_at_k_value=recall_value,
                relation_bytes=relation_bytes,
                index_bytes=index_bytes,
                explain=explain,
                failure_reasons=reasons,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if cleanup_armed:
                try:
                    cleanup_benchmark_index(self._pool, self._target, identity)
                except Exception as cleanup_error:
                    cleanup_report = None if report is None else dict(report)
                    if cleanup_report is not None:
                        cleanup_report["status"] = "failed"
                        cleanup_report["passed"] = False
                        reasons = list(cleanup_report.get("failure_reasons", []))
                        if primary_error is not None:
                            primary_reason = _safe_failure_reason(primary_error)
                            if primary_reason not in reasons:
                                reasons.append(primary_reason)
                        if "cleanup_failed" not in reasons:
                            reasons.append("cleanup_failed")
                        cleanup_report["failure_reasons"] = reasons
                    raise BenchmarkCleanupError(
                        primary_error=primary_error,
                        report=cleanup_report,
                    ) from cleanup_error
        if report is None:
            raise BenchmarkExecutionError("benchmark did not produce a report")
        return report


def _exact_ground_truth(
    config: BenchmarkConfig,
    queries: Sequence[Sequence[float]],
) -> tuple[tuple[str, ...], ...]:
    numpy = require_numpy()
    query_matrix = numpy.asarray(queries, dtype=numpy.float32)
    if query_matrix.shape != (config.query_count, config.dimension):
        raise BenchmarkExecutionError("query matrix has an invalid shape")
    if not bool(numpy.isfinite(query_matrix).all()):
        raise BenchmarkExecutionError("query matrix contains non-finite values")
    query_norms = numpy.linalg.norm(query_matrix, axis=1).astype(
        numpy.float32,
        copy=False,
    )
    best: list[list[tuple[float, str]]] = [[] for _ in queries]
    for start, vector_batch in generate_vector_batches(config):
        vector_norms = numpy.linalg.norm(vector_batch, axis=1).astype(
            numpy.float32,
            copy=False,
        )
        with numpy.errstate(divide="ignore", invalid="ignore"):
            scores = numpy.divide(
                vector_batch @ query_matrix.T,
                vector_norms[:, None] * query_norms[None, :],
                out=numpy.zeros((len(vector_batch), len(queries)), dtype=numpy.float32),
                where=(vector_norms[:, None] != 0.0)
                & (query_norms[None, :] != 0.0),
            )
        scores = numpy.clip(scores, numpy.float32(-1.0), numpy.float32(1.0))
        for query_index in range(len(queries)):
            local_limit = min(config.top_k, len(vector_batch))
            local_indices = numpy.argsort(
                -scores[:, query_index],
                kind="stable",
            )[:local_limit]
            candidates = best[query_index]
            candidates.extend(
                (
                    float(scores[int(row_index), query_index]),
                    f"m2-benchmark-evidence-{start + int(row_index):010d}",
                )
                for row_index in local_indices
            )
            candidates.sort(key=lambda item: (-item[0], item[1]))
            del candidates[config.top_k:]
    return tuple(
        tuple(evidence_id for _, evidence_id in candidates)
        for candidates in best
    )


def _plan_payload(row: object | None) -> list[object]:
    if isinstance(row, Mapping):
        value = next(iter(row.values()), None)
    elif isinstance(row, (tuple, list)):
        value = row[0] if row else None
    else:
        value = row
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise BenchmarkExecutionError("EXPLAIN returned invalid JSON") from error
    if not isinstance(value, list):
        raise BenchmarkExecutionError("EXPLAIN returned an invalid plan")
    return value


def _plan_summary(payload: Sequence[object]) -> dict[str, object]:
    root: Mapping[str, object] = {}
    planning_ms = 0.0
    execution_ms = 0.0
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        plan = item.get("Plan")
        if isinstance(plan, Mapping) and not root:
            root = plan
        if item.get("Planning Time") is not None:
            planning_ms = float(item["Planning Time"])
        if item.get("Execution Time") is not None:
            execution_ms = float(item["Execution Time"])
    return {
        "root_node_type": str(root.get("Node Type", "unknown")),
        "planning_ms": planning_ms,
        "execution_ms": execution_ms,
        "shared_hit_blocks": int(root.get("Shared Hit Blocks", 0)),
        "shared_read_blocks": int(root.get("Shared Read Blocks", 0)),
        "shared_dirtied_blocks": int(root.get("Shared Dirtied Blocks", 0)),
        "shared_written_blocks": int(root.get("Shared Written Blocks", 0)),
    }


def _validate_query_vector(vector: Sequence[float], dimension: int) -> None:
    _validate_bounded_int(
        dimension,
        field="dimension",
        minimum=1,
        maximum=MAX_DIMENSION,
    )
    if len(vector) != dimension or any(
        type(value) not in {int, float} or not math.isfinite(float(value))
        for value in vector
    ):
        raise ValueError("query vector has invalid dimension or values")


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"


def _database_name_from_dsn(dsn: str) -> str | None:
    try:
        parsed = urlparse(dsn)
        if parsed.scheme in {"postgres", "postgresql"}:
            # Access these parsed properties inside the guarded block: malformed
            # bracketed IPv6 hosts and other URL attributes can raise ValueError.
            _ = parsed.hostname
            path = parsed.path.lstrip("/")
            if path:
                return path
            query_database = parse_qs(parsed.query).get("dbname")
            if query_database:
                return query_database[0]
            return None
    except Exception:
        raise BenchmarkConfigurationError(
            "the PostgreSQL test DSN is malformed"
        ) from None
    if "dbname=" not in dsn:
        return None
    for fragment in dsn.split():
        if fragment.startswith("dbname="):
            value = fragment.partition("=")[2]
            return value or None
    return None


def _validate_disposable_target(dsn: object, database_name: object) -> None:
    if not isinstance(dsn, str) or not dsn.strip() or "\x00" in dsn:
        raise BenchmarkConfigurationError(
            f"{TEST_DATABASE_URL_ENV} must name a disposable PostgreSQL database"
        )
    if (
        not isinstance(database_name, str)
        or not database_name.strip()
        or "\x00" in database_name
    ):
        raise BenchmarkConfigurationError(
            f"{TEST_DATABASE_NAME_ENV} must confirm the disposable database name"
        )
    parsed_name = _database_name_from_dsn(dsn)
    if parsed_name is None:
        raise BenchmarkConfigurationError(
            "the PostgreSQL test DSN must contain a concrete database name"
        )
    if database_name != parsed_name:
        raise BenchmarkConfigurationError(
            f"{TEST_DATABASE_NAME_ENV} does not match the PostgreSQL test DSN"
        )
    lowered = database_name.lower()
    if lowered in _RESERVED_DATABASES:
        raise BenchmarkConfigurationError(
            "the benchmark cannot target a reserved PostgreSQL database"
        )
    if _DISPOSABLE_NAME.search(lowered) is None:
        raise BenchmarkConfigurationError(
            "the PostgreSQL database name must contain test, ci, or tmp"
        )


def _validate_bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")


def _safe_failure_reason(error: BaseException) -> str:
    if isinstance(error, BenchmarkConfigurationError):
        return str(error)
    if isinstance(error, BenchmarkExecutionError):
        return str(error)
    return "PostgreSQL benchmark execution failed"


def failure_reasons_for_error(error: BaseException) -> tuple[str, ...]:
    """Map an exception to non-secret, stable report failure categories."""

    if isinstance(error, BenchmarkCleanupError):
        return error.failure_reasons
    return (_safe_failure_reason(error),)


def _validated_number(value: object, field: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _required_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) not in {int, float} or int(value) != value or int(value) < 0:
        raise BenchmarkExecutionError("PostgreSQL size query returned an invalid value")
    return int(value)


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(float(value)):
        raise BenchmarkExecutionError("benchmark produced a non-finite measurement")
    return float(value)


def _json_ready(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


__all__ = [
    "BenchmarkCleanupError",
    "BenchmarkConfig",
    "BenchmarkConfigurationError",
    "BenchmarkError",
    "BenchmarkExecutionError",
    "BenchmarkIdentity",
    "BenchmarkProfile",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CHUNK_COUNT",
    "DEFAULT_DIMENSION",
    "DEFAULT_MAX_P95_MS",
    "DEFAULT_MIN_RECALL_AT_K",
    "DEFAULT_QUERY_COUNT",
    "DEFAULT_SEED",
    "DEFAULT_TOP_K",
    "TARGET_SCALE_PROFILE",
    "M2PgVectorBenchmark",
    "DisposableDatabaseTarget",
    "MAX_BATCH_SIZE",
    "MAX_CHUNK_COUNT",
    "MAX_DIMENSION",
    "MAX_QUERY_COUNT",
    "MAX_TOP_K",
    "MAX_WORK_UNITS",
    "build_report",
    "build_search_sql",
    "cleanup_benchmark_index",
    "collect_storage_bytes",
    "cosine_score",
    "exact_top_k",
    "evaluate_thresholds",
    "failure_reasons_for_error",
    "explain_search",
    "generate_queries",
    "generate_vector_batches",
    "generate_vectors",
    "make_benchmark_identity",
    "percentile_ms",
    "read_environment",
    "recall_at_k",
    "require_numpy",
    "require_disposable_test_database_url",
    "serialize_report",
]
