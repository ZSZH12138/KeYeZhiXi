from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from course_insight.modules.m7_local_model import privacy_reviewer as target
from course_insight.modules.m7_local_model.privacy_reviewer import (
    PRIVACY_NORMALIZATION_VERSION,
    SEMANTIC_PRIVACY_LABELS,
    DenyAllPrivacyReviewer,
    FailClosedPrivacyReviewer,
    PresidioSpacyPrivacyReviewer,
    PrivacyReviewResult,
    SklearnSemanticPrivacyReviewer,
    build_required_m7_privacy_reviewer,
    load_sklearn_privacy_reviewer,
    normalize_privacy_text,
)


SAFE_PROBABILITIES = (0.95, 0.02, 0.01, 0.01, 0.01)
ADDRESS_PROBABILITIES = (0.02, 0.92, 0.02, 0.02, 0.02)


class _CoefficientMatrix:
    shape = (len(SEMANTIC_PRIVACY_LABELS), 2)


class _FixtureVectorizer:
    def get_feature_names_out(self) -> list[str]:
        return ["feature-a", "feature-b"]


class _FixtureClassifier:
    classes_ = list(SEMANTIC_PRIVACY_LABELS)
    n_features_in_ = 2
    coef_ = _CoefficientMatrix()


class _FixtureArtifact:
    classes_ = list(SEMANTIC_PRIVACY_LABELS)

    def __init__(self, probabilities: object = (SAFE_PROBABILITIES,)) -> None:
        self.named_steps = {
            "tfidf": _FixtureVectorizer(),
            "classifier": _FixtureClassifier(),
        }
        self.probabilities = probabilities
        self.last_text: str | None = None

    def predict_proba(self, texts: list[str]) -> object:
        self.last_text = texts[0]
        return self.probabilities


class _RaisingArtifact(_FixtureArtifact):
    def predict_proba(self, texts: list[str]) -> object:
        raise RuntimeError(f"must not escape: {texts[0]}")


class _AnalyzerResult:
    def __init__(
        self,
        entity_type: str,
        score: float,
        *,
        start: int = 0,
        end: int = 1,
    ) -> None:
        self.entity_type = entity_type
        self.score = score
        self.start = start
        self.end = end


class _Analyzer:
    def __init__(self, results: object) -> None:
        self.results = results

    def analyze(self, **kwargs: object) -> object:
        assert kwargs["language"] == "zh"
        assert kwargs["score_threshold"] == 0.45
        return self.results


def _privacy_manifest(model_bytes: bytes) -> dict[str, object]:
    return {
        "schema_version": "1",
        "model_id": "fixture-privacy",
        "model_version": "1.0.0",
        "adapter_type": "sklearn_privacy",
        "labels": list(SEMANTIC_PRIVACY_LABELS),
        "normalization_version": PRIVACY_NORMALIZATION_VERSION,
        "training_dataset_sha256": "0" * 64,
        "model_artifact_sha256": sha256(model_bytes).hexdigest(),
        "library_versions": {
            "python": target.platform.python_version(),
            "scikit_learn": target.importlib.metadata.version(
                "scikit-learn"
            ),
            "joblib": target.importlib.metadata.version("joblib"),
        },
        "created_at": "2026-08-16T00:00:00+00:00",
        "vectorizer": {"type": "_FixtureVectorizer", "analyzer": "char"},
        "classifier": {
            "type": "_FixtureClassifier",
            "class_weight": "balanced",
        },
        "training_provenance": {
            "dataset_manifest_sha256": "0" * 64,
            "seed": 17,
        },
    }


def _write_artifact(
    runtime_dir: Path,
    *,
    manifest_updates: dict[str, object] | None = None,
) -> tuple[Path, bytes]:
    model_dir = runtime_dir / "privacy-model"
    model_dir.mkdir(parents=True)
    model_bytes = b"trusted fixture joblib bytes"
    (model_dir / "model.joblib").write_bytes(model_bytes)
    manifest = {
        **_privacy_manifest(model_bytes),
        **({} if manifest_updates is None else manifest_updates),
    }
    (model_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return model_dir, model_bytes


def _load_fixture(
    monkeypatch: pytest.MonkeyPatch,
    runtime_dir: Path,
    artifact: object,
) -> SklearnSemanticPrivacyReviewer:
    model_dir, model_bytes = _write_artifact(runtime_dir)
    real_import = target.importlib.import_module

    def fake_import(name: str) -> Any:
        if name == "joblib":
            return SimpleNamespace(load=lambda stream: _loaded(stream, artifact))
        return real_import(name)

    monkeypatch.setattr(target.importlib, "import_module", fake_import)
    reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=runtime_dir,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256=sha256(model_bytes).hexdigest(),
        expected_manifest_sha256=_manifest_sha256(model_dir),
    )
    assert isinstance(reviewer, SklearnSemanticPrivacyReviewer)
    return reviewer


