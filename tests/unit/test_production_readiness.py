from __future__ import annotations

from scripts.check_production_readiness import check_production_settings


def test_production_readiness_requires_postgresql_and_secure_defaults() -> None:
    report = check_production_settings(
        {
            "environment": "production",
            "database_backend": "postgresql",
            "pgvector_enabled": True,
            "secure_cookies": True,
            "allowed_hosts": ["course.example.edu"],
            "migrations_applied": True,
            "m6_mode": "rules",
            "m6_rollout_percentage": 0.0,
        }
    )
    assert report["status"] == "passed"


def test_production_readiness_blocks_sqlite_and_nonzero_m6_rollout() -> None:
    report = check_production_settings(
        {
            "environment": "production",
            "database_backend": "sqlite",
            "m6_rollout_percentage": 0.1,
        }
    )
    assert report["status"] == "blocked"
    assert "database_must_be_postgresql" in report["reasons"]
    assert "m6_rollout_must_be_zero" in report["reasons"]

