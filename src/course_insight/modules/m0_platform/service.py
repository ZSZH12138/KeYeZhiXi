"""Formal M0 platform service boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import EventAck, LearningEvent
from course_insight.contracts.platform import ActorContext, AsyncJobStatus
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.logging import append_json_log
from course_insight.infrastructure.sqlite import (
    SCHEMA_VERSION,
    connect_sqlite,
    current_schema_version,
    migrate,
)


T = TypeVar("T", bound=ContractModel)
_FIXED_TIME = datetime(
    2026,
    7,
    15,
    9,
    0,
    tzinfo=timezone(timedelta(hours=8)),
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
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = connect_sqlite(self._database_path)
            try:
                migrate(connection)
            finally:
                connection.close()
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

        self._deliver_event_outbox()
        accepted: list[LearningEvent] = []
        duplicate_ids: list[str] = []
        seen_input: set[str] = set()
        connection = None
        try:
            connection = connect_sqlite(self._database_path)
            connection.execute("BEGIN IMMEDIATE")
            for event in events:
                event_id = event.idempotency_key()
                if event_id in seen_input:
                    continue
                seen_input.add(event_id)
                exists = connection.execute(
                    "SELECT 1 FROM m0_learning_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if exists is not None:
                    duplicate_ids.append(event_id)
                    continue
                connection.execute(
                    """
                    INSERT INTO m0_learning_events(
                        event_id, idempotency_key, event_type, occurred_at, payload
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.idempotency_key(),
                        event.event_type,
                        event.occurred_at.isoformat(),
                        dumps_json(event.payload),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO m0_event_outbox(event_id, record)
                    VALUES (?, ?)
                    """,
                    (event.event_id, dumps_json(event.to_dict())),
                )
                accepted.append(event)
            connection.execute("COMMIT")
        except Exception as error:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise DomainError(
                code="EVENT_PERSIST_FAILED",
                module="m0",
                message="learning events could not be persisted transactionally",
                details={"event_count": len(events)},
                recoverable=True,
            ) from error
        finally:
            if connection is not None:
                connection.close()
        self._deliver_event_outbox()
        persisted_at = max(
            (event.occurred_at for event in events),
            default=_FIXED_TIME,
        )
        return EventAck(
            accepted_event_ids=[event.event_id for event in accepted],
            duplicate_event_ids=duplicate_ids,
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
            connection = connect_sqlite(self._database_path)
            try:
                database_ok = current_schema_version(connection) == SCHEMA_VERSION
            finally:
                connection.close()
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

    def _deliver_event_outbox(self) -> None:
        connection = None
        try:
            connection = connect_sqlite(self._database_path)
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT event_id, record FROM m0_event_outbox ORDER BY event_id"
            ).fetchall()
            logged_ids = self._logged_event_ids()
            for row in pending:
                event_id = str(row["event_id"])
                record = json.loads(str(row["record"]))
                if type(record) is not dict or record.get("event_id") != event_id:
                    raise DomainError(
                        code="EVENT_PERSIST_FAILED",
                        module="m0",
                        message="event outbox record is invalid",
                        details={"event_id": event_id},
                        recoverable=True,
                    )
                if event_id not in logged_ids:
                    append_json_log(
                        self._runtime_dir / "audit" / "learning_events.jsonl",
                        record,
                    )
                    logged_ids.add(event_id)
                connection.execute(
                    "DELETE FROM m0_event_outbox WHERE event_id = ?",
                    (event_id,),
                )
            connection.execute("COMMIT")
        except Exception as error:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise DomainError(
                code="EVENT_PERSIST_FAILED",
                module="m0",
                message="learning event audit outbox could not be delivered",
                recoverable=True,
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def _logged_event_ids(self) -> set[str]:
        log_path = self._runtime_dir / "audit" / "learning_events.jsonl"
        if not log_path.exists():
            return set()
        event_ids: set[str] = set()
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if type(record) is not dict or not isinstance(
                    record.get("event_id"),
                    str,
                ):
                    raise DomainError(
                        code="EVENT_PERSIST_FAILED",
                        module="m0",
                        message="learning event audit log contains an invalid row",
                        recoverable=True,
                    )
                event_ids.add(record["event_id"])
        except Exception as error:
            raise DomainError(
                code="EVENT_PERSIST_FAILED",
                module="m0",
                message="learning event audit log could not be scanned",
                recoverable=True,
            ) from error
        return event_ids

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
