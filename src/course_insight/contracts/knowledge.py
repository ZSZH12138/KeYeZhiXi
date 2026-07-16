"""M3 concept, item, rubric, blueprint, Q-matrix, and bundle contracts."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


_OBJECTIVE_ITEM_TYPES = frozenset(
    {
        "multiple_choice",
        "multiple_select",
        "true_false",
        "numeric",
        "fill_blank",
        "term_blank",
        "objective",
        "选择题",
        "判断题",
        "数值填空题",
        "术语填空题",
    }
)


def _normalized_text(value: str) -> str:
    """Normalize user-maintained labels without changing stored text."""
    return " ".join(value.split()).casefold()


def _require_unique(values: list[Any], *, entity: str) -> None:
    """Raise one stable cross-field error for duplicated identifiers."""

    if len(values) != len(set(values)):
        raise DomainError(
            code="DUPLICATE_IDENTIFIER",
            module="m3",
            message=f"{entity} identifiers must be unique",
            details={"entity": entity},
        )


def _finite_score_sum(
    values: list[float],
    *,
    code: str,
    message: str,
    details: dict[str, Any],
) -> float:
    """Sum scores without exposing numeric-library overflow exceptions."""
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        total = None
    if total is None or not math.isfinite(total):
        raise DomainError(
            code=code,
            module="m3",
            message=message,
            details=details,
        ) from None
    return total


def _copy_json_value(value: Any, *, active_container_ids: set[int]) -> Any:
    """Validate one lossless JSON value while rebuilding every container."""
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if type(value) not in {list, dict}:
        raise ValueError("answer keys accept only lossless JSON values")

    container_id = id(value)
    if container_id in active_container_ids:
        raise ValueError("answer keys must not contain recursive containers")
    active_container_ids.add(container_id)
    try:
        if type(value) is list:
            return [
                _copy_json_value(item, active_container_ids=active_container_ids)
                for item in value
            ]
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _copy_json_value(item, active_container_ids=active_container_ids)
                for key, item in value.items()}
    finally:
        active_container_ids.remove(container_id)


class KnowledgeConcept(ContractModel):
    """Teacher-maintained concept node used across assessment and diagnosis."""

    concept_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    description: str
    aliases: list[str]
    status: str = Field(min_length=1)

    def matches_name(self, text: str) -> bool:
        """Match a normalized canonical name or alias exactly."""
        candidate = _normalized_text(text)
        names = (self.name, *self.aliases)
        return any(candidate == _normalized_text(name) for name in names)


class PrerequisiteRelation(ContractModel):
    """Directed concept relation where ``from`` precedes ``to``."""

    from_concept_id: str = Field(min_length=1)
    to_concept_id: str = Field(min_length=1)
    relation_type: Literal["prerequisite", "related"]
    strength: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    def is_prerequisite(self) -> bool:
        """Return whether the relation participates in prerequisite closure."""
        return self.relation_type == "prerequisite"


class MisconceptionTag(ContractModel):
    """Course-defined misconception linked to one or more concepts."""

    misconception_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    concept_ids: list[str]
    evidence_rules: list[str]

    def validate_business_rules(self) -> None:
        """Reject repeated concept references within one misconception."""

        _require_unique(self.concept_ids, entity="misconception concept")

    def applies_to(self, concept_id: str) -> bool:
        """Return whether the misconception is defined for a concept."""
        return concept_id in self.concept_ids


class RubricCriterion(ContractModel):
    """One auditable scoring point in a teacher-approved rubric."""

    criterion_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    max_score: float = Field(ge=0.0, allow_inf_nan=False)
    expected_student_evidence: str
    course_evidence_ids: list[str]

    def validate_business_rules(self) -> None:
        """Reject repeated course-evidence references."""

        _require_unique(self.course_evidence_ids, entity="criterion evidence")

    def allows(self, score: float) -> bool:
        """Return whether a proposed score is finite and within the maximum."""
        if type(score) not in {int, float}:
            return False
        if type(score) is float and not math.isfinite(score):
            return False
        return 0.0 <= score <= self.max_score


class ReviewPolicy(ContractModel):
    """Thresholds that route uncertain or divergent scores to review."""

    low_confidence_threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    double_score_disagreement_threshold: float = Field(ge=0.0, allow_inf_nan=False)
    require_evidence_for_positive_score: bool

    def needs_review(self, confidence: float, disagreement: float) -> bool:
        """Apply the documented low-confidence or high-disagreement rule."""
        return (
            confidence < self.low_confidence_threshold
            or disagreement > self.double_score_disagreement_threshold
        )


class Rubric(ContractModel):
    """Versioned scoring rubric whose criterion maxima equal its total."""

    rubric_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    total_score: float = Field(ge=0.0, allow_inf_nan=False)
    criteria: list[RubricCriterion]
    review_policy: ReviewPolicy
    status: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Validate criterion identity and rubric score conservation."""

        _require_unique(
            [criterion.criterion_id for criterion in self.criteria],
            entity="rubric criterion",
        )
        criterion_total = self.criterion_score_sum()
        if not math.isclose(
            criterion_total, self.total_score, rel_tol=0.0, abs_tol=1e-9
        ):
            raise DomainError(
                code="RUBRIC_TOTAL_MISMATCH",
                module="m3",
                message="rubric criterion maxima must equal the rubric total",
                details={
                    "rubric_id": self.rubric_id,
                    "criterion_total": criterion_total,
                    "total_score": self.total_score,
                },
            )

    def criterion(self, criterion_id: str) -> RubricCriterion:
        """Return one criterion by identifier."""

        for criterion in self.criteria:
            if criterion.criterion_id == criterion_id:
                return criterion.model_copy(deep=True)
        raise DomainError(
            code="RUBRIC_CRITERION_NOT_FOUND",
            module="m3",
            message="rubric criterion was not found",
            details={"criterion_id": criterion_id},
        )

    def criterion_score_sum(self) -> float:
        """Return a numerically stable sum of criterion maxima."""
        return _finite_score_sum(
            [criterion.max_score for criterion in self.criteria],
            code="RUBRIC_TOTAL_MISMATCH",
            message="rubric criterion maxima must equal the rubric total",
            details={"rubric_id": self.rubric_id},
        )


