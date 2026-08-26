from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.knowledge import QMatrixEntry
from course_insight.contracts.learning_models import (
    ConceptResponseSequence,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.m2_file_repository import FileM2Repository
from course_insight.infrastructure.m3_file_repository import FileM3Repository
from course_insight.modules.m1_course_governance.parsers import (
    default_parser_registry,
    parse_source,
)
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    load_dataset,
    split_by_group,
)
from course_insight.modules.m5_learner_class_state.bkt import BktEngine
from course_insight.modules.m5_learner_class_state.dina import DinaEngine
from course_insight.modules.m8_assessment_scoring.irt_2pl import TwoPLCalibrator
from scripts.prepare_five_day_acceptance import (
    bind_course_package,
    refresh_manifest,
)


COURSEWARE_NAMES = (
    "01_network_layers.md",
    "02_ip_addressing.txt",
    "03_transport_protocols.docx",
    "04_performance_metrics.pdf",
    "05_fault_diagnosis.pptx",
)

_EXTENDED_PROPERTIES_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
)
_PRESENTATION_NS = (
    "http://schemas.openxmlformats.org/presentationml/2006/main"
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_pptx_slide_metadata(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            application = ElementTree.fromstring(
                archive.read("docProps/app.xml")
            )
            presentation = ElementTree.fromstring(
                archive.read("ppt/presentation.xml")
            )
    except (BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise ValueError("PPTX package metadata is invalid") from error

    declared_text = application.findtext(
        f"{{{_EXTENDED_PROPERTIES_NS}}}Slides"
    )
    actual_count = len(
        presentation.findall(f".//{{{_PRESENTATION_NS}}}sldId")
    )
    try:
        declared_count = int(declared_text or "")
    except ValueError as error:
        raise ValueError("PPTX slide metadata is invalid") from error
    if actual_count < 1 or declared_count != actual_count:
        raise ValueError(
            "PPTX slide metadata mismatch: "
            f"declared={declared_count}, actual={actual_count}"
        )


def verify_static_pack(pack_dir: Path) -> dict[str, Any]:
    pack_dir = pack_dir.resolve()
    manifest = _read_json(pack_dir / "manifest.json")
    checksum_failures = [
        relative
        for relative, expected in manifest["file_checksums"].items()
        if not (pack_dir / relative).is_file()
        or _sha256(pack_dir / relative) != expected
    ]
    if checksum_failures:
        raise ValueError(f"manifest checksum mismatch: {checksum_failures}")

    pptx_path = pack_dir / "courseware" / "05_fault_diagnosis.pptx"
    if pptx_path.is_file():
        _verify_pptx_slide_metadata(pptx_path)

    with (pack_dir / "expected" / "coverage_matrix.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        coverage = list(csv.DictReader(source))
    modules = {f"M{index}": [] for index in range(10)}
    missing_paths: list[str] = []
    for row in coverage:
        modules[row["module"]].append(row["feature_id"])
        if row["external_data_required"] != "yes":
            continue
        for relative in row["data_paths"].split("|"):
            if relative and not (pack_dir / relative).exists():
                missing_paths.append(relative)
    if missing_paths:
        raise ValueError(f"missing external data paths: {sorted(set(missing_paths))}")

    intent_dataset = load_dataset(pack_dir / "scenarios" / "intent_examples.jsonl")
    intent_partitions = split_by_group(intent_dataset.examples, seed=20260824)
    if {item.label for item in intent_dataset.examples} != set(EXPECTED_LABELS):
        raise ValueError("intent labels are incomplete")
    if any(
        {item.label for item in partition} != set(EXPECTED_LABELS)
        for partition in (
            intent_partitions.train,
            intent_partitions.validation,
            intent_partitions.test,
        )
    ):
        raise ValueError("an intent split is missing labels")

    observations = [
        LearningObservation.model_validate(row)
        for row in _read_jsonl(
            pack_dir / "model_data" / "learning_observations.jsonl"
        )
    ]
    sequences = [
        ConceptResponseSequence.model_validate(row)
        for row in _read_jsonl(pack_dir / "model_data" / "bkt_sequences.jsonl")
    ]
    for item in observations:
        item.validate_business_rules()
    for sequence in sequences:
        sequence.validate_business_rules()

    return {
        "status": "passed",
        "modules": modules,
        "checks": {
            "manifest_checksums": "passed",
            "external_data_paths": "passed",
            "intent_contract": "passed",
            "learning_model_contracts": "passed",
        },
        "counts": {
            "coverage_features": len(coverage),
            "intent_examples": len(intent_dataset.examples),
            "learning_observations": len(observations),
            "bkt_sequences": len(sequences),
        },
    }


def _build_batches(observations: list[LearningObservation]) -> list[LearningObservationBatch]:
    grouped: dict[str, list[LearningObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.learner_id].append(observation)
    return [
        LearningObservationBatch(
            batch_id=f"batch_{learner_id}",
            learner_id=learner_id,
            observations=sorted(rows, key=lambda item: item.occurred_at),
            watermark=max(rows, key=lambda item: item.occurred_at).observation_id,
            created_at=max(item.occurred_at for item in rows),
        )
        for learner_id, rows in sorted(grouped.items())
    ]


def _verify_negative_parsers(pack_dir: Path) -> dict[str, str]:
    fixtures = {
        "unsupported_legacy.ppt": "rejected",
        "empty.md": "rejected",
        "invalid_utf8.txt": "rejected",
        "corrupt.docx": "rejected",
        "corrupt.pptx": "rejected",
        "corrupt.pdf": "rejected",
        "scanned_no_text_layer.pdf": "ocr_required",
        "encrypted.pdf": "rejected",
    }
    results: dict[str, str] = {}
    for name, expected in fixtures.items():
        path = pack_dir / "invalid_fixtures" / name
        try:
            parse_source(path.name, path.read_bytes())
        except Exception as error:  # Deliberately records only safe type/code metadata.
            code = getattr(error, "code", None)
            results[name] = "ocr_required" if code == "COURSE_PDF_OCR_REQUIRED" else "rejected"
        else:
            results[name] = "unexpected_success"
        if results[name] != expected:
            raise ValueError(
                f"negative parser fixture {name} returned {results[name]}, expected {expected}"
            )
    return results


def verify_runtime_pack(pack_dir: Path, runtime_dir: Path) -> dict[str, Any]:
    pack_dir = pack_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    courseware = [pack_dir / "courseware" / name for name in COURSEWARE_NAMES]
    m1 = M1CourseGovernanceService(
        default_parser_registry(),
        lambda payload: hashlib.sha256(payload).hexdigest(),
        FileM1Repository(runtime_dir),
    )
    package = m1.import_course(
        courseware,
        pack_dir / "governance" / "course_metadata.json",
        pack_dir / "governance" / "source_authorization.csv",
        runtime_dir / "import_output",
    )
    bind_course_package(pack_dir, runtime_dir / "import_output" / "course_package.json")

    m2 = M2EvidenceRetrievalService(
        runtime_dir / "indexes",
        "lexical",
        FileM2Repository(runtime_dir),
    )
    index_ref = m2.build_index(package)
    retrieval_results: dict[str, dict[str, Any]] = {}
    for row in _read_json(pack_dir / "scenarios" / "retrieval_queries.json")[
        "queries"
    ]:
        query = EvidenceQuery(
            query_id=row["query_id"],
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            query_text=row["text"],
            concept_ids=row["concept_ids"],
            item_id=None,
            use_case="qa",
            top_k=3,
            min_relevance=0.05,
        )
        bundle = m2.retrieve(query, index_ref)
        bundle.validate_business_rules()
        combined_text = "\n".join(chunk.text for chunk in bundle.evidence_chunks)
        expected_terms = row.get("expected_terms", [])
        if expected_terms and not any(term in combined_text for term in expected_terms):
            raise ValueError(f"retrieval query {row['query_id']} missed expected terms")
        relevances = [chunk.relevance for chunk in bundle.evidence_chunks]
        if (
            row.get("expected") == "low_relevance_or_empty"
            and relevances
            and max(relevances) >= 0.30
        ):
            raise ValueError(
                f"retrieval query {row['query_id']} was not low relevance: {max(relevances):.3f}"
            )
        retrieval_results[row["query_id"]] = {
            "hit_count": len(bundle.evidence_chunks),
            "citation_ids": bundle.citation_ids(),
            "relevances": relevances,
        }

    seed_dir = pack_dir / "seeds"
    m3 = M3KnowledgeBundleService(FileM3Repository(runtime_dir), None)
    bundle = m3.build_knowledge_bundle(
        package,
        seed_dir / "concept.json",
        seed_dir / "item.json",
        seed_dir / "rubric.json",
        seed_dir / "blueprint.json",
        seed_dir / "prerequisite.json",
        seed_dir / "misconception.json",
    )

    observation_rows = [
        LearningObservation.model_validate(row)
        for row in _read_jsonl(
            pack_dir / "model_data" / "learning_observations.jsonl"
        )
    ]
    observed_item_keys = {
        (item.item_id, item.item_version) for item in observation_rows
    }
    q_matrix = [
        QMatrixEntry.model_validate(row)
        for row in _read_json(seed_dir / "item.json")["q_matrix"]
        if (row["item_id"], row["item_version"]) in observed_item_keys
    ]
    dina_model = DinaEngine(max_iterations=60).fit(
        _build_batches(observation_rows), q_matrix
    )
    bkt_sequences = [
        ConceptResponseSequence.model_validate(row)
        for row in _read_jsonl(pack_dir / "model_data" / "bkt_sequences.jsonl")
    ]
    bkt_model = BktEngine(max_iterations=60).fit(bkt_sequences)
    irt_result = TwoPLCalibrator().fit(
        observation_rows,
        datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    tutoring_paths = _read_json(
        pack_dir / "scenarios" / "tutoring_state_paths.json"
    )
    privacy_cases = _read_json(
        pack_dir / "scenarios" / "privacy_and_subjective_answers.json"
    )
    review_cases = _read_json(
        pack_dir / "scenarios" / "teacher_review_cases.json"
    )
    blueprints = _read_json(seed_dir / "blueprint.json")["blueprints"]

    return {
        "status": "passed",
        "M1": {
            "status": package.status,
            "source_count": len(package.source_documents),
            "chunk_count": len(package.content_chunks),
            "checksum": package.checksum,
            "negative_fixtures": _verify_negative_parsers(pack_dir),
        },
        "M2": {
            "status": index_ref.status,
            "index_checksum": index_ref.checksum,
            "queries": retrieval_results,
        },
        "M3": {
            "knowledge_bundle_id": bundle.knowledge_bundle_id,
            "concept_count": len(bundle.concepts),
            "item_count": len(bundle.items),
        },
        "M4": {
            "labels": sorted(EXPECTED_LABELS),
            "example_count": len(
                load_dataset(pack_dir / "scenarios" / "intent_examples.jsonl").examples
            ),
        },
        "M5": {
            "dina_learners": dina_model.learner_count,
            "dina_items": len(dina_model.item_parameters),
            "bkt_learners": bkt_model.learner_count,
            "bkt_observations": bkt_model.observation_count,
        },
        "M6": {
            "valid_paths": sum(
                1 for item in tutoring_paths["cases"] if "expected_state" in item
            ),
            "invalid_paths": sum(
                1 for item in tutoring_paths["cases"] if "expected_error" in item
            ),
        },
        "M7": {
            "privacy_cases": len(privacy_cases["cases"]),
        },
        "M8": {
            "blueprints": len(blueprints),
            "teacher_review_cases": len(review_cases["cases"]),
            "irt_status": irt_result.status,
            "irt_converged": irt_result.converged,
            "irt_items": len(irt_result.parameter_set.item_parameters),
        },
        "M9": {
            "learner_profiles": sum(
                1
                for _ in (pack_dir / "model_data" / "learner_profiles.csv")
                .read_text(encoding="utf-8-sig")
                .splitlines()[1:]
            ),
            "report_inputs_ready": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the Course Insight five-day acceptance data pack."
    )
    parser.add_argument(
        "--pack", type=Path, default=Path("examples/five_day_acceptance")
    )
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()

    static_report = verify_static_pack(args.pack)
    runtime_context = (
        nullcontext(args.runtime_dir)
        if args.runtime_dir is not None
        else tempfile.TemporaryDirectory(prefix="course-insight-scheme6-")
    )
    if args.static_only:
        dynamic_report: dict[str, Any] | None = None
    else:
        with runtime_context as runtime_value:
            dynamic_report = verify_runtime_pack(args.pack, Path(runtime_value))
    report = {
        "schema_version": 1,
        "status": "passed",
        "verified_at": datetime.now(tz=UTC).isoformat(),
        "static": static_report,
        "runtime": dynamic_report,
    }
    report_path = args.report or args.pack / "verification_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if report_path.resolve().is_relative_to(args.pack.resolve()):
        refresh_manifest(args.pack)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