def _loaded(stream: object, artifact: object) -> object:
    assert getattr(stream, "read")() == b"trusted fixture joblib bytes"
    return artifact


def _manifest_sha256(model_dir: Path) -> str:
    return sha256((model_dir / "manifest.json").read_bytes()).hexdigest()


def _fixed_result(
    decision: target.PrivacyReviewDecision,
    *,
    reason: str,
    finding_type: str | None = None,
) -> PrivacyReviewResult:
    return PrivacyReviewResult(
        decision=decision,
        reason_codes=(reason,),
        finding_types=(() if finding_type is None else (finding_type,)),
        finding_count=0 if finding_type is None else 1,
        reviewer_ids=(f"fixture-{decision}",),
    )


class _FixedReviewer:
    def __init__(self, result: PrivacyReviewResult) -> None:
        self.result = result
        self.reviewer_id = result.reviewer_ids[0]

    def review(self, text: str) -> PrivacyReviewResult:
        del text
        return self.result


def test_result_safe_record_never_contains_source_or_match_text() -> None:
    secret = "我的身份证号是11010519491231002X"
    result = PrivacyReviewResult(
        decision="block",
        reason_codes=("presidio_pii_detected",),
        finding_types=("cn_id_card",),
        finding_count=1,
        reviewer_ids=("fixture-reviewer",),
    )

    rendered = repr(result) + json.dumps(result.safe_record(), ensure_ascii=False)

    assert secret not in rendered
    assert "11010519491231002X" not in rendered
    assert set(result.safe_record()) == {
        "decision",
        "reason_codes",
        "finding_types",
        "finding_count",
        "model_confidence",
        "model_margin",
        "reviewer_ids",
    }


def test_normalization_is_nfkc_casefolded_and_whitespace_stable() -> None:
    assert normalize_privacy_text("  Ｍｙ\n NAME\t是 张三  ") == "my name 是 张三"


def test_presidio_blocks_without_returning_text_or_offsets() -> None:
    secret = "我叫张三，住在朝阳路8号"
    reviewer = PresidioSpacyPrivacyReviewer(
        _analyzer=_Analyzer(
            [
                _AnalyzerResult("PERSON", 0.91, start=2, end=4),
                _AnalyzerResult("LOCATION", 0.88, start=7, end=12),
            ]
        ),
        model_name="zh_core_web_sm",
        model_version="3.8.0",
        presidio_version="2.2.364",
        spacy_version="3.8.13",
    )

    result = reviewer.review(secret)

    assert result.decision == "block"
    assert result.finding_types == ("location", "person")
    assert result.finding_count == 2
    rendered = repr(result) + json.dumps(result.safe_record(), ensure_ascii=False)
    assert secret not in rendered
    assert "张三" not in rendered
    assert "start" not in result.safe_record()
    assert "end" not in result.safe_record()


@pytest.mark.parametrize(
    ("text", "entity_type", "start", "end"),
    [
        ("张三证明了这个结论", "PERSON", 0, 2),
        ("北京市的纬度约为北纬40度", "LOCATION", 0, 3),
        ("中国科学院成立于1949年", "ORGANIZATION", 0, 5),
        ("2024年1月1日到2024年1月2日相差9天", "DATE_TIME", 0, 9),
        ("我在北大读书。", "ORGANIZATION", 2, 4),
        ("我就读于清华大学。", "ORGANIZATION", 4, 8),
        ("我的生日是2008年5月1日。", "DATE_TIME", 5, 14),
        ("我是某宗教信徒。", "NRP", 2, 6),
    ],
)
def test_presidio_blocks_every_detected_entity_without_keyword_exceptions(
    text: str,
    entity_type: str,
    start: int,
    end: int,
) -> None:
    reviewer = PresidioSpacyPrivacyReviewer(
        _analyzer=_Analyzer(
            [_AnalyzerResult(entity_type, 0.99, start=start, end=end)]
        ),
        model_name="zh_core_web_sm",
        model_version="3.8.0",
        presidio_version="2.2.364",
        spacy_version="3.8.13",
    )

    result = reviewer.review(text)

    assert result.decision == "block"
    assert result.finding_types == (entity_type.lower(),)


