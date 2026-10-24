"""Post-processing: acceleration, displacement, transmitted force, bending stress, findings, Campbell diagram.

Sign and unit conventions:
- Accelerations reported in g (default RMS basis: peak / sqrt(2)).
- Displacements reported in mm.
- Transmitted forces in N.
- Stresses in MPa.
- Yield and allowable stresses in MPa.
- Physical units internally: SI (m, kg, N, Pa, s, Hz, rad/s).
"""

from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np

from app.presets import MATERIALS
from app.schemas import (
    AnalysisMeta,
    AnalyzeResponse,
    CampbellBlock,
    CampbellLine,
    ComponentSummary,
    Finding,
    MaxStress,
    MeshNode,
    ModeInfo,
    PositionRef,
    SweepResult,
)
from app.solver.assemble import Model
from app.solver.elements import frame_element, transform
from app.solver.harmonic import ACCEL_BASIS, build_unit_force
from app.solver.mesh import Mesh
from app.solver.pipeline import PipelineResult

# Consequence-aware resonance severity thresholds (Rule B2)
# A resonance crossing within 5% indicates an operational frequency coincidence with a structural mode.
# If mechanical consequences are within acceptable engineering boundaries (all sensitive payloads under
# limit_g, structural stresses well below yield, deflections below clearance), the resonance is graded
# WARNING rather than CRITICAL, preventing alarm fatigue while truthfully flagging the crossing.
RESONANCE_CROSSING_CRITICAL_BAND: float = 0.05
STRESS_RATIO_CRITICAL_THRESHOLD: float = 0.50
CLEARANCE_MM: float = 5.0


def stress_from_displacements(
    model: Model,
    mesh: Mesh,
    x_full: np.ndarray,
) -> np.ndarray:
    """Calculate peak normal stress (bending + axial) for all elements from displacements.

    Using local end forces f_l = k_l @ T @ u_e, and sigma = |M| * c / I + |N| / A
    using the larger end moment |M| = max(|M1|, |M2|) and larger axial force |N| = max(|N1|, |N2|).

    Args:
        model: Model assembly dataclass.
        mesh: Mesh dataclass.
        x_full: Full displacement array. Shape (n_dofs,) for single state
               or (n_freq, n_dofs) for frequency sweep.

    Returns:
        sigma: Stress array in Pascals (Pa).
               Shape (n_elements,) if x_full is 1D, or (n_freq, n_elements) if 2D.
    """
    is_1d = (x_full.ndim == 1)
    if is_1d:
        X = x_full[None, :]
    else:
        X = x_full

    n_freq = X.shape[0]
    n_elem = len(mesh.elements)
    sigma = np.zeros((n_freq, n_elem), dtype=float)

    for i, elem in enumerate(mesh.elements):
        n1 = elem.node_idx1
        n2 = elem.node_idx2
        dofs = [3 * n1, 3 * n1 + 1, 3 * n1 + 2, 3 * n2, 3 * n2 + 1, 3 * n2 + 2]

        k_local, _ = frame_element(elem.E, elem.A, elem.I, elem.rho, elem.L)
        T = transform(elem.cos, elem.sin)
        # Transformation from global to local: u_local = T @ u_global
        # Local end forces: f_l = k_local @ u_local = (k_local @ T) @ u_global
        k_t = k_local @ T

        # u_elem shape: (6, n_freq)
        u_elem = X[:, dofs].T
        f_local = k_t @ u_elem  # shape (6, n_freq)

        # Local forces: [N1, V1, M1, N2, V2, M2]
        N1 = np.abs(f_local[0, :])
        M1 = np.abs(f_local[2, :])
        N2 = np.abs(f_local[3, :])
        M2 = np.abs(f_local[4, :] if False else f_local[5, :])

        N_peak = np.maximum(N1, N2)
        M_peak = np.maximum(M1, M2)

        # Normal stress: sigma = M * c / I + N / A
        sigma[:, i] = (M_peak * elem.c / elem.I) + (N_peak / elem.A)

    if is_1d:
        return sigma[0, :]
    return sigma


