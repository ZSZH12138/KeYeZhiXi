"""契约生产者、消费者与允许的模块自历史来源图。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


class ProducerEndpoint(ContractModel):
    """一个契约对象的正式生产方法与返回位置。"""

    method: str = Field(min_length=1)
    output_path: str = Field(min_length=1)


class ConsumerEndpoint(ContractModel):
    """一个契约对象进入下游时的方法、参数名与取值位置。"""

    method: str = Field(min_length=1)
    parameter: str = Field(min_length=1)
    input_path: str = Field(default="$", min_length=1)


class ContractFlow(ContractModel):
    """一个根契约的生产者和消费者集合。"""

    producers: list[ProducerEndpoint]
    consumers: list[ConsumerEndpoint]
    allow_multiple_producers: bool = False

    def validate_business_rules(self) -> None:
        """拒绝完全重复的边，避免来源图给出矛盾计数。"""

        producer_edges = [
            (producer.method, producer.output_path) for producer in self.producers
        ]
        consumer_edges = [
            (consumer.method, consumer.parameter, consumer.input_path)
            for consumer in self.consumers
        ]
        if len(producer_edges) != len(set(producer_edges)):
            raise DomainError(
                code="DUPLICATE_PROVENANCE_EDGE",
                module="contracts",
                message="producer provenance edges must be unique",
            )
        if len(consumer_edges) != len(set(consumer_edges)):
            raise DomainError(
                code="DUPLICATE_PROVENANCE_EDGE",
                module="contracts",
                message="consumer provenance edges must be unique",
            )


class SelfHistoryEdge(ContractModel):
    """同一模块显式允许读取自身上一版本输出的边。"""

    contract_name: str = Field(min_length=1)
    producer: ProducerEndpoint
    consumer: ConsumerEndpoint

    def validate_business_rules(self) -> None:
        """自历史的生产者和消费者必须属于同一模块。"""

        producer_module = self.producer.method.partition(".")[0]
        consumer_module = self.consumer.method.partition(".")[0]
        if producer_module != consumer_module:
            raise DomainError(
                code="INVALID_SELF_HISTORY",
                module="contracts",
                message="self-history endpoints must belong to the same module",
                details={"contract_name": self.contract_name},
            )


class ContractProvenanceGraph(ContractModel):
    """可校验的根契约来源图。"""

    contracts: dict[str, ContractFlow]
    allowed_self_history: list[SelfHistoryEdge]

    def validate_business_rules(self) -> None:
        """校验根契约名称和自历史声明本身的唯一性。"""

        if any(not name.strip() for name in self.contracts):
            raise DomainError(
                code="INVALID_CONTRACT_NAME",
                module="contracts",
                message="contract provenance names must not be blank",
            )
        history_keys = [
            (
                history.contract_name,
                history.consumer.method,
                history.consumer.parameter,
            )
            for history in self.allowed_self_history
        ]
        if len(history_keys) != len(set(history_keys)):
            raise DomainError(
                code="DUPLICATE_SELF_HISTORY",
                module="contracts",
                message="self-history declarations must be unique",
            )

    def unproduced_inputs(self) -> list[str]:
        """返回存在消费边但没有正式生产者的根契约名称。"""

        return sorted(
            name
            for name, flow in self.contracts.items()
            if flow.consumers and not flow.producers
        )

    def duplicate_producers(self) -> list[str]:
        """返回未经批准却声明多个生产方法的根契约名称。"""

        return sorted(
            name
            for name, flow in self.contracts.items()
            if len(flow.producers) > 1 and not flow.allow_multiple_producers
        )

    def assert_valid(self) -> None:
        """来源缺失或生产者冲突时抛出确定性业务异常。"""

        unproduced = self.unproduced_inputs()
        duplicates = self.duplicate_producers()
        if unproduced or duplicates:
            raise DomainError(
                code="CONTRACT_PROVENANCE_INVALID",
                module="contracts",
                message="contract provenance graph is incomplete or ambiguous",
                details={
                    "duplicate_producers": duplicates,
                    "unproduced_inputs": unproduced,
                },
            )


def load_contract_provenance(path: Path) -> ContractProvenanceGraph:
    """从 UTF-8 JSON 加载来源图并执行结构与业务校验。"""

    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    return ContractProvenanceGraph.from_dict(payload)
