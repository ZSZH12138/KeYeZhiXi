from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from course_insight.application.coordinator import AppCoordinator
from course_insight.application.retrieval import retrieve_for_application
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import RetrievalPolicy


def _query() -> SimpleNamespace:
    return SimpleNamespace(query_id="query-1", use_case="grading", top_k=3)


def test_application_retrieval_uses_formal_policy_entrypoint() -> None:
    calls: list[tuple[object, object, object, str | None]] = []

    class FormalM2:
        def retrieve_with_policy(self, query, index, policy, *, request_id=None):
            calls.append((query, index, policy, request_id))
            return "formal-result"

        def retrieve(self, **_kwargs):
            raise AssertionError("legacy retrieval must not be used")

    result = retrieve_for_application(
        FormalM2(), _query(), "index-1", request_id="request-1"
    )

    assert result == "formal-result"
    assert len(calls) == 1
    _, _, policy, request_id = calls[0]
    assert policy.policy_id == "application-lexical-v1"
    assert policy.strategy == "lexical"
    assert policy.top_k == 3
    assert request_id == "request-1"


def test_application_retrieval_forwards_governed_strategy_policy() -> None:
    calls: list[object] = []
    policy = RetrievalPolicy(
        policy_id="production-hybrid-v2",
        strategy="hybrid",
        top_k=3,
        lexical_weight=0.4,
        vector_weight=0.6,
        rerank=False,
    )

    class FormalM2:
        def retrieve_with_policy(self, query, index, forwarded_policy, *, request_id=None):
            del query, index, request_id
            calls.append(forwarded_policy)
            return "formal-result"

    assert retrieve_for_application(
        FormalM2(), _query(), "index-1", policy=policy
    ) == "formal-result"
    assert calls == [policy]


def test_application_retrieval_keeps_legacy_test_double_compatibility() -> None:
    class LegacyM2:
        def retrieve(self, *, evidence_query, evidence_index_ref):
            return (evidence_query, evidence_index_ref)

    query = _query()
    assert retrieve_for_application(LegacyM2(), query, "index-1") == (
        query,
        "index-1",
    )


def test_initialize_course_routes_approved_publication_to_m3() -> None:
    calls: list[dict[str, object]] = []

    class M3:
        def build_knowledge_bundle(self, **_kwargs: object) -> object:
            raise AssertionError("production publication must use an approved review")

        def build_knowledge_bundle_after_approval(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return "approved-bundle"

    coordinator = AppCoordinator(
        SimpleNamespace(initialize=lambda: None),
        SimpleNamespace(import_course=lambda **_kwargs: "course-package"),
        SimpleNamespace(build_index=lambda **_kwargs: "index-ref"),
        M3(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    result = coordinator.initialize_course(
        raw_course_files=[Path("course.md")],
        course_metadata_path=Path("metadata.json"),
        source_authorization_path=None,
        output_dir=Path("runtime"),
        concept_seed_path=Path("concept.json"),
        item_seed_path=Path("item.json"),
        rubric_seed_path=Path("rubric.json"),
        blueprint_seed_path=Path("blueprint.json"),
        prerequisite_seed_path=None,
        misconception_seed_path=None,
        teacher_review_id="review-1",
        teacher_review_version=3,
    )

    assert result == {
        "course_package": "course-package",
        "index_ref": "index-ref",
        "knowledge_bundle": "approved-bundle",
    }
    assert calls[0]["review_id"] == "review-1"
    assert calls[0]["review_version"] == 3


def test_initialize_course_rejects_partial_teacher_review_identity() -> None:
    coordinator = AppCoordinator(
        SimpleNamespace(initialize=lambda: None),
        SimpleNamespace(import_course=lambda **_kwargs: "course-package"),
        SimpleNamespace(build_index=lambda **_kwargs: "index-ref"),
        SimpleNamespace(build_knowledge_bundle=lambda **_kwargs: "bundle"),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    with pytest.raises(DomainError, match="teacher review id and version"):
        coordinator.initialize_course(
            raw_course_files=[],
            course_metadata_path=Path("metadata.json"),
            source_authorization_path=None,
            output_dir=Path("runtime"),
            concept_seed_path=Path("concept.json"),
            item_seed_path=Path("item.json"),
            rubric_seed_path=Path("rubric.json"),
            blueprint_seed_path=Path("blueprint.json"),
            prerequisite_seed_path=None,
            misconception_seed_path=None,
            teacher_review_id="review-1",
        )
