"""Checksum-pinned local adapters for public short-answer datasets.

No downloader is provided.  Callers must acquire and review datasets outside
this repository, transform them into the documented JSONL shape, and pass the
expected package SHA-256 through an independent channel.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._common import (
    EvaluationInputError,
    load_json_bytes,
    load_jsonl_bytes,
    require_exact_keys,
    require_finite_number,
    require_local_regular_file,
    require_safe_id,
    require_sha256,
    sha256_bytes,
)


DatasetFamily = Literal["CESA_ASAP_ZH", "SCIENTSBANK"]
SplitName = Literal["train", "calibration", "test"]

_MANIFEST_SCHEMA = "local_short_answer_eval_v1"
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_family",
        "dataset_id",
        "dataset_version",
        "adapter",
        "records_file",
        "records_sha256",
        "license",
        "license_confirmed",
        "license_evidence",
    }
)
_CESA_FIELDS = frozenset(
    {
        "case_id",
        "question_id",
        "answer",
        "gold_score",
        "score_min",
        "score_max",
        "score_step",
    }
)
_SCIENTSBANK_FIELDS = frozenset(
    {"case_id", "question_id", "answer", "label"}
)
_SCIENTSBANK_SCORES = {
    "correct": 2.0,
    "partially_correct": 1.0,
    "partially_correct_incomplete": 1.0,
    "contradictory": 0.0,
    "irrelevant": 0.0,
    "non_domain": 0.0,
}
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_RECORDS_BYTES = 256 * 1024 * 1024
_MAX_RECORDS = 500_000


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One locally held answer and its bounded ordinal scoring target."""

    case_id: str
    question_id: str
    answer_text: str
    gold_score: float
    score_min: float
    score_max: float
    score_step: float

    def __post_init__(self) -> None:
        require_safe_id(self.case_id, field_name="case_id")
        require_safe_id(self.question_id, field_name="question_id")
        if type(self.answer_text) is not str or not self.answer_text.strip():
            raise EvaluationInputError("answer must be non-blank text")
        if len(self.answer_text) > 100_000:
            raise EvaluationInputError("answer exceeds the local evaluation limit")
        minimum = require_finite_number(self.score_min, field_name="score_min")
        maximum = require_finite_number(self.score_max, field_name="score_max")
        score = require_finite_number(self.gold_score, field_name="gold_score")
        step = require_finite_number(
            self.score_step,
            field_name="score_step",
            minimum=1e-12,
        )
        if maximum <= minimum or not minimum <= score <= maximum:
            raise EvaluationInputError("evaluation score range is invalid")
        levels = (maximum - minimum) / step
        if not math.isclose(levels, round(levels), abs_tol=1e-9):
            raise EvaluationInputError("score_step must partition the score range")
        position = (score - minimum) / step
        if not math.isclose(position, round(position), abs_tol=1e-9):
            raise EvaluationInputError("gold_score must lie on the score scale")

    @property
    def normalized_gold(self) -> float:
        return (self.gold_score - self.score_min) / (
            self.score_max - self.score_min
        )


@dataclass(frozen=True, slots=True)
class DatasetPackage:
    """Validated, deduplicated local dataset package."""

    dataset_family: DatasetFamily
    dataset_id: str
    dataset_version: str
    adapter: str
    package_sha256: str
    records_sha256: str
    license: str
    license_confirmed: bool
    cases: tuple[EvaluationCase, ...]
    duplicate_case_ids: tuple[str, ...]

    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    """Question-disjoint train/calibration/test assignment."""

    seed: str
    assignments: dict[str, SplitName]
    train_question_ids: tuple[str, ...]
    calibration_question_ids: tuple[str, ...]
    test_question_ids: tuple[str, ...]

    def split_for(self, case_id: str) -> SplitName:
        try:
            return self.assignments[case_id]
        except KeyError:
            raise EvaluationInputError("case is absent from the split") from None


