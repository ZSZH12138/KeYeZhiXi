from __future__ import annotations


from scripts.verify_m6_controlled_rollout import verify_evidence


def test_missing_real_evidence_is_blocked_without_claiming_rollout() -> None:
    report = verify_evidence(None)
    assert report["status"] == "blocked"
    assert report["passed"] is False
    assert report["rollout_percentage"] == 0.0


def test_complete_evidence_passes_only_for_zero_rollout_rules_gate() -> None:
    report = verify_evidence(
        {
            "policy_id": "policy-1",
            "dataset_identity": "dataset-1",
            "ope_status": "sufficient_data",
            "approved": True,
            "mode": "rules",
            "rollout_percentage": 0.0,
            "global_kill_switch": False,
        }
    )
    assert report["status"] == "passed"
    assert report["passed"] is True

