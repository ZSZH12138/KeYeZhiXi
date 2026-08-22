"""Build a local, original computer-network trial course for MVP manual testing.

Writes only into runtime/ and a local config/app.json copy. Does not store
passwords or API keys. Re-run with --force to replace an existing trial course.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from course_insight.application.factory import build_application
from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.evidence import evidence_id_for_chunk
from course_insight.infrastructure.config import load_platform_settings
from course_insight.infrastructure.json_io import write_json
from course_insight.modules.m0_platform.django_app.runtime import (
    restore_course_runtime_manifest,
)
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
COURSE_ID = "course_network"
CLASS_ID = "class_01"
PACKAGE_ID = "package_network_trial"
BUNDLE_ID = "bundle_network_trial"
SOURCE_ID = "source_network_notes"
FILE_NAME = "computer-network-trial-notes.md"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

COURSE_TEXT = """# Computer Network Trial Notes

These notes are original teaching notes for a local MVP trial. They are not
copied from a published textbook.

## IPv6 addresses

An IPv6 address has 128 bits, written as eight hexadecimal groups. Leading
zeros in a group may be omitted. One contiguous run of all-zero groups may be
replaced by '::', and '::' may appear at most once in an address.

## Subnet prefix

A prefix length such as /24 means the first 24 bits identify the network. The
remaining host bits equal 32 minus 24, which is 8 host bits in IPv4.

## TCP and UDP

TCP is connection-oriented and uses a three-way handshake before data transfer.
UDP sends datagrams without establishing a connection and does not provide the
same delivery guarantee as TCP.

## Throughput

Throughput is the number of bits successfully transferred divided by the
elapsed time. Changing the numeric values in a template does not change the
definition.
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare local MVP manual-trial runtime snapshots.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing trial course in runtime/",
    )
    args = parser.parse_args()
    _ensure_app_json()
    settings = load_platform_settings(project_root=PROJECT_ROOT)
    manifest = (
        settings.runtime_dir / "snapshots" / "course_runtime_manifest.json"
    )
    if manifest.is_file() and not args.force:
        print(
            "runtime snapshots already exist; pass --force to rebuild them",
            file=sys.stderr,
        )
        return 2
    if args.force and settings.runtime_dir.exists():
        shutil.rmtree(settings.runtime_dir)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    container = build_application(settings)
    try:
        container.m0_service.initialize()
        source_bytes = _canonical_text(COURSE_TEXT).encode("utf-8")
        package = _course_package(source_bytes)
        container.m1_service._repository.save_course_import(  # noqa: SLF001
            package,
            _course_import_snapshot(package, source_bytes),
        )
        index = container.m2_service.build_index(package)
        seed_dir = settings.runtime_dir / "teacher-seeds"
        paths = _write_seed_files(seed_dir, package)
        bundle = container.m3_service.build_knowledge_bundle(
            package,
            paths["concept"],
            paths["item"],
            paths["rubric"],
            paths["blueprint"],
            paths["prerequisite"],
            paths["misconception"],
        )
        snapshot_dir = settings.runtime_dir / "snapshots"
        container.m0_service.save_contract_snapshot(
            package,
            snapshot_dir / "course-package.json",
        )
        container.m0_service.save_contract_snapshot(
            index,
            snapshot_dir / "evidence-index.json",
        )
        container.m0_service.save_contract_snapshot(
            bundle,
            snapshot_dir / "knowledge-bundle.json",
        )
        state_path = settings.runtime_dir / "policies" / "state.json"
        teacher_path = settings.runtime_dir / "policies" / "teacher.json"
        write_json(
            state_path,
            {
                "aggregation_policy_version": "1.0.0",
                "class_id": CLASS_ID,
                "class_size": 1,
                "consolidating_threshold": 0.5,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 1.0,
                "misconception_activation_threshold": 0.5,
            },
        )
        write_json(
            teacher_path,
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.5,
                "weak_mastery_threshold": 0.8,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            },
        )
        write_json(
            manifest,
            {
                "schema_version": 1,
                "courses": [
                    {
                        "course_id": COURSE_ID,
                        "course_package_ref": "snapshots/course-package.json",
                        "evidence_index_ref": "snapshots/evidence-index.json",
                        "knowledge_bundle_ref": "snapshots/knowledge-bundle.json",
                        "state_policy_ref": "policies/state.json",
                        "teacher_threshold_policy_ref": "policies/teacher.json",
                    }
                ],
            },
        )
        restore_course_runtime_manifest(
            container=container,
            runtime_dir=settings.runtime_dir,
        )
    except BaseException:
        try:
            container.close()
        except BaseException:
            pass
        raise
    container.close()
    print(f"course_id={COURSE_ID}")
    print(f"class_id={CLASS_ID}")
    print(f"runtime_dir={settings.runtime_dir}")
    print("Next: python manage.py migrate")
    print("Then: python manage.py sync_roles --apply")
    print("Then set passwords for pseudonym_student_001 and pseudonym_teacher_001")
    return 0


