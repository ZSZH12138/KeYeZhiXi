"""Deterministic zero-argument M5 service stub."""

from typing import cast

from course_insight.modules.m5_learner_class_state.repository import M5Repository
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)


class M5StateServiceStub(M5StateService):
    """Instantiate M5 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(
            cast(M5Repository, object()),
            DeterministicStateUpdatePolicy(),
            DeterministicClassAggregationPolicy(),
        )
