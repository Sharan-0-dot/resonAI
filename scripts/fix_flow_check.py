"""Diagnostic script for FIX-FLOW CHECK (ResonAI).

Reproduces and diagnoses the workflow:
1. Load drone preset
2. Run /analyze (full solver pipeline)
3. Run /optimize with default settings
4. Apply candidate[0].design exactly as UI would
5. Run /analyze on applied candidate design
6. Output 3 findings tables (BEFORE, AFTER, DIFF) and sensitive component metrics.
"""

from pathlib import Path
import sys

# Ensure backend modules can be imported
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import numpy as np

from app.presets import get_demo_design
from app.solver.pipeline import solve_design
from app.solver.post import build_response
from app.optimizer.optimize import optimize_design
from app.schemas import DesignModel, LocksModel


def finding_key(f: dict) -> tuple:
    ftype = f.get("type", "")
    if ftype == "resonance":
        return ("resonance", f.get("mode"))
    elif ftype in ("limit_exceeded", "mount_amplification"):
        return (ftype, f.get("component"))
    else:
        return (ftype, f.get("component") or f.get("mode"))


def format_table(headers: list, rows: list) -> str:
    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            col_widths[i] = max(col_widths[i], len(str(val)))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    row_lines = [
        " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(r))
        for r in rows
    ]
    return f"{header_line}\n{sep_line}\n" + "\n".join(row_lines)


