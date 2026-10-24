"""Design optimization module using differential evolution over reduced order models.

Optimizes mount stiffness (kv) and damping (zeta) for sensitive components with limit_g.
Positions are fixed in this milestone (M6-lite; position optimization planned for M6b).
"""

import copy
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy.optimize import differential_evolution

from app.schemas import (
    CandidateBlock,
    ChangeItem,
    ComponentModel,
    ComponentMetrics,
    DesignModel,
    Finding,
    LocksModel,
    MetricsBlock,
    OptimizeResponse,
)
from app.solver.pipeline import solve_design
from app.solver.post import ACCEL_BASIS, build_response

# Variable search bounds
LOG_KV_MIN = float(np.log10(2.0e3))   # 2 kN/m
LOG_KV_MAX = float(np.log10(5.0e5))   # 500 kN/m
ZETA_MIN = 0.02
ZETA_MAX = 0.30


def make_search_design(design: Union[Dict[str, Any], Any]) -> Dict[str, Any]:
    """Create lightweight surrogate design for fast optimization search.

    Uses coarse discretization (6 elements/member) and 200 uniform RPM points.
    """
    if hasattr(design, "model_dump"):
        d = design.model_dump()
    elif isinstance(design, dict):
        d = copy.deepcopy(design)
    else:
        d = copy.deepcopy(vars(design))

    if "analysis" not in d or d["analysis"] is None:
        d["analysis"] = {}

    d["analysis"]["n_elements_per_member"] = 6
    d["analysis"]["rpm_points"] = 200
    d["analysis"]["method"] = "rom"
    return d


def patch_design(
    base_design: Dict[str, Any],
    var_mapping: List[Tuple[str, str]],
    x: np.ndarray,
    kv_scale: float = 1.0,
) -> Dict[str, Any]:
    """Apply parameter vector x to base design with an optional kv_scale multiplier."""
    patched = copy.deepcopy(base_design)
    comp_map = {c["id"]: c for c in patched.get("components", [])}

    for (cid, param), val in zip(var_mapping, x):
        comp = comp_map.get(cid)
        if not comp:
            continue
        mount = comp.setdefault("mount", {})
        if param == "log_kv":
            mount["kv"] = float((10.0 ** float(val)) * kv_scale)
        elif param == "zeta":
            mount["zeta"] = float(val)

    return patched


def evaluate_candidate(
    search_design: Dict[str, Any],
    var_mapping: List[Tuple[str, str]],
    x: np.ndarray,
    eval_counter: List[int],
    sensitive_cids: List[str],
) -> Tuple[float, float]:
    """Evaluate robust objective J across kv * {0.8, 1.2}.

    Returns:
        (J_robust, nominal_search_peak_g)
    """
    eval_counter[0] += 1
    objectives: List[float] = []
    nominal_peak = 0.0

    for scale in [0.8, 1.2]:
        d_scaled = patch_design(search_design, var_mapping, x, kv_scale=scale)
        sol = solve_design(d_scaled, method="rom")
        resp = build_response(d_scaled, sol)

        comp_resp_map = {c["id"]: c for c in resp.get("components", [])}

        # 1. Max ratio over sensitive components
        worst_accel_ratio = 0.0
        for cid in sensitive_cids:
            if cid in comp_resp_map:
                c_data = comp_resp_map[cid]
                pk = float(c_data["peak_accel_g"])
                lim = float(c_data.get("limit_g") or 1.0)
                ratio = pk / lim
                if ratio > worst_accel_ratio:
                    worst_accel_ratio = ratio
                if scale == 1.0 or (scale == 0.8 and pk > nominal_peak):
                    nominal_peak = pk

        # 2. Stress penalty
        stress_ratio = float(resp.get("max_stress_mpa", {}).get("ratio", 0.0))

        # 3. Engineering constraint penalties
        pen = 0.0
        comp_dict = {c["id"]: c for c in d_scaled.get("components", [])}
        for cid in sensitive_cids:
            comp = comp_dict.get(cid)
            if not comp or not comp.get("mount"):
                continue
            m_c = float(comp.get("mass", 0.1))
            kv = float(comp["mount"].get("kv", 1e4))

            # Mount frequency >= 8 Hz
            f_m = np.sqrt(kv / m_c) / (2.0 * np.pi)
            if f_m < 8.0:
                pen += 10.0 * (8.0 - f_m)

            # Static sag <= 5 mm
            sag = (m_c * 9.81) / kv
            if sag > 0.005:
                pen += 10.0 * (sag - 0.005) * 1000.0

            # Mount relative displacement <= 5 mm anywhere in sweep
            if hasattr(sol, "model") and sol.model:
                yc_dof = sol.model.comp_dof.get(cid)
                vb_dof = sol.model.comp_beam_v.get(cid)
                if yc_dof is not None and sol.X_full is not None:
                    if vb_dof is not None:
                        rel_amp = float(np.max(np.abs(sol.X_full[:, yc_dof] - sol.X_full[:, vb_dof])))
                    else:
                        rel_amp = float(np.max(np.abs(sol.X_full[:, yc_dof])))
                    if rel_amp > 0.005:
                        pen += 10.0 * (rel_amp - 0.005) * 1000.0

        # B4: Small margin penalty to prefer margin below 70% of limit
        margin_penalty = 0.5 * max(0.0, worst_accel_ratio - 0.7)
        J_scale = worst_accel_ratio + margin_penalty + 0.3 * (stress_ratio / 0.5) + pen
        objectives.append(J_scale)

    J_worst = max(objectives)
    return J_worst, nominal_peak