def compute_max_stress(
    design: Dict[str, Any],
    model: Model,
    mesh: Mesh,
    x_full_list: List[np.ndarray],
    rpms: np.ndarray,
) -> Dict[str, Any]:
    """Compute peak stress across elements, frequencies, and harmonic excitation orders.

    Returns:
        dict with {value (MPa), member, at_rpm, yield_mpa, ratio}
    """
    member_material_map: Dict[str, str] = {}
    for m in design.get("members", []):
        mid = m["id"] if isinstance(m, dict) else m.id
        mat = m["material"] if isinstance(m, dict) else m.material
        member_material_map[mid] = mat

    # Evaluate stresses across all order solutions
    overall_max_sigma_pa = 0.0
    max_elem_idx = 0
    max_rpm_val = float(rpms[0]) if len(rpms) > 0 else 0.0

    for x_full in x_full_list:
        sigma_grid = stress_from_displacements(model, mesh, x_full)  # (n_freq, n_elem)
        peak_per_order = np.max(sigma_grid)
        if peak_per_order > overall_max_sigma_pa:
            overall_max_sigma_pa = float(peak_per_order)
            freq_idx, elem_idx = np.unravel_index(np.argmax(sigma_grid), sigma_grid.shape)
            max_elem_idx = int(elem_idx)
            max_rpm_val = float(rpms[freq_idx])

    if mesh.elements:
        worst_elem = mesh.elements[max_elem_idx]
        member_id = worst_elem.member
    else:
        member_id = design.get("members", [{}])[0].get("id", "m1")

    mat_name = member_material_map.get(member_id, "aluminium_6061")
    yield_mpa = float(MATERIALS.get(mat_name, {}).get("yield_mpa", 240.0))

    sigma_mpa = overall_max_sigma_pa / 1.0e6
    ratio = sigma_mpa / yield_mpa if yield_mpa > 0.0 else 0.0

    return {
        "value": round(sigma_mpa, 2),
        "member": member_id,
        "at_rpm": round(max_rpm_val, 1),
        "yield_mpa": round(yield_mpa, 1),
        "ratio": round(ratio, 4),
    }


def generate_campbell(
    result: PipelineResult,
    design: Dict[str, Any],
) -> CampbellBlock:
    """Generate Campbell diagram data with non-rigid natural frequencies and excitation lines."""
    non_rigid_freqs = [
        round(float(f), 2)
        for i, f in enumerate(result.modes_f_hz)
        if not result.is_rigid_body[i] and f > 0.1
    ]
    # Retain up to 10 non-rigid frequencies
    mode_freqs_hz = non_rigid_freqs[:10]

    lines: List[CampbellLine] = []
    seen_orders = set()

    for comp in design.get("components", []):
        c_dict = comp if isinstance(comp, dict) else comp.model_dump()
        exc = c_dict.get("excitation")
        if not exc:
            continue

        cid = c_dict.get("id", "comp")
        orders = exc.get("orders", [1.0])
        blades = exc.get("blades")

        for o in orders:
            o_float = float(o)
            if o_float not in seen_orders:
                seen_orders.add(o_float)
                order_lbl = f"{int(o_float)}x" if o_float.is_integer() else f"{o_float:g}x"
                lines.append(CampbellLine(label=f"{order_lbl} {cid}", order=o_float))

        if blades and int(blades) > 1:
            b_float = float(blades)
            if b_float not in seen_orders:
                seen_orders.add(b_float)
                lines.append(CampbellLine(label=f"{int(b_float)}x blade pass ({cid})", order=b_float))

    if not lines:
        lines.append(CampbellLine(label="1x excitation", order=1.0))

    return CampbellBlock(mode_freqs_hz=mode_freqs_hz, lines=lines)


