"""Optional local privacy reviewers for outbound student text.

The deterministic redaction policy in :mod:`privacy` remains the first line of
defence.  This module supplies a second, local-only review layer for semantic
privacy risks which deterministic patterns cannot classify reliably.

``review`` is deliberately fail-closed: only ``allow`` permits an outbound
call.  Both ``review`` and ``block`` mean that the caller must keep the text on
platform.  Results contain stable categories and aggregate scores only; match
text, offsets, source text, and exception details are never returned or logged.

The scikit-learn loader accepts only checksum-pinned artifacts from a trusted,
administrator-controlled directory.  Joblib deserialization can execute code,
so neither learner uploads nor request-provided paths are valid inputs.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from numbers import Real
from operator import index
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


PrivacyReviewDecision = Literal["allow", "review", "block"]

PRIVACY_NORMALIZATION_VERSION = "m7-privacy-normalization-v1"
SEMANTIC_PRIVACY_LABELS = (
    "SAFE",
    "SELF_ADDRESS",
    "SELF_FAMILY",
    "SELF_HEALTH",
    "SELF_IDENTITY",
)

_SEMANTIC_FINDING_TYPES = {
    "SELF_ADDRESS": "semantic_address",
    "SELF_FAMILY": "semantic_family",
    "SELF_HEALTH": "semantic_health",
    "SELF_IDENTITY": "semantic_identity",
}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$")
_MANIFEST_SCHEMA_VERSION = "1"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_PROBABILITY_TOLERANCE = 1e-9
_EXPECTED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "model_id",
        "model_version",
        "adapter_type",
        "labels",
        "normalization_version",
        "training_dataset_sha256",
        "model_artifact_sha256",
        "library_versions",
        "created_at",
        "vectorizer",
        "classifier",
        "training_provenance",
    }
)
_EXPECTED_LIBRARY_FIELDS = frozenset({"python", "scikit_learn", "joblib"})


@dataclass(frozen=True, slots=True)
class PrivacyReviewResult:
    """Text-free, ephemeral result for the local orchestration layer."""

    decision: PrivacyReviewDecision
    reason_codes: tuple[str, ...]
    finding_types: tuple[str, ...] = ()
    finding_count: int = 0
    model_confidence: float | None = None
    model_margin: float | None = None
    reviewer_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"allow", "review", "block"}:
            raise ValueError("privacy review decision is invalid")
        _validate_tokens(self.reason_codes, field_name="reason codes")
        _validate_tokens(self.finding_types, field_name="finding types")
        _validate_tokens(self.reviewer_ids, field_name="reviewer ids")
        if not self.reason_codes:
            raise ValueError("privacy review reason codes must not be empty")
        if type(self.finding_count) is not int or self.finding_count < 0:
            raise ValueError("privacy review finding count is invalid")
        if self.finding_count < len(self.finding_types):
            raise ValueError("privacy review finding count is inconsistent")
        if self.decision == "allow" and (
            self.finding_count != 0 or self.finding_types
        ):
            raise ValueError("allowed privacy reviews cannot contain findings")
        _validate_optional_probability(
            self.model_confidence,
            field_name="model confidence",
        )
        _validate_optional_probability(
            self.model_margin,
            field_name="model margin",
        )
        if (
            self.model_confidence is not None
            and self.model_margin is not None
            and self.model_margin > self.model_confidence
        ):
            raise ValueError("privacy review model margin is invalid")

    def safe_record(self) -> dict[str, object]:
        """Return bounded local diagnostics, never input or matched text.

        Durable call audits deliberately collapse finding types to a generic
        sensitive/uncertain flag in :mod:`privacy`; this richer record is not
        a persistence contract.
        """

        return {
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "finding_types": list(self.finding_types),
            "finding_count": self.finding_count,
            "model_confidence": self.model_confidence,
            "model_margin": self.model_margin,
            "reviewer_ids": list(self.reviewer_ids),
        }


@runtime_checkable
class PrivacyReviewer(Protocol):
    """Local privacy decision point used before any remote-model transport."""

    @property
    def reviewer_id(self) -> str:
        """Return a stable, non-sensitive implementation identifier."""

    def review(self, text: str) -> PrivacyReviewResult:
        """Return ``allow`` only when this reviewer completed successfully."""


@dataclass(frozen=True, slots=True)
class DenyAllPrivacyReviewer:
    """Fail-closed reviewer for disabled, unavailable, or invalid backends."""

    reason_code: str = "privacy_reviewer_unavailable"
    reviewer_id: str = "privacy-unavailable"

    def __post_init__(self) -> None:
        _validate_token(self.reason_code, field_name="reason code")
        _validate_token(self.reviewer_id, field_name="reviewer id")

    def review(self, text: str) -> PrivacyReviewResult:
        del text
        return PrivacyReviewResult(
            decision="review",
            reason_codes=(self.reason_code,),
            reviewer_ids=(self.reviewer_id,),
        )


@dataclass(frozen=True, slots=True)
class PresidioSpacyPrivacyReviewer:
    """Presidio analyzer backed by a locally installed Chinese spaCy model."""

    _analyzer: Any = field(repr=False)
    model_name: str
    model_version: str
    presidio_version: str
    spacy_version: str
    score_threshold: float = 0.45
    max_text_characters: int = 50_000

    def __post_init__(self) -> None:
        _validate_token(self.model_name, field_name="spaCy model name")
        _validate_token(self.model_version, field_name="spaCy model version")
        _validate_package_version(self.presidio_version)
        _validate_package_version(self.spacy_version)
        if len(self.model_name) > 64 or len(self.model_version) > 56:
            raise ValueError("spaCy model identity is invalid")
        _validate_probability(self.score_threshold, field_name="score threshold")
        if (
            type(self.max_text_characters) is not int
            or self.max_text_characters < 1
            or self.max_text_characters > 1_000_000
        ):
            raise ValueError("privacy review text limit is invalid")
    @property
    def reviewer_id(self) -> str:
        configuration = "|".join(
            (
                self.presidio_version,
                self.spacy_version,
                format(self.score_threshold, ".12g"),
                str(self.max_text_characters),
            )
        )
        fingerprint = sha256(configuration.encode("utf-8")).hexdigest()[:24]
        return (
            f"presidio-spacy:{self.model_name}:{self.model_version}:"
            f"cfg.{fingerprint}"
        )

    def review(self, text: str) -> PrivacyReviewResult:
        invalid = _invalid_text_result(text, reviewer_id=self.reviewer_id)
        if invalid is not None:
            return invalid
        if len(text) > self.max_text_characters:
            return _review_result(
                "privacy_input_too_large",
                reviewer_id=self.reviewer_id,
            )
        try:
            raw_results = self._analyzer.analyze(
                text=text,
                language="zh",
                score_threshold=self.score_threshold,
            )
            if not isinstance(raw_results, (list, tuple)):
                raise ValueError("invalid analyzer result")
            finding_types: list[str] = []
            finding_count = 0
            for raw in raw_results:
                entity_type = _presidio_entity_type(raw)
                score = _presidio_score(raw)
                if score < self.score_threshold:
                    continue
                normalized = _public_presidio_finding(entity_type)
                finding_count += 1
                finding_types.append(normalized)
        except Exception:
            return _review_result(
                "presidio_backend_error",
                reviewer_id=self.reviewer_id,
            )

        if finding_count:
            return PrivacyReviewResult(
                decision="block",
                reason_codes=("presidio_pii_detected",),
                finding_types=tuple(dict.fromkeys(sorted(finding_types))),
                finding_count=finding_count,
                reviewer_ids=(self.reviewer_id,),
            )
        return PrivacyReviewResult(
            decision="allow",
            reason_codes=("presidio_review_passed",),
            reviewer_ids=(self.reviewer_id,),
        )


def build_presidio_spacy_reviewer(
    *,
    expected_model_version: str,
    expected_presidio_version: str,
    expected_spacy_version: str,
    model_name: str = "zh_core_web_sm",
    score_threshold: float = 0.45,
    max_text_characters: int = 50_000,
) -> PrivacyReviewer:
    """Build the optional Presidio/spaCy backend without importing it eagerly.

    Any configuration, import, model, or initialization failure returns a
    reviewer whose decision is always ``review``.  It never falls back to an
    allow-capable keyword-only implementation.
    """

    try:
        _validate_token(model_name, field_name="spaCy model name")
        _validate_token(expected_model_version, field_name="spaCy model version")
        presidio_version = _installed_package_version(
            "presidio-analyzer",
            expected_presidio_version,
        )
        spacy_version = _installed_package_version(
            "spacy",
            expected_spacy_version,
        )
        model_version = _installed_package_version(
            model_name,
            expected_model_version,
        )

        analyzer_module = importlib.import_module("presidio_analyzer")
        engine_module = importlib.import_module("presidio_analyzer.nlp_engine")
        analyzer_class = getattr(analyzer_module, "AnalyzerEngine")
        provider_class = getattr(engine_module, "NlpEngineProvider")
        if not callable(analyzer_class) or not callable(provider_class):
            raise RuntimeError("optional privacy backend is invalid")
        provider = provider_class(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": "zh", "model_name": model_name},
                ],
            }
        )
        nlp_engine = provider.create_engine()
        analyzer = analyzer_class(
            nlp_engine=nlp_engine,
            supported_languages=["zh"],
        )
        return PresidioSpacyPrivacyReviewer(
            _analyzer=analyzer,
            model_name=model_name,
            model_version=model_version,
            presidio_version=presidio_version,
            spacy_version=spacy_version,
            score_threshold=score_threshold,
            max_text_characters=max_text_characters,
        )
    except Exception:
        return DenyAllPrivacyReviewer(
            reason_code="presidio_backend_unavailable",
            reviewer_id="presidio-spacy-unavailable",
        )


@dataclass(frozen=True, slots=True)
class _PrivacyManifest:
    model_id: str
    model_version: str
    manifest_sha256: str
    model_artifact_sha256: str
    training_dataset_sha256: str
    python_version: str
    scikit_learn_version: str
    joblib_version: str
    vectorizer_type: str
    classifier_type: str


@dataclass(frozen=True, slots=True)
class SklearnSemanticPrivacyReviewer:
    """Semantic self-disclosure classifier using a verified local pipeline."""

    _artifact: Any = field(repr=False)
    _manifest: _PrivacyManifest = field(repr=False)
    minimum_safe_confidence: float = 0.90
    minimum_safe_margin: float = 0.30
    minimum_sensitive_confidence: float = 0.65
    max_text_characters: int = 50_000

    def __post_init__(self) -> None:
        _validate_probability(
            self.minimum_safe_confidence,
            field_name="minimum safe confidence",
        )
        _validate_probability(
            self.minimum_safe_margin,
            field_name="minimum safe margin",
        )
        _validate_probability(
            self.minimum_sensitive_confidence,
            field_name="minimum sensitive confidence",
        )
        if (
            type(self.max_text_characters) is not int
            or self.max_text_characters < 1
            or self.max_text_characters > 1_000_000
        ):
            raise ValueError("privacy review text limit is invalid")

    @property
    def reviewer_id(self) -> str:
        configuration = "|".join(
            (
                self._manifest.model_artifact_sha256,
                self._manifest.manifest_sha256,
                format(self.minimum_safe_confidence, ".12g"),
                format(self.minimum_safe_margin, ".12g"),
                format(self.minimum_sensitive_confidence, ".12g"),
                str(self.max_text_characters),
            )
        )
        fingerprint = sha256(configuration.encode("utf-8")).hexdigest()[:24]
        return (
            f"sklearn:{self._manifest.model_id}:{self._manifest.model_version}:"
            f"cfg.{fingerprint}"
        )

    def review(self, text: str) -> PrivacyReviewResult:
        invalid = _invalid_text_result(text, reviewer_id=self.reviewer_id)
        if invalid is not None:
            return invalid
        if len(text) > self.max_text_characters:
            return _review_result(
                "privacy_input_too_large",
                reviewer_id=self.reviewer_id,
            )
        try:
            normalized = normalize_privacy_text(text)
            if not normalized:
                raise ValueError("invalid normalized text")
            probabilities = _validated_probabilities(
                self._artifact.predict_proba([normalized]),
                class_count=len(SEMANTIC_PRIVACY_LABELS),
            )
            ranked = sorted(
                range(len(probabilities)),
                key=probabilities.__getitem__,
                reverse=True,
            )
            top_index, second_index = ranked[:2]
            confidence = probabilities[top_index]
            margin = confidence - probabilities[second_index]
            top_label = SEMANTIC_PRIVACY_LABELS[top_index]
        except Exception:
            return _review_result(
                "semantic_backend_error",
                reviewer_id=self.reviewer_id,
            )

        if top_label == "SAFE":
            if (
                confidence >= self.minimum_safe_confidence
                and margin >= self.minimum_safe_margin
            ):
                return PrivacyReviewResult(
                    decision="allow",
                    reason_codes=("semantic_review_passed",),
                    model_confidence=confidence,
                    model_margin=margin,
                    reviewer_ids=(self.reviewer_id,),
                )
            return PrivacyReviewResult(
                decision="review",
                reason_codes=("semantic_safe_uncertain",),
                model_confidence=confidence,
                model_margin=margin,
                reviewer_ids=(self.reviewer_id,),
            )

        finding_type = _SEMANTIC_FINDING_TYPES[top_label]
        if confidence >= self.minimum_sensitive_confidence:
            return PrivacyReviewResult(
                decision="block",
                reason_codes=("semantic_sensitive_detected",),
                finding_types=(finding_type,),
                finding_count=1,
                model_confidence=confidence,
                model_margin=margin,
                reviewer_ids=(self.reviewer_id,),
            )
        return PrivacyReviewResult(
            decision="review",
            reason_codes=("semantic_sensitive_uncertain",),
            finding_types=(finding_type,),
            finding_count=1,
            model_confidence=confidence,
            model_margin=margin,
            reviewer_ids=(self.reviewer_id,),
        )


def load_sklearn_privacy_reviewer(
    model_dir: Path,
    *,
    runtime_dir: Path,
    expected_model_id: str,
    expected_model_version: str,
    expected_model_sha256: str,
    expected_manifest_sha256: str,
    minimum_safe_confidence: float = 0.90,
    minimum_safe_margin: float = 0.30,
    minimum_sensitive_confidence: float = 0.65,
    max_text_characters: int = 50_000,
) -> PrivacyReviewer:
    """Load a trusted local artifact, returning deny-all on every failure."""

    try:
        _validate_external_identity(
            expected_model_id,
            expected_model_version,
            expected_model_sha256,
            expected_manifest_sha256,
        )
        root = _validated_model_root(model_dir, runtime_dir=runtime_dir)
        manifest_bytes = _read_stable_artifact_file(
            root,
            "manifest.json",
            max_bytes=_MAX_MANIFEST_BYTES,
            file_kind="manifest",
        )
        manifest_checksum = sha256(manifest_bytes).hexdigest()
        if manifest_checksum != expected_manifest_sha256:
            raise _ArtifactValidationError
        model_bytes = _read_stable_artifact_file(
            root,
            "model.joblib",
            max_bytes=_MAX_MODEL_BYTES,
            file_kind="model",
        )
        manifest = _parse_manifest(
            manifest_bytes,
            manifest_sha256=manifest_checksum,
        )
        if (
            manifest.model_id != expected_model_id
            or manifest.model_version != expected_model_version
        ):
            raise _ArtifactValidationError
        artifact_checksum = sha256(model_bytes).hexdigest()
        if (
            artifact_checksum != manifest.model_artifact_sha256
            or artifact_checksum != expected_model_sha256
        ):
            raise _ArtifactValidationError

        _require_runtime_library_versions(manifest)
        joblib = importlib.import_module("joblib")
        artifact = joblib.load(BytesIO(model_bytes))
        _validate_artifact(artifact, manifest)
        return SklearnSemanticPrivacyReviewer(
            _artifact=artifact,
            _manifest=manifest,
            minimum_safe_confidence=minimum_safe_confidence,
            minimum_safe_margin=minimum_safe_margin,
            minimum_sensitive_confidence=minimum_sensitive_confidence,
            max_text_characters=max_text_characters,
        )
    except (ImportError, ModuleNotFoundError):
        return DenyAllPrivacyReviewer(
            reason_code="semantic_dependencies_unavailable",
            reviewer_id="sklearn-privacy-unavailable",
        )
    except Exception:
        return DenyAllPrivacyReviewer(
            reason_code="semantic_artifact_unavailable",
            reviewer_id="sklearn-privacy-unavailable",
        )


@dataclass(frozen=True, slots=True)
class FailClosedPrivacyReviewer:
    """Require every configured local reviewer to pass before transmission."""

    reviewers: tuple[PrivacyReviewer, ...] = field(repr=False)
    ensemble_id: str = "privacy-ensemble-v1"

    def __post_init__(self) -> None:
        if type(self.reviewers) is not tuple:
            raise ValueError("privacy reviewers must be a tuple")
        _validate_token(self.ensemble_id, field_name="ensemble id")

    @property
    def reviewer_id(self) -> str:
        identities: list[str] = []
        for reviewer in self.reviewers:
            try:
                identity = reviewer.reviewer_id
                _validate_token(identity, field_name="reviewer id")
            except Exception:
                identity = "privacy-reviewer-invalid"
            identities.append(identity)
        configuration = "|".join(identities) if identities else "none"
        fingerprint = sha256(configuration.encode("utf-8")).hexdigest()[:24]
        return f"{self.ensemble_id}:cfg.{fingerprint}"

    def review(self, text: str) -> PrivacyReviewResult:
        invalid = _invalid_text_result(text, reviewer_id=self.reviewer_id)
        if invalid is not None:
            return invalid
        if not self.reviewers:
            return _review_result(
                "privacy_reviewer_unavailable",
                reviewer_id=self.reviewer_id,
            )

        results: list[PrivacyReviewResult] = []
        for reviewer in self.reviewers:
            try:
                result = reviewer.review(text)
                if not isinstance(result, PrivacyReviewResult):
                    raise TypeError("invalid privacy reviewer result")
            except Exception:
                result = _review_result(
                    "privacy_reviewer_error",
                    reviewer_id=self.reviewer_id,
                )
            results.append(result)

        decision: PrivacyReviewDecision
        if any(result.decision == "block" for result in results):
            decision = "block"
        elif any(result.decision == "review" for result in results):
            decision = "review"
        else:
            decision = "allow"

        reason_codes = _unique_sorted(
            code for result in results for code in result.reason_codes
        )
        finding_types = _unique_sorted(
            finding for result in results for finding in result.finding_types
        )
        reviewer_ids = _unique_sorted(
            identity for result in results for identity in result.reviewer_ids
        )
        numeric_results = [
            result for result in results if result.model_confidence is not None
        ]
        model_confidence = (
            numeric_results[0].model_confidence
            if len(numeric_results) == 1
            else None
        )
        model_margin = (
            numeric_results[0].model_margin
            if len(numeric_results) == 1
            else None
        )
        return PrivacyReviewResult(
            decision=decision,
            reason_codes=reason_codes,
            finding_types=finding_types,
            finding_count=sum(result.finding_count for result in results),
            model_confidence=model_confidence,
            model_margin=model_margin,
            reviewer_ids=reviewer_ids,
        )


CompositePrivacyReviewer = FailClosedPrivacyReviewer


def build_required_m7_privacy_reviewer(
    *,
    runtime_dir: Path,
    model_dir: Path,
    expected_presidio_version: str,
    expected_spacy_version: str,
    expected_spacy_model_version: str,
    expected_semantic_model_id: str,
    expected_semantic_model_version: str,
    expected_semantic_model_sha256: str,
    expected_semantic_manifest_sha256: str,
    spacy_model_name: str = "zh_core_web_sm",
    presidio_score_threshold: float = 0.45,
    minimum_safe_confidence: float = 0.90,
    minimum_safe_margin: float = 0.30,
    minimum_sensitive_confidence: float = 0.65,
    max_text_characters: int = 50_000,
) -> FailClosedPrivacyReviewer:
    """Build the required entity-plus-semantic outbound privacy gate.

    Both components are always present in the ensemble.  Either builder may
    return a deny-all reviewer, but a missing component can never silently
    degrade the gate to the remaining backend.
    """

    entity_reviewer = build_presidio_spacy_reviewer(
        expected_model_version=expected_spacy_model_version,
        expected_presidio_version=expected_presidio_version,
        expected_spacy_version=expected_spacy_version,
        model_name=spacy_model_name,
        score_threshold=presidio_score_threshold,
        max_text_characters=max_text_characters,
    )
    semantic_reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=runtime_dir,
        expected_model_id=expected_semantic_model_id,
        expected_model_version=expected_semantic_model_version,
        expected_model_sha256=expected_semantic_model_sha256,
        expected_manifest_sha256=expected_semantic_manifest_sha256,
        minimum_safe_confidence=minimum_safe_confidence,
        minimum_safe_margin=minimum_safe_margin,
        minimum_sensitive_confidence=minimum_sensitive_confidence,
        max_text_characters=max_text_characters,
    )
    return FailClosedPrivacyReviewer(
        reviewers=(entity_reviewer, semantic_reviewer),
        ensemble_id="m7-required-privacy-v1",
    )


def normalize_privacy_text(text: str) -> str:
    """Apply the versioned normalization expected by the semantic artifact."""

    if type(text) is not str:
        raise TypeError("privacy text must be a string")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split())


def _invalid_text_result(
    text: object,
    *,
    reviewer_id: str,
) -> PrivacyReviewResult | None:
    if type(text) is not str or not text.strip():
        return _review_result(
            "privacy_input_invalid",
            reviewer_id=reviewer_id,
        )
    return None


def _review_result(reason_code: str, *, reviewer_id: str) -> PrivacyReviewResult:
    return PrivacyReviewResult(
        decision="review",
        reason_codes=(reason_code,),
        reviewer_ids=(reviewer_id,),
    )


def _presidio_entity_type(raw: object) -> str:
    entity_type = getattr(raw, "entity_type")
    if not isinstance(entity_type, str):
        raise ValueError("invalid entity type")
    normalized = entity_type.strip().upper()
    _validate_entity_type(normalized)
    return normalized


def _presidio_score(raw: object) -> float:
    score = getattr(raw, "score")
    _validate_probability(score, field_name="Presidio score")
    return float(score)


def _public_presidio_finding(entity_type: str) -> str:
    public = entity_type.lower()
    if _SAFE_TOKEN.fullmatch(public) is None:
        return "other_pii"
    return public


def _validate_entity_type(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is None
    ):
        raise ValueError("privacy entity type is invalid")


def _validate_tokens(values: object, *, field_name: str) -> None:
    if type(values) is not tuple:
        raise ValueError(f"privacy review {field_name} are invalid")
    try:
        if len(values) != len(set(values)):
            raise ValueError(f"privacy review {field_name} are invalid")
    except TypeError:
        raise ValueError(f"privacy review {field_name} are invalid") from None
    for value in values:
        _validate_token(value, field_name=field_name)


def _validate_token(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise ValueError(f"privacy review {field_name} is invalid")


def _validate_probability(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"privacy review {field_name} is invalid")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError(f"privacy review {field_name} is invalid")


def _validate_optional_probability(value: object, *, field_name: str) -> None:
    if value is not None:
        _validate_probability(value, field_name=field_name)


def _unique_sorted(values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _installed_package_version(
    package_name: str,
    expected_version: str,
) -> str:
    _validate_package_version(expected_version)
    installed_version = importlib.metadata.version(package_name)
    _validate_package_version(installed_version)
    if installed_version != expected_version:
        raise RuntimeError("optional privacy package version mismatch")
    return installed_version


def _validate_package_version(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character.isspace() for character in value)
    ):
        raise ValueError("optional privacy package version is invalid")


class _ArtifactValidationError(RuntimeError):
    pass


def _validate_external_identity(
    model_id: object,
    model_version: object,
    model_sha256: object,
    manifest_sha256: object,
) -> None:
    _validate_model_token(model_id, max_length=96)
    _validate_model_token(model_version, max_length=56)
    _validated_sha256(model_sha256)
    _validated_sha256(manifest_sha256)


def _validated_model_root(model_dir: object, *, runtime_dir: object) -> Path:
    if (
        not isinstance(model_dir, Path)
        or not model_dir.is_absolute()
        or ".." in model_dir.parts
        or not isinstance(runtime_dir, Path)
        or not runtime_dir.is_absolute()
        or ".." in runtime_dir.parts
    ):
        raise _ArtifactValidationError
    try:
        runtime_root = runtime_dir.resolve(strict=True)
        if not stat.S_ISDIR(runtime_root.stat().st_mode):
            raise _ArtifactValidationError
        if model_dir.is_symlink():
            raise _ArtifactValidationError
        root = model_dir.resolve(strict=True)
        if root == runtime_root or not root.is_relative_to(runtime_root):
            raise _ArtifactValidationError
        if not stat.S_ISDIR(root.stat().st_mode):
            raise _ArtifactValidationError
    except _ArtifactValidationError:
        raise
    except (OSError, RuntimeError):
        raise _ArtifactValidationError from None
    return root


def _read_stable_artifact_file(
    root: Path,
    name: str,
    *,
    max_bytes: int,
    file_kind: str,
) -> bytes:
    candidate = root / name
    try:
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode) or candidate.is_symlink():
            raise _ArtifactValidationError
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not stat.S_ISREG(before.st_mode):
            raise _ArtifactValidationError
        _require_single_link(before)
        _require_size_within_limit(before.st_size, max_bytes)

        with candidate.open("rb") as artifact_file:
            opened = os.fstat(artifact_file.fileno())
            _require_stable_snapshot(before, opened)
            _require_single_link(opened)
            _require_size_within_limit(opened.st_size, max_bytes)
            payload = artifact_file.read(max_bytes + 1)
            after = os.fstat(artifact_file.fileno())

        _require_stable_snapshot(opened, after)
        _require_single_link(after)
        if len(payload) != opened.st_size or len(payload) > max_bytes:
            raise _ArtifactValidationError
        return payload
    except _ArtifactValidationError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _ArtifactValidationError from None


def _require_single_link(file_stat: os.stat_result) -> None:
    if file_stat.st_nlink != 1:
        raise _ArtifactValidationError


def _require_size_within_limit(size: int, limit: int) -> None:
    if size < 0 or size > limit:
        raise _ArtifactValidationError


def _require_stable_snapshot(
    expected: os.stat_result,
    actual: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(actual.st_mode)
        or _snapshot_identity(expected) != _snapshot_identity(actual)
    ):
        raise _ArtifactValidationError


def _snapshot_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _parse_manifest(
    payload: bytes,
    *,
    manifest_sha256: str,
) -> _PrivacyManifest:
    try:
        text = payload.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _ArtifactValidationError from None
    return _validated_manifest(raw, manifest_sha256=manifest_sha256)


def _validated_manifest(
    raw: object,
    *,
    manifest_sha256: str,
) -> _PrivacyManifest:
    if not isinstance(raw, dict) or set(raw) != _EXPECTED_MANIFEST_FIELDS:
        raise _ArtifactValidationError
    if (
        raw["schema_version"] != _MANIFEST_SCHEMA_VERSION
        or raw["adapter_type"] != "sklearn_privacy"
        or raw["normalization_version"] != PRIVACY_NORMALIZATION_VERSION
        or raw["labels"] != list(SEMANTIC_PRIVACY_LABELS)
    ):
        raise _ArtifactValidationError

    model_id = _manifest_token(raw["model_id"], max_length=96)
    model_version = _manifest_token(raw["model_version"], max_length=56)
    model_artifact_sha256 = _validated_sha256(raw["model_artifact_sha256"])
    training_dataset_sha256 = _validated_sha256(raw["training_dataset_sha256"])
    library_versions = _validated_library_versions(raw["library_versions"])
    _validated_created_at(raw["created_at"])
    vectorizer_type = _metadata_type(raw["vectorizer"])
    classifier_type = _metadata_type(raw["classifier"])
    provenance = raw["training_provenance"]
    if (
        not isinstance(provenance, dict)
        or not provenance
        or any(
            not isinstance(key, str) or not key.strip() or len(key) > 128
            for key in provenance
        )
    ):
        raise _ArtifactValidationError
    return _PrivacyManifest(
        model_id=model_id,
        model_version=model_version,
        manifest_sha256=_validated_sha256(manifest_sha256),
        model_artifact_sha256=model_artifact_sha256,
        training_dataset_sha256=training_dataset_sha256,
        python_version=library_versions["python"],
        scikit_learn_version=library_versions["scikit_learn"],
        joblib_version=library_versions["joblib"],
        vectorizer_type=vectorizer_type,
        classifier_type=classifier_type,
    )


def _manifest_token(value: object, *, max_length: int) -> str:
    try:
        _validate_model_token(value, max_length=max_length)
    except ValueError:
        raise _ArtifactValidationError from None
    assert isinstance(value, str)
    return value


def _validate_model_token(value: object, *, max_length: int) -> None:
    _validate_token(value, field_name="model identity")
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError("privacy model identity is invalid")


def _validated_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _ArtifactValidationError
    return value


def _validated_library_versions(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != _EXPECTED_LIBRARY_FIELDS
        or any(
            not isinstance(version, str)
            or not version
            or len(version) > 128
            or any(character.isspace() for character in version)
            for version in value.values()
        )
    ):
        raise _ArtifactValidationError
    assert isinstance(value, dict)
    return {
        key: str(value[key])
        for key in sorted(_EXPECTED_LIBRARY_FIELDS)
    }


def _require_runtime_library_versions(manifest: _PrivacyManifest) -> None:
    try:
        runtime_versions = {
            "python": platform.python_version(),
            "scikit_learn": importlib.metadata.version("scikit-learn"),
            "joblib": importlib.metadata.version("joblib"),
        }
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise _ArtifactValidationError from None
    expected_versions = {
        "python": manifest.python_version,
        "scikit_learn": manifest.scikit_learn_version,
        "joblib": manifest.joblib_version,
    }
    if runtime_versions != expected_versions:
        raise _ArtifactValidationError


def _validated_created_at(value: object) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise _ArtifactValidationError
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        raise _ArtifactValidationError from None
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise _ArtifactValidationError


def _metadata_type(value: object) -> str:
    if not isinstance(value, dict) or not value:
        raise _ArtifactValidationError
    metadata_type = value.get("type")
    if not isinstance(metadata_type, str) or not metadata_type.strip():
        raise _ArtifactValidationError
    if any(
        not isinstance(key, str) or not key.strip() or len(key) > 128
        for key in value
    ):
        raise _ArtifactValidationError
    if len(metadata_type) > 128:
        raise _ArtifactValidationError
    return metadata_type


def _validate_artifact(artifact: object, manifest: _PrivacyManifest) -> None:
    predictor = getattr(artifact, "predict_proba", None)
    named_steps = getattr(artifact, "named_steps", None)
    if not callable(predictor) or not isinstance(named_steps, Mapping):
        raise _ArtifactValidationError
    if set(named_steps) != {"tfidf", "classifier"}:
        raise _ArtifactValidationError
    vectorizer = named_steps["tfidf"]
    classifier = named_steps["classifier"]
    if (
        type(vectorizer).__name__ != manifest.vectorizer_type
        or type(classifier).__name__ != manifest.classifier_type
    ):
        raise _ArtifactValidationError
    _validate_model_classes(getattr(artifact, "classes_", None))
    _validate_model_classes(getattr(classifier, "classes_", None))
    _validate_feature_shape(vectorizer, classifier)


def _validate_model_classes(raw_classes: object) -> None:
    classes = _plain_sequence(raw_classes)
    if classes is None or tuple(classes) != SEMANTIC_PRIVACY_LABELS:
        raise _ArtifactValidationError


def _validate_feature_shape(vectorizer: object, classifier: object) -> None:
    get_features = getattr(vectorizer, "get_feature_names_out", None)
    if not callable(get_features):
        raise _ArtifactValidationError
    try:
        vectorizer_features = len(get_features())
        classifier_features = index(getattr(classifier, "n_features_in_"))
        coefficient_shape = tuple(getattr(classifier, "coef_").shape)
    except (AttributeError, TypeError, ValueError):
        raise _ArtifactValidationError from None
    if (
        vectorizer_features <= 0
        or classifier_features != vectorizer_features
        or coefficient_shape
        != (len(SEMANTIC_PRIVACY_LABELS), vectorizer_features)
    ):
        raise _ArtifactValidationError


def _validated_probabilities(
    raw_probabilities: object,
    *,
    class_count: int,
) -> tuple[float, ...]:
    matrix = _plain_sequence(raw_probabilities)
    if matrix is None or len(matrix) != 1:
        raise _ArtifactValidationError
    row = _plain_sequence(matrix[0])
    if row is None or len(row) != class_count:
        raise _ArtifactValidationError
    probabilities: list[float] = []
    for value in row:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise _ArtifactValidationError
        probability = float(value)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise _ArtifactValidationError
        probabilities.append(probability)
    if not math.isclose(
        math.fsum(probabilities),
        1.0,
        rel_tol=_PROBABILITY_TOLERANCE,
        abs_tol=_PROBABILITY_TOLERANCE,
    ):
        raise _ArtifactValidationError
    return tuple(probabilities)


def _plain_sequence(value: object) -> Sequence[object] | None:
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        value = to_list()
    if isinstance(value, (list, tuple)):
        return value
    return None


class _DuplicateManifestKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite manifest value")


__all__ = [
    "CompositePrivacyReviewer",
    "DenyAllPrivacyReviewer",
    "FailClosedPrivacyReviewer",
    "PRIVACY_NORMALIZATION_VERSION",
    "PresidioSpacyPrivacyReviewer",
    "PrivacyReviewDecision",
    "PrivacyReviewResult",
    "PrivacyReviewer",
    "SEMANTIC_PRIVACY_LABELS",
    "SklearnSemanticPrivacyReviewer",
    "build_required_m7_privacy_reviewer",
    "build_presidio_spacy_reviewer",
    "load_sklearn_privacy_reviewer",
    "normalize_privacy_text",
]
