"""Tests for FIX-FLOW CHECK: Consequence-aware resonance severity, optimizer verification alignment, findings diff, and operating range.
"""

import copy
import pytest

from app.presets import get_demo_design
from app.solver.pipeline import solve_design
from app.solver.post import build_response
from app.optimizer.optimize import optimize_design
from app.schemas import DesignModel, LocksModel


def test_generic_consequence_aware_resonance_rule():
    """Verify generic consequence-aware resonance rule (B2):

    - If resonance crossing is within 5% AND a sensitive component exceeds its limit -> critical.
    - If resonance crossing is within 5% AND all sensitive components are well under limits with low stress -> warning with full numbers in detail.
    """
    base_drone = get_demo_design("drone")

    # Case A: Component limit is strictly exceeded (0.1 g limit vs ~3.5 g peak)
    drone_a = copy.deepcopy(base_drone)
    drone_a["components"][2]["limit_g"] = 0.1  # camera limit 0.1 g
    drone_a["components"][3]["limit_g"] = 0.1  # imu limit 0.1 g

    sol_a = solve_design(drone_a, method="rom")
    resp_a = build_response(drone_a, sol_a)

    res_findings_a = [f for f in resp_a["findings"] if f["type"] == "resonance"]
    assert len(res_findings_a) > 0, "Resonance findings must exist"
    # Mode 4 crossing at ~3185 RPM is within 5% crossing and camera/imu exceed limit -> MUST be critical
    crit_res_a = [f for f in res_findings_a if f["severity"] == "critical" and f.get("mode") == 4]
    assert len(crit_res_a) == 1, "Mode 4 resonance must be critical when sensitive components exceed limits"

    # Case B: Identical structure, but sensitive components have huge limits (100.0 g) and stress is low
    drone_b = copy.deepcopy(base_drone)
    drone_b["components"][2]["limit_g"] = 100.0  # camera limit 100 g (safe)
    drone_b["components"][3]["limit_g"] = 100.0  # imu limit 100 g (safe)

    sol_b = solve_design(drone_b, method="rom")
    resp_b = build_response(drone_b, sol_b)

    res_findings_b = [f for f in resp_b["findings"] if f["type"] == "resonance"]
    assert len(res_findings_b) > 0, "Resonance findings must still exist (not deleted)"
    # Mode 4 crossing at ~3185 RPM is within 5% crossing but consequences are completely safe -> MUST be warning
    mode4_b = next((f for f in res_findings_b if f.get("mode") == 4), None)
    assert mode4_b is not None, "Mode 4 resonance must still be reported"
    assert mode4_b["severity"] == "warning", f"Mode 4 should be warning when safe, got {mode4_b['severity']}"
    assert "Resonance remains at" in mode4_b["detail"], "Detail should state resonance remains"
    assert "but no component exceeds its limit" in mode4_b["detail"], "Detail should explain consequence"
    assert "camera" in mode4_b["detail"] and "imu" in mode4_b["detail"], "Detail must report component status"
    assert "stress" in mode4_b["detail"], "Detail must report stress percentage"


def test_rpm_max_9000_mode5_resonance():
    """Verify mode-5 resonance is detected when rpm_max = 9000 (A4/B7)."""
    drone = get_demo_design("drone")
    drone["analysis"]["rpm_max"] = 9000.0

    sol = solve_design(drone, method="rom")
    resp = build_response(drone, sol)

    mode5_finding = next(
        (f for f in resp["findings"] if f["type"] == "resonance" and f.get("mode") == 5),
        None,
    )
    assert mode5_finding is not None, "Mode 5 resonance must be detected with rpm_max = 9000"
    assert 5900 <= mode5_finding["rpm"] <= 6200, f"Mode 5 RPM should be ~6067, got {mode5_finding['rpm']}"


