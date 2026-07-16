"""Formal M3 knowledge-bundle service boundary."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    MisconceptionTag,
    PrerequisiteRelation,
    QMatrixEntry,
    Rubric,
)
from course_insight.infrastructure.json_io import read_json
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository


class M3KnowledgeBundleService:
    """Validate seed files into one publishable knowledge bundle."""

    def __init__(self, repository: M3Repository, schema_validator: Any) -> None:
        self._repository = repository
        self._schema_validator = schema_validator
        self._last_course_package: CoursePackage | None = None

    def build_knowledge_bundle(
        self,
        course_package: CoursePackage,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None = None,
        misconception_seed_path: Path | None = None,
    ) -> KnowledgeBundle:
        """Build the versioned knowledge, item, rubric, and blueprint bundle.

        原始输入：M1 课程包、四个必需种子文件和两个可选种子文件。
        契约来源：CoursePackage 与教师确认的本地 JSON 种子。
        返回消费者：M4、M5、M8 和 M9 服务。
        业务校验：Q 矩阵、量规总分、版本和题目审核状态必须一致。
        错误码：Q_MATRIX_CONFLICT。
        """

        if course_package.status != "ready":
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge publication requires a ready course package",
                details={"course_package_id": course_package.course_package_id},
            )
        concept_seed = self._load_seed(concept_seed_path)
        item_seed = self._load_seed(item_seed_path)
        rubric_seed = self._load_seed(rubric_seed_path)
        blueprint_seed = self._load_seed(blueprint_seed_path)
        prerequisite_seed = (
            self._load_seed(prerequisite_seed_path)
            if prerequisite_seed_path is not None
            else {}
        )
        misconception_seed = (
            self._load_seed(misconception_seed_path)
            if misconception_seed_path is not None
            else {}
        )
        try:
            bundle = KnowledgeBundle(
                knowledge_bundle_id=self._seed_text(
                    concept_seed,
                    "knowledge_bundle_id",
                ),
                course_package_id=course_package.course_package_id,
                course_id=course_package.course_id,
                bundle_version=self._seed_text(concept_seed, "bundle_version"),
                concepts=self._models(
                    concept_seed,
                    "concepts",
                    KnowledgeConcept,
                ),
                prerequisite_relations=self._models(
                    prerequisite_seed,
                    "prerequisite_relations",
                    PrerequisiteRelation,
                ),
                misconception_tags=self._models(
                    misconception_seed,
                    "misconception_tags",
                    MisconceptionTag,
                ),
                items=self._models(item_seed, "items", ItemCard),
                rubrics=self._models(rubric_seed, "rubrics", Rubric),
                blueprints=self._models(
                    blueprint_seed,
                    "blueprints",
                    AssessmentBlueprint,
                ),
                q_matrix=self._models(item_seed, "q_matrix", QMatrixEntry),
                status="published",
                published_at=self._seed_text(concept_seed, "published_at"),
            )
        except (TypeError, ValueError) as error:
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed payload failed contract validation",
                details={"reason": type(error).__name__},
            ) from error
        if callable(self._schema_validator):
            validation_result = self._schema_validator(bundle)
            if validation_result is False:
                raise DomainError(
                    code="Q_MATRIX_CONFLICT",
                    module="m3",
                    message="knowledge schema validator rejected the bundle",
                    details={"knowledge_bundle_id": bundle.knowledge_bundle_id},
                )
        self._last_course_package = course_package
        save = getattr(self._repository, "save_knowledge_bundle", None)
        if callable(save):
            save(bundle)
        return bundle

    @staticmethod
    def _load_seed(path: Path) -> dict[str, Any]:
        try:
            payload = read_json(path)
        except (OSError, ValueError) as error:
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed file could not be read",
                details={"path": str(path), "reason": type(error).__name__},
            ) from error
        if not isinstance(payload, dict):
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed root must be a JSON object",
                details={"path": str(path)},
            )
        return payload

    @staticmethod
    def _seed_text(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed is missing required text metadata",
                details={"field": key},
            )
        return value

    @staticmethod
    def _models(
        payload: Mapping[str, Any],
        key: str,
        model_type: type[Any],
    ) -> list[Any]:
        values = payload.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(item, dict) for item in values
        ):
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed collection must contain JSON objects",
                details={"field": key},
            )
        return [model_type.model_validate(item) for item in values]