def test_presidio_error_is_review_and_exception_text_is_not_exposed() -> None:
    class RaisingAnalyzer:
        def analyze(self, **kwargs: object) -> object:
            raise RuntimeError(str(kwargs["text"]))

    secret = "我妈妈电话是13800138000"
    reviewer = PresidioSpacyPrivacyReviewer(
        _analyzer=RaisingAnalyzer(),
        model_name="zh_core_web_sm",
        model_version="3.8.0",
        presidio_version="2.2.364",
        spacy_version="3.8.13",
    )

    result = reviewer.review(secret)

    assert result.decision == "review"
    assert result.reason_codes == ("presidio_backend_error",)
    assert secret not in repr(result)


def test_missing_presidio_or_spacy_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(package_name: str) -> str:
        raise target.importlib.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(target.importlib.metadata, "version", unavailable)

    reviewer = target.build_presidio_spacy_reviewer(
        expected_model_version="3.8.0",
        expected_presidio_version="2.2.364",
        expected_spacy_version="3.8.13",
    )

    assert isinstance(reviewer, DenyAllPrivacyReviewer)
    assert reviewer.review("普通数学答案").decision == "review"


def test_required_factory_never_degrades_to_one_available_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entity = _FixedReviewer(
        _fixed_result("allow", reason="presidio_review_passed")
    )
    unavailable_semantic = DenyAllPrivacyReviewer(
        reason_code="semantic_artifact_unavailable",
        reviewer_id="semantic-unavailable",
    )
    monkeypatch.setattr(
        target,
        "build_presidio_spacy_reviewer",
        lambda **_kwargs: entity,
    )
    monkeypatch.setattr(
        target,
        "load_sklearn_privacy_reviewer",
        lambda *_args, **_kwargs: unavailable_semantic,
    )

    reviewer = build_required_m7_privacy_reviewer(
        runtime_dir=tmp_path,
        model_dir=tmp_path / "privacy-model",
        expected_presidio_version="2.2.364",
        expected_spacy_version="3.8.13",
        expected_spacy_model_version="3.8.0",
        expected_semantic_model_id="m7-semantic-privacy",
        expected_semantic_model_version="1",
        expected_semantic_model_sha256="0" * 64,
        expected_semantic_manifest_sha256="0" * 64,
    )

    assert len(reviewer.reviewers) == 2
    result = reviewer.review("普通数学答案")
    assert result.decision == "review"
    assert "semantic_artifact_unavailable" in result.reason_codes


def test_verified_semantic_artifact_allows_only_high_confidence_safe_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = _FixtureArtifact()
    reviewer = _load_fixture(monkeypatch, tmp_path, artifact)

    result = reviewer.review("  Ｘ = ２  ")

    assert result.decision == "allow"
    assert result.model_confidence == pytest.approx(0.95)
    assert result.model_margin == pytest.approx(0.93)
    assert artifact.last_text == "x = 2"


def test_semantic_artifact_blocks_high_confidence_self_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewer = _load_fixture(
        monkeypatch,
        tmp_path,
        _FixtureArtifact((ADDRESS_PROBABILITIES,)),
    )

    result = reviewer.review("我家住在朝阳路8号")

    assert result.decision == "block"
    assert result.reason_codes == ("semantic_sensitive_detected",)
    assert result.finding_types == ("semantic_address",)


def test_semantic_uncertainty_requires_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probabilities = ((0.55, 0.20, 0.10, 0.10, 0.05),)
    reviewer = _load_fixture(
        monkeypatch,
        tmp_path,
        _FixtureArtifact(probabilities),
    )

    result = reviewer.review("张三以每秒五米运动")

    assert result.decision == "review"
    assert result.reason_codes == ("semantic_safe_uncertain",)


@pytest.mark.parametrize(
    "artifact",
    [
        _RaisingArtifact(),
        _FixtureArtifact(((float("nan"), 0.25, 0.25, 0.25, 0.25),)),
        _FixtureArtifact(((0.9, 0.05, 0.05),)),
    ],
)
def test_semantic_runtime_failures_never_allow_or_expose_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: object,
) -> None:
    reviewer = _load_fixture(monkeypatch, tmp_path, artifact)
    secret = "我的病史是哮喘"

    result = reviewer.review(secret)

    assert result.decision == "review"
    assert result.reason_codes == ("semantic_backend_error",)
    assert secret not in repr(result)


def test_bad_artifact_checksum_returns_deny_all(tmp_path: Path) -> None:
    model_dir, _ = _write_artifact(tmp_path)

    reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=tmp_path,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256="f" * 64,
        expected_manifest_sha256=_manifest_sha256(model_dir),
    )

    assert isinstance(reviewer, DenyAllPrivacyReviewer)
    result = reviewer.review("普通数学答案")
    assert result.decision == "review"
    assert result.reason_codes == ("semantic_artifact_unavailable",)


