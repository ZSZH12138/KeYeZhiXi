from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.django_app.error_mapping import (
    domain_error_status,
)


def test_missing_grading_evidence_is_not_reported_as_an_invalid_request() -> None:
    error = DomainError(
        code="EVIDENCE_REQUIRED",
        module="m7",
        message="governed grading requires course evidence",
        recoverable=True,
    )

    assert domain_error_status(error) == 503


def test_class_aggregation_failure_is_not_reported_as_an_invalid_request() -> None:
    error = DomainError(
        code="CLASS_AGGREGATION_INVALID",
        module="m5",
        message="assessed learners exceed the configured class size",
        recoverable=True,
    )

    assert domain_error_status(error) == 503


def test_dynamic_class_roster_errors_have_stable_safe_statuses() -> None:
    invalid = DomainError(
        code="CLASS_ROSTER_INVALID",
        module="m0",
        message="class roster is empty",
        recoverable=True,
    )
    unavailable = DomainError(
        code="CLASS_ROSTER_UNAVAILABLE",
        module="m0",
        message="class roster store is unavailable",
        recoverable=True,
    )

    assert domain_error_status(invalid) == 409
    assert domain_error_status(unavailable) == 503