class ParameterRule(ContractModel):
    """Bounded parameter vocabulary for a teacher-reviewed item template."""

    name: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    minimum: float | None = Field(allow_inf_nan=False)
    maximum: float | None = Field(allow_inf_nan=False)
    choices: list[str]
    constraints: list[str]

    def validate_business_rules(self) -> None:
        """Require an ordered numeric range when both bounds are present."""

        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise DomainError(
                code="PARAMETER_RANGE_INVALID",
                module="m3",
                message="parameter minimum must not exceed its maximum",
                details={"name": self.name},
            )
        _require_unique(self.choices, entity="parameter choice")

    def accepts(self, value: Any) -> bool:
        """Check the small deterministic type, choice, and range vocabulary."""

        normalized_type = _normalized_text(self.value_type)
        if normalized_type in {"integer", "int"}:
            type_matches = type(value) is int
        elif normalized_type in {"number", "float"}:
            type_matches = type(value) in {int, float}
        elif normalized_type in {"string", "str"}:
            type_matches = isinstance(value, str)
        elif normalized_type in {"boolean", "bool"}:
            type_matches = isinstance(value, bool)
        else:
            return False
        if not type_matches:
            return False
        if self.choices:
            try:
                if str(value) not in self.choices:
                    return False
            except (OverflowError, ValueError):
                return False
        if type(value) in {int, float}:
            if type(value) is float and not math.isfinite(value):
                return False
            if self.minimum is not None and value < self.minimum:
                return False
            if self.maximum is not None and value > self.maximum:
                return False
        return True