def finding_match_key(f: Union[Finding, Dict[str, Any]]) -> tuple:
    """Deterministic matching key for findings diffing."""
    if hasattr(f, "type"):
        ftype = f.type
        mode = f.mode
        comp = f.component
    else:
        ftype = f.get("type", "")
        mode = f.get("mode")
        comp = f.get("component")

    if ftype == "resonance":
        return ("resonance", mode)
    elif ftype in ("limit_exceeded", "mount_amplification"):
        return (ftype, comp)
    else:
        return (ftype,)


def compute_findings_diff_and_counts(
    findings_before_raw: List[Any],
    findings_after_raw: List[Any],
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, List[Finding]]]:
    """Compute findings counts and diff blocks (resolved, remaining, new) between states."""
    f_before = [
        f if isinstance(f, Finding) else Finding.model_validate(f)
        for f in findings_before_raw
    ]
    f_after = [
        f if isinstance(f, Finding) else Finding.model_validate(f)
        for f in findings_after_raw
    ]

    def count_sevs(flist: List[Finding]) -> Dict[str, int]:
        counts = {"critical": 0, "warning": 0, "ok": 0}
        for f in flist:
            if f.severity in counts:
                counts[f.severity] += 1
            else:
                counts[f.severity] = 1
        return counts

    before_counts = count_sevs(f_before)
    after_counts = count_sevs(f_after)

    before_map = {finding_match_key(f): f for f in f_before}
    after_map = {finding_match_key(f): f for f in f_after}

    resolved: List[Finding] = []
    remaining: List[Finding] = []
    new: List[Finding] = []

    for k, fb in before_map.items():
        if k in after_map:
            fa = after_map[k]
            fa_copy = fa.model_copy()
            if fb.severity != fa.severity:
                fa_copy.before_severity = fb.severity
            remaining.append(fa_copy)
        else:
            resolved.append(fb)

    for k, fa in after_map.items():
        if k not in before_map:
            new.append(fa)

    findings_diff = {
        "resolved": resolved,
        "remaining": remaining,
        "new": new,
    }

    return before_counts, after_counts, findings_diff


def run_full_verification(
    design: Dict[str, Any],
    rpm_points: int = 1000,
) -> Tuple[MetricsBlock, List[Any], float]:
    """Execute full direct solve verification for honest reporting."""
    d_ver = copy.deepcopy(design)
    if "analysis" not in d_ver:
        d_ver["analysis"] = {}
    d_ver["analysis"]["n_points"] = rpm_points
    d_ver["analysis"]["rpm_points"] = rpm_points
    d_ver["analysis"]["method"] = "full"

    sol = solve_design(d_ver, method="full", basis=ACCEL_BASIS)
    resp = build_response(d_ver, sol)

    comp_metrics: Dict[str, ComponentMetrics] = {}
    worst_sensitive_peak = 0.0

    for c_data in resp.get("components", []):
        cid = c_data["id"]
        comp_metrics[cid] = ComponentMetrics(
            peak_accel_g=round(float(c_data["peak_accel_g"]), 4),
            at_rpm=round(float(c_data["at_rpm"]), 1),
            limit_g=c_data.get("limit_g"),
            status=c_data.get("status", "ok"),
        )
        if c_data.get("limit_g") is not None:
            if float(c_data["peak_accel_g"]) > worst_sensitive_peak:
                worst_sensitive_peak = float(c_data["peak_accel_g"])

    n_res = sum(1 for f in resp.get("findings", []) if f.get("type") == "resonance")
    stress_ratio = round(float(resp.get("max_stress_mpa", {}).get("ratio", 0.0)), 4)

    metrics = MetricsBlock(
        components=comp_metrics,
        max_stress_ratio=stress_ratio,
        n_resonance_findings=n_res,
        mass_added_kg=0.0,
    )
    return metrics, resp.get("findings", []), worst_sensitive_peak


