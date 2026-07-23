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
    ) -> None:
        self._database_path = database_path
        self._runtime_dir = runtime_dir
        self._config_dir = config_dir
        self._repository: M0Repository = SQLiteM0Repository(database_path)
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
            self._event_store.deliver_pending()
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
        """Declare the future Django shell as a deliberately skipped job.

        原始输入：M0 认证边界产生的 ActorContext 和请求时间。
        契约来源：platform.ActorContext 与 platform.AsyncJobStatus。
        返回消费者：AppCoordinator 架构连通性检查。
        业务校验：只保留匿名 actor 标识，不启动 Django 或后台任务。
        错误码：无；当前空实现固定返回 skipped。
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
            # Deliver only records left by prior calls. Newly inserted rows stay
            # observable in the outbox for a later worker/retry cycle.
            self._event_store.deliver_pending()
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
            return model_type.from_json_file(target)
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