def compute_package_sha256(package_dir: Path) -> str:
    """Hash exact manifest and record bytes using a stable package framing."""

    root, manifest_path, manifest_bytes, manifest = _manifest_at(package_dir)
    records_name = _records_name(manifest)
    records_path = root / records_name
    records_checked = require_local_regular_file(
        records_path,
        max_bytes=_MAX_RECORDS_BYTES,
    )
    if records_checked.parent != root:
        raise EvaluationInputError("records_file must remain inside the package")
    try:
        records_bytes = records_checked.read_bytes()
    except OSError:
        raise EvaluationInputError("dataset records are unavailable") from None
    framed = (
        b"manifest.json\0"
        + manifest_bytes
        + b"\0"
        + records_name.encode("utf-8")
        + b"\0"
        + records_bytes
    )
    del manifest_path
    return sha256_bytes(framed)


def load_local_dataset_package(
    package_dir: Path,
    *,
    expected_package_sha256: str,
) -> DatasetPackage:
    """Load a reviewed local CESA/ASAP-ZH or SciEntsBank package.

    ``expected_package_sha256`` is mandatory and must come from outside the
    package.  This prevents an edited manifest from authorizing its own data.
    """

    expected = require_sha256(
        expected_package_sha256,
        field_name="expected_package_sha256",
    )
    root, _, manifest_bytes, manifest = _manifest_at(package_dir)
    package_checksum = compute_package_sha256(root)
    if package_checksum != expected:
        raise EvaluationInputError("dataset package SHA-256 mismatch")

    records_name = _records_name(manifest)
    records_path = require_local_regular_file(
        root / records_name,
        max_bytes=_MAX_RECORDS_BYTES,
    )
    try:
        records_bytes = records_path.read_bytes()
    except OSError:
        raise EvaluationInputError("dataset records are unavailable") from None
    records_checksum = sha256_bytes(records_bytes)
    if records_checksum != require_sha256(
        manifest["records_sha256"],
        field_name="records_sha256",
    ):
        raise EvaluationInputError("dataset records SHA-256 mismatch")

    family, adapter = _manifest_identity(manifest)
    license_name, license_confirmed = _validated_license(manifest)
    raw_records = load_jsonl_bytes(
        records_bytes,
        field_name="dataset records",
        max_records=_MAX_RECORDS,
    )
    cases = [
        _adapt_record(raw, family=family, adapter=adapter)
        for raw in raw_records
    ]
    unique, duplicate_ids = _deduplicate(cases)
    return DatasetPackage(
        dataset_family=family,
        dataset_id=require_safe_id(
            manifest["dataset_id"],
            field_name="dataset_id",
        ),
        dataset_version=require_safe_id(
            manifest["dataset_version"],
            field_name="dataset_version",
        ),
        adapter=adapter,
        package_sha256=package_checksum,
        records_sha256=records_checksum,
        license=license_name,
        license_confirmed=license_confirmed,
        cases=tuple(unique),
        duplicate_case_ids=tuple(sorted(duplicate_ids)),
    )


