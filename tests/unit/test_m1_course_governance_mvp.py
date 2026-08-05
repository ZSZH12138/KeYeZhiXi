from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m1_course_governance.parsers import ParsedBlock, ParsedSource
from course_insight.modules.m1_course_governance.service import M1CourseGovernanceService


class _Repository:
    def __init__(self) -> None:
        self.saved = []

    def save_course_package(self, package: object) -> None:
        self.saved.append(package)


def _parsed(name: str, payload: bytes) -> ParsedSource:
    del name
    text = payload.decode("utf-8")
    return ParsedSource(
        media_type="text/plain",
        page_count=None,
        blocks=tuple(
            ParsedBlock(text=value, locator=f"paragraph:{index}", ordinal=index)
            for index, value in enumerate(text.split("|"), start=1)
            if value
        ),
    )


def _metadata(path: Path, **overrides: object) -> Path:
    payload = {
        "course_package_id": "pkg-1",
        "course_id": "course-1",
        "package_version": "v1",
        "course_name": "Course",
        "imported_at": "2026-08-03T00:00:00+00:00",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _manifest(path: Path, rows: list[dict[str, str]], *, fields: list[str] | None = None) -> Path:
    names = fields or [
        "file_name", "source_id", "expected_sha256", "authorized_by", "authorized_at", "license_note",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(name: str, source_id: str, content: bytes, **overrides: str) -> dict[str, str]:
    result = {
        "file_name": name,
        "source_id": source_id,
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "authorized_by": "Teacher",
        "authorized_at": "2026-08-01T00:00:00+00:00",
        "license_note": "course use",
    }
    result.update(overrides)
    return result


def _service(repository: _Repository, parser: object = _parsed, hash_tool: object = None) -> M1CourseGovernanceService:
    return M1CourseGovernanceService(
        {".txt": parser},
        hashlib.sha256 if hash_tool is None else hash_tool,
        repository,  # type: ignore[arg-type]
    )


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_imports_sorted_authorized_sources_with_stable_content_identities(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha.txt"
    beta = tmp_path / "beta.txt"
    alpha.write_bytes(b"first|second")
    beta.write_bytes(b"third")
    repository = _Repository()
    package = _service(repository, hash_tool=_sha).import_course(
        [beta, alpha],
        _metadata(tmp_path / "metadata.json"),
        _manifest(tmp_path / "authorization.csv", [_row("alpha.txt", "source-a", alpha.read_bytes()), _row("beta.txt", "source-b", beta.read_bytes())]),
        tmp_path / "output",
    )
    assert [source.source_id for source in package.source_documents] == ["source-a", "source-b"]
    assert [chunk.locator for chunk in package.content_chunks] == ["paragraph:1", "paragraph:2", "paragraph:1"]
    assert all(chunk.chunk_id.startswith("chunk_") and len(chunk.chunk_id) == 70 for chunk in package.content_chunks)
    assert repository.saved == [package]
    assert json.loads((tmp_path / "output" / "parse_failures.json").read_text()) == []


def test_input_order_does_not_change_package_or_chunks(tmp_path: Path) -> None:
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    manifest = _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", one.read_bytes()), _row("two.txt", "b", two.read_bytes())])
    first = _service(_Repository(), hash_tool=_sha).import_course([one, two], _metadata(tmp_path / "first.json"), manifest, tmp_path / "first")
    second = _service(_Repository(), hash_tool=_sha).import_course([two, one], _metadata(tmp_path / "second.json"), manifest, tmp_path / "second")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_chunk_id_depends_on_source_locator_and_normalized_text(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"same")
    manifest = _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", source.read_bytes())])
    package = _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), manifest, tmp_path / "output")
    chunk = package.content_chunks[0]
    expected = hashlib.sha256(b"a\0paragraph:1\0" + hashlib.sha256(b"same").hexdigest().encode()).hexdigest()
    assert chunk.chunk_id == f"chunk_{expected}"


def test_blocks_follow_parser_ordinal_not_lexical_locator_order(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    def parser(name: str, payload: bytes) -> ParsedSource:
        del name, payload
        return ParsedSource("text/plain", None, (
            ParsedBlock("first", "paragraph:2", 1),
            ParsedBlock("second", "paragraph:10", 2),
        ))
    package = _service(_Repository(), parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert [chunk.text for chunk in package.content_chunks] == ["first", "second"]


@pytest.mark.parametrize("field,value", [
    ("file_name", "../unsafe.txt"), ("source_id", "../unsafe"), ("expected_sha256", "A" * 64), ("authorized_by", ""), ("authorized_at", ""), ("license_note", ""),
])
def test_rejects_invalid_authorization_fields(tmp_path: Path, field: str, value: str) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    row = _row("one.txt", "a", source.read_bytes())
    row[field] = value
    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [row]), tmp_path / "output")


@pytest.mark.parametrize("fields", [
    ["file_name", "source_id", "authorized_by", "authorized_at", "license_note", "unexpected"],
    ["file_name", "source_id", "expected_sha256", "authorized_by", "authorized_at", "license_note", "file_name"],
])
def test_rejects_non_exact_authorization_header(tmp_path: Path, fields: list[str]) -> None:
    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        _service(_Repository())._load_authorizations(_manifest(tmp_path / "authorization.csv", [], fields=fields))


def test_single_source_legacy_hash_fallback_still_requires_csv_authorization(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    row = _row("one.txt", "a", source.read_bytes())
    row.pop("expected_sha256")
    package = _service(_Repository(), hash_tool=_sha).import_course(
        [source], _metadata(tmp_path / "metadata.json", source_sha256=_sha(source.read_bytes())),
        _manifest(tmp_path / "authorization.csv", [row], fields=["file_name", "source_id", "authorized_by", "authorized_at", "license_note"]), tmp_path / "output")
    assert package.source_documents[0].sha256 == _sha(b"one")


def test_multi_source_cannot_use_legacy_hash_fallback(tmp_path: Path) -> None:
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    one_row = _row("one.txt", "a", one.read_bytes())
    two_row = _row("two.txt", "b", two.read_bytes())
    one_row.pop("expected_sha256")
    two_row.pop("expected_sha256")
    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        _service(_Repository(), hash_tool=_sha).import_course([one, two], _metadata(tmp_path / "metadata.json", source_sha256=_sha(one.read_bytes())), _manifest(tmp_path / "authorization.csv", [one_row, two_row], fields=["file_name", "source_id", "authorized_by", "authorized_at", "license_note"]), tmp_path / "output")


def test_six_column_manifest_cannot_leave_expected_hash_blank(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    with pytest.raises(DomainError) as raised:
        _service(_Repository())._load_authorizations(
            _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one", expected_sha256="")])
        )
    assert raised.value.code == "UNAUTHORIZED_SOURCE"


def test_uses_captured_bytes_once_for_hash_and_byte_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"before")
    reads = 0
    original_read = Path.read_bytes
    def read_once(path: Path) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read_once)
    parser_payloads: list[bytes] = []
    def parser(name: str, payload: bytes) -> ParsedSource:
        parser_payloads.append(payload)
        source.write_bytes(b"after")
        return _parsed(name, payload)
    package = _service(_Repository(), parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"before")]), tmp_path / "output")
    assert reads == 1 and parser_payloads == [b"before"] and package.source_documents[0].sha256 == _sha(b"before")