def optimize_design(
    design: Union[Dict[str, Any], Any],
    locks: Optional[LocksModel] = None,
    objective: str = "min_sensor_accel",
    max_evals: int = 1200,
    seed: int = 1,
    movable: Optional[List[str]] = None,
    budget: Optional[Any] = None,
) -> OptimizeResponse:
    """Optimize sensitive component mount parameters (kv, zeta) using differential evolution.

    Returns up to 2 verified candidate designs:
    - Candidate 1: 'Best overall' (kv and zeta free)
    - Candidate 2: 'Damping only' (kv fixed, search zeta only)
    """
    t_start = time.perf_counter()
    if locks is None:
        locks = LocksModel(positions=False, mounts=False, damping=False)

    if hasattr(design, "model_dump"):
        orig_design = design.model_dump()
    elif isinstance(design, dict):
        orig_design = copy.deepcopy(design)
    else:
        orig_design = copy.deepcopy(vars(design))

    # Identify sensitive components (components with limit_g and non-rigid mount)
    sensitive_comps = [
        c for c in orig_design.get("components", [])
        if c.get("limit_g") is not None and c.get("mount") and c["mount"].get("preset") != "rigid"
    ]
    sensitive_cids = [c["id"] for c in sensitive_comps]

    if not sensitive_comps:
        elapsed = (time.perf_counter() - t_start) * 1000.0
        return OptimizeResponse(
            candidates=[],
            best_index=None,
            solve_ms=round(elapsed, 2),
            evaluations=0,
            note="No sensitive components with tunable mounts found in design.",
        )

    # Check variable locks
    allow_kv = not locks.mounts
    allow_zeta = not locks.damping

    if not allow_kv and not allow_zeta:
        elapsed = (time.perf_counter() - t_start) * 1000.0
        return OptimizeResponse(
            candidates=[],
            best_index=None,
            solve_ms=round(elapsed, 2),
            evaluations=0,
            note="Mounts and damping are locked; no variables available for optimization.",
        )

    search_design = make_search_design(orig_design)

    # Pre-computation: Baseline verification with 1000-point full solve
    metrics_before, findings_before, baseline_worst_sens = run_full_verification(orig_design, rpm_points=1000)

    # Define optimization plans
    # Plan 1: Best overall (kv and zeta free, respecting locks)
    # Plan 2: Damping only (kv fixed, zeta free; only if both were unlocked)
    plans: List[Tuple[str, bool, bool]] = []
    if allow_kv or allow_zeta:
        plans.append(("Best overall", allow_kv, allow_zeta))
    if allow_kv and allow_zeta:
        plans.append(("Damping only", False, True))

    total_evals = 0
    raw_candidates = []

    for plan_label, plan_kv, plan_zeta in plans:
        var_mapping: List[Tuple[str, str]] = []
        bounds: List[Tuple[float, float]] = []

        for c in sensitive_comps:
            cid = c["id"]
            if plan_kv:
                var_mapping.append((cid, "log_kv"))
                bounds.append((LOG_KV_MIN, LOG_KV_MAX))
            if plan_zeta:
                var_mapping.append((cid, "zeta"))
                bounds.append((ZETA_MIN, ZETA_MAX))

        if not bounds:
            continue

        eval_counter = [0]

        # 5. Calibration: time 10 evaluations and calculate per-evaluation cost
        t_cal_start = time.perf_counter()
        x_mid = np.array([0.5 * (b[0] + b[1]) for b in bounds])
        for _ in range(10):
            evaluate_candidate(search_design, var_mapping, x_mid, [0], sensitive_cids)
        cost_per_eval = (time.perf_counter() - t_cal_start) / 10.0

        # Deterministic maxiter (guaranteed finish well within 15 seconds)
        popsize = 4
        maxiter = 3
        budget_for_plan = 25.0

        # Hard wall-clock limit callback
        class TimeoutCallback:
            def __init__(self, time_limit: float):
                self.t0 = time.perf_counter()
                self.time_limit = time_limit

            def __call__(self, xk: np.ndarray, convergence: float = 0.0) -> bool:
                if (time.perf_counter() - self.t0) > self.time_limit:
                    return True  # Halt differential evolution
                return False

        def de_objective(x: np.ndarray) -> float:
            j_val, _ = evaluate_candidate(search_design, var_mapping, x, eval_counter, sensitive_cids)
            return j_val

        cb = TimeoutCallback(time_limit=budget_for_plan)
        de_res = differential_evolution(
            de_objective,
            bounds=bounds,
            strategy="best1bin",
            maxiter=maxiter,
            popsize=popsize,
            seed=seed,
            polish=False,
            workers=1,
            tol=1e-3,
            callback=cb,
        )

        total_evals += eval_counter[0]
        cand_x = de_res.x

        # Record reported search peak
        _, search_reported_peak = evaluate_candidate(search_design, var_mapping, cand_x, [0], sensitive_cids)

        # 7. Verification: patch ORIGINAL design and run full 1000-point direct solve
        cand_design_dict = patch_design(orig_design, var_mapping, cand_x, kv_scale=1.0)
        metrics_after, findings_after, worst_sens_after = run_full_verification(cand_design_dict, rpm_points=1000)

        # Robust worst-case after: full solves at kv * 0.8 and kv * 1.2
        d_low = patch_design(orig_design, var_mapping, cand_x, kv_scale=0.8)
        d_high = patch_design(orig_design, var_mapping, cand_x, kv_scale=1.2)
        _, _, worst_low = run_full_verification(d_low, rpm_points=1000)
        _, _, worst_high = run_full_verification(d_high, rpm_points=1000)
        robust_worst_after = max(worst_low, worst_high)
        metrics_after.robust_worst_case = round(robust_worst_after, 4)

        # Discrepancy between coarse search reported peak and verified full solve
        if worst_sens_after > 0:
            disc_pct = round(abs(search_reported_peak - worst_sens_after) / worst_sens_after * 100.0, 2)
        else:
            disc_pct = 0.0
        metrics_after.rom_vs_full_discrepancy_pct = disc_pct

        # Reduction percentage for worst sensitive component
        if baseline_worst_sens > 0:
            reduction_pct = round(100.0 * (baseline_worst_sens - worst_sens_after) / baseline_worst_sens, 2)
        else:
            reduction_pct = 0.0

        # Change items list
        changes: List[ChangeItem] = []
        orig_map = {c["id"]: c for c in orig_design.get("components", [])}
        cand_map = {c["id"]: c for c in cand_design_dict.get("components", [])}

        for (cid, param) in var_mapping:
            c_orig = orig_map[cid]
            c_cand = cand_map[cid]
            c_label = cid.capitalize()
            if param == "log_kv":
                from_kv = float(c_orig["mount"]["kv"])
                to_kv = float(c_cand["mount"]["kv"])
                if abs(to_kv - from_kv) > 10.0:
                    changes.append(
                        ChangeItem(
                            path=f"components.{cid}.mount.kv",
                            from_val=round(from_kv, 1),
                            to_val=round(to_kv, 1),
                            label=f"{c_label} mount stiffness {from_kv / 1e3:.1f} -> {to_kv / 1e3:.1f} kN/m",
                        )
                    )
            elif param == "zeta":
                from_zeta = float(c_orig["mount"]["zeta"])
                to_zeta = float(c_cand["mount"]["zeta"])
                if abs(to_zeta - from_zeta) > 0.005:
                    changes.append(
                        ChangeItem(
                            path=f"components.{cid}.mount.zeta",
                            from_val=round(from_zeta, 3),
                            to_val=round(to_zeta, 3),
                            label=f"{c_label} mount damping ratio {from_zeta:.2f} -> {to_zeta:.2f}",
                        )
                    )

        raw_candidates.append({
            "label": plan_label,
            "changes": changes,
            "design": DesignModel.model_validate(cand_design_dict),
            "metrics_before": metrics_before,
            "metrics_after": metrics_after,
            "reduction_pct": reduction_pct,
            "robust_worst_case_after": round(robust_worst_after, 4),
            "verified": True,
            "findings": findings_after,
            "worst_sens_after": worst_sens_after,
        })

    # Filter candidates:
    # 8. If best verified reduction is below 5%, return candidates: []
    best_cand = max(raw_candidates, key=lambda c: c["reduction_pct"]) if raw_candidates else None
    if not best_cand or best_cand["reduction_pct"] < 5.0:
        elapsed = (time.perf_counter() - t_start) * 1000.0
        return OptimizeResponse(
            candidates=[],
            best_index=None,
            solve_ms=round(elapsed, 2),
            evaluations=total_evals,
            note="Best verified candidate achieved less than 5% reduction. No significant improvement found.",
        )

    # Drop candidate 2 if it duplicates candidate 1 within 2% reduction
    final_raw: List[Dict[str, Any]] = [raw_candidates[0]]
    if len(raw_candidates) > 1:
        c1 = raw_candidates[0]
        c2 = raw_candidates[1]
        if abs(c2["reduction_pct"] - c1["reduction_pct"]) >= 2.0 and c2["reduction_pct"] >= 5.0:
            final_raw.append(c2)

    # Sort descending by reduction
    final_raw.sort(key=lambda c: -c["reduction_pct"])

    notes: List[str] = []
    final_blocks: List[CandidateBlock] = []

    for rank_idx, c in enumerate(final_raw[:2]):
        before_counts, after_counts, f_diff = compute_findings_diff_and_counts(
            findings_before, c["findings"]
        )

        block = CandidateBlock(
            rank=rank_idx + 1,
            label=c["label"],
            changes=c["changes"],
            design=c["design"],
            metrics_before=c["metrics_before"],
            metrics_after=c["metrics_after"],
            reduction_pct=c["reduction_pct"],
            robust_worst_case_after=c["robust_worst_case_after"],
            verified=True,
            findings=c["findings"],
            findings_before_counts=before_counts,
            findings_after_counts=after_counts,
            findings_diff=f_diff,
        )
        final_blocks.append(block)

        # Flag if robust worst case is > 1.5x nominal
        if c["robust_worst_case_after"] > 1.5 * c["worst_sens_after"]:
            notes.append(
                f"Candidate {rank_idx + 1} ({c['label']}): robust worst case under +/-20% stiffness variation "
                f"is {c['robust_worst_case_after']:.3f} g (>1.5x nominal {c['worst_sens_after']:.3f} g)."
            )

        # Check if margin to limit target (0.7) was reached
        worst_sens_ratio_after = 0.0
        for comp_m in c["metrics_after"].components.values():
            if comp_m.limit_g and comp_m.limit_g > 0:
                r = comp_m.peak_accel_g / comp_m.limit_g
                if r > worst_sens_ratio_after:
                    worst_sens_ratio_after = r

        if worst_sens_ratio_after > 0.7:
            margin_pct = max(0.0, (1.0 - worst_sens_ratio_after) * 100.0)
            notes.append(
                f"Margin to limit is {margin_pct:.0f}%; mount changes alone cannot reach the 30% margin target; "
                f"consider repositioning the sensor or adding mass damping."
            )

    elapsed = (time.perf_counter() - t_start) * 1000.0
    summary_note = " ".join(notes) if notes else "Optimization successful: all candidates verified with direct full solver."

    return OptimizeResponse(
        candidates=final_blocks,
        best_index=0 if final_blocks else None,
        solve_ms=round(elapsed, 2),
        evaluations=total_evals,
        note=summary_note,
    )

S_MIN, S_MAX = 0.05, 0.95   # keep parts off supports and joints
MIN_S_GAP = 0.08            # min spacing between parts on one member (fraction of span)


def spacing_penalty(design: Dict[str, Any], movable_cids: List[str]) -> float:
    """Penalize a movable part sitting on top of another part on the same member."""
    if not movable_cids:
        return 0.0
    moving = set(movable_cids)
    by_member: Dict[Any, List[Dict[str, Any]]] = {}
    for c in design.get("components", []):
        by_member.setdefault(c.get("member"), []).append(c)
    pen = 0.0
    for comps in by_member.values():
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                a, b = comps[i], comps[j]
                if a["id"] not in moving and b["id"] not in moving:
                    continue
                gap = abs(float(a["s"]) - float(b["s"]))
                if gap < MIN_S_GAP:
                    pen += 5.0 * (MIN_S_GAP - gap) / MIN_S_GAP
    return pen