def generate_findings(
    design: Dict[str, Any],
    result: PipelineResult,
    max_stress_dict: Dict[str, Any],
    basis: str = ACCEL_BASIS,
) -> List[Finding]:
    """Generate deterministic, rule-based engineering findings.

    Rules checked:
    a. resonance: mode within 15% of excitation order line with modal participation > 5%.
    b. limit_exceeded: sensitive component accel > limit_g (or > 0.7 * limit_g).
    c. stress: max stress ratio > 0.5 warning, > 0.8 critical.
    d. mount_amplification: mount natural frequency gives isolation boundary sqrt(2)*f_m in operating range.
    e. displacement: peak node displacement > 5 mm (CLEARANCE_MM).
    """
    findings_raw: List[Finding] = []
    analysis_cfg = design.get("analysis", {})
    if hasattr(analysis_cfg, "model_dump"):
        analysis_cfg = analysis_cfg.model_dump()

    rpm_min = float(analysis_cfg.get("rpm_min", 500.0))
    rpm_max = float(analysis_cfg.get("rpm_max", 12000.0))

    components = design.get("components", [])

    # Collect excitation orders
    comp_excitations = []
    for c in components:
        c_dict = c if isinstance(c, dict) else c.model_dump()
        exc = c_dict.get("excitation")
        if exc:
            comp_excitations.append((c_dict, exc))

    # Assess physical consequences across the entire operating range (Rule B2)
    stress_ratio_val = float(max_stress_dict.get("ratio", 0.0))
    max_disp_val = (
        float(np.max(result.node_amp_mm))
        if (result.node_amp_mm is not None and result.node_amp_mm.size > 0)
        else 0.0
    )

    sensitive_comp_data = []
    for c in components:
        c_dict = c if isinstance(c, dict) else c.model_dump()
        lim_g = c_dict.get("limit_g")
        if lim_g is not None:
            cid = c_dict.get("id", "comp")
            peak_g = float(np.max(result.component_accel_g[cid])) if cid in result.component_accel_g else 0.0
            sensitive_comp_data.append({
                "id": cid,
                "limit_g": float(lim_g),
                "peak_g": peak_g,
                "exceeded": peak_g > float(lim_g),
            })

    no_limits_defined = (len(sensitive_comp_data) == 0)
    any_limit_exceeded = any(sc["exceeded"] for sc in sensitive_comp_data)
    stress_critical = (stress_ratio_val > STRESS_RATIO_CRITICAL_THRESHOLD)
    clearance_violated = (max_disp_val > CLEARANCE_MM)

    has_critical_consequence = (no_limits_defined or any_limit_exceeded or stress_critical or clearance_violated)

    # --- Rule a: Resonance ---
    # Check each non-rigid mode (ONE finding per mode)
    non_rigid_indices = [i for i, rb in enumerate(result.is_rigid_body) if not rb and result.modes_f_hz[i] > 0.1]

    for m_idx in non_rigid_indices:
        f_mode = float(result.modes_f_hz[m_idx])
        phi_free = result.Phi[:, m_idx]

        resonant_sources = []
        for c_dict, exc in comp_excitations:
            cid = c_dict.get("id", "motor")
            orders = exc.get("orders", [1.0])
            for o in orders:
                o_val = float(o)
                # Compute modal participation of this order's force pattern
                f_unit = build_unit_force(result.model, design, order=o_val)
                # Participation across all non-rigid modes to find max
                part_all = [
                    abs(complex(result.Phi[:, k].T @ f_unit))
                    for k in non_rigid_indices
                ]
                max_part = max(part_all) if part_all else 0.0
                modal_part = abs(complex(phi_free.T @ f_unit))
                rel_part = modal_part / max_part if max_part > 1e-12 else 0.0

                if rel_part < 0.05:
                    continue  # Mode is not excited by this force pattern

                f_exc_min = o_val * (rpm_min / 60.0)
                f_exc_max = o_val * (rpm_max / 60.0)

                # Find closest excitation frequency
                if f_exc_min <= f_mode <= f_exc_max:
                    diff_ratio = 0.0
                    rpm_closest = 60.0 * f_mode / o_val
                    f_closest = f_mode
                elif f_mode < f_exc_min:
                    diff_ratio = (f_exc_min - f_mode) / f_mode
                    rpm_closest = rpm_min
                    f_closest = f_exc_min
                else:
                    diff_ratio = (f_mode - f_exc_max) / f_mode
                    rpm_closest = rpm_max
                    f_closest = f_exc_max

                if diff_ratio <= 0.15:
                    resonant_sources.append({
                        "cid": cid,
                        "order": o_val,
                        "rpm_closest": rpm_closest,
                        "f_closest": f_closest,
                        "diff_ratio": diff_ratio,
                    })

        if resonant_sources:
            # Sort by closest approach
            resonant_sources.sort(key=lambda s: s["diff_ratio"])
            best_source = resonant_sources[0]
            min_diff = best_source["diff_ratio"]

            # Unique excitation sources text
            unique_cids = []
            for s in resonant_sources:
                if s["cid"] not in unique_cids:
                    unique_cids.append(s["cid"])

            if len(unique_cids) == 1:
                sources_str = f"'{unique_cids[0]}'"
            elif len(unique_cids) == 2:
                sources_str = f"'{unique_cids[0]}' and '{unique_cids[1]}'"
            else:
                quoted_cids = [f"'{c}'" for c in unique_cids[:-1]]
                sources_str = f"{', '.join(quoted_cids)}, and '{unique_cids[-1]}'"

            o_val = best_source["order"]
            order_str = f"{int(o_val)}x" if o_val.is_integer() else f"{o_val:g}x"
            pct_str = f"{min_diff * 100.0:.1f}%"

            if min_diff <= RESONANCE_CROSSING_CRITICAL_BAND:
                if has_critical_consequence:
                    severity = "critical"
                    detail = (
                        f"{order_str} excitation from {sources_str} at {best_source['rpm_closest']:.0f} RPM ({best_source['f_closest']:.1f} Hz) "
                        f"is within {pct_str} of mode {m_idx + 1} ({f_mode:.1f} Hz)"
                    )
                else:
                    severity = "warning"
                    comp_parts = [
                        f"{sc['id']} {sc['peak_g']:.2f} g {basis.upper()} of {sc['limit_g']:.2f} g"
                        for sc in sensitive_comp_data
                    ]
                    comp_summary_str = ", ".join(comp_parts)
                    detail = (
                        f"Resonance remains at {best_source['rpm_closest']:.0f} RPM ({best_source['f_closest']:.1f} Hz) "
                        f"but no component exceeds its limit: {comp_summary_str}, stress {stress_ratio_val * 100.0:.1f}% of yield. "
                        f"Avoid dwelling at this speed."
                    )
            else:
                severity = "warning"
                detail = (
                    f"{order_str} excitation from {sources_str} at {best_source['rpm_closest']:.0f} RPM ({best_source['f_closest']:.1f} Hz) "
                    f"is within {pct_str} of mode {m_idx + 1} ({f_mode:.1f} Hz)"
                )

            # Find position of mode's largest amplitude mesh node
            phi_full = result.model.expand(phi_free)
            n_mesh = len(result.mesh.nodes)
            ux_mesh = phi_full[0 : 3 * n_mesh : 3]
            uy_mesh = phi_full[1 : 3 * n_mesh : 3]
            node_amps = np.hypot(ux_mesh, uy_mesh)
            max_node_idx = int(np.argmax(node_amps))
            best_node = result.mesh.nodes[max_node_idx]

            findings_raw.append(
                Finding(
                    id="tmp",
                    severity=severity,
                    type="resonance",
                    component=", ".join(unique_cids),
                    rpm=round(best_source["rpm_closest"], 1),
                    value=round(min_diff * 100.0, 2),
                    mode=m_idx + 1,
                    mode_f_hz=round(f_mode, 2),
                    position=PositionRef(member=best_node.member or "m1", s=round(best_node.s or 0.0, 3)),
                    detail=detail,
                )
            )

    # --- Rule b: Limit Exceeded ---
    for c in components:
        c_dict = c if isinstance(c, dict) else c.model_dump()
        cid = c_dict.get("id")
        limit_g = c_dict.get("limit_g")
        if limit_g is None or cid not in result.component_accel_g:
            continue

        acc_arr = result.component_accel_g[cid]
        peak_acc = float(np.max(acc_arr))
        peak_idx = int(np.argmax(acc_arr))
        peak_rpm = float(result.rpm[peak_idx])

        if peak_acc > 0.7 * float(limit_g):
            severity = "critical" if peak_acc > float(limit_g) else "warning"
            ratio_pct = (peak_acc / float(limit_g)) * 100.0
            margin_str = f"{peak_acc:.2f} g {basis.upper()} = {ratio_pct:.0f}% of limit"

            # Find nearest non-rigid mode
            f_peak = peak_rpm / 60.0
            if non_rigid_indices:
                diffs = [abs(float(result.modes_f_hz[k]) - f_peak) for k in non_rigid_indices]
                best_k = non_rigid_indices[int(np.argmin(diffs))]
                nearest_mode_idx = best_k + 1
                nearest_mode_f = float(result.modes_f_hz[best_k])
                freq_ratio = f_peak / nearest_mode_f if nearest_mode_f > 0.0 else 1.0
            else:
                nearest_mode_idx = 1
                nearest_mode_f = f_peak
                freq_ratio = 1.0

            detail = (
                f"Component '{cid}' peak acceleration {margin_str} (limit {float(limit_g):.2f} g {basis.upper()}) at {peak_rpm:.0f} RPM "
                f"(excitation is {freq_ratio:.2f}x of mode {nearest_mode_idx} at {nearest_mode_f:.1f} Hz)"
            )

            findings_raw.append(
                Finding(
                    id="tmp",
                    severity=severity,
                    type="limit_exceeded",
                    component=cid,
                    rpm=round(peak_rpm, 1),
                    value=round(peak_acc, 3),
                    limit=round(float(limit_g), 3),
                    mode=nearest_mode_idx,
                    mode_f_hz=round(nearest_mode_f, 2),
                    position=PositionRef(member=c_dict.get("member", "m1"), s=round(float(c_dict.get("s", 0.0)), 3)),
                    detail=detail,
                )
            )

    # --- Rule c: Stress ---
    stress_ratio = float(max_stress_dict.get("ratio", 0.0))
    if stress_ratio > 0.5:
        severity = "critical" if stress_ratio > 0.8 else "warning"
        s_val = float(max_stress_dict["value"])
        y_val = float(max_stress_dict["yield_mpa"])
        rpm_val = float(max_stress_dict["at_rpm"])
        mem_val = str(max_stress_dict["member"])

        detail = (
            f"Peak screening stress of {s_val:.1f} MPa at {rpm_val:.0f} RPM on member '{mem_val}' "
            f"exceeds {stress_ratio * 100.0:.0f}% of yield strength ({y_val:.1f} MPa)"
        )

        findings_raw.append(
            Finding(
                id="tmp",
                severity=severity,
                type="stress",
                rpm=round(rpm_val, 1),
                value=round(s_val, 2),
                limit=round(y_val, 2),
                position=PositionRef(member=mem_val, s=0.5),
                detail=detail,
            )
        )

    # --- Rule d: Mount Amplification (ONE grouped finding) ---
    # Lowest excitation frequency in operating range
    all_orders = []
    for _, exc in comp_excitations:
        all_orders.extend(exc.get("orders", [1.0]))
    min_order = min(all_orders) if all_orders else 1.0
    f_exc_min_global = min_order * (rpm_min / 60.0)

    amplified_comps = []
    for c in components:
        c_dict = c if isinstance(c, dict) else c.model_dump()
        cid = c_dict.get("id")
        mount = c_dict.get("mount")
        mass = float(c_dict.get("mass", 0.0))
        if not mount or mass <= 0.0:
            continue

        preset = mount.get("preset", "")
        kv = float(mount.get("kv", 1e5))
        # Skip purely rigid structural mounts (not intended as isolators)
        if preset == "rigid" or kv >= 1e7:
            continue
        # f_m = sqrt(k / m) / (2 * pi)
        f_mount = np.sqrt(kv / mass) / (2.0 * np.pi)
        boundary_f = np.sqrt(2.0) * f_mount

        if boundary_f > f_exc_min_global:
            amplified_comps.append({
                "cid": cid,
                "f_mount": f_mount,
                "boundary_f": boundary_f,
                "member": c_dict.get("member", "m1"),
                "s": float(c_dict.get("s", 0.0)),
            })

    if amplified_comps:
        # Group into ONE finding listing all affected components
        comp_summary_strs = [
            f"{ac['cid']} ({ac['f_mount']:.1f} Hz)"
            for ac in amplified_comps
        ]
        detail = (
            f"Mount frequencies for {', '.join(comp_summary_strs)} sit in vibration amplification zones (r < sqrt(2)) "
            f"within the operating range"
        )
        min_f = min(ac["f_mount"] for ac in amplified_comps)
        first_comp = amplified_comps[0]

        findings_raw.append(
            Finding(
                id="tmp",
                severity="warning",
                type="mount_amplification",
                component=", ".join(ac["cid"] for ac in amplified_comps),
                value=round(float(min_f), 1),
                limit=round(float(np.sqrt(2.0) * min_f), 1),
                position=PositionRef(member=first_comp["member"], s=round(first_comp["s"], 3)),
                detail=detail,
            )
        )

    # --- Rule e: Displacement Clearance ---
    if result.node_amp_mm is not None and result.node_amp_mm.size > 0:
        max_disp = float(np.max(result.node_amp_mm))
        if max_disp > CLEARANCE_MM:
            freq_idx, node_idx = np.unravel_index(np.argmax(result.node_amp_mm), result.node_amp_mm.shape)
            rpm_disp = float(result.rpm[freq_idx])
            crit_node = result.mesh.nodes[int(node_idx)]

            detail = (
                f"Peak structural deflection of {max_disp:.2f} mm at {rpm_disp:.0f} RPM "
                f"exceeds clearance limit of {CLEARANCE_MM:.1f} mm"
            )

            findings_raw.append(
                Finding(
                    id="tmp",
                    severity="warning",
                    type="displacement",
                    rpm=round(rpm_disp, 1),
                    value=round(max_disp, 2),
                    limit=CLEARANCE_MM,
                    position=PositionRef(member=crit_node.member or "m1", s=round(crit_node.s or 0.0, 3)),
                    detail=detail,
                )
            )

    # Sort findings: critical (0) > warning (1) > ok (2) > info (3), then by value descending
    sev_rank = {"critical": 0, "warning": 1, "ok": 2, "info": 3}
    findings_raw.sort(key=lambda f: (sev_rank.get(f.severity, 99), -(f.value or 0.0)))

    # Assign persistent finding IDs: f1, f2, ...
    for i, finding in enumerate(findings_raw):
        finding.id = f"f{i + 1}"

    return findings_raw