@pytest.mark.parametrize("name,payload", [("legacy.ppt", b"x"), ("unknown.bin", b"x"), ("empty.txt", b"")])
def test_parse_failures_are_aggregated_and_do_not_save(tmp_path: Path, name: str, payload: bytes) -> None:
    source = tmp_path / name
    source.write_bytes(payload)
    repository = _Repository()
    with pytest.raises(DomainError, match="COURSE_PARSE_FAILED"):
        _service(repository, hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row(name, "a", payload)]), tmp_path / "output")
    failures = json.loads((tmp_path / "output" / "parse_failures.json").read_text())
    assert failures[0]["file_name"] == name and failures[0]["code"] == "COURSE_PARSE_FAILED"
    assert repository.saved == [] and not (tmp_path / "output" / "course_package.json").exists()


def test_ppt_is_rejected_even_if_a_registry_entry_is_supplied(tmp_path: Path) -> None:
    source = tmp_path / "legacy.ppt"
    source.write_bytes(b"x")
    service = M1CourseGovernanceService({".ppt": _parsed}, _sha, _Repository())
    with pytest.raises(DomainError, match="COURSE_PARSE_FAILED"):
        service.import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("legacy.ppt", "a", b"x")]), tmp_path / "output")


def test_failures_are_sorted_and_use_priority_without_host_paths(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha.txt"
    beta = tmp_path / "beta.bin"
    alpha.write_bytes(b"alpha")
    beta.write_bytes(b"beta")
    repository = _Repository()
    with pytest.raises(DomainError) as raised:
        _service(repository, hash_tool=_sha).import_course([beta, alpha], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("alpha.txt", "a", b"wrong"), _row("beta.bin", "b", beta.read_bytes())]), tmp_path / "output")
    assert raised.value.code == "SOURCE_HASH_MISMATCH"
    serialized = (tmp_path / "output" / "parse_failures.json").read_text()
    assert str(tmp_path) not in serialized
    assert [row["file_name"] for row in json.loads(serialized)] == ["alpha.txt", "beta.bin"]
    assert repository.saved == []