def split_by_question(
    package: DatasetPackage,
    *,
    train_fraction: float = 0.60,
    calibration_fraction: float = 0.20,
    test_fraction: float = 0.20,
    seed: str = "m7-question-split-v1",
) -> DatasetSplit:
    """Create deterministic, question-disjoint partitions.

    Question IDs, rather than individual answers, are assigned.  Consequently
    no response to a held-out question can appear in training or calibration.
    """

    fractions = (train_fraction, calibration_fraction, test_fraction)
    if any(
        isinstance(value, bool)
        or type(value) not in {int, float}
        or not math.isfinite(float(value))
        or float(value) <= 0.0
        for value in fractions
    ) or not math.isclose(sum(fractions), 1.0, abs_tol=1e-12):
        raise EvaluationInputError("split fractions must be positive and sum to one")
    require_safe_id(seed, field_name="split seed")
    questions = sorted({case.question_id for case in package.cases})
    if len(questions) < 3:
        raise EvaluationInputError(
            "at least three questions are required for leakage-safe splitting"
        )
    ordered = sorted(
        questions,
        key=lambda question_id: hashlib.sha256(
            f"{seed}|{package.dataset_id}|{question_id}".encode("utf-8")
        ).digest(),
    )
    train_count, calibration_count, test_count = _partition_counts(
        len(ordered),
        fractions,
    )
    train = tuple(sorted(ordered[:train_count]))
    calibration = tuple(
        sorted(ordered[train_count : train_count + calibration_count])
    )
    test = tuple(sorted(ordered[-test_count:]))
    split_by_question_id: dict[str, SplitName] = {
        **{question_id: "train" for question_id in train},
        **{question_id: "calibration" for question_id in calibration},
        **{question_id: "test" for question_id in test},
    }
    assignments = {
        case.case_id: split_by_question_id[case.question_id]
        for case in package.cases
    }
    if len(assignments) != len(package.cases):
        raise EvaluationInputError("case identifiers must be unique after deduplication")
    return DatasetSplit(
        seed=seed,
        assignments=assignments,
        train_question_ids=train,
        calibration_question_ids=calibration,
        test_question_ids=test,
    )


