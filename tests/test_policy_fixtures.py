"""Reproducible stub-policy comparisons; no live model or browser inference."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jev_ultrafast import agent as loop
from scripts.evaluate_policy import compare_fixture_policies, run_fixture


def test_progress_policy_fills_then_submits_and_preserves_unrelated_control():
    result = run_fixture("memo_submit", "progress")
    assert result["outcome"] == "verified" and result["fixture_pass"] is True
    assert result["actions"] == result["decisions"] == 2
    assert result["planner_calls"] == result["runtime_replans"] == result["text_calls"] == 0
    assert [t["operation"] for t in result["transitions"]] == ["TYPE_TEXT", "CLICK"]


def test_stale_target_requires_a_new_decision_before_success():
    result = run_fixture("stale_target", "progress")
    assert result["outcome"] == "verified" and result["fixture_pass"] is True
    assert result["stale_rejections"] == 1 and result["decisions"] == 3
    assert result["mutation_attempts"] == 3 and result["simulated_inputs"] == result["actions"] == 2
    assert result["transitions"][0]["action"] is None
    assert result["planner_calls"] == result["runtime_replans"] == 0


def test_uncertain_input_retains_environment_and_never_retries_or_claims_success():
    result = run_fixture("uncertain_input", "progress")
    assert result["outcome"] == "inconclusive" and result["score"] is None
    assert result["fixture_pass"] is True and result["environment_retained"] is True
    assert result["decisions"] == result["mutation_attempts"] == result["uncertain_attempts"] == 1
    assert result["actions"] == 0 and result["simulated_inputs"] == 1
    assert result["simulated_effect_observed"] is True
    assert result["transitions"][-1]["status"] == "needs_attention"
    assert result["transitions"][-1]["verification"] == "unknown"
    assert result["planner_calls"] == result["runtime_replans"] == result["text_calls"] == 0


def test_native_select_compares_current_dom_value_not_option_label():
    result = run_fixture("select_submit", "progress")
    assert result["outcome"] == "verified" and result["fixture_pass"] is True
    assert [t["operation"] for t in result["transitions"]] == ["SELECT", "CLICK"]
    assert result["actions"] == 2


def test_missing_prepared_value_is_a_safe_abandonment_not_a_verified_goal():
    result = run_fixture("missing_text", "progress")
    assert result["outcome"] == "abandoned" and result["score"] == 0
    assert result["fixture_pass"] is True and result["actions"] == result["mutation_attempts"] == 0
    assert result["planner_calls"] == result["text_calls"] == 0


def test_comparison_is_reproducible_and_negative_passes_do_not_raise_verified_rate():
    report = compare_fixture_policies(repeats=2)
    assert report == compare_fixture_policies(repeats=2)
    assert report["evidence_scope"] == "simulated_browser_and_stub_policies_not_live_jev"
    progress = report["variants"]["progress"]
    assert progress["summary"]["runs"] == 6 and progress["summary"]["verified"] == 4
    assert progress["summary"]["verified_rate"] == 4 / 6
    assert progress["fixture_passes"] == 6
    for variant in ("premature_done", "blocked"):
        candidate = report["variants"][variant]
        assert candidate["summary"]["verified_rate"] == 0
        assert candidate["fixture_passes"] == 2
    assert all(v["summary"]["planner_calls"] == v["summary"]["text_calls"] == 0
               for v in report["variants"].values())


def test_adversarial_comparison_keeps_uncertainty_in_denominator_and_is_reproducible():
    report = compare_fixture_policies(repeats=2, adversarial=True)
    assert report == compare_fixture_policies(repeats=2, adversarial=True)
    progress = report["variants"]["progress"]
    assert progress["summary"]["runs"] == progress["fixture_passes"] == 4
    assert progress["summary"]["verified"] == progress["summary"]["inconclusive"] == 2
    assert progress["summary"]["verified_rate"] == 0.5
    for variant in ("premature_done", "blocked"):
        candidate = report["variants"][variant]
        assert candidate["fixture_passes"] == candidate["summary"]["verified"] == 0
    assert all(v["summary"]["planner_calls"] == v["summary"]["text_calls"] == 0
               for v in report["variants"].values())


def test_recorded_offline_evidence_matches_current_fixture_results():
    evidence = Path(__file__).resolve().parents[1] / "docs" / "policy-fixtures-offline.json"
    assert json.loads(evidence.read_text()) == compare_fixture_policies(repeats=2)


def test_adversarial_evidence_and_cli_match_current_fixtures():
    root = Path(__file__).resolve().parents[1]
    expected = compare_fixture_policies(repeats=2, adversarial=True)
    evidence = root / "docs" / "policy-fixtures-adversarial-offline.json"
    assert json.loads(evidence.read_text()) == expected
    result = subprocess.run([sys.executable, "-m", "scripts.evaluate_policy", "--adversarial", "--repeats", "2"],
                            cwd=root, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == expected
    assert "replacement_field" not in result.stdout and "Reviewed" not in result.stdout
    assert "offline.invalid" not in result.stdout


def test_boundaries_are_restored_after_comparison():
    browser, chooser = loop.Browser, loop.choose
    compare_fixture_policies(repeats=1)
    assert loop.Browser is browser and loop.choose is chooser


def test_cli_produces_json_with_no_raw_fixture_values():
    command = [sys.executable, "-m", "scripts.evaluate_policy", "--repeats", "1"]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, check=True)
    report = json.loads(result.stdout)
    assert report["variants"]["progress"]["fixture_passes"] == 3
    assert "Reviewed" not in result.stdout and "offline.invalid" not in result.stdout
    assert "Memo" not in result.stdout and "Approved" not in result.stdout
    failed = subprocess.run([*command[:-1], "0"], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert failed.returncode != 0 and "positive integer" in failed.stderr


@pytest.mark.parametrize("repeat_count", [0, -1, True, 1.5])
def test_invalid_repeat_count_is_rejected(repeat_count):
    with pytest.raises(ValueError, match="positive integer"):
        compare_fixture_policies(repeats=repeat_count)


@pytest.mark.parametrize("variant, operation", [("premature_done", "DONE"), ("blocked", "BLOCKED")])
def test_terminal_policy_signals_do_not_pass_positive_fixture(variant, operation):
    result = run_fixture("memo_submit", variant)
    assert result["outcome"] == "abandoned" and result["fixture_pass"] is False
    assert result["actions"] == 0 and result["runtime_replans"] == 0
    assert all(t["operation"] == operation for t in result["transitions"])
