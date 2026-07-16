"""Deterministic local M3 service stub."""

from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService


class _MemoryM3Repository:
    def __init__(self) -> None:
        self.bundles: dict[tuple[str, str], KnowledgeBundle] = {}

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        self.bundles[(bundle.knowledge_bundle_id, bundle.bundle_version)] = bundle

    def get_knowledge_bundle(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> KnowledgeBundle | None:
        return self.bundles.get((knowledge_bundle_id, bundle_version))


def _accept_schema(bundle: KnowledgeBundle) -> bool:
    return bool(bundle.concepts and bundle.items and bundle.blueprints)


class M3KnowledgeBundleServiceStub(M3KnowledgeBundleService):
    """Instantiate M3 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository: M3Repository = _MemoryM3Repository()
        super().__init__(repository, _accept_schema)
