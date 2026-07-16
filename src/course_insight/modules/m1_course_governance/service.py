"""Formal M1 course-governance service boundary."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import read_json, write_json
from course_insight.modules.m1_course_governance.repository import M1Repository


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            code="COURSE_PARSE_FAILED",
            module="m1",
            message="course metadata is missing a required text field",
            details={"field": name},
        )
    return value
class M1CourseGovernanceService:
    """Govern authorized raw course files into one versioned package."""

    def __init__(
        self,
        parser_registry: Any,
        hash_tool: Any,
        repository: M1Repository,
    ) -> None:
        self._parser_registry = parser_registry
        self._hash_tool = hash_tool
        self._repository = repository

    def import_course(
        self,
        raw_course_files: list[Path],
        course_metadata_path: Path,
        source_authorization_path: Path | None,
        output_dir: Path,
    ) -> CoursePackage:
        """Import authorized course material into a governed package.

        原始输入：课程文件、元数据、可选授权表和输出目录。
        契约来源：教师授权的本地课程资料。
        返回消费者：M2 证据检索和 M3 知识包构建。
        业务校验：授权、哈希、版本和逐文件解析结果必须一致。
        错误码：COURSE_PARSE_FAILED。
        """

        if not raw_course_files:
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="course import requires at least one source file",
            )
        if source_authorization_path is None:
            raise DomainError(
                code="UNAUTHORIZED_SOURCE",
                module="m1",
                message="course sources require an authorization manifest",
            )
        metadata = read_json(course_metadata_path)
        if not isinstance(metadata, dict):
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="course metadata must be a JSON object",
            )
        authorization_rows = self._load_authorizations(source_authorization_path)
        source_documents: list[SourceDocument] = []
        source_authorizations: list[SourceAuthorization] = []
        content_chunks: list[ContentChunk] = []
        chunk_number = 0
        for source_path in raw_course_files:
            document, authorization, chunks = self._import_source(
                source_path,
                metadata,
                authorization_rows,
                chunk_number,
            )
            chunk_number += len(chunks)
            source_documents.append(document)
            source_authorizations.append(authorization)
            content_chunks.extend(chunks)

        candidate = CoursePackage(
            course_package_id=_required_text(metadata, "course_package_id"),
            course_id=_required_text(metadata, "course_id"),
            package_version=_required_text(metadata, "package_version"),
            source_documents=source_documents,
            content_chunks=content_chunks,
            source_authorizations=source_authorizations,
            imported_at=_required_text(metadata, "imported_at"),
            status="ready",
            checksum="pending",
        )
        package = CoursePackage.model_validate(
            {**candidate.model_dump(mode="python"), "checksum": candidate.recalculate_checksum()}
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        package.to_json_file(output_dir / "course_package.json")
        write_json(output_dir / "parse_failures.json", [])
        save = getattr(self._repository, "save_course_package", None)
        if callable(save):
            save(package)
        return package

    def _load_authorizations(self, path: Path) -> dict[str, dict[str, str]]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = {
                    row.get("file_name", ""): dict(row)
                    for row in csv.DictReader(source)
                    if row.get("file_name")
                }
        except (OSError, csv.Error) as error:
            raise DomainError(
                code="UNAUTHORIZED_SOURCE",
                module="m1",
                message="source authorization manifest could not be read",
                details={"path": str(path), "reason": type(error).__name__},
            ) from error
        return rows

    def _import_source(
        self,
        path: Path,
        metadata: Mapping[str, Any],
        authorization_rows: Mapping[str, Mapping[str, str]],
        prior_chunk_count: int,
    ) -> tuple[SourceDocument, SourceAuthorization, list[ContentChunk]]:
        if not path.is_file():
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="course source file does not exist",
                details={"path": str(path)},
            )
        authorization_payload = authorization_rows.get(path.name)
        if authorization_payload is None:
            raise DomainError(
                code="UNAUTHORIZED_SOURCE",
                module="m1",
                message="course source is absent from the authorization manifest",
                details={"file_name": path.name},
            )
        parser = self._resolve_parser(path)
        try:
            raw_bytes = path.read_bytes()
            text = parser(path)
        except (OSError, UnicodeError, ValueError) as error:
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="authorized course source could not be parsed",
                details={"file_name": path.name, "reason": type(error).__name__},
            ) from error
        digest = self._hash_bytes(raw_bytes)
        expected_digest = metadata.get("source_sha256")
        if isinstance(expected_digest, str) and digest != expected_digest.casefold():
            raise DomainError(
                code="SOURCE_HASH_MISMATCH",
                module="m1",
                message="course source hash differs from governed metadata",
                details={"file_name": path.name},
            )
        source_id = authorization_payload.get("source_id", "")
        if not source_id:
            raise DomainError(
                code="UNAUTHORIZED_SOURCE",
                module="m1",
                message="authorization entry requires a source identifier",
                details={"file_name": path.name},
            )
        document = SourceDocument(
            source_id=source_id,
            file_name=path.name,
            media_type="text/markdown",
            sha256=digest,
            page_count=None,
            title=_required_text(metadata, "course_name"),
            version=_required_text(metadata, "package_version"),
        )
        authorization = SourceAuthorization(
            source_id=source_id,
            authorized_by=authorization_payload.get("authorized_by", ""),
            license_note=authorization_payload.get("license_note", ""),
            authorized_at=authorization_payload.get("authorized_at", ""),
        )
        paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
        chunks = [
            ContentChunk(
                chunk_id=f"chunk_{prior_chunk_count + index:03d}",
                source_id=source_id,
                text=paragraph,
                locator=f"paragraph:{prior_chunk_count + index}",
                concept_hints=[],
                sha256=hashlib.sha256(paragraph.encode("utf-8")).hexdigest(),
            )
            for index, paragraph in enumerate(paragraphs, start=1)
        ]
        return document, authorization, chunks

    def _resolve_parser(self, path: Path) -> Callable[[Path], str]:
        if not isinstance(self._parser_registry, Mapping):
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="parser registry is unavailable",
            )
        parser = self._parser_registry.get(path.suffix.casefold())
        if not callable(parser):
            raise DomainError(
                code="COURSE_PARSE_FAILED",
                module="m1",
                message="course source type has no registered parser",
                details={"suffix": path.suffix.casefold()},
            )
        return parser

    def _hash_bytes(self, payload: bytes) -> str:
        if not callable(self._hash_tool):
            raise DomainError(
                code="SOURCE_HASH_MISMATCH",
                module="m1",
                message="source hash tool is unavailable",
            )
        try:
            digest = self._hash_tool(payload)
        except (TypeError, ValueError) as error:
            raise DomainError(
                code="SOURCE_HASH_MISMATCH",
                module="m1",
                message="source hash tool rejected the course bytes",
                details={"reason": type(error).__name__},
            ) from error
        if not isinstance(digest, str) or len(digest) != 64:
            raise DomainError(
                code="SOURCE_HASH_MISMATCH",
                module="m1",
                message="source hash tool must return a SHA-256 hexadecimal string",
            )
        return digest.casefold()
