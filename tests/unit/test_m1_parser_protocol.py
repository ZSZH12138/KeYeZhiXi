from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m1_course_governance.parser_protocol import (
    ParserEntry,
    ParserRegistry,
    UnsupportedParser,
)
from course_insight.modules.m1_course_governance.parsers import (
    ParsedBlock,
    ParsedSource,
    default_parser_registry,
)
from course_insight.modules.m1_course_governance.service import M1CourseGovernanceService


def _parsed(name: str, payload: bytes) -> ParsedSource:
    del name
    return ParsedSource(
        media_type="text/plain",
        page_count=None,
        blocks=(ParsedBlock(payload.decode("utf-8"), "paragraph:1", 1),),
    )


def _metadata(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "course_package_id": "package-parser",
                "course_id": "course-parser",
                "package_version": "v1",
                "course_name": "Parser Course",
                "imported_at": "2026-08-03T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return path


def _authorization(path: Path, source: Path) -> Path:
    path.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"{source.name},source-parser,{hashlib.sha256(source.read_bytes()).hexdigest()},Teacher,"
        "2026-08-01T00:00:00+00:00,course-use\n",
        encoding="utf-8",
    )
    return path


def test_registry_normalizes_extensions_and_exposes_safe_parser_metadata() -> None:
    entry = ParserEntry(
        extension="TXT",
        media_type="text/plain",
        parser_version="plain-v2",
        capabilities=frozenset({"text"}),
        max_bytes=32,
        parser=_parsed,
    )
    registry = ParserRegistry((entry,))

    assert registry.resolve("lesson.TxT") is entry
    assert registry.metadata_for("lesson.txt") == {
        "extension": ".txt",
        "media_type": "text/plain",
        "parser_id": "m1.parser.txt",
        "parser_version": "plain-v2",
        "capabilities": ["text"],
        "max_bytes": 32,
    }


def test_parser_entry_normalizes_capability_iterables_to_immutable_metadata() -> None:
    entry = ParserEntry(
        extension=".txt",
        media_type="text/plain",
        parser_version="plain-v1",
        capabilities={"text"},  # type: ignore[arg-type]
        max_bytes=32,
        parser=_parsed,
    )

    assert entry.capabilities == frozenset({"text"})


def test_registry_rejects_duplicate_normalized_extensions() -> None:
    entry = ParserEntry(
        extension=".TXT",
        media_type="text/plain",
        parser_version="plain-v1",
        capabilities=frozenset(),
        max_bytes=32,
        parser=_parsed,
    )

    with pytest.raises(ValueError, match="duplicate parser extension"):
        ParserRegistry((entry, replace(entry, parser_version="plain-v2")))

    registry = ParserRegistry((entry,))
    with pytest.raises(ValueError, match="duplicate parser extension"):
        registry.register(replace(entry, parser_version="plain-v3"))


def test_registry_rejects_invalid_limits_and_unknown_extension() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        ParserEntry(
            extension=".txt",
            media_type="text/plain",
            parser_version="plain-v1",
            capabilities=frozenset(),
            max_bytes=0,
            parser=_parsed,
        )

    registry = ParserRegistry(())
    with pytest.raises(KeyError, match="unsupported parser extension"):
        registry.resolve("lesson.bin")


def test_registry_rejects_path_like_parser_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        ParserEntry(
            extension=".txt",
            media_type="text/plain",
            parser_version="plain-v1",
            capabilities=frozenset(),
            max_bytes=32,
            parser=_parsed,
            parser_id=r"C:\private\parser",
        )


def test_registry_keeps_legacy_ppt_rejection_and_built_in_entries() -> None:
    registry = default_parser_registry()

    assert {
        registry.resolve(name).extension
        for name in ("lesson.md", "lesson.TXT", "lesson.pdf", "lesson.docx", "lesson.pptx")
    } == {".md", ".txt", ".pdf", ".docx", ".pptx"}
    with pytest.raises(UnsupportedParser):
        registry.resolve("legacy.PPT")


def test_registry_enforces_size_before_parser_execution() -> None:
    called = False

    def parser(name: str, payload: bytes) -> ParsedSource:
        nonlocal called
        called = True
        return _parsed(name, payload)

    registry = ParserRegistry(
        (
            ParserEntry(
                extension=".txt",
                media_type="text/plain",
                parser_version="plain-v1",
                capabilities=frozenset({"text"}),
                max_bytes=3,
                parser=parser,
            ),
        )
    )

    with pytest.raises(ValueError, match="maximum input bytes"):
        registry.parse("lesson.txt", b"four")
    assert called is False


def test_service_records_parser_identity_and_version_without_raw_inputs(tmp_path: Path) -> None:
    source = tmp_path / "lesson.TXT"
    source.write_bytes(b"lesson")
    registry = ParserRegistry(
        (
            ParserEntry(
                extension=".txt",
                media_type="text/plain",
                parser_version="plain-v1",
                capabilities=frozenset({"text"}),
                max_bytes=32,
                parser=_parsed,
            ),
        )
    )
    repository = _Repository()

    package = M1CourseGovernanceService(
        registry,
        lambda payload: hashlib.sha256(payload).hexdigest(),
        repository,  # type: ignore[arg-type]
    ).import_course(
        [source],
        _metadata(tmp_path / "metadata.json"),
        _authorization(tmp_path / "authorization.csv", source),
        tmp_path / "output",
    )

    stored_metadata = json.loads(repository.snapshot_metadata.decode("utf-8"))
    assert stored_metadata["parser_metadata"] == {
        "source-parser": {
            "extension": ".txt",
            "media_type": "text/plain",
            "parser_id": "m1.parser.txt",
            "parser_version": "plain-v1",
            "capabilities": ["text"],
            "max_bytes": 32,
        },
    }
    serialized = json.dumps(stored_metadata)
    assert "lesson" not in serialized
    assert str(tmp_path) not in serialized
    assert package.content_chunks[0].chunk_id.startswith("chunk_")


def test_service_maps_parser_size_failure_to_existing_domain_error(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"too-large")
    registry = ParserRegistry(
        (
            ParserEntry(
                extension=".txt",
                media_type="text/plain",
                parser_version="plain-v1",
                capabilities=frozenset(),
                max_bytes=3,
                parser=_parsed,
            ),
        )
    )

    with pytest.raises(DomainError) as raised:
        M1CourseGovernanceService(
            registry,
            lambda payload: hashlib.sha256(payload).hexdigest(),
            _Repository(),  # type: ignore[arg-type]
        ).import_course(
            [source],
            _metadata(tmp_path / "metadata.json"),
            _authorization(tmp_path / "authorization.csv", source),
            tmp_path / "output",
        )

    assert raised.value.code == "COURSE_PARSE_FAILED"


class _Repository:
    def __init__(self) -> None:
        self.snapshot_metadata = b""

    def save_course_import(self, package: object, snapshot: object) -> None:
        del package
        self.snapshot_metadata = snapshot.course_metadata_bytes  # type: ignore[attr-defined]
