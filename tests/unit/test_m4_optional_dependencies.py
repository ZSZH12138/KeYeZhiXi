from __future__ import annotations

from importlib import metadata
import subprocess
import sys
import textwrap

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def test_ml_packages_are_only_declared_for_approved_extras() -> None:
    requirements = [
        Requirement(value)
        for value in metadata.requires("course-insight") or ()
    ]

    def enabled_names(extra: str) -> set[str]:
        return {
            canonicalize_name(requirement.name)
            for requirement in requirements
            if requirement.marker is None
            or requirement.marker.evaluate({"extra": extra})
        }

    optional_names = {"scikit-learn", "joblib"}
    assert optional_names.isdisjoint(enabled_names(""))
    assert optional_names <= enabled_names("intent")
    assert optional_names <= enabled_names("privacy")
    for requirement in requirements:
        if canonicalize_name(requirement.name) in optional_names:
            assert requirement.marker is not None
            assert not requirement.marker.evaluate({"extra": ""})
            assert any(
                requirement.marker.evaluate({"extra": extra})
                for extra in ("intent", "privacy")
            )


def test_rules_import_does_not_import_optional_packages() -> None:
    code = (
        "import sys; "
        "import course_insight.modules.m4_task_orchestration.service; "
        "assert 'sklearn' not in sys.modules; "
        "assert 'joblib' not in sys.modules"
    )

    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_adapter_module_import_does_not_import_optional_packages() -> None:
    code = (
        "import sys; "
        "import course_insight.modules.m4_task_orchestration.sklearn_adapter; "
        "assert 'sklearn' not in sys.modules; "
        "assert 'joblib' not in sys.modules"
    )

    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_missing_optional_packages_only_fail_when_loading_artifact() -> None:
    code = textwrap.dedent(
        """
        import hashlib
        import json
        import sys
        import tempfile
        from pathlib import Path

        class OptionalPackageBlocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition(".")[0] in {"joblib", "sklearn"}:
                    raise ModuleNotFoundError(
                        f"blocked optional package: {fullname}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, OptionalPackageBlocker())

        from course_insight.modules.m4_task_orchestration.sklearn_adapter import (
            IntentArtifactError,
            load_sklearn_intent_adapter,
        )

        assert "sklearn" not in sys.modules
        assert "joblib" not in sys.modules

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "artifact"
            root.mkdir()
            model = b"trusted-admin-artifact-placeholder"
            (root / "model.joblib").write_bytes(model)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "model_id": "fixture",
                        "model_version": "1",
                        "adapter_type": "sklearn",
                        "supported_task_types": [
                            "qa",
                            "diagnostic",
                            "practice",
                            "correction",
                            "stage_assessment",
                        ],
                        "normalization_version": "m4-text-normalization-v1",
                        "training_dataset_checksum": "0" * 64,
                        "model_artifact_checksum": hashlib.sha256(model).hexdigest(),
                        "library_versions": {
                            "python": "3.12",
                            "scikit_learn": "1.9",
                            "joblib": "1.5",
                        },
                        "created_at": "2026-07-27T00:00:00+00:00",
                        "vectorizer": {"type": "TfidfVectorizer"},
                        "classifier": {"type": "LogisticRegression"},
                        "training_provenance": {"seed": 17},
                    }
                ),
                encoding="utf-8",
            )
            try:
                load_sklearn_intent_adapter(
                    root,
                    runtime_dir=root.parent,
                    expected_model_id="fixture",
                    expected_model_version="1",
                    expected_model_sha256=hashlib.sha256(model).hexdigest(),
                )
            except IntentArtifactError as error:
                assert str(error) == "optional intent dependencies are unavailable"
            else:
                raise AssertionError("artifact load unexpectedly succeeded")
        """
    )

    subprocess.run([sys.executable, "-I", "-c", code], check=True)
