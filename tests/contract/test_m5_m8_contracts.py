"""M5/M8 authoritative evidence contract tests."""

from course_insight.contracts.learning_models import LearningObservation
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from tests.factories.m5_m8 import (
    UTC_TIME,
    make_knowledge_bundle,
    make_paper,
    make_rubric,
    make_submission,
)


class _RecordRepository:
    def __init__(self, record: FrozenAssessmentRecord) -> None:
        self.record = record

    def get_paper_record(self, paper_id: str) -> FrozenAssessmentRecord | None:
        return self.record if paper_id == self.record.paper.paper_id else None


def test_old_paper_uses_its_frozen_rubric_version() -> None:
    paper = make_paper(subjective=True)
    record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric(version="1.0.0")],
    )
    service = M8AssessmentService(
        _RecordRepository(record),
        RuleScorer(),
        object(),
    )

    preparation = service.prepare_scoring(
        paper,
        make_submission(paper),
        make_knowledge_bundle(rubric_version="2.0.0"),
    )

    assert preparation.rubric_scoring_tasks[0].rubric.version == "1.0.0"


def test_learning_observation_roundtrip_keeps_binary_policy() -> None:
    observation = LearningObservation(
        observation_id="obs_1",
        learner_id="learner_1",
        course_id="course_1",
        class_id="class_1",
        attempt_id="attempt_1",
        item_id="item_2",
        item_version="1.0.0",
        concept_ids=["concept_2"],
        score=1.0,
        max_score=1.0,
        response_outcome="correct",
        outcome_policy_version="1.0.0",
        source_audit_id="audit_1",
        source_audit_version=1,
        occurred_at=UTC_TIME,
    )

    restored = LearningObservation.model_validate(
        observation.model_dump(mode="json")
    )

    assert restored == observation


def test_m5_m8_public_modules_export_only_real_service_boundaries() -> None:
    """Catch test-only placeholder services leaking into production imports."""

    from course_insight.modules import m5_learner_class_state as m5
    from course_insight.modules import m8_assessment_scoring as m8

    assert m5.__all__ == ["M5Repository", "M5StateService"]
    assert m8.__all__ == ["M8AssessmentService", "M8Repository"]
    assert not hasattr(m5, "M5StateServiceStub")
    assert not hasattr(m8, "M8AssessmentServiceStub")
