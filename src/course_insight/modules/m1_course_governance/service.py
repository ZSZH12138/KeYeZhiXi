"""Formal M1 course-governance service boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import write_json
from course_insight.modules.m1_course_governance.parsers import (
    ParsedBlock,
    ParsedSource,
)
from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m1_course_governance.authorization import (
    _AuthorizationRow,
    parse_authorizations,
)
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_PRIORITY = {
    "UNAUTHORIZED_SOURCE": 0,
    "SOURCE_HASH_MISMATCH": 1,
    "COURSE_PARSE_FAILED": 2,
}
def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError(
            code="COURSE_PARSE_FAILED", module="m1",
            message="course metadata is missing a required text field",
            details={"field": name},
        )
    return value


def _domain(
    code: str,
    message: str,
    *,
    file_name: str | None = None,
    reason: str | None = None,
) -> DomainError:
    details: dict[str, str] = {}
    if file_name is not None:
        details["file_name"] = file_name
    if reason is not None:
        details["reason"] = reason
    return DomainError(code=code, module="m1", message=message, details=details)


class M1CourseGovernanceService:
    """Govern authorized raw course files into one versioned package."""

    def __init__(self, parser_registry: Any, hash_tool: Any, repository: M1Repository) -> None:
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
        """Create a deterministic ready package only from authorized sources."""
        self._prepare_output_dir(output_dir)
        failures: list[dict[str, str]] = []
        if not raw_course_files:
            self._write_failures(output_dir, [self._failure("", _domain("COURSE_PARSE_FAILED", "course import requires at least one source file"))])
            raise _domain("COURSE_PARSE_FAILED", "course import requires at least one source file")
        if source_authorization_path is None:
            self._write_failures(output_dir, [self._failure("", _domain("UNAUTHORIZED_SOURCE", "course sources require an authorization manifest"))])
            raise _domain("UNAUTHORIZED_SOURCE", "course sources require an authorization manifest")
        try:
            metadata_bytes = self._read_input_bytes(course_metadata_path)
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            self._write_failures(output_dir, [self._failure("", _domain("COURSE_PARSE_FAILED", "course metadata could not be read", reason=type(error).__name__))])
            raise _domain("COURSE_PARSE_FAILED", "course metadata could not be read") from error
        if not isinstance(metadata, dict):
            self._write_failures(output_dir, [self._failure("", _domain("COURSE_PARSE_FAILED", "course metadata must be a JSON object"))])
            raise _domain("COURSE_PARSE_FAILED", "course metadata must be a JSON object")
        try:
            authorization_bytes = self._read_input_bytes(source_authorization_path)
            authorizations = parse_authorizations(authorization_bytes)
        except DomainError as error:
            self._write_failures(output_dir, [self._failure("", error)])
            raise
        except OSError as error:
            mapped = _domain(
                "UNAUTHORIZED_SOURCE", "source authorization manifest could not be read",
                reason=type(error).__name__,
            )
            self._write_failures(output_dir, [self._failure("", mapped)])
            raise mapped from error

        paths = sorted(raw_course_files, key=lambda value: (value.name, str(value)))
        file_names = [path.name for path in paths]
        if len(file_names) != len(set(file_names)):
            error = _domain("UNAUTHORIZED_SOURCE", "each imported source file name must be unique")
            self._write_failures(output_dir, [self._failure(name, error) for name in sorted(file_names)])
            raise error
        if len(paths) != 1 and any(row.legacy_hash for row in authorizations.values()):
            error = _domain("UNAUTHORIZED_SOURCE", "legacy authorization hash fallback requires exactly one source")
            self._write_failures(
                output_dir,
                [
                    self._failure(row.file_name, error)
                    for row in authorizations.values()
                    if row.legacy_hash
                ],
            )
            raise error
        expected_names = set(file_names)
        authorized_names = set(authorizations)
        for name in sorted(expected_names ^ authorized_names):
            failures.append(self._failure(name, _domain("UNAUTHORIZED_SOURCE", "authorization manifest sources must exactly match input sources", file_name=name)))
        if failures:
            self._write_failures(output_dir, failures)
            raise _domain("UNAUTHORIZED_SOURCE", "authorization manifest sources must exactly match input sources")

        imported: list[tuple[SourceDocument, SourceAuthorization, list[ContentChunk], bytes]] = []
        for path in paths:
            try:
                imported.append(
                    self._import_source(
                        path,
                        metadata,
                        authorizations,
                        0,
                        source_count=len(paths),
                    )
                )
            except DomainError as error:
                failures.append(self._failure(path.name, error))
        if failures:
            self._write_failures(output_dir, failures)
            first = min(failures, key=lambda value: (_FAILURE_PRIORITY[value["code"]], value["file_name"], value["reason"]))
            raise _domain(first["code"], "course import has governed source failures")

        imported.sort(key=lambda value: (value[0].source_id, value[0].file_name))
        documents = [value[0] for value in imported]
        authorizations_out = [value[1] for value in imported]
        chunks = [chunk for _, _, source_chunks, _ in imported for chunk in source_chunks]
        try:
            candidate = CoursePackage(
                course_package_id=_required_text(metadata, "course_package_id"),
                course_id=_required_text(metadata, "course_id"),
                package_version=_required_text(metadata, "package_version"),
                source_documents=documents,
                content_chunks=chunks,
                source_authorizations=authorizations_out,
                imported_at=_required_text(metadata, "imported_at"),
                status="ready", checksum="pending",
            )
            if candidate.imported_at.tzinfo is None:
                raise ValueError("imported_at must include a timezone")
            if any(item.authorized_at.tzinfo is None for item in authorizations_out):
                raise ValueError("authorized_at must include a timezone")
            package = CoursePackage.model_validate({**candidate.model_dump(mode="python"), "checksum": candidate.recalculate_checksum()})
        except (DomainError, ValidationError, ValueError, TypeError) as error:
            self._write_failures(output_dir, [self._failure("", _domain("COURSE_PARSE_FAILED", "course package contract validation failed", reason=type(error).__name__))])
            raise _domain("COURSE_PARSE_FAILED", "course package contract validation failed") from error
        save_complete = getattr(self._repository, "save_course_import", None)
        save = getattr(self._repository, "save_course_package", None)
        if not callable(save_complete) and not callable(save):
            error = _domain(
                "COURSE_PARSE_FAILED",
                "course package persistence is unavailable",
            )
            self._write_failures(output_dir, [self._failure("", error)])
            raise error
        try:
            if callable(save_complete):
                save_complete(
                    package,
                    CourseImportSnapshot(
                        course_metadata_bytes=metadata_bytes,
                        source_authorization_bytes=authorization_bytes,
                        source_payloads=tuple(
                            SourcePayload(
                                source_id=document.source_id,
                                file_name=document.file_name,
                                raw_bytes=raw_bytes,
                            )
                            for document, _, _, raw_bytes in imported
                        ),
                    ),
                )
            else:
                save(package)
        except DomainError as error:
            if (
                error.module == "m1"
                and error.code in {"COURSE_VERSION_CONFLICT", "COURSE_ARTIFACT_INVALID"}
                and not error.details
            ):
                self._write_failures(output_dir, [self._failure("", error)])
                raise
            self._write_failures(
                output_dir,
                [
                    self._failure(
                        "",
                        _domain(
                            "COURSE_PARSE_FAILED",
                            "course package persistence failed",
                            reason=type(error).__name__,
                        ),
                    )
                ],
            )
            raise _domain(
                "COURSE_PARSE_FAILED",
                "course package persistence failed",
            ) from error
        except Exception as error:
            self._write_failures(
                output_dir,
                [
                    self._failure(
                        "",
                        _domain(
                            "COURSE_PARSE_FAILED",
                            "course package persistence failed",
                            reason=type(error).__name__,
                        ),
                    )
                ],
            )
            raise _domain(
                "COURSE_PARSE_FAILED",
                "course package persistence failed",
            ) from error
        self._write_package(output_dir, package)
        self._write_failures(output_dir, [])
        return package

    def _load_authorizations(self, path: Path) -> dict[str, _AuthorizationRow]:
        try:
            return parse_authorizations(self._read_input_bytes(path))
        except DomainError:
            raise
        except OSError as error:
            raise _domain("UNAUTHORIZED_SOURCE", "source authorization manifest could not be read", reason=type(error).__name__) from error

    def _import_source(
        self,
        path: Path,
        metadata: Mapping[str, Any],
        authorization_rows: Mapping[str, _AuthorizationRow],
        prior_chunk_count: int,
        *,
        source_count: int = 1,
    ) -> tuple[SourceDocument, SourceAuthorization, list[ContentChunk], bytes]:
        del prior_chunk_count
        if not path.is_file():
            raise _domain("COURSE_PARSE_FAILED", "course source file does not exist")
        authorization = authorization_rows.get(path.name)
        if authorization is None:
            raise _domain("UNAUTHORIZED_SOURCE", "course source is absent from the authorization manifest", file_name=path.name)
        try:
            raw_bytes = path.read_bytes()
        except OSError as error:
            raise _domain("COURSE_PARSE_FAILED", "course source could not be read", file_name=path.name, reason=type(error).__name__) from error
        digest = self._hash_bytes(raw_bytes)
        expected = authorization.expected_sha256
        if authorization.legacy_hash and source_count == 1:
            fallback = metadata.get("source_sha256")
            expected = fallback if isinstance(fallback, str) else ""
        if not _SHA256_RE.fullmatch(expected) or not hmac.compare_digest(digest, expected):
            code = "UNAUTHORIZED_SOURCE" if not _SHA256_RE.fullmatch(expected) else "SOURCE_HASH_MISMATCH"
            raise _domain(code, "source authorization hash is invalid" if code == "UNAUTHORIZED_SOURCE" else "course source hash differs from authorization manifest", file_name=path.name)
        parsed = self._parse(path, raw_bytes)
        source_id = authorization.source_id
        try:
            document = SourceDocument(
                source_id=source_id,
                file_name=path.name,
                media_type=parsed.media_type,
                sha256=digest,
                page_count=parsed.page_count,
                title=_required_text(metadata, "course_name"),
                version=_required_text(metadata, "package_version"),
            )
            source_authorization = SourceAuthorization(
                source_id=source_id,
                authorized_by=authorization.authorized_by,
                license_note=authorization.license_note,
                authorized_at=authorization.authorized_at,
            )
        except (ValidationError, DomainError, ValueError, TypeError) as error:
            raise _domain("COURSE_PARSE_FAILED", "source contract validation failed", file_name=path.name, reason=type(error).__name__) from error
        try:
            chunks = [
                ContentChunk(
                    chunk_id=(
                        "chunk_"
                        + hashlib.sha256(
                            (
                                f"{source_id}\0{block.locator}\0"
                                f"{hashlib.sha256(block.text.encode('utf-8')).hexdigest()}"
                            ).encode("utf-8")
                        ).hexdigest()
                    ),
                    source_id=source_id,
                    text=block.text,
                    locator=block.locator,
                    concept_hints=[],
                    sha256=hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                )
                for block in parsed.blocks
            ]
        except Exception as error:
            raise _domain(
                "COURSE_PARSE_FAILED",
                "source chunks could not be constructed",
                file_name=path.name,
                reason=type(error).__name__,
            ) from error
        if not chunks:
            raise _domain("COURSE_PARSE_FAILED", "course source contains no text blocks", file_name=path.name)
        return document, source_authorization, chunks, raw_bytes

    def _parse(self, path: Path, raw_bytes: bytes) -> ParsedSource:
        parser = self._resolve_parser(path)
        try:
            result = parser(path.name, raw_bytes)
            if isinstance(result, ParsedSource):
                return self._canonicalize_parsed_source(result)
            if isinstance(result, str):
                return self._canonicalize_parsed_source(
                    self._legacy_text_source(result)
                )
            raise ValueError("parser did not return ParsedSource")
        except Exception as error:
            raise _domain("COURSE_PARSE_FAILED", "authorized course source could not be parsed", file_name=path.name, reason=type(error).__name__) from error

    @staticmethod
    def _legacy_text_source(text: str) -> ParsedSource:
        normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
        blocks = [part.strip() for part in normalized.split("\n\n") if part.strip()]
        if not blocks:
            raise ValueError("source contains no text blocks")
        return ParsedSource(
            media_type="text/plain",
            page_count=None,
            blocks=tuple(
                ParsedBlock(
                    text=value,
                    locator=f"paragraph:{index}",
                    ordinal=index,
                )
                for index, value in enumerate(blocks, start=1)
            ),
        )

    @staticmethod
    def _canonicalize_parsed_source(source: ParsedSource) -> ParsedSource:
        if (
            not isinstance(source.media_type, str)
            or not source.media_type
            or source.media_type != source.media_type.strip()
        ):
            raise ValueError("parser media type is invalid")
        if source.page_count is not None and (
            type(source.page_count) is not int or source.page_count <= 0
        ):
            raise ValueError("parser page count is invalid")
        if type(source.blocks) is not tuple:
            raise ValueError("parser blocks must be a tuple")
        canonical_blocks: list[ParsedBlock] = []
        locators: set[str] = set()
        ordinals: set[int] = set()
        for block in source.blocks:
            if not isinstance(block, ParsedBlock):
                raise ValueError("parser block is invalid")
            if (
                not isinstance(block.locator, str)
                or not block.locator
                or block.locator != block.locator.strip()
                or "\x00" in block.locator
                or block.locator in locators
            ):
                raise ValueError("parser block locator is invalid")
            if (
                type(block.ordinal) is not int
                or block.ordinal <= 0
                or block.ordinal in ordinals
            ):
                raise ValueError("parser block ordinal is invalid")
            if not isinstance(block.text, str):
                raise ValueError("parser block text is invalid")
            text = "\n".join(
                unicodedata.normalize("NFKC", line).rstrip()
                for line in block.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            ).strip()
            if not text:
                raise ValueError("parser block text is empty")
            locators.add(block.locator)
            ordinals.add(block.ordinal)
            canonical_blocks.append(
                ParsedBlock(text=text, locator=block.locator, ordinal=block.ordinal)
            )
        if ordinals != set(range(1, len(canonical_blocks) + 1)):
            raise ValueError("parser block ordinals must be contiguous")
        return ParsedSource(
            media_type=source.media_type,
            page_count=source.page_count,
            blocks=tuple(sorted(canonical_blocks, key=lambda block: block.ordinal)),
        )

    def _resolve_parser(self, path: Path) -> Callable[..., object]:
        if path.suffix.casefold() == ".ppt":
            raise _domain("COURSE_PARSE_FAILED", "legacy PowerPoint sources are not supported", file_name=path.name)
        if not isinstance(self._parser_registry, Mapping):
            raise _domain("COURSE_PARSE_FAILED", "parser registry is unavailable")
        parser = self._parser_registry.get(path.suffix.casefold())
        if not callable(parser):
            raise _domain("COURSE_PARSE_FAILED", "course source type has no registered parser", file_name=path.name)
        return parser

    def _hash_bytes(self, payload: bytes) -> str:
        if not callable(self._hash_tool):
            raise _domain("SOURCE_HASH_MISMATCH", "source hash tool is unavailable")
        try:
            digest = self._hash_tool(payload)
        except Exception as error:
            raise _domain("SOURCE_HASH_MISMATCH", "source hash tool rejected the course bytes", reason=type(error).__name__) from error
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise _domain("SOURCE_HASH_MISMATCH", "source hash tool must return a lowercase SHA-256 hexadecimal string")
        return digest

    @staticmethod
    def _read_input_bytes(path: Path) -> bytes:
        with path.open("rb") as source:
            return source.read()

    @staticmethod
    def _failure(file_name: str, error: DomainError) -> dict[str, str]:
        return {"file_name": file_name, "code": error.code, "reason": error.message}

    @staticmethod
    def _write_failures(output_dir: Path, failures: list[dict[str, str]]) -> None:
        try:
            write_json(
                output_dir / "parse_failures.json",
                sorted(
                    failures,
                    key=lambda value: (
                        value["file_name"],
                        value["code"],
                        value["reason"],
                    ),
                ),
            )
        except Exception as error:
            raise _domain(
                "COURSE_PARSE_FAILED",
                "course failure report could not be written",
                reason=type(error).__name__,
            ) from error

    @staticmethod
    def _prepare_output_dir(output_dir: Path) -> None:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            raise _domain(
                "COURSE_PARSE_FAILED",
                "course output directory is unavailable",
                reason=type(error).__name__,
            ) from error

    @staticmethod
    def _write_package(output_dir: Path, package: CoursePackage) -> None:
        try:
            package.to_json_file(output_dir / "course_package.json")
        except Exception as error:
            raise _domain(
                "COURSE_PARSE_FAILED",
                "course package compatibility export could not be written",
                reason=type(error).__name__,
            ) from error
