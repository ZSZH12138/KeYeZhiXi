"""Student mastery profile is built only from confirmed learner state."""

from __future__ import annotations

from course_insight.contracts.knowledge import KnowledgeBundle, MisconceptionTag
from course_insight.contracts.state import (
    ConceptState,
    LearnerStateSnapshot,
    MisconceptionStrength,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    student_profile_view,
)
from tests.factories.m5_m8 import UTC_TIME, make_knowledge_bundle


def _thresholds() -> dict[str, float]:
    return {
        "mastered_threshold": 0.8,
        "consolidating_threshold": 0.4,
        "misconception_activation_threshold": 0.5,
    }


def test_student_profile_is_empty_without_confirmed_state() -> None:
    profile = student_profile_view(
        snapshot=None,
        knowledge_bundle=make_knowledge_bundle(),
        **_thresholds(),
    )

    assert profile.ready is False
    assert profile.mastered == ()
    assert profile.consolidating == ()
    assert profile.priority_support == ()
    assert "还没有" in profile.message


def test_student_profile_groups_concepts_and_shows_active_misconceptions() -> None:
    bundle = make_knowledge_bundle()
    concept = bundle.concepts[0]
    tagged = bundle.model_copy(
        update={
            "misconception_tags": [
                MisconceptionTag(
                    misconception_id="mis_double_colon",
                    name="重复使用双冒号",
                    description="IPv6 compact form used twice",
                    concept_ids=[concept.concept_id],
                    evidence_rules=[],
                )
            ]
        }
    )
    snapshot = LearnerStateSnapshot(
        snapshot_id="snapshot_1",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_student_001",
        state_version=1,
        concept_states=[
            ConceptState(
                concept_id=concept.concept_id,
                mastery_probability=0.2,
                mastery_confidence=0.8,
                misconceptions=[
                    MisconceptionStrength(
                        misconception_id="mis_double_colon",
                        strength=0.9,
                        evidence_count=2,
                        last_seen_at=UTC_TIME,
                    )
                ],
                hint_dependency=0.1,
                recent_correction_rate=0.0,
                evidence_count=3,
                updated_at=UTC_TIME,
            )
        ],
        overall_mastery=0.2,
        evidence_count=3,
        updated_at=UTC_TIME,
    )

    profile = student_profile_view(
        snapshot=snapshot,
        knowledge_bundle=tagged,
        **_thresholds(),
    )

    assert profile.ready is True
    assert [item.concept_id for item in profile.priority_support] == [
        concept.concept_id
    ]
    assert profile.priority_support[0].misconceptions == ("重复使用双冒号",)
    assert profile.priority_support[0].source_locator == concept.chapter_id
    assert profile.next_practice_concept_ids == (concept.concept_id,)