def _manifest_at(
    package_dir: Path,
) -> tuple[Path, Path, bytes, dict[str, Any]]:
    if not isinstance(package_dir, Path):
        raise EvaluationInputError("package_dir must be a pathlib.Path")
    if str(package_dir).lower().startswith(("http://", "https://")):
        raise EvaluationInputError("automatic dataset download is forbidden")
    try:
        if package_dir.is_symlink():
            raise EvaluationInputError("dataset package symlinks are forbidden")
        root = package_dir.resolve(strict=True)
    except EvaluationInputError:
        raise
    except OSError:
        raise EvaluationInputError("dataset package is unavailable") from None
    if not root.is_dir():
        raise EvaluationInputError("dataset package must be a local directory")
    manifest_path = require_local_regular_file(
        root / "manifest.json",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError:
        raise EvaluationInputError("dataset manifest is unavailable") from None
    manifest = load_json_bytes(manifest_bytes, field_name="dataset manifest")
    require_exact_keys(manifest, _MANIFEST_FIELDS, field_name="dataset manifest")
    if manifest["schema_version"] != _MANIFEST_SCHEMA:
        raise EvaluationInputError("dataset manifest schema version is unsupported")
    return root, manifest_path, manifest_bytes, manifest


def _records_name(manifest: dict[str, Any]) -> str:
    name = manifest.get("records_file")
    if (
        not isinstance(name, str)
        or name != Path(name).name
        or not name.endswith(".jsonl")
        or name in {".", ".."}
    ):
        raise EvaluationInputError("records_file must be one local JSONL basename")
    return name


def _manifest_identity(
    manifest: dict[str, Any],
) -> tuple[DatasetFamily, str]:
    family = manifest["dataset_family"]
    adapter = manifest["adapter"]
    expected = {
        "CESA_ASAP_ZH": "cesa_asap_zh_v1",
        "SCIENTSBANK": "scient_bank_v1",
    }
    if family not in expected or adapter != expected[family]:
        raise EvaluationInputError("dataset family and adapter are inconsistent")
    return family, adapter


def _validated_license(manifest: dict[str, Any]) -> tuple[str, bool]:
    license_name = manifest["license"]
    confirmed = manifest["license_confirmed"]
    evidence = manifest["license_evidence"]
    if type(confirmed) is not bool or not isinstance(license_name, str):
        raise EvaluationInputError("dataset license metadata is invalid")
    if not confirmed:
        if license_name != "NOASSERTION" or evidence is not None:
            raise EvaluationInputError(
                "unreviewed dataset licenses must remain NOASSERTION"
            )
        return license_name, False
    expected_fields = {"reviewer", "reviewed_at", "source_url"}
    require_exact_keys(evidence, expected_fields, field_name="license evidence")
    if license_name == "NOASSERTION":
        raise EvaluationInputError("confirmed license must name the reviewed license")
    for key in expected_fields:
        if not isinstance(evidence[key], str) or not evidence[key].strip():
            raise EvaluationInputError("license evidence fields must be non-blank")
    return license_name, True


def _adapt_record(
    raw: dict[str, Any],
    *,
    family: DatasetFamily,
    adapter: str,
) -> EvaluationCase:
    del adapter
    if family == "CESA_ASAP_ZH":
        require_exact_keys(raw, _CESA_FIELDS, field_name="CESA/ASAP-ZH record")
        return EvaluationCase(
            case_id=require_safe_id(raw["case_id"], field_name="case_id"),
            question_id=require_safe_id(
                raw["question_id"],
                field_name="question_id",
            ),
            answer_text=_answer(raw["answer"]),
            gold_score=require_finite_number(
                raw["gold_score"],
                field_name="gold_score",
            ),
            score_min=require_finite_number(
                raw["score_min"],
                field_name="score_min",
            ),
            score_max=require_finite_number(
                raw["score_max"],
                field_name="score_max",
            ),
            score_step=require_finite_number(
                raw["score_step"],
                field_name="score_step",
            ),
        )
    require_exact_keys(raw, _SCIENTSBANK_FIELDS, field_name="SciEntsBank record")
    label = raw["label"]
    if label not in _SCIENTSBANK_SCORES:
        raise EvaluationInputError("SciEntsBank label is unsupported")
    return EvaluationCase(
        case_id=require_safe_id(raw["case_id"], field_name="case_id"),
        question_id=require_safe_id(raw["question_id"], field_name="question_id"),
        answer_text=_answer(raw["answer"]),
        gold_score=_SCIENTSBANK_SCORES[label],
        score_min=0.0,
        score_max=2.0,
        score_step=1.0,
    )


def _answer(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise EvaluationInputError("answer must be non-blank text")
    return value


def _deduplicate(
    cases: list[EvaluationCase],
) -> tuple[list[EvaluationCase], list[str]]:
    by_case_id: dict[str, EvaluationCase] = {}
    by_content: dict[tuple[str, str], EvaluationCase] = {}
    unique: list[EvaluationCase] = []
    duplicates: list[str] = []
    for case in cases:
        if case.case_id in by_case_id:
            raise EvaluationInputError("duplicate case_id is forbidden")
        by_case_id[case.case_id] = case
        normalized = " ".join(
            unicodedata.normalize("NFKC", case.answer_text).casefold().split()
        )
        content_identity = (
            case.question_id,
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )
        prior = by_content.get(content_identity)
        if prior is None:
            by_content[content_identity] = case
            unique.append(case)
            continue
        if (
            prior.gold_score != case.gold_score
            or prior.score_min != case.score_min
            or prior.score_max != case.score_max
            or prior.score_step != case.score_step
        ):
            raise EvaluationInputError("duplicate answer has conflicting labels")
        duplicates.append(case.case_id)
    return unique, duplicates


def _partition_counts(
    total: int,
    fractions: tuple[float, float, float],
) -> tuple[int, int, int]:
    counts = [1, 1, 1]
    remaining = total - 3
    if remaining:
        raw = [remaining * value for value in fractions]
        floors = [int(math.floor(value)) for value in raw]
        counts = [base + extra for base, extra in zip(counts, floors, strict=True)]
        for index in sorted(
            range(3),
            key=lambda item: (raw[item] - floors[item], -item),
            reverse=True,
        )[: remaining - sum(floors)]:
            counts[index] += 1
    assert sum(counts) == total
    return counts[0], counts[1], counts[2]


__all__ = [
    "DatasetFamily",
    "DatasetPackage",
    "DatasetSplit",
    "EvaluationCase",
    "SplitName",
    "compute_package_sha256",
    "load_local_dataset_package",
    "split_by_question",
]
