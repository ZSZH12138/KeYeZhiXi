"""Formal M0 platform service boundary."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import EventAck, LearningEvent
from course_insight.contracts.platform import ActorContext, AsyncJobStatus
from course_insight.infrastructure.sqlite import SQLiteM0Repository
from course_insight.modules.m0_platform.event_store import M0EventStore
from course_insight.modules.m0_platform.repository import M0Repository
from course_insight.modules.m0_platform.workflow import AssessmentRun


T = TypeVar("T", bound=ContractModel)
_FIXED_TIME = datetime(
    2026,
    7,
    15,
    9,
    0,
    tzinfo=timezone(timedelta(hours=8)),
)
_HOST_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_SENSITIVE_SNAPSHOT_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "credential",
        "credentials",
        "full_name",
        "password",
        "phone_number",
        "private_key",
        "real_name",
        "refresh_token",
        "secret",
        "student_number",
    }
)


class M0PlatformService:
    """Coordinate local persistence, snapshots, and platform health."""

    def __init__(
        self,
        database_path: Path,
        runtime_dir: Path,
        config_dir: Path,
        *,
        repository: M0Repository | None = None,
    ) -> None:
        self._database_path = database_path
        self._runtime_dir = runtime_dir
        self._config_dir = config_dir
        self._repository: M0Repository = (
            SQLiteM0Repository(database_path)
            if repository is None
            else repository
        )
        self._event_store = M0EventStore(
            repository=self._repository,
            audit_log_path=runtime_dir / "audit" / "learning_events.jsonl",
        )

    def initialize(self) -> None:
        """Initialize the local platform boundary.

        原始输入：构造函数中的数据库、运行目录和配置目录。
        契约来源：M0 平台配置边界。
        返回消费者：全部模块及命令行协调器。
        业务校验：目录可写且数据库迁移可应用。
        错误码：DATABASE_UNAVAILABLE。
        """

        try:
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            self._config_dir.mkdir(parents=True, exist_ok=True)
            if not self._runtime_dir.is_dir() or not self._config_dir.is_dir():
                raise OSError("platform directories are unavailable")
            self._repository.initialize()
        except Exception as error:
            raise DomainError(
                code="DATABASE_UNAVAILABLE",
                module="m0",
                message="local platform storage could not be initialized",
                details={"reason": type(error).__name__},
                recoverable=True,
            ) from error

    def prepare_django_frontend(
        self,
        actor_context: ActorContext,
        requested_at: datetime,
    ) -> AsyncJobStatus:
        """Preserve the legacy empty architecture-scaffold declaration.

        The production Django shell is started and checked through ``manage.py``
        and the M0 health endpoints. This existing method remains a deliberately
        skipped placeholder because ``ArchitectureScaffoldResult`` requires
        every component of that legacy aggregate to be empty or skipped.
        """

        return AsyncJobStatus(
            job_id=f"django_frontend_{actor_context.actor_id}",
            job_type="django_frontend",
            status="skipped",
            progress=0.0,
            result_ref=None,
            error_code=None,
            created_at=requested_at,
            finished_at=requested_at,
        )

    def append_learning_events(self, events: list[LearningEvent]) -> EventAck:
        """Persist replay-safe learning events.

        原始输入：M8 评分结果产生的学习事件列表。
        契约来源：ScoringResultBundle.learning_events。
        返回消费者：总集成、审计日志和重放检查。
        业务校验：event_id 和幂等键唯一且事件负载可序列化。
        错误码：EVENT_PERSIST_FAILED。
        """

        try:
            accepted_ids, duplicate_ids = self._repository.append_events(events)
        except Exception as error:
            raise DomainError(
                code="EVENT_PERSIST_FAILED",
                module="m0",
                message="learning events could not be persisted transactionally",
                details={"event_count": len(events)},
                recoverable=True,
            ) from error
        persisted_at = max(
            (event.occurred_at for event in events),
            default=_FIXED_TIME,
        )
        return EventAck(
            accepted_event_ids=list(accepted_ids),
            duplicate_event_ids=list(duplicate_ids),
            failed_event_ids=[],
            persisted_at=persisted_at,
        )

    def record_assessment_run(self, run: AssessmentRun) -> AssessmentRun:
        """Insert, recover, or safely adopt payload-free workflow metadata."""

        return self._repository.insert_or_get_assessment_run(run)

    def adopt_legacy_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """CAS-fill frozen dependencies on one exact pre-v9 workflow row."""

        return self._repository.adopt_legacy_assessment_run(run)

    def get_assessment_run(self, operation_id: str) -> AssessmentRun | None:
        return self._repository.get_assessment_run(operation_id)

    def list_assessment_runs(self) -> tuple[AssessmentRun, ...]:
        """Load every workflow row for offline legacy inventory."""

        return self._repository.list_assessment_runs()

    def get_assessment_run_by_paper(
        self,
        paper_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        return self._repository.get_assessment_run_by_paper(
            paper_id,
            operation=operation,
            status=status,
        )

    def get_assessment_run_by_attempt(
        self,
        attempt_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        return self._repository.get_assessment_run_by_attempt(
            attempt_id,
            operation=operation,
            status=status,
        )

    def claim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun | None:
        return self._repository.claim_assessment_run(
            operation_id,
            worker_id=worker_id,
            now=now,
            lease_until=lease_until,
        )

    def renew_assessment_run_lease(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        return self._repository.renew_assessment_run_lease(
            operation_id,
            expected_version=expected_version,
            worker_id=worker_id,
            now=now,
            lease_until=lease_until,
        )

    def advance_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        checkpoint: str,
        worker_id: str,
        now: datetime,
        feedback_id: str | None = None,
        report_id: str | None = None,
        scoring_result_checksum: str | None = None,
        state_version: int | None = None,
        previous_state_frozen: bool | None = None,
        previous_learner_snapshot_id: str | None = None,
        previous_learner_state_version: int | None = None,
        previous_class_snapshot_id: str | None = None,
        previous_class_state_version: int | None = None,
        policy_id: str | None = None,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
        artifact_sha256: str | None = None,
        feature_schema_version: str | None = None,
        action_space_version: str | None = None,
        gate_policy_version: str | None = None,
    ) -> AssessmentRun:
        return self._repository.advance_assessment_run(
            operation_id,
            expected_version=expected_version,
            checkpoint=checkpoint,
            worker_id=worker_id,
            now=now,
            feedback_id=feedback_id,
            report_id=report_id,
            scoring_result_checksum=scoring_result_checksum,
            state_version=state_version,
            previous_state_frozen=previous_state_frozen,
            previous_learner_snapshot_id=previous_learner_snapshot_id,
            previous_learner_state_version=previous_learner_state_version,
            previous_class_snapshot_id=previous_class_snapshot_id,
            previous_class_state_version=previous_class_state_version,
            policy_id=policy_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            artifact_sha256=artifact_sha256,
            feature_schema_version=feature_schema_version,
            action_space_version=action_space_version,
            gate_policy_version=gate_policy_version,
        )

    def reclaim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        return self._repository.reclaim_assessment_run(
            operation_id,
            worker_id=worker_id,
            now=now,
            lease_until=lease_until,
        )

    def complete_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
    ) -> AssessmentRun:
        return self._repository.complete_assessment_run(
            operation_id,
            expected_version=expected_version,
            worker_id=worker_id,
            now=now,
        )

    def fail_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        now: datetime,
    ) -> AssessmentRun:
        return self._repository.fail_assessment_run(
            operation_id,
            expected_version=expected_version,
            worker_id=worker_id,
            error_code=error_code,
            now=now,
        )

    def park_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        status: str,
        now: datetime,
        previous_state_frozen: bool | None = None,
        previous_learner_snapshot_id: str | None = None,
        previous_learner_state_version: int | None = None,
        previous_class_snapshot_id: str | None = None,
        previous_class_state_version: int | None = None,
        scoring_result_checksum: str | None = None,
    ) -> AssessmentRun:
        return self._repository.park_assessment_run(
            operation_id,
            expected_version=expected_version,
            worker_id=worker_id,
            status=status,
            now=now,
            previous_state_frozen=previous_state_frozen,
            previous_learner_snapshot_id=previous_learner_snapshot_id,
            previous_learner_state_version=previous_learner_state_version,
            previous_class_snapshot_id=previous_class_snapshot_id,
            previous_class_state_version=previous_class_state_version,
            scoring_result_checksum=scoring_result_checksum,
        )

    def resume_parked_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        return self._repository.resume_parked_assessment_run(
            operation_id,
            worker_id=worker_id,
            now=now,
            lease_until=lease_until,
        )

    def finish_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
        scoring_result_checksum: str | None = None,
        state_version: int | None = None,
        feedback_id: str | None = None,
        report_id: str | None = None,
    ) -> AssessmentRun:
        return self._repository.finish_assessment_run(
            operation_id,
            expected_version=expected_version,
            worker_id=worker_id,
            now=now,
            scoring_result_checksum=scoring_result_checksum,
            state_version=state_version,
            feedback_id=feedback_id,
            report_id=report_id,
        )

    def save_contract_snapshot(self, obj: ContractModel, path: Path) -> Path:
        """Save one validated contract snapshot.

        原始输入：任一 ContractModel 对象和目标文件路径。
        契约来源：所有跨模块根契约输出。
        返回消费者：联调检查、离线重放和人工审计。
        业务校验：对象必须保持契约有效且目标位于允许目录。
        错误码：CONFIG_INVALID。
        """

        target = self._snapshot_path(path)
        try:
            self._validate_snapshot_payload(obj.to_dict())
            target.parent.mkdir(parents=True, exist_ok=True)
            obj.to_json_file(target)
        except Exception as error:
            raise DomainError(
                code="CONFIG_INVALID",
                module="m0",
                message="contract snapshot could not be saved",
                details={"contract": type(obj).__name__},
                recoverable=True,
            ) from error
        return path

    def load_contract_snapshot(self, model_type: type[T], path: Path) -> T:
        """Load one snapshot as its declared contract type.

        原始输入：ContractModel 子类和 UTF-8 JSON 文件路径。
        契约来源：M0 保存的联调快照。
        返回消费者：请求重放的任一模块服务。
        业务校验：JSON 必须通过目标类的结构和业务规则。
        错误码：CONFIG_INVALID。
        """

        target = self._snapshot_path(path)
        try:
            restored = model_type.from_json_file(target)
            self._validate_snapshot_payload(restored.to_dict())
            return restored
        except Exception as error:
            raise DomainError(
                code="CONFIG_INVALID",
                module="m0",
                message="contract snapshot could not be loaded",
                details={"contract": model_type.__name__},
                recoverable=True,
            ) from error

    def health_check(self) -> dict[str, str]:
        """Return component health without exposing secrets.

        原始输入：构造函数中的平台路径配置。
        契约来源：M0 初始化状态和本地资源检查。
        返回消费者：命令行健康检查和总协调器。
        业务校验：仅返回组件状态，不返回凭据或原始配置。
        错误码：DATABASE_UNAVAILABLE。
        """

        try:
            database_ok = self._repository.schema_is_current()
            runtime_ok = self._runtime_dir.is_dir()
            config_ok = self._config_dir.is_dir()
        except Exception as error:
            raise DomainError(
                code="DATABASE_UNAVAILABLE",
                module="m0",
                message="local platform health could not be determined",
                details={"reason": type(error).__name__},
                recoverable=True,
            ) from error
        return {
            "config": "ok" if config_ok else "unavailable",
            "database": "ok" if database_ok else "unavailable",
            "runtime": "ok" if runtime_ok else "unavailable",
        }

    @classmethod
    def _validate_snapshot_payload(cls, value: Any) -> None:
        if type(value) is dict:
            for key, item in value.items():
                if key.casefold() in _SENSITIVE_SNAPSHOT_FIELDS:
                    raise ValueError("snapshot contains a sensitive field")
                cls._validate_snapshot_payload(item)
            return
        if type(value) is list:
            for item in value:
                cls._validate_snapshot_payload(item)
            return
        if isinstance(value, str) and cls._is_host_absolute_path(value):
            raise ValueError("snapshot contains a host absolute path")

    @staticmethod
    def _is_host_absolute_path(value: str) -> bool:
        return bool(_HOST_ABSOLUTE_PATH.match(value))

    def _snapshot_path(self, path: Path) -> Path:
        target = path.resolve()
        runtime_root = self._runtime_dir.resolve()
        if not target.is_relative_to(runtime_root):
            raise DomainError(
                code="CONFIG_INVALID",
                module="m0",
                message="contract snapshots must remain inside the runtime directory",
                recoverable=True,
            )
        return target
