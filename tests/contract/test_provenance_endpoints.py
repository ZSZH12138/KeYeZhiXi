from __future__ import annotations

import json
from inspect import signature
from pathlib import Path

from course_insight.contracts.assessment import ScoringPreparationResult
from course_insight.contracts.evidence import (
    EvidenceBundle,
    EvidenceIndexRef,
    EvidenceQuery,
)
from course_insight.contracts.intelligence import ArchitectureScaffoldResult
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    KnowledgeBundle,
)
from course_insight.contracts.provenance import load_contract_provenance
from course_insight.contracts.tutoring import TutoringControlResult
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
SCHEMA_PATH = Path("contracts/schemas")
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


def test_m6_public_signature_and_the_84_schema_surface_are_frozen() -> None:
    """Catch private policy learning leaking into the public contract surface."""

    parameters = signature(
        M6TutoringControlService.decide_next_action
    ).parameters

    assert tuple(parameters) == (
        "self",
        "task_plan",
        "scoring_result_bundle",
        "state_update_result",
        "previous_session_state_snapshot",
    )
    assert len(tuple(SCHEMA_PATH.glob("*.schema.json"))) == 84


def test_evidence_binding_schemas_match_models_and_keep_new_fields_optional() -> None:
    """Keep additive M2 bindings schema-compatible through M6/M8 consumers."""

    contract_types = (
        EvidenceIndexRef,
        EvidenceQuery,
        EvidenceBundle,
        ScoringPreparationResult,
        TutoringControlResult,
        ArchitectureScaffoldResult,
    )
    schemas: dict[str, dict[str, object]] = {}
    for contract_type in contract_types:
        path = SCHEMA_PATH / f"{contract_type.__name__}.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema == contract_type.model_json_schema(mode="validation")
        schemas[contract_type.__name__] = schema

    direct_optional_fields = {
        "EvidenceIndexRef": ("course_package_checksum",),
        "EvidenceQuery": (
            "course_package_checksum",
            "required_evidence_ids",
        ),
        "EvidenceBundle": (
            "course_package_id",
            "course_package_checksum",
            "index_checksum",
        ),
    }
    for schema_name, field_names in direct_optional_fields.items():
        required = schemas[schema_name].get("required", [])
        assert isinstance(required, list)
        assert not set(field_names) & set(required)

    nested_definitions = {
        "ScoringPreparationResult": (
            "EvidenceQuery",
            ("course_package_checksum", "required_evidence_ids"),
        ),
        "TutoringControlResult": (
            "EvidenceQuery",
            ("course_package_checksum", "required_evidence_ids"),
        ),
        "ArchitectureScaffoldResult": (
            "EvidenceIndexRef",
            ("course_package_checksum",),
        ),
    }
    for schema_name, (definition_name, field_names) in nested_definitions.items():
        definitions = schemas[schema_name].get("$defs", {})
        assert isinstance(definitions, dict)
        definition = definitions[definition_name]
        assert isinstance(definition, dict)
        required = definition.get("required", [])
        assert isinstance(required, list)
        assert not set(field_names) & set(required)


def test_m3_binding_schemas_match_models_and_keep_new_fields_optional() -> None:
    """Keep additive M3 bindings compatible with M4 and M8 consumers."""

    contract_types = (KnowledgeBundle, BlueprintSection, AssessmentBlueprint)
    schemas: dict[str, dict[str, object]] = {}
    for contract_type in contract_types:
        path = SCHEMA_PATH / f"{contract_type.__name__}.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema == contract_type.model_json_schema(mode="validation")
        schemas[contract_type.__name__] = schema

    knowledge_required = schemas["KnowledgeBundle"].get("required", [])
    assert isinstance(knowledge_required, list)
    assert not {
        "course_package_checksum",
        "concept_evidence_ids",
    } & set(knowledge_required)

    blueprint_section_required = schemas["BlueprintSection"].get("required", [])
    assert isinstance(blueprint_section_required, list)
    assert "anchor_item_versions" not in blueprint_section_required

    definitions = schemas["AssessmentBlueprint"].get("$defs", {})
    assert isinstance(definitions, dict)
    section_definition = definitions["BlueprintSection"]
    assert isinstance(section_definition, dict)
    nested_required = section_definition.get("required", [])
    assert isinstance(nested_required, list)
    assert "anchor_item_versions" not in nested_required
