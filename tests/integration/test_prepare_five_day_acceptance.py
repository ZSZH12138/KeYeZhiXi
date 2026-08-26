from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from course_insight.contracts.learning_models import (
    ConceptResponseSequence,
    LearningObservation,
)
from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    load_dataset,
    split_by_group,
)
from scripts.prepare_five_day_acceptance import build_data_pack, refresh_manifest
from scripts.verify_five_day_acceptance import verify_static_pack


REQUIRED_TEXT_OUTPUTS = {
    "README.md",
    "courseware/01_network_layers.md",
    "courseware/02_ip_addressing.txt",
    "governance/course_metadata.json",
    "governance/source_authorization.csv",
    "governance/source_authorization_tampered.csv",
    "seeds/concept.json",
    "seeds/item.json",
    "seeds/rubric.json",
    "seeds/blueprint.json",
    "seeds/prerequisite.json",
    "seeds/misconception.json",
    "scenarios/intent_examples.jsonl",
    "scenarios/manual_student_answers.json",
    "scenarios/tutoring_state_paths.json",
    "scenarios/privacy_and_subjective_answers.json",
    "scenarios/teacher_review_cases.json",
    "model_data/learning_observations.jsonl",
    "model_data/bkt_sequences.jsonl",
    "model_data/learner_profiles.csv",
    "expected/coverage_matrix.csv",
    "expected/expected_results.json",
    "manifest.json",
}


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_data_pack_is_complete_deterministic_and_contract_valid(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_manifest = build_data_pack(first)
    second_manifest = build_data_pack(second)

    assert REQUIRED_TEXT_OUTPUTS <= {
        path.relative_to(first).as_posix()
        for path in first.rglob("*")
        if path.is_file()
    }
    assert first_manifest["schema_version"] == 1
    assert first_manifest["file_checksums"] == second_manifest["file_checksums"]
    for relative, expected_sha256 in first_manifest["file_checksums"].items():
        payload = (first / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256

    intent_path = first / "scenarios" / "intent_examples.jsonl"
    dataset = load_dataset(intent_path)
    partitions = split_by_group(dataset.examples, seed=20260824)
    assert {item.label for item in dataset.examples} == set(EXPECTED_LABELS)
    for partition in (partitions.train, partitions.validation, partitions.test):
        assert {item.label for item in partition} == set(EXPECTED_LABELS)

    observations = _jsonl(first / "model_data" / "learning_observations.jsonl")
    assert len({row["learner_id"] for row in observations}) == 200
    item_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for row in observations:
        observation = LearningObservation.model_validate(row)
        observation.validate_business_rules()
        item_outcomes[observation.item_id][observation.response_outcome] += 1
    assert len(item_outcomes) >= 16
    assert all(sum(counts.values()) >= 50 for counts in item_outcomes.values())
    assert all(set(counts) == {"correct", "incorrect"} for counts in item_outcomes.values())

    sequences = _jsonl(first / "model_data" / "bkt_sequences.jsonl")
    students_per_concept: dict[str, set[str]] = defaultdict(set)
    for row in sequences:
        sequence = ConceptResponseSequence.model_validate(row)
        sequence.validate_business_rules()
        students_per_concept[sequence.concept_id].add(sequence.learner_id)
        assert len(sequence.responses) >= 5
    assert len(students_per_concept) >= 4
    assert all(len(learners) >= 100 for learners in students_per_concept.values())

    concepts = json.loads((first / "seeds" / "concept.json").read_text(encoding="utf-8"))
    items = json.loads((first / "seeds" / "item.json").read_text(encoding="utf-8"))
    rubrics = json.loads((first / "seeds" / "rubric.json").read_text(encoding="utf-8"))
    blueprints = json.loads((first / "seeds" / "blueprint.json").read_text(encoding="utf-8"))
    assert len(concepts["concepts"]) == 8
    assert len(items["items"]) >= 24
    assert len(items["q_matrix"]) >= len(items["items"])
    assert len(rubrics["rubrics"]) >= 2
    assert {rubric["status"] for rubric in rubrics["rubrics"]} == {"published"}
    assert len(blueprints["blueprints"]) >= 3

    retrieval_queries = json.loads(
        (first / "scenarios" / "retrieval_queries.json").read_text(encoding="utf-8")
    )["queries"]
    no_match = next(row for row in retrieval_queries if row["query_id"] == "q_no_match")
    assert no_match["expected"] == "low_relevance_or_empty"
    assert no_match["text"] == "higgsboson_xqz_987"

    with (first / "expected" / "coverage_matrix.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        coverage = list(csv.DictReader(source))
    assert {row["module"] for row in coverage} == {f"M{index}" for index in range(10)}
    assert all(row["feature_id"] and row["expected_result"] for row in coverage)
    for row in coverage:
        if row["external_data_required"] == "yes":
            assert row["data_paths"]
            assert all((first / item).exists() for item in row["data_paths"].split("|"))


def test_model_scale_data_is_pseudonymous_and_reproducible(tmp_path: Path) -> None:
    output = tmp_path / "pack"
    build_data_pack(output)

    observations = (output / "model_data" / "learning_observations.jsonl").read_text(
        encoding="utf-8"
    )
    profiles = (output / "model_data" / "learner_profiles.csv").read_text(
        encoding="utf-8-sig"
    )
    forbidden = ("@", "13800138000", "张三", "李四", "password", "secret")
    assert all(token not in observations for token in forbidden)
    assert all(token not in profiles for token in forbidden)
    assert "learner_0001" in observations
    assert "learner_0200" in observations


def test_static_verifier_confirms_m0_through_m9_coverage(tmp_path: Path) -> None:
    output = tmp_path / "pack"
    build_data_pack(output)

    report = verify_static_pack(output)

    assert report["status"] == "passed"
    assert set(report["modules"]) == {f"M{index}" for index in range(10)}
    assert report["checks"]["manifest_checksums"] == "passed"
    assert report["checks"]["external_data_paths"] == "passed"


def test_static_verifier_rejects_pptx_with_inconsistent_slide_metadata(
    tmp_path: Path,
) -> None:
    output = tmp_path / "pack"
    build_data_pack(output)
    pptx = output / "courseware" / "05_fault_diagnosis.pptx"
    with ZipFile(pptx, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "docProps/app.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Test exporter</Application>
  <Slides>0</Slides>
</Properties>""",
        )
        archive.writestr(
            "ppt/presentation.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>""",
        )
    refresh_manifest(output)

    with pytest.raises(ValueError, match="PPTX slide metadata mismatch"):
        verify_static_pack(output)