class ItemCard(ContractModel):
    """Versioned course item with concept, misconception, and evidence links."""

    item_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    stem: str = Field(min_length=1)
    item_type: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    misconception_ids: list[str]
    difficulty_level: int = Field(ge=0)
    cognitive_level: str = Field(min_length=1)
    parameter_rules: list[ParameterRule]
    answer_key: dict[str, Any]
    rubric_id: str | None
    source_evidence_ids: list[str]
    status: str = Field(min_length=1)

    @field_validator("answer_key", mode="before")
    @classmethod
    def _validate_and_copy_answer_key(cls, value: Any) -> dict[str, Any]:
        copied = _copy_json_value(value, active_container_ids=set())
        if type(copied) is not dict:
            raise ValueError("answer_key must be a JSON object")
        return copied

    def validate_business_rules(self) -> None:
        """Validate per-item identifiers without resolving bundle references."""

        _require_unique(self.concept_ids, entity="item concept")
        _require_unique(self.misconception_ids, entity="item misconception")
        rule_names = [rule.name for rule in self.parameter_rules]
        _require_unique(rule_names, entity="parameter rule")
        _require_unique(self.source_evidence_ids, entity="item source evidence")
        if not self.is_objective() and self.rubric_id is None:
            raise DomainError(
                code="SUBJECTIVE_RUBRIC_REQUIRED",
                module="m3",
                message="non-objective items require a rubric reference",
                details={"item_id": self.item_id, "item_type": self.item_type},
            )

    def is_objective(self) -> bool:
        """Classify the documented deterministic item families."""

        return _normalized_text(self.item_type) in _OBJECTIVE_ITEM_TYPES

    def is_approved(self) -> bool:
        """Use the project-plan teacher approval state verbatim."""

        return _normalized_text(self.status) == "teacher_approved"

    def max_score(self, bundle: KnowledgeBundle) -> float:
        """Resolve subjective rubric score or the objective answer-key convention."""

        if self.rubric_id is not None:
            return bundle.get_rubric(self.rubric_id).total_score

        # ItemCard has no objective max-score field, so governed source data keeps
        # this value in answer_key until ItemInstance freezes the paper.
        score = self.answer_key.get("max_score")
        try:
            numeric_score = float(score) if type(score) in {int, float} else None
        except (OverflowError, ValueError):
            numeric_score = None
        if numeric_score is None or not math.isfinite(numeric_score):
            raise DomainError(
                code="ITEM_SCORE_UNDEFINED",
                module="m3",
                message="objective item answer_key must define a finite max_score",
                details={"item_id": self.item_id},
            )
        if numeric_score < 0.0:
            raise DomainError(
                code="ITEM_SCORE_UNDEFINED",
                module="m3",
                message="objective item max_score must not be negative",
                details={"item_id": self.item_id},
            )
        return numeric_score