def test_bad_manifest_checksum_fails_before_joblib_deserialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir, model_bytes = _write_artifact(tmp_path)
    real_import = target.importlib.import_module

    def guarded_import(name: str) -> Any:
        if name == "joblib":
            pytest.fail("joblib must not be imported before manifest approval")
        return real_import(name)

    monkeypatch.setattr(target.importlib, "import_module", guarded_import)

    reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=tmp_path,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256=sha256(model_bytes).hexdigest(),
        expected_manifest_sha256="f" * 64,
    )

    assert reviewer.review("普通数学答案").decision == "review"


def test_manifest_runtime_version_mismatch_fails_before_deserialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir, model_bytes = _write_artifact(
        tmp_path,
        manifest_updates={
            "library_versions": {
                "python": "0.0.0",
                "scikit_learn": target.importlib.metadata.version(
                    "scikit-learn"
                ),
                "joblib": target.importlib.metadata.version("joblib"),
            }
        },
    )
    real_import = target.importlib.import_module

    def guarded_import(name: str) -> Any:
        if name == "joblib":
            pytest.fail("version mismatch must fail before joblib load")
        return real_import(name)

    monkeypatch.setattr(target.importlib, "import_module", guarded_import)

    reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=tmp_path,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256=sha256(model_bytes).hexdigest(),
        expected_manifest_sha256=_manifest_sha256(model_dir),
    )

    assert reviewer.review("普通数学答案").decision == "review"


def test_corrupt_joblib_payload_returns_deny_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_dir, model_bytes = _write_artifact(tmp_path)
    real_import = target.importlib.import_module

    def fake_import(name: str) -> Any:
        if name == "joblib":
            def fail_to_load(stream: object) -> object:
                del stream
                raise ValueError("corrupt payload details must not escape")

            return SimpleNamespace(load=fail_to_load)
        return real_import(name)

    monkeypatch.setattr(target.importlib, "import_module", fake_import)

    reviewer = load_sklearn_privacy_reviewer(
        model_dir,
        runtime_dir=tmp_path,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256=sha256(model_bytes).hexdigest(),
        expected_manifest_sha256=_manifest_sha256(model_dir),
    )

    assert isinstance(reviewer, DenyAllPrivacyReviewer)
    result = reviewer.review("普通数学答案")
    assert result.decision == "review"
    assert "corrupt payload" not in repr(result)


def test_artifact_outside_runtime_directory_is_rejected(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    outside_dir, model_bytes = _write_artifact(tmp_path / "outside-root")

    reviewer = load_sklearn_privacy_reviewer(
        outside_dir,
        runtime_dir=runtime_dir,
        expected_model_id="fixture-privacy",
        expected_model_version="1.0.0",
        expected_model_sha256=sha256(model_bytes).hexdigest(),
        expected_manifest_sha256=_manifest_sha256(outside_dir),
    )

    assert reviewer.review("普通数学答案").decision == "review"


def test_empty_ensemble_and_invalid_input_are_fail_closed() -> None:
    reviewer = FailClosedPrivacyReviewer(reviewers=())

    assert reviewer.review("普通数学答案").decision == "review"
    assert reviewer.review("").decision == "review"
    assert reviewer.review(None).decision == "review"  # type: ignore[arg-type]


def test_ensemble_uses_block_then_review_then_allow_precedence() -> None:
    allow = _FixedReviewer(
        _fixed_result("allow", reason="allow_backend_passed")
    )
    review = _FixedReviewer(
        _fixed_result("review", reason="review_backend_uncertain")
    )
    block = _FixedReviewer(
        _fixed_result(
            "block",
            reason="block_backend_detected",
            finding_type="person",
        )
    )

    assert (
        FailClosedPrivacyReviewer((allow, allow)).review("答案").decision
        == "allow"
    )
    assert (
        FailClosedPrivacyReviewer((allow, review)).review("答案").decision
        == "review"
    )
    blocked = FailClosedPrivacyReviewer((review, block, allow)).review("答案")
    assert blocked.decision == "block"
    assert blocked.finding_types == ("person",)


def test_child_reviewer_exception_is_converted_to_review() -> None:
    class RaisingReviewer:
        reviewer_id = "raising-reviewer"

        def review(self, text: str) -> PrivacyReviewResult:
            raise RuntimeError(text)

    secret = "我叫张三"
    result = FailClosedPrivacyReviewer((RaisingReviewer(),)).review(secret)

    assert result.decision == "review"
    assert result.reason_codes == ("privacy_reviewer_error",)
    assert secret not in repr(result)