def run_check():
    print("=" * 80)
    print("RESONAI FIX-FLOW CHECK: DRONE PRESET")
    print("=" * 80)

    # 1. Load drone preset
    drone_dict = get_demo_design("drone")
    design_before = DesignModel.model_validate(drone_dict)
    n_points_analyze = design_before.analysis.n_points if design_before.analysis else 400

    # 2. Run Analyze BEFORE
    sol_before = solve_design(design_before, method="rom")
    resp_before = build_response(design_before, sol_before)
    findings_before = resp_before.get("findings", [])

    # 3. Run Optimize (default settings)
    opt_resp = optimize_design(
        design=design_before,
        locks=LocksModel(),
        max_evals=1200,
        seed=1,
    )

    if not opt_resp.candidates:
        print("[ERROR] Optimizer produced 0 candidates!")
        return

    cand0 = opt_resp.candidates[0]
    cand_design = cand0.design
    metrics_after_opt = cand0.metrics_after

    # 4. Run Analyze AFTER (on candidate[0].design exactly)
    sol_after = solve_design(cand_design, method="rom")
    resp_after = build_response(cand_design, sol_after)
    findings_after = resp_after.get("findings", [])

    # Table 1: Findings BEFORE
    print("\n" + "=" * 80)
    print("TABLE 1: FINDINGS BEFORE (Baseline Drone)")
    print("=" * 80)
    rows_before = []
    for f in findings_before:
        rpm_str = f"{f.get('rpm'):.0f}" if f.get("rpm") is not None else "-"
        val_str = f"{f.get('value'):.2f}" if f.get("value") is not None else "-"
        rows_before.append([
            f.get("id"),
            f.get("severity"),
            f.get("type"),
            str(f.get("component") or f"mode {f.get('mode')}"),
            rpm_str,
            val_str,
            (f.get("detail", "")[:70] + "...") if len(f.get("detail", "")) > 70 else f.get("detail", ""),
        ])
    print(format_table(["ID", "Severity", "Type", "Component/Mode", "RPM", "Value", "Detail"], rows_before))

    # Table 2: Findings AFTER
    print("\n" + "=" * 80)
    print("TABLE 2: FINDINGS AFTER (Analyze on Candidate[0].design)")
    print("=" * 80)
    rows_after = []
    for f in findings_after:
        rpm_str = f"{f.get('rpm'):.0f}" if f.get("rpm") is not None else "-"
        val_str = f"{f.get('value'):.2f}" if f.get("value") is not None else "-"
        rows_after.append([
            f.get("id"),
            f.get("severity"),
            f.get("type"),
            str(f.get("component") or f"mode {f.get('mode')}"),
            rpm_str,
            val_str,
            (f.get("detail", "")[:70] + "...") if len(f.get("detail", "")) > 70 else f.get("detail", ""),
        ])
    print(format_table(["ID", "Severity", "Type", "Component/Mode", "RPM", "Value", "Detail"], rows_after))

    # Table 3: Diff: Resolved / Remaining / New
    print("\n" + "=" * 80)
    print("TABLE 3: FINDINGS DIFF (Resolved / Remaining / New)")
    print("=" * 80)
    before_map = {finding_key(f): f for f in findings_before}
    after_map = {finding_key(f): f for f in findings_after}

    all_keys = list(dict.fromkeys(list(before_map.keys()) + list(after_map.keys())))
    diff_rows = []

    for k in all_keys:
        f_b = before_map.get(k)
        f_a = after_map.get(k)
        k_str = f"{k[0]}: {k[1]}" if len(k) > 1 and k[1] is not None else k[0]

        if f_b and not f_a:
            diff_rows.append([
                "RESOLVED",
                k_str,
                f"{f_b.get('severity')} -> None",
                f_b.get("detail", "")[:65],
            ])
        elif f_b and f_a:
            sev_b = f_b.get("severity")
            sev_a = f_a.get("severity")
            sev_change = f"{sev_b} -> {sev_a}" if sev_b != sev_a else f"{sev_b} (unchanged)"
            diff_rows.append([
                "REMAINING",
                k_str,
                sev_change,
                f_a.get("detail", "")[:65],
            ])
        elif not f_b and f_a:
            diff_rows.append([
                "NEW",
                k_str,
                f"None -> {f_a.get('severity')}",
                f_a.get("detail", "")[:65],
            ])

    print(format_table(["Status", "Finding Key", "Severity Shift", "Detail"], diff_rows))

    # Detailed metrics for sensitive components
    print("\n" + "=" * 80)
    print("SENSITIVE COMPONENT METRICS COMPARISON")
    print("=" * 80)
    comp_before_map = {c["id"]: c for c in resp_before.get("components", [])}
    comp_after_map = {c["id"]: c for c in resp_after.get("components", [])}
    sensitive_comps = [c for c in drone_dict.get("components", []) if c.get("limit_g") is not None]

    sens_rows = []
    for sc in sensitive_comps:
        cid = sc["id"]
        lim = sc.get("limit_g")
        cb = comp_before_map.get(cid, {})
        ca = comp_after_map.get(cid, {})
        opt_c = metrics_after_opt.components.get(cid) if metrics_after_opt and metrics_after_opt.components else None

        pk_before = cb.get("peak_accel_g", 0.0)
        pk_after_analyze = ca.get("peak_accel_g", 0.0)
        pk_opt_verified = opt_c.peak_accel_g if opt_c else 0.0

        diff_analyze_vs_opt = (
            abs(pk_after_analyze - pk_opt_verified) / pk_opt_verified * 100.0
            if pk_opt_verified > 0 else 0.0
        )

        sens_rows.append([
            cid,
            f"{lim:.2f} g",
            f"{pk_before:.4f} g",
            f"{pk_after_analyze:.4f} g",
            f"{pk_opt_verified:.4f} g",
            f"{diff_analyze_vs_opt:.2f}%",
        ])

    print(format_table([
        "Component", "Limit", "Peak Before", "Peak After (Analyze)", "Peak After (Opt Verif)", "Discrepancy"
    ], sens_rows))

    # Discretization and Grid info
    print("\n" + "=" * 80)
    print("RPM GRID & REFINEMENT INFO")
    print("=" * 80)
    print(f"- /analyze grid points: {len(sol_before.rpm)} uniform points (min={sol_before.rpm[0]:.0f}, max={sol_before.rpm[-1]:.0f})")
    print(f"- Optimizer search grid: 200 points (coarse surrogate)")
    print(f"- Optimizer verification grid: 1000 points (full direct solve)")
    print(f"- Stress ratio BEFORE: {resp_before.get('max_stress_mpa', {}).get('ratio', 0.0):.4f} ({resp_before.get('max_stress_mpa', {}).get('value', 0.0):.2f} MPa)")
    print(f"- Stress ratio AFTER (Analyze): {resp_after.get('max_stress_mpa', {}).get('ratio', 0.0):.4f} ({resp_after.get('max_stress_mpa', {}).get('value', 0.0):.2f} MPa)")
    print(f"- Stress ratio AFTER (Opt Verif): {metrics_after_opt.max_stress_ratio:.4f}")

    # Changes applied by optimizer
    print("\n" + "=" * 80)
    print("OPTIMIZER CANDIDATE CHANGES")
    print("=" * 80)
    for ch in cand0.changes:
        print(f"  * {ch.label} ({ch.path}: {ch.from_val} -> {ch.to_val})")


if __name__ == "__main__":
    run_check()