class BlueprintSection(ContractModel):
    """One score, type, concept, and difficulty constraint group."""

    section_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    item_count: int = Field(ge=0)
    score: float = Field(ge=0.0, allow_inf_nan=False)
    item_types: list[str]
    concept_weights: dict[str, float]
    difficulty_range: tuple[int, int]
    anchor_item_ids: list[str]

    @field_validator("concept_weights")
    @classmethod
    def _validate_concept_weight_ranges(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        invalid_weight = any(
            not math.isfinite(weight) or not 0.0 <= weight <= 1.0
            for weight in value.values()
        )
        if invalid_weight:
            raise ValueError("concept weights must be finite values in [0, 1]")
        return value

    @field_validator("difficulty_range")
    @classmethod
    def _validate_difficulty_bounds(
        cls,
        value: tuple[int, int],
    ) -> tuple[int, int]:
        if any(bound < 0 for bound in value):
            raise ValueError("difficulty bounds must not be negative")
        return value

    def validate_business_rules(self) -> None:
        """Validate score weights, difficulty ordering, and anchor identity."""

        lower, upper = self.difficulty_range
        if lower > upper:
            raise DomainError(
                code="DIFFICULTY_RANGE_INVALID",
                module="m3",
                message="blueprint difficulty lower bound must not exceed upper bound",
                details={"section_id": self.section_id},
            )
        if self.concept_weights and not math.isclose(
            math.fsum(self.concept_weights.values()),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise DomainError(
                code="CONCEPT_WEIGHT_TOTAL_INVALID",
                module="m3",
                message="non-empty concept weights must sum to one",
                details={"section_id": self.section_id},
            )
        _require_unique(self.item_types, entity="blueprint item type")
        _require_unique(self.anchor_item_ids, entity="blueprint anchor item")

    def accepts(self, item: ItemCard) -> bool:
        """Apply type, closed difficulty, and concept-overlap constraints."""

        lower, upper = self.difficulty_range
        type_allowed = not self.item_types or item.item_type in self.item_types
        difficulty_allowed = lower <= item.difficulty_level <= upper
        concept_allowed = not self.concept_weights or bool(
            set(item.concept_ids).intersection(self.concept_weights)
        )
        return type_allowed and difficulty_allowed and concept_allowed


class AssessmentBlueprint(ContractModel):
    """Versioned teacher constraint set for comparable personalized papers."""

    blueprint_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    sections: list[BlueprintSection]
    total_score: float = Field(ge=0.0, allow_inf_nan=False)
    duration_minutes: int = Field(gt=0)
    status: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Validate section identity and total score conservation."""

        _require_unique(
            [section.section_id for section in self.sections],
            entity="blueprint section",
        )
        section_total = self.score_sum()
        if not math.isclose(
            section_total, self.total_score, rel_tol=0.0, abs_tol=1e-9
        ):
            raise DomainError(
                code="BLUEPRINT_TOTAL_MISMATCH",
                module="m3",
                message="blueprint section scores must equal the blueprint total",
                details={"blueprint_id": self.blueprint_id},
            )

    def section(self, section_id: str) -> BlueprintSection:
        """Return one section by identifier."""

        for section in self.sections:
            if section.section_id == section_id:
                return section.model_copy(deep=True)
        raise DomainError(
            code="BLUEPRINT_SECTION_NOT_FOUND",
            module="m3",
            message="assessment blueprint section was not found",
            details={"section_id": section_id},
        )

    def score_sum(self) -> float:
        """Return a numerically stable sum of section scores."""

        return _finite_score_sum(
            [section.score for section in self.sections],
            code="BLUEPRINT_TOTAL_MISMATCH",
            message="blueprint section scores must equal the blueprint total",
            details={"blueprint_id": self.blueprint_id},
        )


class QMatrixEntry(ContractModel):
    """Weighted item-version to concept relation in the Q matrix."""

    item_id: str = Field(min_length=1)
    item_version: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    weight: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    def is_active(self) -> bool:
        """Return whether the item contributes evidence for the concept."""

        return self.weight > 0.0


class KnowledgeBundle(ContractModel):
    """Validated M3 output shared unchanged with M4, M5, M8, and M9."""

    knowledge_bundle_id: str = Field(min_length=1)
    course_package_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    bundle_version: str = Field(min_length=1)
    concepts: list[KnowledgeConcept]
    prerequisite_relations: list[PrerequisiteRelation]
    misconception_tags: list[MisconceptionTag]
    items: list[ItemCard]
    rubrics: list[Rubric]
    blueprints: list[AssessmentBlueprint]
    q_matrix: list[QMatrixEntry]
    status: Literal["draft", "published"]
    published_at: datetime | None

    def validate_business_rules(self) -> None:
        """Validate unique identity, references, publication, and Q alignment."""

        self._validate_unique_identifiers()
        self._validate_references()
        if self.status == "published" and self.published_at is None:
            raise DomainError(
                code="PUBLISHED_AT_REQUIRED",
                module="m3",
                message="a published knowledge bundle requires published_at",
            )
        if self.status == "published":
            unapproved_ids = [
                item.item_id for item in self.items if not item.is_approved()
            ]
            if unapproved_ids:
                raise DomainError(
                    code="UNAPPROVED_ITEM",
                    module="m3",
                    message="published knowledge bundles require approved items",
                    details={"item_ids": unapproved_ids},
                )

    def _validate_unique_identifiers(self) -> None:
        """Apply the version-aware identity rules from the contract table."""

        concept_ids = [concept.concept_id for concept in self.concepts]
        _require_unique(concept_ids, entity="concept")
        _require_unique(
            [tag.misconception_id for tag in self.misconception_tags],
            entity="misconception",
        )
        _require_unique([rubric.rubric_id for rubric in self.rubrics], entity="rubric")
        _require_unique(
            [blueprint.blueprint_id for blueprint in self.blueprints],
            entity="blueprint",
        )
        _require_unique(
            [(item.item_id, item.version) for item in self.items],
            entity="item version",
        )
        _require_unique(
            [
                (entry.item_id, entry.item_version, entry.concept_id)
                for entry in self.q_matrix
            ],
            entity="Q-matrix entry",
        )
        _require_unique(
            [
                (
                    relation.from_concept_id,
                    relation.to_concept_id,
                    relation.relation_type,
                )
                for relation in self.prerequisite_relations
            ],
            entity="concept relation",
        )

    def _validate_references(self) -> None:
        """Resolve every M3-internal link before a bundle crosses modules."""

        concept_ids = {concept.concept_id for concept in self.concepts}
        misconception_ids = {tag.misconception_id for tag in self.misconception_tags}
        rubric_ids = {rubric.rubric_id for rubric in self.rubrics}
        item_by_key = {(item.item_id, item.version): item for item in self.items}
        self._validate_course_structure_references(
            concept_ids=concept_ids,
            misconception_ids=misconception_ids,
            rubric_ids=rubric_ids,
        )
        self._validate_blueprint_references(concept_ids=concept_ids)
        self._validate_q_matrix(
            concept_ids=concept_ids,
            item_by_key=item_by_key,
        )

    def _validate_course_structure_references(
        self,
        *,
        concept_ids: set[str],
        misconception_ids: set[str],
        rubric_ids: set[str],
    ) -> None:
        """Validate concept, misconception, item, and rubric links."""

        for relation in self.prerequisite_relations:
            if (
                relation.from_concept_id not in concept_ids
                or relation.to_concept_id not in concept_ids
            ):
                self._raise_missing_reference("prerequisite concept")
        for tag in self.misconception_tags:
            if not set(tag.concept_ids) <= concept_ids:
                self._raise_missing_reference("misconception concept")
        for item in self.items:
            if not set(item.concept_ids) <= concept_ids:
                self._raise_missing_reference("item concept")
            if not set(item.misconception_ids) <= misconception_ids:
                self._raise_missing_reference("item misconception")
            if item.rubric_id is not None and item.rubric_id not in rubric_ids:
                self._raise_missing_reference("item rubric")

    def _validate_blueprint_references(self, *, concept_ids: set[str]) -> None:
        """Validate course, concept, and approved anchor links in blueprints."""

        item_ids = {item.item_id for item in self.items}
        for blueprint in self.blueprints:
            if blueprint.course_id != self.course_id:
                self._raise_missing_reference("blueprint course")
            for section in blueprint.sections:
                if not set(section.concept_weights) <= concept_ids:
                    self._raise_missing_reference("blueprint concept")
                if not set(section.anchor_item_ids) <= item_ids:
                    self._raise_missing_reference("blueprint anchor item")
                for anchor_id in section.anchor_item_ids:
                    if not any(
                        item.item_id == anchor_id and item.is_approved()
                        for item in self.items
                    ):
                        raise DomainError(
                            code="UNAPPROVED_ITEM",
                            module="m3",
                            message="blueprint anchor items must be approved",
                            details={"item_id": anchor_id},
                        )

    def _validate_q_matrix(
        self,
        *,
        concept_ids: set[str],
        item_by_key: dict[tuple[str, str], ItemCard],
    ) -> None:
        """Validate Q references and exact active alignment with item concepts."""

        active_q_pairs: set[tuple[str, str, str]] = set()
        for entry in self.q_matrix:
            item = item_by_key.get((entry.item_id, entry.item_version))
            if item is None or entry.concept_id not in concept_ids:
                self._raise_q_conflict()
            if entry.is_active():
                if entry.concept_id not in item.concept_ids:
                    self._raise_q_conflict()
                active_q_pairs.add(
                    (entry.item_id, entry.item_version, entry.concept_id)
                )
        declared_pairs = {
            (item.item_id, item.version, concept_id)
            for item in self.items
            for concept_id in item.concept_ids
        }
        if active_q_pairs != declared_pairs:
            self._raise_q_conflict()

    @staticmethod
    def _raise_missing_reference(reference: str) -> None:
        raise DomainError(
            code="KNOWLEDGE_REFERENCE_MISSING",
            module="m3",
            message="knowledge bundle contains an unresolved internal reference",
            details={"reference": reference},
        )

    @staticmethod
    def _raise_q_conflict() -> None:
        raise DomainError(
            code="Q_MATRIX_CONFLICT",
            module="m3",
            message="Q-matrix entries must align with item concepts and versions",
        )

    def get_concept(self, concept_id: str) -> KnowledgeConcept:
        """Return one concept by identifier."""

        for concept in self.concepts:
            if concept.concept_id == concept_id:
                return concept.model_copy(deep=True)
        raise DomainError(
            code="CONCEPT_NOT_FOUND",
            module="m3",
            message="knowledge concept was not found",
            details={"concept_id": concept_id},
        )

    def get_item(self, item_id: str, version: str | None = None) -> ItemCard:
        """Return one exact version, or the sole version when omitted."""

        matches = [item for item in self.items if item.item_id == item_id]
        if version is not None:
            matches = [item for item in matches if item.version == version]
        if not matches:
            raise DomainError(
                code="ITEM_NOT_FOUND",
                module="m3",
                message="knowledge item was not found",
                details={"item_id": item_id, "version": version},
            )
        if version is None and len(matches) > 1:
            raise DomainError(
                code="ITEM_VERSION_REQUIRED",
                module="m3",
                message="item version is required when multiple versions exist",
                details={"item_id": item_id},
            )
        return matches[0].model_copy(deep=True)

    def get_rubric(self, rubric_id: str) -> Rubric:
        """Return one active rubric by identifier."""

        for rubric in self.rubrics:
            if rubric.rubric_id == rubric_id:
                return rubric.model_copy(deep=True)
        raise DomainError(
            code="RUBRIC_NOT_FOUND",
            module="m3",
            message="knowledge rubric was not found",
            details={"rubric_id": rubric_id},
        )

    def get_blueprint(self, blueprint_id: str) -> AssessmentBlueprint:
        """Return one assessment blueprint by identifier."""

        for blueprint in self.blueprints:
            if blueprint.blueprint_id == blueprint_id:
                return blueprint.model_copy(deep=True)
        raise DomainError(
            code="BLUEPRINT_NOT_FOUND",
            module="m3",
            message="assessment blueprint was not found",
            details={"blueprint_id": blueprint_id},
        )

    def approved_items(self) -> list[ItemCard]:
        """Return approved items in source order as a new list."""

        return [
            item.model_copy(deep=True) for item in self.items if item.is_approved()
        ]

    def prerequisite_closure(self, concept_id: str) -> set[str]:
        """Return all transitive prerequisite ancestors, excluding the target."""

        self.get_concept(concept_id)
        incoming: dict[str, list[str]] = {}
        for relation in self.prerequisite_relations:
            if relation.is_prerequisite():
                incoming.setdefault(relation.to_concept_id, []).append(
                    relation.from_concept_id
                )

        closure: set[str] = set()
        visited = {concept_id}
        pending = list(incoming.get(concept_id, ()))
        while pending:
            prerequisite_id = pending.pop()
            if prerequisite_id in visited:
                continue
            visited.add(prerequisite_id)
            closure.add(prerequisite_id)
            pending.extend(incoming.get(prerequisite_id, ()))
        return closure
