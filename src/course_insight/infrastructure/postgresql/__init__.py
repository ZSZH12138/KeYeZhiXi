"""PostgreSQL infrastructure adapters for the existing module protocols."""

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresError,
    PostgresMigrationError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
)
from course_insight.infrastructure.postgresql.m4_repository import (
    PostgresM4Repository,
)
from course_insight.infrastructure.postgresql.m5_repository import (
    PostgresM5Repository,
)
from course_insight.infrastructure.postgresql.m6_repository import (
    PostgresM6Repository,
)
from course_insight.infrastructure.postgresql.m7_repository import (
    PostgresM7Repository,
)
from course_insight.infrastructure.postgresql.m8_repository import (
    PostgresM8Repository,
)
from course_insight.infrastructure.postgresql.m9_repository import (
    PostgresM9Repository,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    MigrationReport,
    current_schema_version,
    run_migrations,
    schema_is_current,
)
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)

__all__ = [
    "MigrationReport",
    "PostgresConnectionError",
    "PostgresError",
    "PostgresM0Repository",
    "PostgresM4Repository",
    "PostgresM5Repository",
    "PostgresM6Repository",
    "PostgresM7Repository",
    "PostgresM8Repository",
    "PostgresM9Repository",
    "PostgresMigrationError",
    "PostgresOperationError",
    "PostgresPool",
    "create_postgres_pool",
    "current_schema_version",
    "run_migrations",
    "schema_is_current",
]
