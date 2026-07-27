"""Internal M0 configuration and role-seed surface."""

from course_insight.infrastructure.config.errors import (
    ConfigurationError,
    RoleSeedError,
)
from course_insight.infrastructure.config.loader import load_platform_settings
from course_insight.infrastructure.config.models import (
    DatabaseSettings,
    IntentSettings,
    LoggingSettings,
    OutboxSettings,
    PlatformSettings,
    SecuritySettings,
    WebSettings,
)
from course_insight.infrastructure.config.roles import (
    RoleGrantSeed,
    RoleSeedDocument,
    RoleSyncPlan,
    build_role_sync_plan,
    load_role_seeds,
)

__all__ = [
    "ConfigurationError",
    "DatabaseSettings",
    "IntentSettings",
    "LoggingSettings",
    "OutboxSettings",
    "PlatformSettings",
    "RoleGrantSeed",
    "RoleSeedDocument",
    "RoleSeedError",
    "RoleSyncPlan",
    "SecuritySettings",
    "WebSettings",
    "build_role_sync_plan",
    "load_platform_settings",
    "load_role_seeds",
]
