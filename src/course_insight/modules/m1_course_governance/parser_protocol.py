"""Stable parser ports and a bounded, versioned M1 parser registry.

The registry is deliberately independent of filesystem access.  Callers give
it a safe file name and the already-captured bytes; the registry is responsible
for selecting a parser and enforcing the parser-specific byte budget before
the adapter is invoked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Protocol, runtime_checkable


DEFAULT_MAX_BYTES = 25 * 1024 * 1024

_DEFAULT_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_DEFAULT_CAPABILITIES = {
    ".md": frozenset({"byte-input", "text"}),
    ".txt": frozenset({"byte-input", "text"}),
    ".pdf": frozenset({"byte-input", "paged"}),
    ".docx": frozenset({"byte-input", "structured"}),
    ".ppt": frozenset({"byte-input", "converted", "paged", "structured"}),
    ".pptx": frozenset({"byte-input", "paged", "structured"}),
}


class ParserProtocolError(ValueError):
    """Private parser boundary error mapped by M1 to ``COURSE_PARSE_FAILED``."""


class ParserInputTooLarge(ParserProtocolError):
    """The captured source exceeds the selected parser's byte budget."""


class UnsupportedParser(ParserProtocolError):
    """The source extension is not accepted by the registry."""


@runtime_checkable
class ParserProtocol(Protocol):
    """Minimal adapter port implemented by every bounded parser."""

    def parse(self, file_name: str, payload: bytes) -> object:
        """Parse captured bytes without reading from the host filesystem."""


@dataclass(frozen=True, slots=True)
class _CallableParser:
    """Adapter retaining compatibility with the existing two-argument callables."""

    function: Callable[[str, bytes], object]

    def parse(self, file_name: str, payload: bytes) -> object:
        return self.function(file_name, payload)


def normalize_extension(extension: str) -> str:
    """Return one safe, case-folded extension suitable for registry keys."""

    if not isinstance(extension, str):
        raise ValueError("parser extension must be text")
    normalized = extension.strip().casefold()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if (
        normalized in {".", ".."}
        or normalized != PurePath(normalized).name
        or any(character in "\\/:?*<>|\"" or ord(character) < 32 for character in normalized)
    ):
        raise ValueError("parser extension must be a safe suffix")
    return normalized


def _safe_identity(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or (
            field in {"identity", "version"}
            and any(character in "\\/:" for character in value)
        )
    ):
        raise ValueError(f"parser {field} must be non-empty safe text")
    return value


def _safe_media_type(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or value.count("/") != 1
        or any(character.isspace() for character in value)
        or any(character in "\\:?*<>|\"" for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("parser media type must be a valid safe media type")
    return value


@dataclass(frozen=True, slots=True)
class ParserEntry:
    """Immutable metadata and adapter binding for one normalized extension."""

    extension: str
    media_type: str
    parser_version: str
    capabilities: Iterable[str]
    max_bytes: int
    parser: ParserProtocol | Callable[[str, bytes], object]
    parser_id: str | None = None

    def __post_init__(self) -> None:
        normalized_extension = normalize_extension(self.extension)
        media_type = _safe_media_type(self.media_type)
        parser_version = _safe_identity(self.parser_version, field="version")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("parser max_bytes must be a positive integer")
        if isinstance(self.capabilities, str):
            raise ValueError("parser capabilities must be an iterable of text flags")
        try:
            capabilities = frozenset(self.capabilities)
        except (TypeError, ValueError) as error:
            raise ValueError("parser capabilities must be an iterable of text flags") from error
        if any(
            not isinstance(capability, str)
            or not capability.strip()
            or capability != capability.strip()
            or "\x00" in capability
            or any(character in "\\/:?*<>|\"" for character in capability)
            for capability in capabilities
        ):
            raise ValueError("parser capabilities must contain safe text")
        parser = self.parser
        if isinstance(parser, ParserProtocol):
            adapter: ParserProtocol = parser
        elif callable(parser):
            adapter = _CallableParser(parser)
        else:
            raise ValueError("parser adapter must be callable or implement ParserProtocol")
        parser_id = self.parser_id
        if parser_id is None:
            parser_id = f"m1.parser{normalized_extension}"
        parser_id = _safe_identity(parser_id, field="identity")
        object.__setattr__(self, "extension", normalized_extension)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "parser_version", parser_version)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "parser", adapter)
        object.__setattr__(self, "parser_id", parser_id)

    def parse(self, file_name: str, payload: bytes) -> object:
        """Run the adapter after enforcing its configured byte limit."""

        if type(payload) is not bytes:
            raise ParserProtocolError("source payload must be bytes")
        if len(payload) > self.max_bytes:
            raise ParserInputTooLarge("source exceeds parser maximum input bytes")
        return self.parser.parse(file_name, payload)

    def metadata(self) -> dict[str, Any]:
        """Return only deterministic, non-sensitive parser metadata."""

        return {
            "extension": self.extension,
            "media_type": self.media_type,
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "capabilities": sorted(self.capabilities),
            "max_bytes": self.max_bytes,
        }