def test_legacy_string_adapter_receives_only_captured_bytes(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")
    received_names: list[str] = []
    def old_parser(name: str, payload: bytes) -> str:
        received_names.append(name)
        source.write_text("changed", encoding="utf-8")
        return payload.decode("utf-8")
    package = _service(_Repository(), old_parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert package.content_chunks[0].text == "one"
    assert package.source_documents[0].sha256 == _sha(b"one")
    assert received_names == ["one.txt"]
    source.write_bytes(b"one")
    def bad_parser(name: str, payload: bytes) -> ParsedSource:
        raise TypeError("parser bug")
    with pytest.raises(DomainError, match="COURSE_PARSE_FAILED"):
        _service(_Repository(), bad_parser, _sha).import_course([source], _metadata(tmp_path / "second.json"), _manifest(tmp_path / "second.csv", [_row("one.txt", "a", b"one")]), tmp_path / "second-output")


def test_one_argument_path_parser_is_not_a_supported_adapter(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")
    def path_parser(path: Path) -> str:
        return path.read_text(encoding="utf-8")
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), path_parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert raised.value.code == "COURSE_PARSE_FAILED"


@pytest.mark.parametrize("kind", ["parser", "hash"])
def test_runtime_errors_from_controlled_tools_have_stable_codes(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    def broken_parser(name: str, payload: bytes) -> ParsedSource:
        raise RuntimeError("adapter unavailable")
    def broken_hash(payload: bytes) -> str:
        raise RuntimeError("hash unavailable")
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), broken_parser if kind == "parser" else _parsed, broken_hash if kind == "hash" else _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert raised.value.code == ("COURSE_PARSE_FAILED" if kind == "parser" else "SOURCE_HASH_MISMATCH")


def test_timezone_validation_is_mapped_to_stable_m1_error(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    with pytest.raises(DomainError, match="COURSE_PARSE_FAILED"):
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json", imported_at="2026-08-03T00:00:00"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")


def test_naive_authorization_time_is_rejected_as_an_authorization_failure(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one", authorized_at="2026-08-01T00:00:00")]), tmp_path / "output")
    assert raised.value.code == "UNAUTHORIZED_SOURCE"


@pytest.mark.parametrize("blocks", [
    (object(),),
    (ParsedBlock("text", "loc", 1), ParsedBlock("text", "loc", 2)),
    (ParsedBlock("one", "a", 1), ParsedBlock("two", "b", 3)),
    (ParsedBlock("text", "bad\x00locator", 1),),
    (ParsedBlock("   ", "locator", 1),),
])
def test_rejects_noncanonical_custom_parser_blocks(tmp_path: Path, blocks: tuple[object, ...]) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    def parser(name: str, payload: bytes) -> ParsedSource:
        del name, payload
        return ParsedSource("text/plain", None, blocks)  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert raised.value.code == "COURSE_PARSE_FAILED"


def test_canonicalizes_custom_parser_block_text_before_hashing(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    def parser(name: str, payload: bytes) -> ParsedSource:
        del name, payload
        return ParsedSource("text/plain", None, (ParsedBlock("  A\r\nＢ  ", "loc", 1),))
    package = _service(_Repository(), parser, _sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert package.content_chunks[0].text == "A\nB"
    assert package.content_chunks[0].sha256 == _sha(b"A\nB")


@pytest.mark.parametrize(
    "source_id",
    [
        " a",
        "a ",
        "C:relative",
        "C:\\absolute",
        "\\\\host\\share",
        "/absolute",
        "CON",
        "NUL",
        "bad\x01",
        "one:stream",
        "a?b",
        "a*b",
        'a"b',
    ],
)
def test_rejects_nonportable_source_ids(tmp_path: Path, source_id: str) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    with pytest.raises(DomainError) as raised:
        _service(_Repository())._load_authorizations(
            _manifest(tmp_path / "authorization.csv", [_row("one.txt", source_id, b"one")])
        )
    assert raised.value.code == "UNAUTHORIZED_SOURCE"


def test_rejects_extra_csv_value_and_unused_authorization_row(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.csv"
    malformed.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"one.txt,a,{_sha(b'one')},Teacher,2026-08-01T00:00:00+00:00,course use,extra\n",
        encoding="utf-8",
    )
    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        _service(_Repository())._load_authorizations(malformed)
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one"), _row("unused.txt", "unused", b"unused")]), tmp_path / "output")


class _BrokenRepository(_Repository):
    def save_course_package(self, package: object) -> None:
        raise RuntimeError("database offline")


def test_repository_failure_preserves_existing_compatibility_export(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "course_package.json"
    existing.write_bytes(b"previous package\n")
    with pytest.raises(DomainError) as raised:
        _service(_BrokenRepository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), output)
    assert raised.value.code == "COURSE_PARSE_FAILED"
    assert existing.read_bytes() == b"previous package\n"


def test_output_failures_are_stable_domain_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    output_file = tmp_path / "output-file"
    output_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), output_file)
    assert raised.value.code == "COURSE_PARSE_FAILED"
    def fail_write(path: object, value: object) -> None:
        raise RuntimeError("disk offline")
    monkeypatch.setattr("course_insight.modules.m1_course_governance.service.write_json", fail_write)
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "second.json"), _manifest(tmp_path / "second.csv", [_row("one.txt", "a", b"one")]), tmp_path / "second")
    assert raised.value.code == "COURSE_PARSE_FAILED"


def test_package_export_failure_is_a_stable_domain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    def fail_export(self: object, path: object) -> None:
        raise RuntimeError("disk offline")
    monkeypatch.setattr(
        "course_insight.contracts.course.CoursePackage.to_json_file",
        fail_export,
    )
    with pytest.raises(DomainError) as raised:
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), tmp_path / "output")
    assert raised.value.code == "COURSE_PARSE_FAILED"


