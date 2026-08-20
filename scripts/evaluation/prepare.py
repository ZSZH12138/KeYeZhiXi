"""Prepare reviewed local JSONL into a checksum-pinned dataset package."""

from __future__ import annotations

from pathlib import Path

from ._common import (
    EvaluationInputError,
    canonical_json_bytes,
    load_jsonl_bytes,
    require_local_regular_file,
    require_safe_id,
    sha256_bytes,
)
from .datasets import compute_package_sha256


def prepare_local_dataset_package(
    *,
    normalized_records_path: Path,
    output_dir: Path,
    dataset_family: str,
    dataset_id: str,
    dataset_version: str,
) -> str:
    """Create a new local-only package; never downloads or overwrites data."""

    adapters = {
        "CESA_ASAP_ZH": "cesa_asap_zh_v1",
        "SCIENTSBANK": "scient_bank_v1",
    }
    if dataset_family not in adapters:
        raise EvaluationInputError("unsupported dataset family")
    require_safe_id(dataset_id, field_name="dataset_id")
    require_safe_id(dataset_version, field_name="dataset_version")
    source = require_local_regular_file(
        normalized_records_path,
        max_bytes=256 * 1024 * 1024,
    )
    records = source.read_bytes()
    load_jsonl_bytes(records, field_name="normalized records", max_records=500_000)
    if output_dir.exists():
        raise EvaluationInputError("output_dir must not already exist")
    parent = output_dir.parent.resolve(strict=True)
    root = parent / output_dir.name
    root.mkdir()
    records_name = "records.jsonl"
    (root / records_name).write_bytes(records)
    manifest = {
        "schema_version": "local_short_answer_eval_v1",
        "dataset_family": dataset_family,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "adapter": adapters[dataset_family],
        "records_file": records_name,
        "records_sha256": sha256_bytes(records),
        "license": "NOASSERTION",
        "license_confirmed": False,
        "license_evidence": None,
    }
    (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return compute_package_sha256(root)


__all__ = ["prepare_local_dataset_package"]
