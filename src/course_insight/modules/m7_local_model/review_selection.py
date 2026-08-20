"""Fail-closed, locally calibrated selection of M7 teacher review work."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
import stat
from statistics import NormalDist
from typing import Any, Literal, Protocol, runtime_checkable

from course_insight.contracts.assessment import (
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.evidence import EvidenceBundle
from course_insight.modules.m7_local_model.policy import (
    M7_REVIEW_FEATURE_SCHEMA_VERSION,
    ReviewSelectionMode,
)
from course_insight.modules.m7_local_model.privacy import OutboundPrivacyResult


REVIEW_SELECTOR_ARTIFACT_SCHEMA_VERSION = (
    "m7-review-selector-artifact-v1"
)
_MAX_ARTIFACT_BYTES = 1024 * 1024
_LOWER_HEX = frozenset("0123456789abcdef")
_FEATURE_NAMES = (
    "raw_risk",
    "answer_characters",
    "criterion_count",
    "evidence_chunk_count",
    "evidence_characters",
    "normalized_total_score",
    "missing_concept_fraction",
    "cited_criterion_fraction",
)
_DEFER_FLAGS = frozenset(
    {
        "low_confidence",
        "review_selector_fallback",
        "review_selector_hard_defer",
        "review_selector_insufficient_data",
        "review_selector_ood",
        "review_selector_risk_limit",
        "review_selector_shadow",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewSelectorBinding:
    """Runtime identities that one selector artifact must match exactly."""

    model_name: str
    model_version: str
    thinking_mode: Literal["thinking", "non_thinking"]
    prompt_id: str
    prompt_version: str
    execution_policy_version: str
    privacy_policy_version: str
    calibration_data_id: str
    calibration_data_version: str
    calibration_data_sha256: str
    split_id: str
    split_sha256: str
    feature_schema_version: str = M7_REVIEW_FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "model_name",
            "model_version",
            "prompt_id",
            "prompt_version",
            "execution_policy_version",
            "privacy_policy_version",
            "calibration_data_id",
            "calibration_data_version",
            "split_id",
            "feature_schema_version",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"selector binding {field_name} is invalid")
        if self.thinking_mode not in {"thinking", "non_thinking"}:
            raise ValueError("selector thinking mode is invalid")
        if self.feature_schema_version != M7_REVIEW_FEATURE_SCHEMA_VERSION:
            raise ValueError("selector feature schema is unsupported")
        _validate_sha256(self.calibration_data_sha256)
        _validate_sha256(self.split_sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_policy_version": self.execution_policy_version,
            "feature_schema_version": self.feature_schema_version,
            "calibration_data_id": self.calibration_data_id,
            "calibration_data_version": self.calibration_data_version,
            "calibration_data_sha256": self.calibration_data_sha256,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "privacy_policy_version": self.privacy_policy_version,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "split_id": self.split_id,
            "split_sha256": self.split_sha256,
            "thinking_mode": self.thinking_mode,
        }


@dataclass(frozen=True, slots=True)
class ReviewSelectionDecision:
    """A local decision; it contains no learner text or model response."""

    configured_mode: ReviewSelectionMode
    effective_mode: Literal["all_review", "shadow", "selective"]
    require_teacher_review: bool
    review_flags: tuple[str, ...]
    audit_flags: tuple[str, ...]
    reason_code: str
    selector_id: str
    artifact_sha256: str | None = None
    raw_risk: float | None = None
    calibrated_risk: float | None = None
    acceptance_error_upper_bound: float | None = None
    candidate_accepted: bool = False

    def __post_init__(self) -> None:
        if self.configured_mode not in {
            "all_review",
            "shadow",
            "selective",
        } or self.effective_mode not in {
            "all_review",
            "shadow",
            "selective",
        }:
            raise ValueError("review selection mode is invalid")
        if type(self.require_teacher_review) is not bool:
            raise ValueError("review decision must be boolean")
        for values in (self.review_flags, self.audit_flags):
            if (
                type(values) is not tuple
                or len(values) != len(set(values))
                or any(type(value) is not str or not value for value in values)
            ):
                raise ValueError("review selection flags are invalid")
        requires_flag = "teacher_review_required" in self.review_flags
        if requires_flag != self.require_teacher_review:
            raise ValueError("teacher-review flag and decision disagree")
        if not set(self.review_flags) <= (
            _DEFER_FLAGS | {"teacher_review_required"}
        ):
            raise ValueError("review selection exposed an unknown public flag")
        if not self.require_teacher_review and self.review_flags:
            raise ValueError("accepted review selection must expose no flags")
        for field_name in ("reason_code", "selector_id"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"review decision {field_name} is invalid")
        if self.artifact_sha256 is not None:
            _validate_sha256(self.artifact_sha256)
        for value in (
            self.raw_risk,
            self.calibrated_risk,
            self.acceptance_error_upper_bound,
        ):
            if value is not None and (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError("review selection risk is invalid")
        if self.candidate_accepted and self.raw_risk is None:
            raise ValueError("accepted candidate requires calibrated risk data")

    def safe_record(self) -> dict[str, Any]:
        """Return bounded selector provenance without any learner content."""

        return {
            "review_selection_mode": self.configured_mode,
            "review_selection_effective_mode": self.effective_mode,
            "review_selection_reason": self.reason_code,
            "review_selector_id": self.selector_id,
            "review_selector_artifact_sha256": self.artifact_sha256,
            "review_candidate_accepted": self.candidate_accepted,
        }


@runtime_checkable
class ReviewSelector(Protocol):
    """Private M7 boundary for choosing whether a score needs review."""

    selector_id: str

    def select(
        self,
        *,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
        result: RubricScoringResult,
        privacy: OutboundPrivacyResult,
    ) -> ReviewSelectionDecision:
        """Return a fail-closed teacher-review decision."""


class AllReviewSelector:
    """Safe baseline used by default and for every selector failure."""

    selector_id = "m7-all-review-v1"

    def __init__(
        self,
        *,
        configured_mode: ReviewSelectionMode = "all_review",
        fallback: bool = False,
    ) -> None:
        if configured_mode not in {"all_review", "shadow", "selective"}:
            raise ValueError("review selection mode is invalid")
        self._configured_mode = configured_mode
        self._fallback = fallback

    def select(
        self,
        *,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
        result: RubricScoringResult,
        privacy: OutboundPrivacyResult,
    ) -> ReviewSelectionDecision:
        del evidence_bundle, privacy
        flags = ["teacher_review_required"]
        if result.confidence < task.rubric.review_policy.low_confidence_threshold:
            flags.append("low_confidence")
        audit_flags = ["review_selector_all_review"]
        reason = "all_review_policy"
        if self._fallback:
            flags.append("review_selector_fallback")
            audit_flags.extend(
                ["review_selector_fallback", "review_selector_deferred"]
            )
            reason = "selector_failure_fallback"
        return ReviewSelectionDecision(
            configured_mode=self._configured_mode,
            effective_mode="all_review",
            require_teacher_review=True,
            review_flags=tuple(flags),
            audit_flags=tuple(audit_flags),
            reason_code=reason,
            selector_id=self.selector_id,
        )


@dataclass(frozen=True, slots=True)
class _FeatureRange:
    minimum: float
    maximum: float

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class _CalibrationSegment:
    raw_risk_upper: float
    calibrated_risk: float
    sample_count: int
    error_count: int


@dataclass(frozen=True, slots=True)
class _SelectionGates:
    minimum_calibration_samples: int
    minimum_acceptance_samples: int
    minimum_segment_samples: int
    acceptance_raw_risk_upper: float
    maximum_calibrated_risk: float
    maximum_acceptance_error_rate: float
    confidence_level: float
    approved_rubric_refs: frozenset[str]
    ood_ranges: dict[str, _FeatureRange]


@dataclass(frozen=True, slots=True)
class _ReviewSelectorArtifact:
    selector_id: str
    selector_version: str
    artifact_sha256: str
    binding: ReviewSelectorBinding
    segments: tuple[_CalibrationSegment, ...]
    gates: _SelectionGates
    acceptance_error_upper_bound: float


@dataclass(frozen=True, slots=True)
class _ReviewFeatures:
    raw_risk: float
    answer_characters: float
    criterion_count: float
    evidence_chunk_count: float
    evidence_characters: float
    normalized_total_score: float
    missing_concept_fraction: float
    cited_criterion_fraction: float

    def to_dict(self) -> dict[str, float]:
        return {
            name: float(getattr(self, name)) for name in _FEATURE_NAMES
        }


class IsotonicReviewSelector:
    """PAV/isotonic risk selector guarded by release and OOD gates."""

    def __init__(
        self,
        *,
        mode: Literal["shadow", "selective"],
        artifact: _ReviewSelectorArtifact,
    ) -> None:
        if mode not in {"shadow", "selective"}:
            raise ValueError("calibrated selector requires shadow/selective mode")
        self._mode = mode
        self._artifact = artifact
        self.selector_id = (
            f"{artifact.selector_id}@{artifact.selector_version}:"
            f"{artifact.artifact_sha256[:12]}"
        )

    @property
    def binding(self) -> ReviewSelectorBinding:
        return self._artifact.binding

    def select(
        self,
        *,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
        result: RubricScoringResult,
        privacy: OutboundPrivacyResult,
    ) -> ReviewSelectionDecision:
        try:
            return self._select(
                task=task,
                evidence_bundle=evidence_bundle,
                result=result,
                privacy=privacy,
            )
        except Exception:
            return AllReviewSelector(
                configured_mode=self._mode,
                fallback=True,
            ).select(
                task=task,
                evidence_bundle=evidence_bundle,
                result=result,
                privacy=privacy,
            )

    def _select(
        self,
        *,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
        result: RubricScoringResult,
        privacy: OutboundPrivacyResult,
    ) -> ReviewSelectionDecision:
        if result.review_flags:
            raise ValueError("selector input must not contain model review flags")
        features = _review_features(task, evidence_bundle, result)
        hard_defer = _hard_defer(task, result, privacy)
        rubric_ref = f"{task.rubric.rubric_id}:{task.rubric.version}"
        if rubric_ref not in self._artifact.gates.approved_rubric_refs:
            hard_defer = True
        if hard_defer:
            return self._defer(
                task=task,
                result=result,
                reason="hard_defer",
                public_flag="review_selector_hard_defer",
                audit_flag="review_selector_hard_defer",
                features=features,
            )
        if any(
            not self._artifact.gates.ood_ranges[name].contains(value)
            for name, value in features.to_dict().items()
        ):
            return self._defer(
                task=task,
                result=result,
                reason="out_of_distribution",
                public_flag="review_selector_ood",
                audit_flag="review_selector_ood",
                features=features,
            )
        segment = next(
            (
                item
                for item in self._artifact.segments
                if features.raw_risk <= item.raw_risk_upper
            ),
            None,
        )
        if (
            segment is None
            or segment.sample_count
            < self._artifact.gates.minimum_segment_samples
        ):
            return self._defer(
                task=task,
                result=result,
                reason="insufficient_calibration_samples",
                public_flag="review_selector_insufficient_data",
                audit_flag="review_selector_insufficient_data",
                features=features,
                segment=segment,
            )
        accepted = (
            features.raw_risk
            <= self._artifact.gates.acceptance_raw_risk_upper
            and segment.calibrated_risk
            <= self._artifact.gates.maximum_calibrated_risk
            and self._artifact.acceptance_error_upper_bound
            <= self._artifact.gates.maximum_acceptance_error_rate
        )
        if not accepted:
            return self._defer(
                task=task,
                result=result,
                reason="calibrated_risk_limit",
                public_flag="review_selector_risk_limit",
                audit_flag="review_selector_risk_limit",
                features=features,
                segment=segment,
            )
        if self._mode == "shadow":
            return ReviewSelectionDecision(
                configured_mode="shadow",
                effective_mode="shadow",
                require_teacher_review=True,
                review_flags=(
                    "teacher_review_required",
                    "review_selector_shadow",
                ),
                audit_flags=(
                    "review_selector_shadow",
                    "review_selector_candidate_accepted",
                    "review_selector_deferred",
                ),
                reason_code="shadow_candidate_accepted",
                selector_id=self.selector_id,
                artifact_sha256=self._artifact.artifact_sha256,
                raw_risk=features.raw_risk,
                calibrated_risk=segment.calibrated_risk,
                acceptance_error_upper_bound=(
                    self._artifact.acceptance_error_upper_bound
                ),
                candidate_accepted=True,
            )
        return ReviewSelectionDecision(
            configured_mode="selective",
            effective_mode="selective",
            require_teacher_review=False,
            review_flags=(),
            audit_flags=(
                "review_selector_selective",
                "review_selector_accepted",
            ),
            reason_code="calibrated_acceptance_set",
            selector_id=self.selector_id,
            artifact_sha256=self._artifact.artifact_sha256,
            raw_risk=features.raw_risk,
            calibrated_risk=segment.calibrated_risk,
            acceptance_error_upper_bound=(
                self._artifact.acceptance_error_upper_bound
            ),
            candidate_accepted=True,
        )

    def _defer(
        self,
        *,
        task: RubricScoringTask,
        result: RubricScoringResult,
        reason: str,
        public_flag: str,
        audit_flag: str,
        features: _ReviewFeatures,
        segment: _CalibrationSegment | None = None,
    ) -> ReviewSelectionDecision:
        flags = ["teacher_review_required", public_flag]
        if result.confidence < task.rubric.review_policy.low_confidence_threshold:
            flags.insert(1, "low_confidence")
        audit_flags = [
            (
                "review_selector_shadow"
                if self._mode == "shadow"
                else "review_selector_selective"
            ),
            "review_selector_deferred",
            audit_flag,
        ]
        return ReviewSelectionDecision(
            configured_mode=self._mode,
            effective_mode=self._mode,
            require_teacher_review=True,
            review_flags=tuple(flags),
            audit_flags=tuple(audit_flags),
            reason_code=reason,
            selector_id=self.selector_id,
            artifact_sha256=self._artifact.artifact_sha256,
            raw_risk=features.raw_risk,
            calibrated_risk=(
                None if segment is None else segment.calibrated_risk
            ),
            acceptance_error_upper_bound=(
                self._artifact.acceptance_error_upper_bound
            ),
            candidate_accepted=False,
        )


def select_teacher_review(
    selector: ReviewSelector,
    *,
    task: RubricScoringTask,
    evidence_bundle: EvidenceBundle,
    result: RubricScoringResult,
    privacy: OutboundPrivacyResult,
    configured_mode: ReviewSelectionMode,
) -> ReviewSelectionDecision:
    """Invoke any selector behind a fail-closed type and exception boundary."""

    try:
        decision = selector.select(
            task=task,
            evidence_bundle=evidence_bundle,
            result=result,
            privacy=privacy,
        )
        if not isinstance(decision, ReviewSelectionDecision):
            raise TypeError("selector returned an invalid decision")
        if decision.configured_mode != configured_mode:
            raise ValueError("selector decision mode drifted from policy")
        return decision
    except Exception:
        return AllReviewSelector(
            configured_mode=configured_mode,
            fallback=True,
        ).select(
            task=task,
            evidence_bundle=evidence_bundle,
            result=result,
            privacy=privacy,
        )


def load_isotonic_review_selector(
    *,
    artifact_path: str | Path,
    expected_artifact_sha256: str,
    expected_binding: ReviewSelectorBinding,
    mode: Literal["shadow", "selective"],
) -> IsotonicReviewSelector:
    """Load one canonical, externally pinned data-only selector artifact."""

    artifact = _load_artifact(
        artifact_path=artifact_path,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_binding=expected_binding,
    )
    return IsotonicReviewSelector(mode=mode, artifact=artifact)


def build_review_selector(
    *,
    mode: ReviewSelectionMode,
    expected_binding: ReviewSelectorBinding,
    artifact_path: str | Path | None = None,
    expected_artifact_sha256: str | None = None,
) -> ReviewSelector:
    """Build the selected runtime, falling back to all-review on any issue."""

    if mode == "all_review":
        return AllReviewSelector()
    try:
        if artifact_path is None or expected_artifact_sha256 is None:
            raise ValueError("shadow/selective review requires a pinned artifact")
        return load_isotonic_review_selector(
            artifact_path=artifact_path,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_binding=expected_binding,
            mode=mode,
        )
    except Exception:
        return AllReviewSelector(configured_mode=mode, fallback=True)


def _review_features(
    task: RubricScoringTask,
    evidence_bundle: EvidenceBundle,
    result: RubricScoringResult,
) -> _ReviewFeatures:
    if result.scoring_task_id != task.scoring_task_id:
        raise ValueError("selector input task identity mismatch")
    criterion_count = len(result.criterion_scores)
    if criterion_count <= 0:
        raise ValueError("selector requires criterion scores")
    maximum = task.max_score()
    normalized_score = 0.0 if maximum == 0.0 else result.total_score / maximum
    cited = sum(
        score.course_evidence_id is not None
        for score in result.criterion_scores
    )
    concept_count = max(1, len(task.item_instance.concept_ids))
    features = _ReviewFeatures(
        raw_risk=1.0 - result.confidence,
        answer_characters=float(len(task.student_answer)),
        criterion_count=float(criterion_count),
        evidence_chunk_count=float(len(evidence_bundle.evidence_chunks)),
        evidence_characters=float(
            sum(len(chunk.text) for chunk in evidence_bundle.evidence_chunks)
        ),
        normalized_total_score=float(normalized_score),
        missing_concept_fraction=(
            len(result.missing_concept_ids) / concept_count
        ),
        cited_criterion_fraction=cited / criterion_count,
    )
    if any(
        not math.isfinite(value) or not 0.0 <= value
        for value in features.to_dict().values()
    ):
        raise ValueError("selector feature is invalid")
    if not 0.0 <= features.raw_risk <= 1.0:
        raise ValueError("selector raw risk is invalid")
    return features


def _hard_defer(
    task: RubricScoringTask,
    result: RubricScoringResult,
    privacy: OutboundPrivacyResult,
) -> bool:
    return (
        privacy.decision != "allowed"
        or not task.student_answer.strip()
        or result.confidence
        < task.rubric.review_policy.low_confidence_threshold
    )


def _load_artifact(
    *,
    artifact_path: str | Path,
    expected_artifact_sha256: str,
    expected_binding: ReviewSelectorBinding,
) -> _ReviewSelectorArtifact:
    _validate_sha256(expected_artifact_sha256)
    path = Path(artifact_path)
    if path.is_symlink():
        raise ValueError("review selector artifact must not be a symlink")
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("review selector artifact file is invalid")
    raw = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(raw) != before.st_size
    ):
        raise ValueError("review selector artifact changed while loading")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_artifact_sha256:
        raise ValueError("review selector artifact SHA-256 mismatch")
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_nonfinite_json,
    )
    if type(payload) is not dict:
        raise ValueError("review selector artifact must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if raw != canonical:
        raise ValueError("review selector artifact must use canonical JSON")
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "selector_id",
            "selector_version",
            "bindings",
            "calibration",
            "gates",
        },
    )
    if payload["schema_version"] != REVIEW_SELECTOR_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported review selector artifact schema")
    selector_id = _text(payload["selector_id"])
    selector_version = _text(payload["selector_version"])
    bindings = payload["bindings"]
    if type(bindings) is not dict or bindings != expected_binding.to_dict():
        raise ValueError("review selector artifact binding mismatch")
    calibration = payload["calibration"]
    _require_exact_keys(calibration, {"method", "segments"})
    if calibration["method"] != "pav_isotonic":
        raise ValueError("review selector calibration method is unsupported")
    raw_segments = calibration["segments"]
    if type(raw_segments) is not list or not 1 <= len(raw_segments) <= 1000:
        raise ValueError("review selector calibration segments are invalid")
    segments = tuple(_parse_segment(item) for item in raw_segments)
    _validate_isotonic_segments(segments)
    gates = _parse_gates(payload["gates"])
    total_samples = sum(segment.sample_count for segment in segments)
    if total_samples < gates.minimum_calibration_samples:
        raise ValueError("review selector calibration sample gate failed")
    accepted_segments = tuple(
        segment
        for segment in segments
        if segment.raw_risk_upper <= gates.acceptance_raw_risk_upper
    )
    if (
        not accepted_segments
        or not any(
            math.isclose(
                segment.raw_risk_upper,
                gates.acceptance_raw_risk_upper,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for segment in segments
        )
    ):
        raise ValueError("acceptance threshold must be an isotonic boundary")
    if any(
        segment.calibrated_risk > gates.maximum_calibrated_risk
        for segment in accepted_segments
    ):
        raise ValueError("accepted PAV segment exceeds calibrated risk gate")
    accepted_samples = sum(item.sample_count for item in accepted_segments)
    accepted_errors = sum(item.error_count for item in accepted_segments)
    if accepted_samples < gates.minimum_acceptance_samples:
        raise ValueError("review selector acceptance sample gate failed")
    upper_bound = _wilson_one_sided_upper(
        errors=accepted_errors,
        samples=accepted_samples,
        confidence_level=gates.confidence_level,
    )
    if upper_bound > gates.maximum_acceptance_error_rate:
        raise ValueError("review selector acceptance risk gate failed")
    return _ReviewSelectorArtifact(
        selector_id=selector_id,
        selector_version=selector_version,
        artifact_sha256=actual_sha256,
        binding=expected_binding,
        segments=segments,
        gates=gates,
        acceptance_error_upper_bound=upper_bound,
    )


def _parse_segment(value: Any) -> _CalibrationSegment:
    _require_exact_keys(
        value,
        {
            "raw_risk_upper",
            "calibrated_risk",
            "sample_count",
            "error_count",
        },
    )
    raw_upper = _probability(value["raw_risk_upper"])
    calibrated = _probability(value["calibrated_risk"])
    sample_count = _positive_int(value["sample_count"])
    error_count = _nonnegative_int(value["error_count"])
    if error_count > sample_count:
        raise ValueError("PAV segment error count exceeds sample count")
    empirical = error_count / sample_count
    if not math.isclose(
        calibrated,
        empirical,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("PAV segment risk must equal its pooled error rate")
    return _CalibrationSegment(
        raw_risk_upper=raw_upper,
        calibrated_risk=calibrated,
        sample_count=sample_count,
        error_count=error_count,
    )


def _validate_isotonic_segments(
    segments: tuple[_CalibrationSegment, ...],
) -> None:
    previous_upper = -1.0
    previous_risk = -1.0
    for segment in segments:
        if (
            segment.raw_risk_upper <= previous_upper
            or segment.calibrated_risk < previous_risk
        ):
            raise ValueError("PAV segments must be strictly ordered and isotonic")
        previous_upper = segment.raw_risk_upper
        previous_risk = segment.calibrated_risk
    if not math.isclose(
        segments[-1].raw_risk_upper,
        1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("PAV segments must cover raw risk through one")


def _parse_gates(value: Any) -> _SelectionGates:
    _require_exact_keys(
        value,
        {
            "minimum_calibration_samples",
            "minimum_acceptance_samples",
            "minimum_segment_samples",
            "acceptance_raw_risk_upper",
            "maximum_calibrated_risk",
            "maximum_acceptance_error_rate",
            "confidence_level",
            "upper_bound_method",
            "approved_rubric_refs",
            "ood_ranges",
        },
    )
    if value["upper_bound_method"] != "wilson_one_sided":
        raise ValueError("review selector upper-bound method is unsupported")
    confidence_level = _probability(value["confidence_level"])
    if not 0.5 < confidence_level < 1.0:
        raise ValueError("review selector confidence level is invalid")
    raw_rubric_refs = value["approved_rubric_refs"]
    if (
        type(raw_rubric_refs) is not list
        or not 1 <= len(raw_rubric_refs) <= 10_000
        or any(type(item) is not str or not item.strip() for item in raw_rubric_refs)
        or len(raw_rubric_refs) != len(set(raw_rubric_refs))
    ):
        raise ValueError("review selector rubric strata are invalid")
    ranges = value["ood_ranges"]
    if type(ranges) is not dict or set(ranges) != set(_FEATURE_NAMES):
        raise ValueError("review selector OOD feature schema is invalid")
    parsed_ranges: dict[str, _FeatureRange] = {}
    for name in _FEATURE_NAMES:
        raw_range = ranges[name]
        _require_exact_keys(raw_range, {"minimum", "maximum"})
        minimum = _nonnegative_number(raw_range["minimum"])
        maximum = _nonnegative_number(raw_range["maximum"])
        if minimum > maximum:
            raise ValueError("review selector OOD range is inverted")
        parsed_ranges[name] = _FeatureRange(minimum, maximum)
    return _SelectionGates(
        minimum_calibration_samples=_positive_int(
            value["minimum_calibration_samples"]
        ),
        minimum_acceptance_samples=_positive_int(
            value["minimum_acceptance_samples"]
        ),
        minimum_segment_samples=_positive_int(
            value["minimum_segment_samples"]
        ),
        acceptance_raw_risk_upper=_probability(
            value["acceptance_raw_risk_upper"]
        ),
        maximum_calibrated_risk=_probability(
            value["maximum_calibrated_risk"]
        ),
        maximum_acceptance_error_rate=_probability(
            value["maximum_acceptance_error_rate"]
        ),
        confidence_level=confidence_level,
        approved_rubric_refs=frozenset(raw_rubric_refs),
        ood_ranges=parsed_ranges,
    )


def _wilson_one_sided_upper(
    *,
    errors: int,
    samples: int,
    confidence_level: float,
) -> float:
    if samples <= 0 or not 0 <= errors <= samples:
        raise ValueError("acceptance-set counts are invalid")
    z = NormalDist().inv_cdf(confidence_level)
    observed = errors / samples
    z_squared = z * z
    denominator = 1.0 + z_squared / samples
    center = observed + z_squared / (2.0 * samples)
    radius = z * math.sqrt(
        observed * (1.0 - observed) / samples
        + z_squared / (4.0 * samples * samples)
    )
    return min(1.0, max(0.0, (center + radius) / denominator))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("review selector artifact has duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"review selector artifact contains {value}")


def _require_exact_keys(value: Any, expected: set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("review selector artifact shape is invalid")


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("review selector text is invalid")
    return value.strip()


def _positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("review selector positive integer is invalid")
    return value


def _nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("review selector integer is invalid")
    return value


def _nonnegative_number(value: Any) -> float:
    if type(value) not in {int, float}:
        raise ValueError("review selector number is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError("review selector number is invalid")
    return number


def _probability(value: Any) -> float:
    number = _nonnegative_number(value)
    if number > 1.0:
        raise ValueError("review selector probability is invalid")
    return number


def _validate_sha256(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or not set(value) <= _LOWER_HEX
    ):
        raise ValueError("expected selector SHA-256 is invalid")


__all__ = [
    "AllReviewSelector",
    "IsotonicReviewSelector",
    "REVIEW_SELECTOR_ARTIFACT_SCHEMA_VERSION",
    "ReviewSelectionDecision",
    "ReviewSelector",
    "ReviewSelectorBinding",
    "build_review_selector",
    "load_isotonic_review_selector",
    "select_teacher_review",
]
