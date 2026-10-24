"""End-to-end vibration solver pipeline: mesh -> assembly -> damping -> modal -> harmonic sweep.

Conventions:
- All units SI.
- Modal setup time and harmonic sweep time recorded in milliseconds.
"""

from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional, Union
import numpy as np

from app.solver.assemble import Model, assemble
from app.solver.damping import build_damping
from app.solver.harmonic import (
    ACCEL_BASIS,
    build_force,
    combine_orders,
    component_response,
    expand_to_full,
    full_solve,
    rom_basis,
    rom_solve,
    transmitted_force,
)
from app.solver.mesh import Mesh, build_mesh
from app.solver.modal import (
    axial_fraction,
    component_energy_fraction,
    mode_labels,
    modal_analysis,
)


@dataclass
class PipelineResult:
    model: Model
    mesh: Mesh
    modes_f_hz: np.ndarray
    Phi: np.ndarray
    is_rigid_body: np.ndarray
    mode_labels: List[str]
    r_modes_kept: int
    rpm: np.ndarray
    freq_hz: np.ndarray
    X_full: np.ndarray  # shape (n_freq, n_total)
    component_accel_g: Dict[str, np.ndarray]
    component_disp_mm: Dict[str, np.ndarray]
    transmitted_force_n: Dict[str, np.ndarray]
    modal_setup_ms: float
    sweep_ms: float
    method: str
    node_amp_mm: Optional[np.ndarray] = None  # shape (n_rpm, n_mesh_nodes) in mm
    X_full_orders: Optional[List[np.ndarray]] = None
    orders: Optional[List[float]] = None


