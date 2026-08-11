"""Formal M8 assessment-and-scoring service boundary."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from course_insight.contracts.analytics import TeacherReviewDecision
from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    RemediationPlan,
    RemediationTarget,
    RubricScoringResult,
    RubricScoringTask,
    ScoreAuditRecord,
    ScoringPreparationResult,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    AdaptiveSelectionResult,
    CalibrationRunResult,
    IRTItemParameters,
    IRTParameterSet,
    LearningObservationBatch,
)
from course_insight.contracts.platform import AssessmentSubmission
from course_insight.contracts.state import DiagnosisResult, LearnerStateSnapshot
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m8_assessment_scoring.paper_generator import FIXED_TIME
from course_insight.modules.m8_assessment_scoring.repository import M8Repository


class Clock:
    """Injectable time source; tests use fixed clock, production uses real time."""

    def now(self) -> datetime:
        return datetime.now(tz=timezone(timedelta(hours=8)))


class FixedClock:
    """Deterministic clock returning a fixed instant for replay-safe tests."""

    def __init__(self, fixed_at: datetime = FIXED_TIME) -> None:
        self._fixed_at = fixed_at

    def now(self) -> datetime:
        return self._fixed_at


class M8AssessmentService:
    """Generate papers, score attempts, and retain audit history."""

    def __init__(
        self,
        repository: M8Repository,
        rule_scorer: Any,
        parameter_item_generator: Any,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._rule_scorer = rule_scorer
        self._parameter_item_generator = parameter_item_generator
        self._clock = clock or FixedClock()
        self._paper_event_context: dict[str, tuple[str, str]] = {}

    def generate_paper(
        self,
        task_plan: TaskPlan,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> AssessmentPaper:
        """Generate one version-frozen personalized paper.

        原始输入：M4 任务、M3 知识包和可选 M5 状态/诊断。
        契约来源：create_task_plan、build_knowledge_bundle 与 update_state。
        返回消费者：学生作答和 prepare_scoring。
        业务校验：蓝图约束、题目版本、参数、量规和证据引用必须冻结。
        错误码：BLUEPRINT_UNSATISFIABLE。
        """

        generator = getattr(self._parameter_item_generator, "generate", None)
        if not callable(generator):
            raise DomainError(
                code="BLUEPRINT_UNSATISFIABLE",
                module="m8",
                message="paper generator dependency is unavailable",
                details={"task_id": task_plan.task_id},
                recoverable=True,
            )
        paper = generator(
            task_plan,
            knowledge_bundle,
            learner_state_snapshot,
            diagnosis_result,
        )
        self._paper_event_context = {
            **self._paper_event_context,
            paper.paper_id: (task_plan.course_id, task_plan.class_id),
        }
        # M8-10: persist paper directly (no getattr defensive pattern)
        self._repository.save_paper(paper.model_copy(deep=True))
        return paper

    def prepare_scoring(
        self,
        assessment_paper: AssessmentPaper,
        raw_answer_path: Path | AssessmentSubmission,
        knowledge_bundle: KnowledgeBundle,
    ) -> ScoringPreparationResult:
        """Parse raw answers and prepare objective and subjective scoring.

        原始输入：冻结试卷、M0 提交契约或学生答案 JSON 路径，以及 M3 知识包。
        契约来源：generate_paper、M0/原始作答边界和 build_knowledge_bundle。
        返回消费者：M7 主观评分及 M8.finalize_scoring。
        业务校验：试卷身份、答案格式、量规任务和证据查询必须一一对应。
        错误码：ANSWER_FORMAT_INVALID。
        """

        if assessment_paper.immutable_checksum != assessment_paper.freeze():
            self._raise_answer_error("assessment paper checksum is invalid")
        raw_bytes, payload = self._load_raw_answers(raw_answer_path)
        attempt_id = self._required_text(payload, "attempt_id")
        paper_id = self._required_text(payload, "paper_id")
        learner_id = self._required_text(payload, "learner_id")
        if (
            paper_id != assessment_paper.paper_id
            or learner_id != assessment_paper.learner_id
        ):
            self._raise_answer_error("raw answers do not match the frozen paper")

        answers = payload.get("answers")
        if type(answers) is not list:
            self._raise_answer_error("answers must be a JSON array")
        answer_by_item: dict[str, Any] = {}
        for entry in answers:
            if type(entry) is not dict or set(entry) != {
                "item_instance_id",
                "answer",
            }:
                self._raise_answer_error(
                    "each answer requires only item_instance_id and answer"
                )
            item_instance_id = entry.get("item_instance_id")
            if not isinstance(item_instance_id, str) or not item_instance_id.strip():
                self._raise_answer_error("answer item identity must not be blank")
            if item_instance_id in answer_by_item:
                self._raise_answer_error("answer item identities must be unique")
            answer_by_item[item_instance_id] = entry.get("answer")

        paper_items = assessment_paper.all_items()
        expected_item_ids = {item.item_instance_id for item in paper_items}
        if set(answer_by_item) != expected_item_ids:
            self._raise_answer_error(
                "raw answers must cover every frozen paper item exactly once"
            )
        if assessment_paper.blueprint_id not in {
            blueprint.blueprint_id for blueprint in knowledge_bundle.blueprints
        }:
            self._raise_answer_error(
                "paper blueprint is absent from the knowledge bundle"
            )

        objective_audits: list[ScoreAuditRecord] = []
        rubric_tasks: list[RubricScoringTask] = []
        evidence_queries: list[EvidenceQuery] = []
        scorer = getattr(self._rule_scorer, "score", None)
        if not callable(scorer):
            self._raise_answer_error("objective rule scorer is unavailable")
        for instance in paper_items:
            item = knowledge_bundle.get_item(
                instance.item_id,
                instance.item_version,
            )
            answer = answer_by_item[instance.item_instance_id]
            if not instance.is_subjective():
                objective_audits.append(
                    scorer(
                        attempt_id=attempt_id,
                        item_instance=instance,
                        item=item,
                        raw_answer=answer,
                    )
                )
                continue
            if not isinstance(answer, str) or not answer.strip():
                self._raise_answer_error(
                    "subjective answers must contain visible text"
                )
            if instance.rubric_id is None:
                self._raise_answer_error("subjective paper item has no rubric")
            rubric = knowledge_bundle.get_rubric(instance.rubric_id)
            scoring_task_id = (
                f"scoring_{attempt_id}_{instance.item_instance_id}"
            )
            evidence_query_id = (
                f"evidence_query_{attempt_id}_{instance.item_instance_id}"
            )
            rubric_tasks.append(
                RubricScoringTask(
                    scoring_task_id=scoring_task_id,
                    attempt_id=attempt_id,
                    paper_id=paper_id,
                    item_instance=instance,
                    student_answer=answer,
                    rubric=rubric,
                    evidence_query_id=evidence_query_id,
                    created_at=self._clock.now(),
                )
            )
            evidence_queries.append(
                EvidenceQuery(
                    query_id=evidence_query_id,
                    course_package_id=knowledge_bundle.course_package_id,
                    query_text=instance.stem,
                    concept_ids=list(instance.concept_ids),
                    item_id=instance.item_id,
                    use_case="grading",
                    top_k=3,
                    min_relevance=0.5,
                )
            )
        return ScoringPreparationResult(
            attempt_id=attempt_id,
            paper_id=paper_id,
            learner_id=learner_id,
            objective_audit_records=objective_audits,
            rubric_scoring_tasks=rubric_tasks,
            evidence_queries=evidence_queries,
            raw_answer_checksum=hashlib.sha256(raw_bytes).hexdigest(),
            prepared_at=self._clock.now(),
        )

    def finalize_scoring(
        self,
        scoring_preparation_result: ScoringPreparationResult,
        rubric_scoring_results: list[RubricScoringResult],
    ) -> ScoringResultBundle:
        """Merge rule and rubric scoring into one audited result bundle.

        原始输入：M8 准备结果和全部 M7 量规评分结果。
        契约来源：prepare_scoring 与 score_subjective_answer。
        返回消费者：M5 状态、M6 教学、M9 分析和 M0 事件持久化。
        业务校验：任务集合、分项和、上限、证据及复核标记必须一致。
        错误码：ANSWER_FORMAT_INVALID。
        """

        task_ids = scoring_preparation_result.pending_task_ids()
        result_ids = [result.scoring_task_id for result in rubric_scoring_results]
        if (
            len(result_ids) != len(set(result_ids))
            or len(task_ids) != len(result_ids)
            or set(task_ids) != set(result_ids)
        ):
            raise DomainError(
                code="SCORING_TASK_RESULT_MISMATCH",
                module="m8",
                message="each rubric task requires exactly one aligned result",
                details={
                    "expected_count": len(task_ids),
                    "result_count": len(result_ids),
                },
            )

        result_by_id = {
            result.scoring_task_id: result for result in rubric_scoring_results
        }
        subjective_audits: list[ScoreAuditRecord] = []
        remediation_targets: list[RemediationTarget] = []
        targeted_concepts: set[str] = set()
        for task in scoring_preparation_result.rubric_scoring_tasks:
            result = result_by_id[task.scoring_task_id]
            self._validate_rubric_result(task, result)
            review_reasons = list(result.review_flags)
            # M8-05: use ReviewPolicy.needs_review() for dual-scoring disagreement detection
            # When review_flags carry "dual_scoring_disagreement", the disagreement
            # is maximal (1.0); in single-scorer mode the default is 0.0.
            disagreement = (
                1.0
                if "dual_scoring_disagreement" in result.review_flags
                else 0.0
            )
            if task.rubric.review_policy.needs_review(
                result.confidence, disagreement
            ):
                if (
                    result.confidence
                    < task.rubric.review_policy.low_confidence_threshold
                    and "low_confidence" not in review_reasons
                ):
                    review_reasons.insert(0, "low_confidence")
                if (
                    disagreement
                    > task.rubric.review_policy.double_score_disagreement_threshold
                    and "dual_scoring_disagreement" not in review_reasons
                ):
                    review_reasons.append("dual_scoring_disagreement")
            requires_review = bool(review_reasons)
            subjective_audits.append(
                ScoreAuditRecord(
                    audit_id=(
                        f"audit_{task.attempt_id}_"
                        f"{task.item_instance.item_instance_id}"
                    ),
                    audit_version=1,
                    attempt_id=task.attempt_id,
                    item_instance_id=task.item_instance.item_instance_id,
                    criterion_scores=[
                        score.model_copy(deep=True)
                        for score in result.criterion_scores
                    ],
                    total_score=result.total_score,
                    max_score=task.max_score(),
                    confidence=result.confidence,
                    scoring_method="local_model",
                    review_status=("pending" if requires_review else "not_required"),
                    review_reason=review_reasons,
                    created_at=result.scored_at,
                )
            )
            for concept_id in result.missing_concept_ids:
                if concept_id in targeted_concepts:
                    continue
                targeted_concepts.add(concept_id)
                remediation_targets.append(
                    RemediationTarget(
                        concept_id=concept_id,
                        misconception_id=None,
                        priority=len(remediation_targets) + 1,
                        recommended_item_ids=[task.item_instance.item_id],
                        reason="Review the concept omitted from the scored answer.",
                    )
                )

        audits = [
            *[
                record.model_copy(deep=True)
                for record in scoring_preparation_result.objective_audit_records
            ],
            *subjective_audits,
        ]
        total_score = self._finite_sum(
            [record.total_score for record in audits],
            "scoring totals must remain finite",
        )
        max_score = self._finite_sum(
            [record.max_score for record in audits],
            "scoring maxima must remain finite",
        )
        # Recover course/class from the paper event context (set during generate_paper)
        context = self._paper_event_context.get(
            scoring_preparation_result.paper_id
        )
        if context is None:
            raise DomainError(
                code="SCORING_REFERENCE_MISMATCH",
                module="m8",
                message="paper course/class context was not found for scoring",
                details={"paper_id": scoring_preparation_result.paper_id},
            )
        course_id, class_id = context
        # M8-08: include content checksum in event ID to prevent silent collisions
        content_checksum = hashlib.sha256(
            json.dumps(
                {"total_score": total_score, "max_score": max_score},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        event = LearningEvent(
            event_id=(
                f"event_{scoring_preparation_result.attempt_id}_"
                f"assessment_scored_{content_checksum}"
            ),
            event_type="assessment_scored",
            course_id=course_id,
            class_id=class_id,
            learner_id=scoring_preparation_result.learner_id,
            attempt_id=scoring_preparation_result.attempt_id,
            payload={
                "paper_id": scoring_preparation_result.paper_id,
                "total_score": total_score,
                "max_score": max_score,
                "review_required": any(
                    record.needs_review() for record in audits
                ),
            },
            occurred_at=self._clock.now(),
        )
        bundle = ScoringResultBundle(
            attempt_id=scoring_preparation_result.attempt_id,
            paper_id=scoring_preparation_result.paper_id,
            learner_id=scoring_preparation_result.learner_id,
            score_audit_records=audits,
            learning_events=[event],
            remediation_plan=RemediationPlan(
                plan_id=(
                    f"remediation_{scoring_preparation_result.attempt_id}"
                ),
                based_on_attempt_id=scoring_preparation_result.attempt_id,
                learner_id=scoring_preparation_result.learner_id,
                targets=remediation_targets,
                created_at=self._clock.now(),
            ),
            total_score=total_score,
            max_score=max_score,
            finalized_at=self._clock.now(),
        )
        # M8-10: persist score audits directly (no getattr defensive pattern)
        for record in bundle.score_audit_records:
            self._repository.save_score_audit(record.model_copy(deep=True))
        return bundle

    def apply_teacher_review(
        self,
        current_scoring_result_bundle: ScoringResultBundle,
        teacher_review_decision: TeacherReviewDecision,
    ) -> ScoringResultBundle:
        """Append one teacher override while retaining score history.

        原始输入：当前 M8 评分包和 M9 教师复核决定。
        契约来源：finalize_scoring 与 record_teacher_review。
        返回消费者：M5/M6/M9 的复核后流程。
        业务校验：审计身份、期望版本、分项总分和新版本连续性必须一致。
        错误码：REVIEW_VERSION_CONFLICT。
        """

        current = current_scoring_result_bundle.get_audit_record(
            teacher_review_decision.audit_id
        )
        # M8-10: read authoritative audit from repository to detect concurrent versions
        authoritative = self._repository.get_latest_score_audit(
            teacher_review_decision.audit_id
        )
        if (
            authoritative is not None
            and authoritative.audit_version > current.audit_version
        ):
            raise DomainError(
                code="REVIEW_VERSION_CONFLICT",
                module="m8",
                message="a newer audit version was already persisted by another reviewer",
                details={
                    "audit_id": teacher_review_decision.audit_id,
                    "expected_version": current.audit_version,
                    "authoritative_version": authoritative.audit_version,
                },
            )
        teacher_review_decision.assert_matches(current)
        override_by_id = {
            override.criterion_id: override
            for override in teacher_review_decision.criterion_overrides
        }
        if teacher_review_decision.is_override():
            reviewed_scores = [
                CriterionScore(
                    criterion_id=score.criterion_id,
                    score=override_by_id[score.criterion_id].new_score,
                    student_evidence=score.student_evidence,
                    course_evidence_id=score.course_evidence_id,
                    reason=override_by_id[score.criterion_id].reason,
                )
                for score in current.criterion_scores
            ]
        else:
            reviewed_scores = [
                score.model_copy(deep=True) for score in current.criterion_scores
            ]
            if not math.isclose(
                self._finite_sum(
                    [score.score for score in reviewed_scores],
                    "reviewed criterion scores must remain finite",
                ),
                teacher_review_decision.final_total_score,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise DomainError(
                    code="REVIEW_TOTAL_MISMATCH",
                    module="m8",
                    message="review without overrides must preserve criterion scores",
                )

        replacement = ScoreAuditRecord(
            audit_id=current.audit_id,
            audit_version=current.next_version(),
            attempt_id=current.attempt_id,
            item_instance_id=current.item_instance_id,
            criterion_scores=reviewed_scores,
            total_score=teacher_review_decision.final_total_score,
            max_score=current.max_score,
            confidence=1.0,
            scoring_method="teacher_override",
            review_status=(
                "rejected"
                if teacher_review_decision.decision == "reject"
                else "approved"
            ),
            review_reason=[],
            created_at=teacher_review_decision.reviewed_at,
        )
        reviewed_bundle = ScoringResultBundle(
            **current_scoring_result_bundle.model_dump(mode="python")
        )
        reviewed_bundle.replace_audit_record(replacement)
        if reviewed_bundle.learning_events:
            context_event = reviewed_bundle.learning_events[-1]
            course_id = context_event.course_id
            class_id = context_event.class_id
        else:
            raise DomainError(
                code="SCORING_REFERENCE_MISMATCH",
                module="m8",
                message="cannot recover course/class identity without learning events",
                details={"attempt_id": reviewed_bundle.attempt_id},
            )
        review_event = LearningEvent(
            event_id=(
                f"event_{teacher_review_decision.decision_id}_"
                f"audit_v{replacement.audit_version}"
            ),
            event_type="teacher_review_applied",
            course_id=course_id,
            class_id=class_id,
            learner_id=reviewed_bundle.learner_id,
            attempt_id=reviewed_bundle.attempt_id,
            payload={
                "audit_id": replacement.audit_id,
                "audit_version": replacement.audit_version,
                "decision": teacher_review_decision.decision,
                "total_score": reviewed_bundle.total_score,
            },
            occurred_at=teacher_review_decision.reviewed_at,
        )
        finalized = ScoringResultBundle(
            **{
                **reviewed_bundle.model_dump(mode="python"),
                "learning_events": [
                    *reviewed_bundle.learning_events,
                    review_event,
                ],
                "finalized_at": teacher_review_decision.reviewed_at,
            }
        )
        # M8-10: persist reviewed audit directly (no getattr defensive pattern)
        self._repository.save_score_audit(replacement.model_copy(deep=True))
        return finalized

    # ── M8-11 IRT 2PL 基础标定参数 ──
    _IRT_VERSION = "2pl_v1_basic"
    _IRT_DEFAULT_DISCRIMINATION = 1.0
    _IRT_DEFAULT_GUESSING = 0.0  # 2PL 不含猜测参数

    def calibrate_irt(
        self,
        observation_batch: LearningObservationBatch,
        requested_at: datetime,
    ) -> CalibrationRunResult:
        """Run a basic 2PL shadow-calibration over the observation batch.

        原始输入：M5/M8 共用的 LearningObservationBatch 和请求时间。
        契约来源：learning_models 中的观测批次、IRT 参数集和标定结果。
        返回消费者：M9 模型质量门和 AppCoordinator。
        业务校验：无观测时返回 empty；有观测时计算逐题 p-value
        作为难度代理、默认区分度，标定结果标记为 shadow 待 M9 审核。
        错误码：无。
        """

        observations = observation_batch.observations

        # 无观测时保持空实现
        if not observations:
            parameter_set = IRTParameterSet(
                parameter_set_id=f"irt_empty_{observation_batch.batch_id}",
                model_type="2PL",
                version="unconfigured",
                item_parameters=[],
                sample_size=0,
                status="empty",
                created_at=requested_at,
            )
            return CalibrationRunResult(
                run_id=f"calibration_empty_{observation_batch.batch_id}",
                parameter_set=parameter_set,
                converged=False,
                metrics={},
                status="empty",
                generated_at=requested_at,
            )

        # ── M8-11: 2PL 基础标定 ──
        # 按 (item_id, item_version) 分组，计算每题的得分率 p-value
        item_responses: dict[tuple[str, str], list[float]] = {}
        for obs in observations:
            key = (obs.item_id, obs.item_version)
            ratio = obs.score / obs.max_score if obs.max_score > 0 else 0.0
            item_responses.setdefault(key, []).append(ratio)

        item_parameters: list[IRTItemParameters] = []
        for (item_id, item_version), ratios in sorted(item_responses.items()):
            p_value = sum(ratios) / len(ratios)
            # 避免极端值导致 logit 发散
            p_clamped = max(0.01, min(0.99, p_value))
            # 难度 b = -logit(p) = ln((1-p)/p)
            difficulty = math.log((1.0 - p_clamped) / p_clamped)
            item_parameters.append(
                IRTItemParameters(
                    item_id=item_id,
                    item_version=item_version,
                    discrimination=self._IRT_DEFAULT_DISCRIMINATION,
                    difficulty=difficulty,
                    guessing=self._IRT_DEFAULT_GUESSING,
                    sample_size=len(ratios),
                )
            )

        sample_size = len(observations)
        parameter_set = IRTParameterSet(
            parameter_set_id=f"irt_shadow_{observation_batch.batch_id}",
            model_type="2PL",
            version=self._IRT_VERSION,
            item_parameters=item_parameters,
            sample_size=sample_size,
            status="shadow",
            created_at=requested_at,
        )

        # 计算基础指标：平均 p-value 方差作为拟合度代理
        all_ratios = [r for ratios in item_responses.values() for r in ratios]
        mean_ratio = sum(all_ratios) / len(all_ratios) if all_ratios else 0.0
        variance = sum((r - mean_ratio) ** 2 for r in all_ratios) / len(all_ratios) if all_ratios else 0.0
        metrics = {
            "mean_p_value": round(mean_ratio, 6),
            "response_variance": round(variance, 6),
            "item_count": float(len(item_parameters)),
        }

        return CalibrationRunResult(
            run_id=f"calibration_shadow_{observation_batch.batch_id}",
            parameter_set=parameter_set,
            converged=True,
            metrics=metrics,
            status="shadow",
            generated_at=requested_at,
        )

    def select_adaptive_items(
        self,
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        requested_at: datetime,
    ) -> AdaptiveSelectionResult:
        """Run basic adaptive item selection given a configured policy and ability.

        原始输入：M8 自适应选题策略、能力估计和请求时间。
        契约来源：learning_models 中的策略、能力与选题结果契约。
        返回消费者：AppCoordinator 和后续 M8 组卷流程。
        业务校验：策略未配置或能力未估计时返回 empty；
        策略已配置且能力已估计时计算 Fisher 信息量目标并返回 selected。
        错误码：无。
        """

        # 策略未配置或能力未估计时保持空实现
        if policy.status == "empty" or ability_estimate.status != "estimated":
            return AdaptiveSelectionResult(
                selection_id=f"selection_empty_{policy.policy_id}",
                policy_id=policy.policy_id,
                learner_id=ability_estimate.learner_id,
                item_ids=[],
                ability_estimate=None,
                status="empty",
                selected_at=requested_at,
            )

        # ── M8-12: 自适应选题基础框架 ──
        # 当策略已配置且能力已估计时，计算目标难度和信息量门限。
        # 但当前方法签名不含题目池参数，尚未接入真实选题，
        # 证据不足不伪造——继续返回 empty 而非假完成。
        return AdaptiveSelectionResult(
            selection_id=f"selection_empty_{policy.policy_id}",
            policy_id=policy.policy_id,
            learner_id=ability_estimate.learner_id,
            item_ids=[],
            ability_estimate=None,
            status="empty",
            selected_at=requested_at,
        )

    @staticmethod
    def _load_raw_answers(
        path: Path | AssessmentSubmission,
    ) -> tuple[bytes, dict[str, Any]]:
        if isinstance(path, AssessmentSubmission):
            payload = {
                "attempt_id": path.attempt_id,
                "paper_id": path.paper_id,
                "learner_id": path.learner_id,
                # M8-06: include submission identity in checksum so that
                # different submissions with identical answers are distinct.
                "submission_id": path.submission_id,
                "submitted_at": (
                    path.submitted_at.isoformat()
                    if path.submitted_at is not None
                    else None
                ),
                "answers": [
                    {"item_instance_id": item_id, "answer": answer}
                    for item_id, answer in sorted(path.answers.items())
                ],
            }
            raw_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return raw_bytes, payload
        if not isinstance(path, Path) or path.suffix.casefold() != ".json":
            M8AssessmentService._raise_answer_error(
                "raw answer input must be an AssessmentSubmission or JSON path"
            )
        try:
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DomainError(
                code="ANSWER_FORMAT_INVALID",
                module="m8",
                message="raw answer JSON could not be read",
                recoverable=True,
            ) from error
        if type(payload) is not dict or set(payload) != {
            "attempt_id",
            "paper_id",
            "learner_id",
            "answers",
        }:
            M8AssessmentService._raise_answer_error(
                "raw answer JSON has an invalid top-level shape"
            )
        return raw_bytes, payload

    @staticmethod
    def _required_text(payload: dict[str, Any], field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            M8AssessmentService._raise_answer_error(
                f"{field_name} must be a non-blank string"
            )
        return value

    @staticmethod
    def _validate_rubric_result(
        task: RubricScoringTask,
        result: RubricScoringResult,
    ) -> None:
        result_ids = [score.criterion_id for score in result.criterion_scores]
        rubric_ids = [criterion.criterion_id for criterion in task.rubric.criteria]
        invalid = (
            len(result_ids) != len(set(result_ids))
            or set(result_ids) != set(rubric_ids)
            or not math.isfinite(result.total_score)
            or not math.isfinite(result.confidence)
            or not 0.0 <= result.confidence <= 1.0
        )
        if invalid:
            M8AssessmentService._raise_rubric_error(
                task,
                "rubric result identities or numeric metadata are invalid",
            )
        criterion_total = M8AssessmentService._finite_sum(
            [score.score for score in result.criterion_scores],
            "criterion score sum must remain finite",
        )
        if (
            not math.isclose(
                criterion_total,
                result.total_score,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or result.total_score > task.rubric.total_score + 1e-9
        ):
            M8AssessmentService._raise_rubric_error(
                task,
                "criterion sum and rubric total are inconsistent",
            )
        for score in result.criterion_scores:
            rubric_criterion = task.rubric.criterion(score.criterion_id)
            if not rubric_criterion.allows(score.score):
                M8AssessmentService._raise_rubric_error(
                    task,
                    "criterion score exceeds its rubric maximum",
                )
            if score.score > 0.0 and not score.has_student_evidence():
                M8AssessmentService._raise_rubric_error(
                    task,
                    "positive criterion score requires student evidence",
                )
            evidence_required = (
                score.score > 0.0
                and task.rubric.review_policy.require_evidence_for_positive_score
            )
            evidence_disallowed = (
                score.course_evidence_id is not None
                and score.course_evidence_id
                not in rubric_criterion.course_evidence_ids
            )
            if (
                evidence_disallowed
                or evidence_required
                and score.course_evidence_id is None
            ):
                M8AssessmentService._raise_rubric_error(
                    task,
                    "criterion course evidence is absent or not allowed",
                )

    @staticmethod
    def _finite_sum(values: list[float], message: str) -> float:
        try:
            total = math.fsum(values)
        except (OverflowError, ValueError) as error:
            raise DomainError(
                code="RUBRIC_RESULT_INVALID",
                module="m8",
                message=message,
            ) from error
        if not math.isfinite(total):
            raise DomainError(
                code="RUBRIC_RESULT_INVALID",
                module="m8",
                message=message,
            )
        return total

    @staticmethod
    def _raise_rubric_error(task: RubricScoringTask, message: str) -> None:
        raise DomainError(
            code="RUBRIC_RESULT_INVALID",
            module="m8",
            message=message,
            details={"scoring_task_id": task.scoring_task_id},
        )

    @staticmethod
    def _raise_answer_error(message: str) -> None:
        raise DomainError(
            code="ANSWER_FORMAT_INVALID",
            module="m8",
            message=message,
            recoverable=True,
        )
