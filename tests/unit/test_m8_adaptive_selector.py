"""Unit and persistence tests for constrained adaptive item selection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from course_insight.contracts import learning_models
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import ItemCard
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    IRTItemParameters,
    IRTParameterSet,
)
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService


NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)


def _selector():
    from course_insight.modules.m8_assessment_scoring.adaptive_selector import (
        AdaptiveItemSelector,
    )

    return AdaptiveItemSelector()


def _exposure(*, counts: dict[str, int] | None = None, total: int = 100):
    snapshot_type = getattr(learning_models, "ItemExposureSnapshot")
    return snapshot_type(
        parameter_set_id="irt_approved_adaptive",
        total_sessions=total,
        item_administered_counts=counts or {},
    )


def _item(
    item_id: str,
    *,
    concept_id: str = "concept_free",
    version: str = "1.0.0",
) -> ItemCard:
    return ItemCard(
        item_id=item_id,
        version=version,
        stem=f"Question {item_id}",
        item_type="multiple_choice",
        concept_ids=[concept_id],
        misconception_ids=[],
        difficulty_level=2,
        cognitive_level="apply",
        parameter_rules=[],
        answer_key={"answer": "A", "max_score": 1.0},
        rubric_id=None,
        source_evidence_ids=[f"evidence_{item_id}"],
        status="teacher_approved",
    )


def _parameter_set(*items: tuple[str, float, float]) -> IRTParameterSet:
    return IRTParameterSet(
        parameter_set_id="irt_approved_adaptive",
        model_type="2PL",
        version="irt-adaptive-v1",
        item_parameters=[
            IRTItemParameters(
                item_id=item_id,
                item_version="1.0.0",
                discrimination=discrimination,
                difficulty=difficulty,
                guessing=0.0,
                sample_size=300,
            )
            for item_id, discrimination, difficulty in items
        ],
        sample_size=300,
        status="approved",
        created_at=NOW,
    )


def _ability() -> AbilityEstimate:
    return AbilityEstimate(
        estimate_id="ability_adaptive",
        learner_id="learner_1",
        parameter_set_id="irt_approved_adaptive",
        theta=0.0,
        standard_error=1.0,
        status="estimated",
        estimated_at=NOW,
    )


def _policy(
    *,
    max_items: int = 1,
    quotas: dict[str, int] | None = None,
    max_exposure: float = 0.20,
) -> AdaptiveSelectionPolicy:
    return AdaptiveSelectionPolicy(
        policy_id="adaptive_policy_1",
        version="1.0.0",
        parameter_set_id="irt_approved_adaptive",
        max_items=max_items,
        concept_quotas=quotas or {},
        max_item_exposure_rate=max_exposure,
        difficulty_range=(-4.0, 4.0),
        status="configured",
    )


def _select(
    *,
    policy: AdaptiveSelectionPolicy,
    parameter_set: IRTParameterSet,
    items: list[ItemCard],
    administered: frozenset[str] = frozenset(),
    exposure=None,
):
    return _selector().select(
        policy=policy,
        ability_estimate=_ability(),
        parameter_set=parameter_set,
        candidate_items=items,
        administered_item_ids=administered,
        exposure_snapshot=exposure or _exposure(),
        requested_at=NOW,
    )


def test_selects_highest_information_available_item() -> None:
    """Catch ranking by difficulty label instead of 2PL Fisher information."""

    parameters = _parameter_set(
        ("highest_information", 1.8, 0.0),
        ("far_from_theta", 1.0, 3.0),
    )
    result = _select(
        policy=_policy(),
        parameter_set=parameters,
        items=[_item("far_from_theta"), _item("highest_information")],
    )

    assert result.status == "selected"
    assert result.item_ids == ["highest_information"]
    assert result.ability_estimate == _ability()


def test_never_repeats_an_administered_item() -> None:
    """Catch selecting a high-information item already seen by the learner."""

    parameters = _parameter_set(
        ("already_seen", 2.0, 0.0),
        ("new_item", 1.0, 0.0),
    )
    result = _select(
        policy=_policy(),
        parameter_set=parameters,
        items=[_item("already_seen"), _item("new_item")],
        administered=frozenset({"already_seen"}),
    )

    assert result.item_ids == ["new_item"]


def test_administered_history_may_be_absent_from_current_candidate_pool() -> None:
    """Catch rejecting a pool that has already removed previously seen items."""

    parameters = _parameter_set(("new_item", 1.0, 0.0))
    result = _select(
        policy=_policy(),
        parameter_set=parameters,
        items=[_item("new_item")],
        administered=frozenset({"old_item_not_in_pool"}),
    )

    assert result.item_ids == ["new_item"]


def test_rejects_shadow_parameters() -> None:
    """Catch an unreviewed shadow model influencing learner decisions."""

    parameters = _parameter_set(("item_1", 1.0, 0.0)).model_copy(
        update={"status": "shadow"}
    )

    with pytest.raises(DomainError, match="approved"):
        _select(
            policy=_policy(),
            parameter_set=parameters,
            items=[_item("item_1")],
        )


def test_meets_concept_quotas_before_free_selection() -> None:
    """Catch global information ranking starving a required concept."""

    parameters = _parameter_set(
        ("free_high", 2.5, 0.0),
        ("quota_lower", 0.8, 0.0),
    )
    result = _select(
        policy=_policy(max_items=2, quotas={"concept_quota": 1}),
        parameter_set=parameters,
        items=[
            _item("free_high"),
            _item("quota_lower", concept_id="concept_quota"),
        ],
    )

    assert result.item_ids == ["quota_lower", "free_high"]


def test_finds_a_feasible_multi_concept_combination_before_failing() -> None:
    """Catch greedy ranking exhausting slots while a valid set still exists."""

    parameters = _parameter_set(
        ("concept_a_high", 2.5, 0.0),
        ("concept_b_high", 2.0, 0.0),
        ("concept_b_c_lower", 0.8, 0.0),
    )
    multi_concept_item = _item(
        "concept_b_c_lower",
        concept_id="concept_b",
    ).model_copy(update={"concept_ids": ["concept_b", "concept_c"]})

    result = _select(
        policy=_policy(
            max_items=2,
            quotas={"concept_a": 1, "concept_b": 1, "concept_c": 1},
        ),
        parameter_set=parameters,
        items=[
            _item("concept_a_high", concept_id="concept_a"),
            _item("concept_b_high", concept_id="concept_b"),
            multi_concept_item,
        ],
    )

    assert result.status == "selected"
    assert result.item_ids == ["concept_a_high", "concept_b_c_lower"]


def test_same_input_has_deterministic_tie_breaking() -> None:
    """Catch input order changing equal-information selection."""

    parameters = _parameter_set(
        ("item_b", 1.0, 0.0),
        ("item_a", 1.0, 0.0),
    )
    first = _select(
        policy=_policy(),
        parameter_set=parameters,
        items=[_item("item_b"), _item("item_a")],
    )
    second = _select(
        policy=_policy(),
        parameter_set=parameters,
        items=[_item("item_a"), _item("item_b")],
    )

    assert first == second
    assert first.item_ids == ["item_a"]


def test_respects_item_exposure_cap() -> None:
    """Catch repeatedly selecting an item already at the exposure limit."""

    parameters = _parameter_set(
        ("overexposed", 2.0, 0.0),
        ("available", 1.0, 0.0),
    )
    result = _select(
        policy=_policy(max_exposure=0.20),
        parameter_set=parameters,
        items=[_item("overexposed"), _item("available")],
        exposure=_exposure(counts={"overexposed": 20, "available": 19}),
    )

    assert result.item_ids == ["available"]


def test_pool_exhaustion_returns_explicit_failure() -> None:
    """Catch fabricating items when no candidate can meet a required quota."""

    parameters = _parameter_set(("free_item", 1.0, 0.0))
    result = _select(
        policy=_policy(quotas={"missing_concept": 1}),
        parameter_set=parameters,
        items=[_item("free_item")],
    )

    assert result.status == "failed"
    assert result.item_ids == []
    assert result.failure_code == "ADAPTIVE_POOL_EXHAUSTED"


def test_service_persists_selection_for_restart_recovery(tmp_path: Path) -> None:
    """Catch adaptive decisions existing only in process memory."""

    database_path = tmp_path / "adaptive.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    parameters = _parameter_set(("item_1", 1.0, 0.0))
    repository.insert_or_get_parameter_set(parameters, course_id="course_1")
    service = M8AssessmentService(
        repository=repository,
        rule_scorer=object(),
        parameter_item_generator=object(),
    )

    selected = service.select_adaptive_items(
        policy=_policy(),
        ability_estimate=_ability(),
        parameter_set=parameters,
        candidate_items=[_item("item_1")],
        administered_item_ids=frozenset(),
        exposure_snapshot=_exposure(),
        requested_at=NOW,
    )

    restarted_repository = SQLiteM8Repository(database_path)
    restarted_repository.initialize()
    assert restarted_repository.get_adaptive_selection(selected.selection_id) == (
        selected
    )


def test_policy_defaults_preserve_existing_callers() -> None:
    """Catch requiring newly added exposure fields from old configured payloads."""

    policy = AdaptiveSelectionPolicy(
        policy_id="legacy_policy",
        version="1.0.0",
        parameter_set_id="irt_approved_adaptive",
        max_items=1,
        concept_quotas={},
        status="configured",
    )

    assert policy.max_item_exposure_rate == pytest.approx(0.20)
    assert policy.difficulty_range == (-4.0, 4.0)


def test_service_adaptive_runtime_guards_and_lightweight_repository_paths(
    tmp_path: Path,
) -> None:
    """Configured selection fails closed, while legacy empty probes stay empty."""

    repository = SQLiteM8Repository(tmp_path / "adaptive-guards.sqlite3")
    repository.initialize()
    service = M8AssessmentService(repository, object(), object())
    configured = _policy()
    parameters = _parameter_set(("item_1", 1.0, 0.0))

    with pytest.raises(ValueError, match="requested_at"):
        service.select_adaptive_items(configured, _ability())

    empty = service.select_adaptive_items(
        AdaptiveSelectionPolicy(
            policy_id="empty_policy",
            version="1.0.0",
            parameter_set_id=None,
            max_items=1,
            concept_quotas={},
            status="empty",
        ),
        _ability(),
        requested_at=NOW,
    )
    assert empty.status == "empty"

    with pytest.raises(DomainError) as missing_inputs:
        service.select_adaptive_items(
            configured,
            _ability(),
            requested_at=NOW,
        )
    assert missing_inputs.value.code == "ADAPTIVE_INPUT_MISSING"

    with pytest.raises(DomainError) as missing_parameters:
        service.select_adaptive_items(
            configured,
            _ability(),
            parameter_set=parameters,
            candidate_items=[_item("item_1")],
            exposure_snapshot=_exposure(),
            requested_at=NOW,
        )
    assert missing_parameters.value.code == "IRT_PARAMETER_SET_NOT_FOUND"

    class ReadOnlyRuntimeRepository:
        def get_parameter_set_course_id(self, _parameter_set_id):
            return "course_1"

    read_only = M8AssessmentService(ReadOnlyRuntimeRepository(), object(), object())
    selected = read_only.select_adaptive_items(
        configured,
        _ability(),
        parameter_set=parameters,
        candidate_items=[_item("item_1")],
        exposure_snapshot=_exposure(),
        requested_at=NOW,
    )
    assert selected.status == "selected"

    class AlteringRuntimeRepository(ReadOnlyRuntimeRepository):
        def insert_or_get_adaptive_selection(self, selection, *, course_id):
            return selection.model_copy(update={"selection_id": "altered_selection"})

    altering = M8AssessmentService(AlteringRuntimeRepository(), object(), object())
    with pytest.raises(RuntimeError, match="adaptive-selection persistence conflict"):
        altering.select_adaptive_items(
            configured,
            _ability(),
            parameter_set=parameters,
            candidate_items=[_item("item_1")],
            exposure_snapshot=_exposure(),
            requested_at=NOW,
        )
