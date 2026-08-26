"""Bounded Windows adapter for converting legacy PowerPoint files to PPTX."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from course_insight.modules.m1_course_governance.parser_protocol import (
    DEFAULT_MAX_BYTES,
)


OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")

_INPUT_ENV = "COURSE_INSIGHT_PPT_INPUT"
_OUTPUT_ENV = "COURSE_INSIGHT_PPT_OUTPUT"
_POWERSHELL_UNAVAILABLE_EXIT = 42
_SAFE_ENV_NAMES = (
    "APPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)
_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$application = $null
$presentation = $null
$inputPath = $env:COURSE_INSIGHT_PPT_INPUT
$outputPath = $env:COURSE_INSIGHT_PPT_OUTPUT
try {
    try {
        $application = New-Object -ComObject PowerPoint.Application
    } catch {
        exit 42
    }
    $application.AutomationSecurity = 3
    $application.DisplayAlerts = 1
    $presentation = $application.Presentations.Open($inputPath, -1, 0, 0)
    $presentation.SaveAs($outputPath, 24)
    exit 0
} catch {
    exit 43
} finally {
    if ($null -ne $presentation) {
        try { $presentation.Close() } catch {}
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation)
    }
    if ($null -ne $application) {
        try { $application.Quit() } catch {}
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
""".strip()


class LegacyPowerPointConverter(Protocol):
    """Convert captured legacy presentation bytes into bounded PPTX bytes."""

    def convert(self, payload: bytes) -> bytes: ...


class LegacyPowerPointConversionError(ValueError):
    """Safe conversion failure that never contains host or document details."""

    code = "LEGACY_PPT_CONVERSION_FAILED"


class LegacyPowerPointConversionUnavailable(LegacyPowerPointConversionError):
    """The host cannot provide Microsoft PowerPoint automation."""

    code = "LEGACY_PPT_CONVERSION_UNAVAILABLE"


class LegacyPowerPointConversionFailed(LegacyPowerPointConversionError):
    """PowerPoint was available but did not produce a bounded PPTX file."""


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class PowerPointComConverter:
    """Run macro-disabled PowerPoint conversion in an isolated temp directory."""

    timeout_seconds: float = 120.0
    max_output_bytes: int = DEFAULT_MAX_BYTES
    powershell_executable: str | None = None
    runner: ProcessRunner = subprocess.run

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        if self.powershell_executable is not None and (
            not isinstance(self.powershell_executable, str)
            or not self.powershell_executable.strip()
        ):
            raise ValueError("powershell_executable must be non-empty text")
        if not callable(self.runner):
            raise ValueError("runner must be callable")

    def convert(self, payload: bytes) -> bytes:
        """Convert one genuine OLE presentation without retaining temp files."""

        if type(payload) is not bytes or not payload.startswith(
            OLE_COMPOUND_FILE_SIGNATURE
        ):
            raise LegacyPowerPointConversionFailed(
                "legacy PowerPoint input is not a valid compound file"
            )
        executable = self._resolve_powershell()
        try:
            with TemporaryDirectory(
                prefix="course-insight-ppt-",
                ignore_cleanup_errors=True,
            ) as temporary:
                root = Path(temporary)
                input_path = root / "source.ppt"
                output_path = root / "converted.pptx"
                input_path.write_bytes(payload)
                result = self.runner(
                    self._command(executable),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=float(self.timeout_seconds),
                    env=self._environment(input_path, output_path),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == _POWERSHELL_UNAVAILABLE_EXIT:
                    raise LegacyPowerPointConversionUnavailable(
                        "Microsoft PowerPoint conversion is unavailable"
                    )
                if result.returncode != 0:
                    raise LegacyPowerPointConversionFailed(
                        "legacy PowerPoint conversion failed"
                    )
                if not output_path.is_file():
                    raise LegacyPowerPointConversionFailed(
                        "legacy PowerPoint conversion did not produce valid output"
                    )
                output_size = output_path.stat().st_size
                if output_size < 1 or output_size > self.max_output_bytes:
                    raise LegacyPowerPointConversionFailed(
                        "legacy PowerPoint conversion did not produce valid output"
                    )
                return output_path.read_bytes()
        except LegacyPowerPointConversionError:
            raise
        except FileNotFoundError as error:
            raise LegacyPowerPointConversionUnavailable(
                "Microsoft PowerPoint conversion is unavailable"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise LegacyPowerPointConversionFailed(
                "legacy PowerPoint conversion timed out"
            ) from error
        except OSError as error:
            raise LegacyPowerPointConversionFailed(
                "legacy PowerPoint conversion failed"
            ) from error

    def _resolve_powershell(self) -> str:
        if self.powershell_executable is not None:
            return self.powershell_executable
        executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if executable is None:
            raise LegacyPowerPointConversionUnavailable(
                "Microsoft PowerPoint conversion is unavailable"
            )
        return executable

    @staticmethod
    def _command(executable: str) -> list[str]:
        encoded = base64.b64encode(
            _POWERSHELL_SCRIPT.encode("utf-16-le")
        ).decode("ascii")
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ]

    @staticmethod
    def _environment(input_path: Path, output_path: Path) -> dict[str, str]:
        environment = {
            name: value
            for name in _SAFE_ENV_NAMES
            if (value := os.environ.get(name))
        }
        return {
            **environment,
            _INPUT_ENV: str(input_path),
            _OUTPUT_ENV: str(output_path),
        }


__all__ = [
    "LegacyPowerPointConversionError",
    "LegacyPowerPointConversionFailed",
    "LegacyPowerPointConversionUnavailable",
    "LegacyPowerPointConverter",
    "OLE_COMPOUND_FILE_SIGNATURE",
    "PowerPointComConverter",
]