def solve_design(
    design: Union[Dict[str, Any], Any],
    rpm_array: Optional[Union[List[float], np.ndarray]] = None,
    method: str = "rom",
    basis: str = ACCEL_BASIS,
) -> PipelineResult:
    """Execute complete vibration analysis pipeline for a structure.

    Args:
        design: Design dict or DesignModel instance.
        rpm_array: Optional explicit array of RPM points. Defaults to analysis config.
        method: 'rom' (reduced order model) or 'full' (direct solve).
        basis: 'rms' (default) or 'peak' for reported component accelerations.
    """
    if hasattr(design, "model_dump"):
        d = design.model_dump()
    elif isinstance(design, dict):
        d = design
    else:
        d = vars(design)

    t_setup_0 = time.perf_counter()

    # 1. Mesh discretization
    mesh = build_mesh(d)

    # 2. Assembly of stiffness, mass, and mount dashpot matrices
    model = assemble(d, mesh)

    # 3. Modal analysis
    f_hz, Phi_ff, is_rb = modal_analysis(model.K_ff, model.M_ff)

    # Identify axial and component DOFs in free space for classification
    axial_free_idx = [
        idx for idx, dof in enumerate(model.free_dofs) if dof < mesh.n_dofs and dof % 3 == 0
    ]
    comp_free_idx = [
        idx for idx, dof in enumerate(model.free_dofs) if dof >= mesh.n_dofs
    ]
    labels = mode_labels(f_hz, Phi_ff, model.M_ff, is_rb, axial_free_idx, comp_free_idx)

    # 4. Determine frequencies for Rayleigh damping
    analysis_cfg = d.get("analysis", {})
    if hasattr(analysis_cfg, "model_dump"):
        analysis_cfg = analysis_cfg.model_dump()

    zeta = float(analysis_cfg.get("beam_zeta", 0.02))
    rpm_min = float(analysis_cfg.get("rpm_min", 500.0))
    rpm_max = float(analysis_cfg.get("rpm_max", 12000.0))
    n_points = int(analysis_cfg.get("n_points", 400))

    # Find first non-rigid mode
    non_rigid_freqs = f_hz[~is_rb]
    f1 = float(non_rigid_freqs[0]) if len(non_rigid_freqs) > 0 else 10.0

    # Collect harmonic orders
    components = d.get("components", [])
    orders_set = set()
    for comp in components:
        exc = comp.get("excitation") if isinstance(comp, dict) else getattr(comp, "excitation", None)
        if exc:
            if hasattr(exc, "model_dump"):
                exc = exc.model_dump()
            orders = exc.get("orders", [1.0])
            for o in orders:
                orders_set.add(float(o))

    if not orders_set:
        orders_set.add(1.0)
    sorted_orders = sorted(list(orders_set))

    f_exc_max = max(sorted_orders) * (rpm_max / 60.0)
    f2 = max(1.5 * f_exc_max, 3.0 * f1)

    # 5. Build damping matrix
    C_ff = build_damping(model, zeta=zeta, f1_hz=f1, f2_hz=f2)

    # 6. ROM basis (default r_min=12)
    Phi_r, r_chosen = rom_basis(Phi_ff, f_hz, f_exc_max=f_exc_max, r_min=12, r_max=20)
    modal_setup_ms = (time.perf_counter() - t_setup_0) * 1000.0

    # 7. Sweep frequencies
    if rpm_array is None:
        rpms_uniform = np.linspace(rpm_min, rpm_max, n_points)
        # B1: Mode-crossing RPM refinement points (union of uniform grid and crossing points)
        refinement_points: List[float] = []
        for fm in non_rigid_freqs:
            if fm <= 0.1:
                continue
            for o in sorted_orders:
                if o > 0:
                    rpm_c = 60.0 * float(fm) / float(o)
                    if rpm_min <= rpm_c <= rpm_max:
                        refinement_points.extend([
                            rpm_c * 0.998,
                            rpm_c,
                            rpm_c * 1.002,
                        ])
        if refinement_points:
            rpms = np.sort(np.unique(np.concatenate([rpms_uniform, np.array(refinement_points, dtype=float)])))
        else:
            rpms = rpms_uniform
    else:
        rpms = np.asarray(rpm_array, dtype=float)

    freqs_hz = rpms / 60.0

    # 8. Harmonic sweep across orders
    t_sweep_0 = time.perf_counter()

    comp_accels_by_order: Dict[str, List[np.ndarray]] = {c["id"]: [] for c in components}
    comp_disps_by_order: Dict[str, List[np.ndarray]] = {c["id"]: [] for c in components}
    trans_force_by_order: Dict[str, List[np.ndarray]] = {c["id"]: [] for c in components}
    X_full_orders: List[np.ndarray] = []

    for order in sorted_orders:
        w_order = 2.0 * np.pi * order * (rpms / 60.0)

        # Precompute vectorized force matrix across all frequencies in one operation
        from app.solver.harmonic import build_force_batch
        F_batch = build_force_batch(model, d, rpms=rpms, order=order)

        if method == "rom":
            X_free = rom_solve(model.K_ff, C_ff, model.M_ff, F_batch, w_order, Phi_r)
        else:
            X_free = full_solve(model.K_ff, C_ff, model.M_ff, F_batch, w_order)

        X_full = expand_to_full(model, X_free)
        X_full_orders.append(X_full)

        # Extract per-component responses for this order
        for comp in components:
            cid = comp["id"]
            d_amp_m, a_amp_g = component_response(model, X_full, cid, w_order, basis=basis)
            comp_accels_by_order[cid].append(a_amp_g)
            comp_disps_by_order[cid].append(d_amp_m * 1000.0)  # mm

            # Transmitted force
            m_c = float(comp["mass"])
            mount_data = comp.get("mount") or {}
            kv = float(mount_data.get("kv", 1e5))
            zeta_m = float(mount_data.get("zeta", 0.08))
            f_trans = transmitted_force(model, X_full, cid, w_order, kv, zeta_m, m_c)
            trans_force_by_order[cid].append(f_trans)

    # Combine maximum amplitude over orders
    component_accel_g = {cid: combine_orders(arrs) for cid, arrs in comp_accels_by_order.items()}
    component_disp_mm = {cid: combine_orders(arrs) for cid, arrs in comp_disps_by_order.items()}
    transmitted_force_n = {cid: combine_orders(arrs) for cid, arrs in trans_force_by_order.items()}

    # Compute node amplitudes across mesh nodes (in mm)
    n_mesh_nodes = len(mesh.nodes)
    node_amps_by_order: List[np.ndarray] = []
    for xf in X_full_orders:
        ux = xf[:, 0 : 3 * n_mesh_nodes : 3]
        uy = xf[:, 1 : 3 * n_mesh_nodes : 3]
        amp_mm = np.hypot(np.abs(ux), np.abs(uy)) * 1000.0
        node_amps_by_order.append(amp_mm)

    node_amp_mm = (
        np.maximum.reduce(node_amps_by_order)
        if node_amps_by_order
        else np.zeros((len(rpms), n_mesh_nodes), dtype=float)
    )

    # Primary X_full is order 1 (or max)
    X_primary = X_full_orders[0] if X_full_orders else np.zeros((len(rpms), model.K.shape[0]), dtype=complex)

    sweep_ms = (time.perf_counter() - t_sweep_0) * 1000.0

    return PipelineResult(
        model=model,
        mesh=mesh,
        modes_f_hz=f_hz,
        Phi=Phi_ff,
        is_rigid_body=is_rb,
        mode_labels=labels,
        r_modes_kept=r_chosen,
        rpm=rpms,
        freq_hz=freqs_hz,
        X_full=X_primary,
        component_accel_g=component_accel_g,
        component_disp_mm=component_disp_mm,
        transmitted_force_n=transmitted_force_n,
        modal_setup_ms=modal_setup_ms,
        sweep_ms=sweep_ms,
        method=method,
        node_amp_mm=node_amp_mm,
        X_full_orders=X_full_orders,
        orders=sorted_orders,
    )
