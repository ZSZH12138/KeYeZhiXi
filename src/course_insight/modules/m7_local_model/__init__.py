"""M7 local-model module."""

from course_insight.modules.m7_local_model.adapter import (
    DeepSeekM7Adapter,
    GovernedM7Adapter,
    GovernedRubricScoringAdapter,
    PlaceholderRubricAdapter,
    RubricScoringAdapter,
)
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.privacy import (
    DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    M7OutboundPrivacyPolicy,
    OutboundPrivacyResult,
    govern_student_answer,
)
from course_insight.modules.m7_local_model.repository import (
    M7ModelAuditRecord,
    M7Repository,
)
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m7_local_model.stubs import M7LocalModelServiceStub

__all__ = [
    "DeepSeekM7Adapter",
    "DEFAULT_M7_EXECUTION_POLICY",
    "DEFAULT_M7_OUTBOUND_PRIVACY_POLICY",
    "GovernedM7Adapter",
    "GovernedRubricScoringAdapter",
    "M7ExecutionPolicy",
    "M7ModelAuditRecord",
    "M7OutboundPrivacyPolicy",
    "M7LocalModelService",
    "M7LocalModelServiceStub",
    "M7Repository",
    "OutboundPrivacyResult",
    "PlaceholderRubricAdapter",
    "RubricScoringAdapter",
    "govern_student_answer",
]