@pytest.mark.parametrize("repository", [None, object()])
def test_repository_persistence_is_required_before_ready_export(
    tmp_path: Path,
    repository: object | None,
) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    output = tmp_path / "output"
    with pytest.raises(DomainError) as raised:
        _service(repository, hash_tool=_sha).import_course(  # type: ignore[arg-type]
            [source],
            _metadata(tmp_path / "metadata.json"),
            _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]),
            output,
        )
    assert raised.value.code == "COURSE_PARSE_FAILED"
    assert not (output / "course_package.json").exists()


def test_package_export_failure_does_not_clear_existing_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "one.txt"
    source.write_bytes(b"one")
    output = tmp_path / "output"
    output.mkdir()
    report = output / "parse_failures.json"
    report.write_bytes(b"[{\"code\":\"previous\"}]\n")
    before = report.read_bytes()
    def fail_export(self: object, path: object) -> None:
        raise RuntimeError("disk offline")
    monkeypatch.setattr(
        "course_insight.contracts.course.CoursePackage.to_json_file",
        fail_export,
    )
    with pytest.raises(DomainError, match="COURSE_PARSE_FAILED"):
        _service(_Repository(), hash_tool=_sha).import_course([source], _metadata(tmp_path / "metadata.json"), _manifest(tmp_path / "authorization.csv", [_row("one.txt", "a", b"one")]), output)
    assert report.read_bytes() == before


@pytest.mark.parametrize(
    "file_name",
    [" one.txt", "one.txt ", "C:one.txt", "C:\\one.txt", "\\\\host\\share.txt", "/one.txt", ".", "..", "CON", "NUL.txt", "bad\x01.txt"],
)
def test_rejects_nonportable_authorization_file_names(
    tmp_path: Path,
    file_name: str,
) -> None:
    with pytest.raises(DomainError) as raised:
        _service(_Repository())._load_authorizations(
            _manifest(tmp_path / "authorization.csv", [_row(file_name, "a", b"one")])
        )
    assert raised.value.code == "UNAUTHORIZED_SOURCE"