def build_response(
    design: Union[Dict[str, Any], Any],
    result: PipelineResult,
    basis: str = ACCEL_BASIS,
) -> Dict[str, Any]:
    """Construct and validate contract-compliant /analyze response from PipelineResult.

    Conforms strictly to 00_MASTER_CONTEXT.md section 6.2 and app.schemas.AnalyzeResponse.
    """
    if hasattr(design, "model_dump"):
        d = design.model_dump()
    elif isinstance(design, dict):
        d = design
    else:
        d = vars(design)

    # 1. Meta
    total_dof = result.model.K.shape[0]
    total_solve_ms = round(result.modal_setup_ms + result.sweep_ms, 2)
    meta = AnalysisMeta(
        n_dof=total_dof,
        n_modes_kept=result.r_modes_kept,
        solve_ms=total_solve_ms,
        method=result.method,
    )

    # 2. Mesh nodes
    mesh_nodes = [
        MeshNode(
            id=node.id,
            x=round(float(node.x), 4),
            y=round(float(node.y), 4),
            member=node.member,
            s=round(float(node.s), 4) if node.s is not None else None,
        )
        for node in result.mesh.nodes
    ]

    # 3. Modes (first ~8 non-rigid modes)
    modes: List[ModeInfo] = []
    mode_count = 0
    for i, f_hz in enumerate(result.modes_f_hz):
        if result.is_rigid_body[i]:
            continue
        mode_count += 1
        phi_free = result.Phi[:, i]
        phi_full = result.model.expand(phi_free)

        # Extract per-mesh-node displacements [ux, uy]
        n_mesh = len(result.mesh.nodes)
        ux_mesh = phi_full[0 : 3 * n_mesh : 3]
        uy_mesh = phi_full[1 : 3 * n_mesh : 3]
        shape_norm = np.max(np.abs(np.column_stack([ux_mesh, uy_mesh])))
        if shape_norm > 1e-12:
            ux_mesh = ux_mesh / shape_norm
            uy_mesh = uy_mesh / shape_norm

        shape_data = [
            [round(float(ux), 5), round(float(uy), 5)]
            for ux, uy in zip(ux_mesh, uy_mesh)
        ]
        label = result.mode_labels[i] if i < len(result.mode_labels) else f"Mode {i + 1}"

        modes.append(
            ModeInfo(
                i=mode_count,
                f_hz=round(float(f_hz), 2),
                shape=shape_data,
                note=label,
            )
        )
        if mode_count >= 8:
            break

    # 4. Sweep
    node_amp = (
        result.node_amp_mm
        if result.node_amp_mm is not None
        else np.zeros((len(result.rpm), len(result.mesh.nodes)), dtype=float)
    )
    sweep = SweepResult(
        rpm=[round(float(r), 1) for r in result.rpm],
        freq_hz=[round(float(f), 2) for f in result.freq_hz],
        node_amp_mm=[[round(float(val), 4) for val in row] for row in node_amp],
        component_accel_g={
            cid: [round(float(v), 4) for v in arr]
            for cid, arr in result.component_accel_g.items()
        },
        component_disp_mm={
            cid: [round(float(v), 4) for v in arr]
            for cid, arr in result.component_disp_mm.items()
        },
        transmitted_force_n={
            cid: [round(float(v), 3) for v in arr]
            for cid, arr in result.transmitted_force_n.items()
        },
    )

    # 5. Stress
    x_full_list = result.X_full_orders if result.X_full_orders is not None else [result.X_full]
    max_stress_dict = compute_max_stress(d, result.model, result.mesh, x_full_list, result.rpm)
    max_stress_mpa = MaxStress(**max_stress_dict)

    # 6. Campbell
    campbell = generate_campbell(result, d)

    # 7. Components summary
    components_summary: List[ComponentSummary] = []
    for c in d.get("components", []):
        c_dict = c if isinstance(c, dict) else c.model_dump()
        cid = c_dict.get("id")
        limit_g = c_dict.get("limit_g")
        if cid in result.component_accel_g:
            arr = result.component_accel_g[cid]
            peak_val = float(np.max(arr))
            peak_idx = int(np.argmax(arr))
            peak_rpm = float(result.rpm[peak_idx])
        else:
            peak_val = 0.0
            peak_rpm = float(result.rpm[0]) if len(result.rpm) > 0 else 0.0

        if limit_g is not None:
            l_val = float(limit_g)
            if peak_val > l_val:
                status = "critical"
            elif peak_val > 0.7 * l_val:
                status = "warning"
            else:
                status = "ok"
        else:
            status = "ok"

        components_summary.append(
            ComponentSummary(
                id=cid,
                peak_accel_g=round(peak_val, 3),
                at_rpm=round(peak_rpm, 1),
                limit_g=round(float(limit_g), 3) if limit_g is not None else None,
                status=status,
            )
        )

    # 8. Findings
    findings = generate_findings(d, result, max_stress_dict, basis=basis)

    response = AnalyzeResponse(
        meta=meta,
        mesh_nodes=mesh_nodes,
        modes=modes,
        sweep=sweep,
        campbell=campbell,
        components=components_summary,
        findings=findings,
        max_stress_mpa=max_stress_mpa,
    )

    return response.model_dump()
