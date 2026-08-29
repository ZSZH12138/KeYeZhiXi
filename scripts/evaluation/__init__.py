"""Offline, privacy-minimized evaluation utilities for M7 and M9.

The package deliberately has no downloader and no import-time network activity.
Dataset text stays in caller-owned local files; reports contain identifiers,
checksums, and aggregate metrics only.
"""

from .datasets import (
    DatasetPackage,
    DatasetSplit,
    EvaluationCase,
    compute_package_sha256,
    load_local_dataset_package,
    split_by_question,
)
from .metrics import ScorePrediction, evaluate_scoring, load_score_predictions
from .live_matrix import LiveCaseResult, LiveEvaluationCase, run_live_matrix
from .narrative import evaluate_narrative_cases, load_narrative_cases
from .prepare import prepare_local_dataset_package

__all__ = [
    "DatasetPackage",
    "DatasetSplit",
    "EvaluationCase",
    "LiveCaseResult",
    "LiveEvaluationCase",
    "ScorePrediction",
    "compute_package_sha256",
    "evaluate_narrative_cases",
    "evaluate_scoring",
    "load_local_dataset_package",
    "load_narrative_cases",
    "load_score_predictions",
    "prepare_local_dataset_package",
    "run_live_matrix",
    "split_by_question",
]