class ParserRegistry:
    """Case-insensitive parser registry with duplicate protection and dispatch."""

    def __init__(self, entries: Iterable[ParserEntry] = ()) -> None:
        self._entries: dict[str, ParserEntry] = {}
        for entry in entries:
            self.register(entry)

    def register(self, entry: ParserEntry) -> None:
        """Register one entry, rejecting normalized extension collisions."""

        if not isinstance(entry, ParserEntry):
            raise ValueError("parser registry entries must be ParserEntry instances")
        if entry.extension in self._entries:
            raise ValueError("duplicate parser extension")
        self._entries[entry.extension] = entry

    def resolve(self, file_name: str) -> ParserEntry:
        """Resolve a basename by its case-folded suffix."""

        extension = PurePath(file_name).suffix.casefold()
        try:
            return self._entries[extension]
        except KeyError as error:
            if extension == ".ppt":
                raise UnsupportedParser(
                    "legacy PowerPoint conversion is unavailable"
                ) from error
            raise KeyError("unsupported parser extension") from error

    def parse(self, file_name: str, payload: bytes) -> object:
        """Resolve and invoke one parser with its bounded input contract."""

        return self.resolve(file_name).parse(file_name, payload)

    def metadata_for(self, file_name: str) -> dict[str, Any]:
        """Return safe metadata for the parser selected by ``file_name``."""

        return self.resolve(file_name).metadata()

    @classmethod
    def from_legacy(cls, parsers: Mapping[str, object]) -> "ParserRegistry":
        """Wrap the pre-S6 extension map without changing its caller contract."""

        if not isinstance(parsers, Mapping):
            raise ValueError("legacy parser registry must be a mapping")
        entries: list[ParserEntry] = []
        for extension, parser in parsers.items():
            try:
                normalized_extension = normalize_extension(extension)
            except ValueError:
                # Keep the old mapping adapter tolerant of unreachable keys;
                # resolution still reports the existing M1 parse failure.
                continue
            if isinstance(parser, ParserEntry):
                if parser.extension != normalized_extension:
                    raise ValueError("parser entry extension does not match registry key")
                entries.append(parser)
                continue
            is_builtin = (
                getattr(parser, "__name__", "") == "parse_source"
                and getattr(parser, "__module__", "") == "course_insight.modules.m1_course_governance.parsers"
            )
            entries.append(
                ParserEntry(
                    extension=normalized_extension,
                    media_type=_DEFAULT_MEDIA_TYPES.get(
                        normalized_extension, "application/octet-stream"
                    ),
                    parser_version="parse-source-v1" if is_builtin else "legacy-v1",
                    capabilities=(
                        _DEFAULT_CAPABILITIES.get(
                            normalized_extension, frozenset({"byte-input"})
                        )
                        if is_builtin
                        else frozenset({"byte-input"})
                    ),
                    max_bytes=DEFAULT_MAX_BYTES,
                    parser=parser,  # type: ignore[arg-type]
                    parser_id=(
                        "m1.parse_source"
                        if is_builtin
                        else f"m1.legacy-parser{normalized_extension}"
                    ),
                )
            )
        return cls(entries)