def _ensure_app_json() -> None:
    config_dir = PROJECT_ROOT / "config"
    target = config_dir / "app.json"
    example = config_dir / "app.example.json"
    if target.is_file() or not example.is_file():
        return
    shutil.copyfile(example, target)


def _canonical_text(value: str) -> str:
    return "\n".join(
        unicodedata.normalize("NFKC", line).rstrip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


def _course_package(source_bytes: bytes) -> CoursePackage:
    text = _canonical_text(source_bytes.decode("utf-8"))
    encoded = text.encode("utf-8")
    text_sha256 = hashlib.sha256(encoded).hexdigest()
    chunk_id = "chunk_" + hashlib.sha256(
        f"{SOURCE_ID}\0section:1\0{text_sha256}".encode("utf-8")
    ).hexdigest()
    chunk = ContentChunk(
        chunk_id=chunk_id,
        source_id=SOURCE_ID,
        text=text,
        locator="section:1",
        concept_hints=[
            "concept_ipv6",
            "concept_subnet",
            "concept_transport",
            "concept_throughput",
        ],
        sha256=text_sha256,
    )
    candidate = CoursePackage(
        course_package_id=PACKAGE_ID,
        course_id=COURSE_ID,
        package_version="1.0.0",
        source_documents=[
            SourceDocument(
                source_id=SOURCE_ID,
                file_name=FILE_NAME,
                media_type="text/markdown",
                sha256=text_sha256,
                page_count=None,
                title="Computer network trial notes",
                version="1.0.0",
            )
        ],
        content_chunks=[chunk],
        source_authorizations=[
            SourceAuthorization(
                source_id=SOURCE_ID,
                authorized_by="Trial teacher",
                license_note="original local trial notes",
                authorized_at=NOW,
            )
        ],
        imported_at=NOW,
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()},
        deep=True,
    )