@pytest.mark.slow
def test_drone_optimization_flow_and_alignment():
    """Verify drone optimization flow:

    1. Baseline has >= 1 critical.
    2. Optimizer generates verified candidate.
    3. Candidate design analyzed with /analyze has 0 critical findings.
    4. Resonances remain present (truthfully reported as warning).
    5. Camera limit_exceeded is not critical.
    6. Analyze-after-apply camera peak matches optimizer metrics_after within 2%.
    """
    drone_dict = get_demo_design("drone")
    design_model = DesignModel.model_validate(drone_dict)

    # 1. Baseline analysis
    sol_base = solve_design(design_model, method="rom")
    resp_base = build_response(design_model, sol_base)
    base_crit_count = sum(1 for f in resp_base["findings"] if f["severity"] == "critical")
    assert base_crit_count >= 1, f"Baseline drone must have critical findings, got {base_crit_count}"

    # 2. Run optimizer
    opt_resp = optimize_design(
        design=design_model,
        locks=LocksModel(),
        max_evals=1200,
        seed=1,
    )
    assert len(opt_resp.candidates) > 0, "Optimizer must return at least 1 candidate"
    cand0 = opt_resp.candidates[0]

    # 3. Analyze candidate design (exact apply path)
    sol_applied = solve_design(cand0.design, method="rom")
    resp_applied = build_response(cand0.design, sol_applied)

    applied_crit_count = sum(1 for f in resp_applied["findings"] if f["severity"] == "critical")
    assert applied_crit_count == 0, (
        f"Applied candidate should have 0 critical findings (safely resolved), got {applied_crit_count}: "
        f"{[f for f in resp_applied['findings'] if f['severity'] == 'critical']}"
    )

    # 4. Resonance findings are still present
    res_findings_applied = [f for f in resp_applied["findings"] if f["type"] == "resonance"]
    assert len(res_findings_applied) >= 2, "Resonances must not be deleted or hidden"
    for rf in res_findings_applied:
        assert rf["severity"] == "warning", f"Safe resonance must be warning, got {rf['severity']}"

    # 5. Camera limit_exceeded is not critical
    cam_limit_findings = [
        f for f in resp_applied["findings"]
        if f["type"] == "limit_exceeded" and f.get("component") == "camera"
    ]
    for clf in cam_limit_findings:
        assert clf["severity"] != "critical", "Camera limit finding must not be critical"

    # 6. Peak alignment within 2%
    cam_comp_applied = next((c for c in resp_applied["components"] if c["id"] == "camera"), None)
    assert cam_comp_applied is not None
    pk_analyze = float(cam_comp_applied["peak_accel_g"])
    pk_opt_metrics = float(cand0.metrics_after.components["camera"].peak_accel_g)

    pct_diff = abs(pk_analyze - pk_opt_metrics) / pk_opt_metrics * 100.0
    assert pct_diff <= 2.0, f"Analyze peak {pk_analyze:.4f} and opt peak {pk_opt_metrics:.4f} differ by {pct_diff:.2f}% (> 2%)"


@pytest.mark.slow
def test_findings_diff_block_and_counts():
    """Verify /optimize candidate findings_diff and count summaries (B5)."""
    drone_dict = get_demo_design("drone")
    design_model = DesignModel.model_validate(drone_dict)

    opt_resp = optimize_design(
        design=design_model,
        locks=LocksModel(),
        max_evals=1200,
        seed=1,
    )
    assert len(opt_resp.candidates) > 0
    cand0 = opt_resp.candidates[0]

    # Verify counts structure
    assert cand0.findings_before_counts is not None
    assert "critical" in cand0.findings_before_counts
    assert "warning" in cand0.findings_before_counts
    assert cand0.findings_before_counts["critical"] >= 2

    assert cand0.findings_after_counts is not None
    assert cand0.findings_after_counts["critical"] == 0

    # Verify findings_diff structure
    assert cand0.findings_diff is not None
    assert "resolved" in cand0.findings_diff
    assert "remaining" in cand0.findings_diff
    assert "new" in cand0.findings_diff

    # Camera and IMU limit_exceeded should be in remaining (with severity downgraded to warning) or resolved
    cam_in_diff = any(
        f.component == "camera" and f.type == "limit_exceeded"
        for f in cand0.findings_diff["remaining"]
    )
    assert cam_in_diff, "Camera limit_exceeded should be in remaining (with downgraded severity)"

    # Check that before_severity was recorded for the downgraded camera finding
    cam_rem = next(
        f for f in cand0.findings_diff["remaining"]
        if f.component == "camera" and f.type == "limit_exceeded"
    )
    assert cam_rem.before_severity == "critical", f"before_severity should be critical, got {cam_rem.before_severity}"
    assert cam_rem.severity == "warning", f"after severity should be warning, got {cam_rem.severity}"
