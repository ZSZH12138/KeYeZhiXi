from __future__ import annotations

from inspect import signature
from pathlib import Path

from course_insight.contracts.provenance import load_contract_provenance
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m1_course_governance.service import M1CourseGovernanceService
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m6_tutoring_fsm.service import M6TutoringControlService
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


PROVENANCE_PATH = Path("contracts/contract_provenance.json")
SERVICE_TYPES = {
    "M0": M0PlatformService,
    "M1": M1CourseGovernanceService,
    "M2": M2EvidenceRetrievalService,
    "M3": M3KnowledgeBundleService,
    "M4": M4TaskOrchestrationService,
    "M5": M5StateService,
    "M6": M6TutoringControlService,
    "M7": M7LocalModelService,
    "M8": M8AssessmentService,
    "M9": M9TeacherAnalyticsService,
}


def test_internal_provenance_references_public_service_boundaries() -> None:
    graph = load_contract_provenance(PROVENANCE_PATH)
    graph.assert_valid()
    missing_methods: list[str] = []
    missing_parameters: list[str] = []

    for flow in graph.contracts.values():
        for producer in flow.producers:
            owner, separator, method_name = producer.method.partition(".")
            if not separator or owner not in SERVICE_TYPES:
                continue
            if not hasattr(SERVICE_TYPES[owner], method_name):
                missing_methods.append(producer.method)
        for consumer in flow.consumers:
            owner, separator, method_name = consumer.method.partition(".")
            if not separator or owner not in SERVICE_TYPES:
                continue
            if not hasattr(SERVICE_TYPES[owner], method_name):
                missing_methods.append(consumer.method)
                continue
            parameters = signature(
                getattr(SERVICE_TYPES[owner], method_name)
            ).parameters
            if consumer.parameter not in parameters:
                missing_parameters.append(
                    f"{consumer.method}:{consumer.parameter}"
                )

    assert sorted(set(missing_methods)) == []
    assert sorted(set(missing_parameters)) == []