def _course_import_snapshot(
    package: CoursePackage,
    source_bytes: bytes,
) -> CourseImportSnapshot:
    metadata_bytes = json.dumps(
        {
            "course_package_id": package.course_package_id,
            "course_id": package.course_id,
            "package_version": package.package_version,
            "course_name": package.source_documents[0].title,
            "imported_at": package.imported_at.isoformat(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=(
            "file_name",
            "source_id",
            "expected_sha256",
            "authorized_by",
            "authorized_at",
            "license_note",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    document = package.source_documents[0]
    authorization = package.source_authorizations[0]
    writer.writerow(
        {
            "file_name": document.file_name,
            "source_id": authorization.source_id,
            "expected_sha256": document.sha256,
            "authorized_by": authorization.authorized_by,
            "authorized_at": authorization.authorized_at.isoformat(),
            "license_note": authorization.license_note,
        }
    )
    return CourseImportSnapshot(
        course_metadata_bytes=metadata_bytes,
        source_authorization_bytes=buffer.getvalue().encode("utf-8"),
        source_payloads=(
            SourcePayload(
                source_id=document.source_id,
                file_name=document.file_name,
                raw_bytes=source_bytes,
            ),
        ),
    )


def _write_seed_files(seed_dir: Path, package: CoursePackage) -> dict[str, Path]:
    seed_dir.mkdir(parents=True, exist_ok=True)
    evidence_id = evidence_id_for_chunk(package.content_chunks[0].chunk_id)
    payloads = {
        "concept": {
            "knowledge_bundle_id": BUNDLE_ID,
            "bundle_version": "1.0.0",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                _concept("concept_ipv6", "IPv6 地址表示", "chapter_addressing"),
                _concept("concept_subnet", "IPv4 前缀与主机位", "chapter_addressing"),
                _concept("concept_transport", "TCP 与 UDP", "chapter_transport"),
                _concept(
                    "concept_throughput",
                    "吞吐量定义",
                    "chapter_performance",
                ),
            ],
            "concept_evidence_ids": {
                "concept_ipv6": [evidence_id],
                "concept_subnet": [evidence_id],
                "concept_transport": [evidence_id],
                "concept_throughput": [evidence_id],
            },
        },
        "item": {
            "items": [
                _true_false(
                    "item_ipv6_anchor",
                    "IPv6 地址里，一段连续的全 0 分组可以写成 ::，并且整地址最多出现一次 ::。",
                    True,
                    "concept_ipv6",
                    evidence_id,
                ),
                _true_false(
                    "item_ipv6_follow",
                    "IPv6 地址允许在同一地址中多次使用 :: 来压缩多段零。",
                    False,
                    "concept_ipv6",
                    evidence_id,
                    misconception_ids=["misconception_double_colon"],
                ),
                _fill(
                    "item_subnet_host_bits",
                    "IPv4 前缀 /24 对应的主机位数是多少？只写数字。",
                    ["8"],
                    "concept_subnet",
                    evidence_id,
                ),
                _fill(
                    "item_transport_handshake",
                    "三次握手属于哪一种传输层协议？请填写 TCP 或 UDP。",
                    ["TCP", "tcp"],
                    "concept_transport",
                    evidence_id,
                    misconception_ids=["misconception_udp_handshake"],
                ),
                _parameterized_protocol(evidence_id),
                _fill(
                    "item_throughput_bits",
                    "若 8000 bit 在 2 秒内成功传送，吞吐量是多少 bit/s？只写数字。",
                    ["4000"],
                    "concept_throughput",
                    evidence_id,
                ),
            ],
            "q_matrix": [
                _q("item_ipv6_anchor", "concept_ipv6"),
                _q("item_ipv6_follow", "concept_ipv6"),
                _q("item_subnet_host_bits", "concept_subnet"),
                _q("item_transport_handshake", "concept_transport"),
                _q("item_protocol_template", "concept_transport"),
                _q("item_throughput_bits", "concept_throughput"),
            ],
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_network_diagnostic",
                    "version": "1.0.0",
                    "course_id": package.course_id,
                    "sections": [
                        _section(
                            "sec_anchor",
                            "公共锚点",
                            "anchor",
                            "concept_ipv6",
                            ["item_ipv6_anchor"],
                        ),
                        _section(
                            "sec_uncertainty",
                            "不确定点",
                            "uncertainty",
                            "concept_subnet",
                            ["item_subnet_host_bits"],
                        ),
                        _section(
                            "sec_misconception",
                            "误区",
                            "misconception",
                            "concept_transport",
                            ["item_transport_handshake"],
                        ),
                        _section(
                            "sec_remediation",
                            "补救",
                            "remediation",
                            "concept_throughput",
                            ["item_throughput_bits"],
                        ),
                    ],
                    "total_score": 4.0,
                    "duration_minutes": 30,
                    "status": "teacher_approved",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {
            "misconception_tags": [
                {
                    "misconception_id": "misconception_double_colon",
                    "name": "认为 :: 可以在同一地址出现多次",
                    "description": "IPv6 压缩规则只允许一次 ::。",
                    "concept_ids": ["concept_ipv6"],
                    "evidence_rules": ["double colon appears more than once"],
                },
                {
                    "misconception_id": "misconception_udp_handshake",
                    "name": "把三次握手当成 UDP 过程",
                    "description": "三次握手属于 TCP。",
                    "concept_ids": ["concept_transport"],
                    "evidence_rules": ["UDP three-way handshake"],
                },
            ]
        },
    }
    paths: dict[str, Path] = {}
    for role, payload in payloads.items():
        path = seed_dir / f"{role}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        paths[role] = path
    return paths


def _concept(concept_id: str, name: str, chapter_id: str) -> dict[str, object]:
    return {
        "concept_id": concept_id,
        "name": name,
        "chapter_id": chapter_id,
        "description": name,
        "aliases": [],
        "status": "published",
    }


def _true_false(
    item_id: str,
    stem: str,
    answer: bool,
    concept_id: str,
    evidence_id: str,
    misconception_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "version": "1.0.0",
        "stem": stem,
        "item_type": "true_false",
        "concept_ids": [concept_id],
        "misconception_ids": list(misconception_ids or []),
        "difficulty_level": 1,
        "cognitive_level": "remember",
        "parameter_rules": [],
        "answer_key": {"answer": answer, "max_score": 1.0},
        "rubric_id": None,
        "source_evidence_ids": [evidence_id],
        "status": "teacher_approved",
    }


def _fill(
    item_id: str,
    stem: str,
    answers: list[str],
    concept_id: str,
    evidence_id: str,
    misconception_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "version": "1.0.0",
        "stem": stem,
        "item_type": "fill_blank",
        "concept_ids": [concept_id],
        "misconception_ids": list(misconception_ids or []),
        "difficulty_level": 2,
        "cognitive_level": "apply",
        "parameter_rules": [],
        "answer_key": {
            "answer": answers[0],
            "answers": answers,
            "max_score": 1.0,
        },
        "rubric_id": None,
        "source_evidence_ids": [evidence_id],
        "status": "teacher_approved",
    }


def _parameterized_protocol(evidence_id: str) -> dict[str, object]:
    return {
        "item_id": "item_protocol_template",
        "version": "1.0.0",
        "stem": "写出本题抽出的协议名称：{protocol}。",
        "item_type": "fill_blank",
        "concept_ids": ["concept_transport"],
        "misconception_ids": [],
        "difficulty_level": 2,
        "cognitive_level": "apply",
        "parameter_rules": [
            {
                "name": "protocol",
                "value_type": "string",
                "minimum": None,
                "maximum": None,
                "choices": ["IPv4", "IPv6", "TCP", "UDP"],
                "constraints": [],
            }
        ],
        "answer_key": {
            "answer": "{protocol}",
            "answers": ["{protocol}"],
            "max_score": 1.0,
        },
        "rubric_id": None,
        "source_evidence_ids": [evidence_id],
        "status": "teacher_approved",
    }


def _q(item_id: str, concept_id: str) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_version": "1.0.0",
        "concept_id": concept_id,
        "weight": 1.0,
    }


def _section(
    section_id: str,
    name: str,
    purpose: str,
    concept_id: str,
    anchors: list[str],
) -> dict[str, object]:
    return {
        "section_id": section_id,
        "name": name,
        "purpose": purpose,
        "item_count": 1,
        "score": 1.0,
        "item_types": [],
        "concept_weights": {concept_id: 1.0},
        "difficulty_range": [1, 3],
        "anchor_item_ids": anchors,
        "anchor_item_versions": {item_id: "1.0.0" for item_id in anchors},
    }


if __name__ == "__main__":
    raise SystemExit(main())
