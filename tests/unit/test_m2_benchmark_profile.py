from __future__ import annotations

from scripts.benchmark_m2_pgvector import build_parser, config_from_args
from course_insight.modules.m2_evidence_retrieval.performance import (
    TARGET_SCALE_PROFILE,
)


def test_target_scale_profile_is_explicit_and_does_not_enable_ann() -> None:
    args = build_parser().parse_args(["--profile", "target"])
    config = config_from_args(args)

    assert config.chunk_count == 50_000
    assert config.dimension == 1_536
    assert config.query_count == 100
    assert config.top_k == 10
    assert config.ann_enabled is False
    assert TARGET_SCALE_PROFILE.profile_id == "m2-target-scale-v1"

