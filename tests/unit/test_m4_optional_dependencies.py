from __future__ import annotations

import subprocess
import sys
import textwrap


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
                        "schema_version": 1,
                        "adapter_id": "fixture",
                        "adapter_version": "1",
                        "model_sha256": hashlib.sha256(model).hexdigest(),
                        "labels": [
                            "correction",
                            "diagnostic",
                            "out_of_scope",
                            "practice",
                            "qa",
                            "stage_assessment",
                        ],
                        "vectorizer": {"type": "TfidfVectorizer"},
                        "classifier": {"type": "LogisticRegression"},
                        "training_provenance": {"seed": 17},
                    }
                ),
                encoding="utf-8",
            )
            try:
                load_sklearn_intent_adapter(root)
            except IntentArtifactError as error:
                assert str(error) == "optional intent dependencies are unavailable"
            else:
                raise AssertionError("artifact load unexpectedly succeeded")
        """
    )

    subprocess.run([sys.executable, "-I", "-c", code], check=True)
