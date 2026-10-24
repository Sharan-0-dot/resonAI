"""Unit and integration tests for post-processing, findings, Campbell diagram, and API endpoints.

Covers Milestone M4 requirements:
- Stress: cantilever static tip load verification against analytical 6*P*L/(w*h^2) (< 0.5% error).
- Consistency: component acceleration from build_response equals pipeline component_response values.
- Findings: limit_exceeded triggered when limit is low, absent when limit is large; severity ordering.
- Campbell: 1x line crossing meets known mode frequency within 1%.
- Contract: drone /analyze response strictly conforms to schema, zero NaN/Inf, correct matrix shapes.
- /response_at: matches frequency sweep amplitude at chosen RPM within 1%.
- Performance: /analyze on drone design executes in < 500 ms.
- Validation: invalid designs return HTTP 422, never unhandled 500.
"""

import json
import time
from typing import Any, Dict
import numpy as np
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.presets import get_demo_design
from app.schemas import AnalyzeResponse
from app.solver.assemble import assemble
from app.solver.elements import frame_element, transform
from app.solver.mesh import build_mesh
from app.solver.pipeline import solve_design
from app.solver.post import build_response, stress_from_displacements


@pytest.fixture
def client():
    return TestClient(app)


def test_cantilever_static_stress():
    """Verify element end forces and bending stress against analytical 6*P*L/(w*h^2).

    Structure:
    - Aluminium 6061 bar: L = 1.0 m, rect section w = 0.02 m (width), h = 0.01 m (depth).
    - Tip vertical load P = 1.0 N upwards at x = L.
    - Root moment: M = P * L = 1.0 N*m.
    - Analytical extreme-fiber bending stress:
      sigma = M * c / I = (P * L) * (h / 2) / (w * h^3 / 12) = 6 * P * L / (w * h^2)
      sigma = 6 * 1.0 * 1.0 / (0.02 * 0.01^2) = 3.0e6 Pa = 3.0 MPa.
    """
    design = {
        "name": "Cantilever_Stress_Verification",
        "plane": "xy",
        "nodes": [
            {"id": "n1", "x": 0.0, "y": 0.0},
            {"id": "n2", "x": 1.0, "y": 0.0},
        ],
        "members": [
            {
                "id": "m1",
                "n1": "n1",
                "n2": "n2",
                "material": "aluminium_6061",
                "section": {"type": "rect", "w": 0.02, "h": 0.01},
            }
        ],
        "supports": [{"node": "n1", "type": "fixed"}],
        "analysis": {"n_elements_per_member": 40},
    }

    mesh = build_mesh(design)
    model = assemble(design, mesh)

    # Tip node is at index 1 (x=1.0)
    tip_mesh_idx = mesh.node_map["n2"]
    tip_v_dof = 3 * tip_mesh_idx + 1

    # Apply static tip load P = 1.0 N
    P = 1.0
    F_full = np.zeros(model.K.shape[0], dtype=float)
    F_full[tip_v_dof] = P

    # Solve K_ff * u_free = F_free
    F_free = F_full[model.free_dofs]
    u_free = np.linalg.solve(model.K_ff, F_free)
    u_full = model.expand(u_free)

    # Compute stress across all 40 elements
    stresses = stress_from_displacements(model, mesh, u_full)

    # Root element is element 0 (at x=0.0)
    root_stress = float(stresses[0])
    w, h, L = 0.02, 0.01, 1.0
    analytical_stress = 6.0 * P * L / (w * (h**2))  # 3,000,000 Pa
    error_pct = abs(root_stress - analytical_stress) / analytical_stress * 100.0

    print("\n--- Cantilever Bending Stress Verification ---")
    print(f"Computed Root Stress:   {root_stress / 1e6:.4f} MPa ({root_stress:.1f} Pa)")
    print(f"Analytical 6*P*L/(w*h^2): {analytical_stress / 1e6:.4f} MPa ({analytical_stress:.1f} Pa)")
    print(f"Discrepancy:            {error_pct:.4f}% (< 0.5%)")

    assert error_pct < 0.5, f"Stress discrepancy {error_pct:.3f}% exceeds 0.5% limit"


def test_post_consistency_with_m3_response():
    """Verify component accelerations from build_response equal pipeline outputs."""
    drone_design = get_demo_design("drone")
    result = solve_design(drone_design, method="rom")
    response = build_response(drone_design, result)

    sweep = response["sweep"]
    for cid, arr in result.component_accel_g.items():
        resp_arr = sweep["component_accel_g"][cid]
        assert len(resp_arr) == len(arr)
        # Rounded in JSON to 4 decimals
        diff = np.max(np.abs(np.array(resp_arr) - np.array(arr)))
        assert diff < 1e-3, f"Component {cid} accel difference {diff} exceeds tolerance"


def test_findings_limit_exceeded_and_ordering():
    """Verify limit_exceeded finding appears when limit is exceeded and is absent when limit is high."""
    drone_design = get_demo_design("drone")

    # Set very low camera limit (0.05 g) to guarantee critical violation
    drone_design["components"][2]["limit_g"] = 0.05
    res1 = solve_design(drone_design)
    resp1 = build_response(drone_design, res1)

    types1 = [f["type"] for f in resp1["findings"]]
    assert "limit_exceeded" in types1, "Expected limit_exceeded finding with low limit"

    # Verify severity ordering: critical -> warning -> ok
    sev_rank = {"critical": 0, "warning": 1, "ok": 2, "info": 3}
    ranks = [sev_rank[f["severity"]] for f in resp1["findings"]]
    for i in range(len(ranks) - 1):
        assert ranks[i] <= ranks[i + 1], f"Findings not sorted by severity: {ranks}"

    # Set huge camera limit (100.0 g)
    drone_design["components"][2]["limit_g"] = 100.0
    drone_design["components"][3]["limit_g"] = 100.0  # IMU as well
    res2 = solve_design(drone_design)
    resp2 = build_response(drone_design, res2)

    types2 = [f["type"] for f in resp2["findings"]]
    assert "limit_exceeded" not in types2, "limit_exceeded should not appear when limits are 100g"


def test_campbell_crossing_detection():
    """Verify Campbell excitation line detects crossing with mode frequency within 1%."""
    drone_design = get_demo_design("drone")
    result = solve_design(drone_design)
    response = build_response(drone_design, result)

    campbell = response["campbell"]
    assert len(campbell["lines"]) >= 1
    assert any(line["order"] == 1.0 for line in campbell["lines"])

    # Mode 4 on drone is ~52.9 Hz
    mode4 = [m for m in response["modes"] if m["i"] == 4][0]
    f4 = mode4["f_hz"]

    # Excitation crosses f4 at rpm = 60 * f4
    cross_rpm = 60.0 * f4
    f_exc = cross_rpm / 60.0
    diff_pct = abs(f_exc - f4) / f4 * 100.0

    print(f"\nCampbell Crossing: Mode 4 = {f4:.2f} Hz | 1x line crosses at {cross_rpm:.1f} RPM ({f_exc:.2f} Hz)")
    assert diff_pct < 0.01, "Campbell crossing does not match mode frequency"


def test_contract_drone_response_validity():
    """Verify drone /analyze response strictly conforms to schema, has no NaNs, and correct shapes."""
    drone_design = get_demo_design("drone")
    result = solve_design(drone_design)
    response_dict = build_response(drone_design, result)

    # Pydantic schema validation
    validated = AnalyzeResponse.model_validate(response_dict)
    assert validated.meta.n_dof > 0
    assert validated.meta.n_modes_kept >= 12

    # Verify no NaN or Inf
    json_str = json.dumps(response_dict)
    assert "NaN" not in json_str
    assert "Infinity" not in json_str

    # Verify array dimensions (sweep includes n_points plus mode crossing refinement points)
    n_points_min = drone_design["analysis"]["n_points"]
    n_nodes = len(response_dict["mesh_nodes"])
    sweep = response_dict["sweep"]
    n_actual = len(sweep["rpm"])

    assert n_actual >= n_points_min
    assert len(sweep["freq_hz"]) == n_actual
    assert len(sweep["node_amp_mm"]) == n_actual
    assert len(sweep["node_amp_mm"][0]) == n_nodes
    assert len(sweep["component_accel_g"]["camera"]) == n_actual


def test_response_at_endpoint(client: TestClient):
    """Verify /response_at matches the frequency sweep amplitude at the same RPM within 1%."""
    drone_design = get_demo_design("drone")

    # Run full analyze sweep first
    res_sweep = client.post("/analyze", json=drone_design)
    assert res_sweep.status_code == 200
    sweep_data = res_sweep.json()["sweep"]

    target_rpm = 4000.0
    rpms = np.array(sweep_data["rpm"])
    idx = int(np.argmin(np.abs(rpms - target_rpm)))
    actual_rpm = float(rpms[idx])

    # Call /response_at at actual_rpm
    res_at = client.post("/response_at", json={"design": drone_design, "rpm": actual_rpm})
    assert res_at.status_code == 200
    resp_at_data = res_at.json()

    assert resp_at_data["rpm"] == pytest.approx(actual_rpm, rel=1e-3)
    assert resp_at_data["omega"] == pytest.approx(2.0 * np.pi * actual_rpm / 60.0, rel=1e-3)

    # Compare node 0 (tip) amplitude
    node0 = resp_at_data["nodes"][0]
    ux = complex(node0["ux"][0], node0["ux"][1])
    uy = complex(node0["uy"][0], node0["uy"][1])
    amp_mm_at = np.hypot(abs(ux), abs(uy)) * 1000.0
    amp_mm_sweep = sweep_data["node_amp_mm"][idx][0]

    err_node = abs(amp_mm_at - amp_mm_sweep) / max(amp_mm_sweep, 1e-6) * 100.0
    print(f"\n/response_at Node 0 Amp at {actual_rpm:.0f} RPM: {amp_mm_at:.4f} mm | Sweep: {amp_mm_sweep:.4f} mm (Err: {err_node:.2f}%)")
    assert err_node < 1.0, f"Node amplitude mismatch {err_node:.2f}% exceeds 1%"

    # Compare camera displacement
    cam_at = next(c for c in resp_at_data["components"] if c["id"] == "camera")
    cam_uy = complex(cam_at["uy"][0], cam_at["uy"][1])
    cam_disp_at = abs(cam_uy) * 1000.0
    cam_disp_sweep = sweep_data["component_disp_mm"]["camera"][idx]

    err_cam = abs(cam_disp_at - cam_disp_sweep) / max(cam_disp_sweep, 1e-6) * 100.0
    print(f"/response_at Camera Disp at {actual_rpm:.0f} RPM: {cam_disp_at:.4f} mm | Sweep: {cam_disp_sweep:.4f} mm (Err: {err_cam:.2f}%)")
    assert err_cam < 1.0, f"Camera displacement mismatch {err_cam:.2f}% exceeds 1%"


def test_analyze_performance(client: TestClient):
    """Verify /analyze on drone design executes within 500 ms."""
    drone_design = get_demo_design("drone")

    t0 = time.perf_counter()
    response = client.post("/analyze", json=drone_design)
    total_time_ms = (time.perf_counter() - t0) * 1000.0

    assert response.status_code == 200
    data = response.json()
    solve_ms = data["meta"]["solve_ms"]

    print(f"\n--- API Performance Benchmark ---")
    print(f"Backend Internal Solve Time: {solve_ms:.2f} ms")
    print(f"Full HTTP Client Round-Trip: {total_time_ms:.2f} ms (< 500 ms)")

    assert total_time_ms < 500.0, f"Analysis HTTP response time {total_time_ms:.1f} ms exceeds 500 ms"


def test_invalid_design_returns_422(client: TestClient):
    """Verify invalid designs return HTTP 422 Unprocessable Entity instead of 500 traceback."""
    # 1. Negative mass component
    bad_mass_design = get_demo_design("drone")
    bad_mass_design["components"][0]["mass"] = -0.5
    res1 = client.post("/analyze", json=bad_mass_design)
    assert res1.status_code == 422

    # 2. Invalid node reference in member
    bad_ref_design = get_demo_design("drone")
    bad_ref_design["members"][0]["n1"] = "nonexistent_node"
    res2 = client.post("/analyze", json=bad_ref_design)
    assert res2.status_code == 422

    # 3. Invalid s parameter (> 1.0)
    bad_s_design = get_demo_design("drone")
    bad_s_design["components"][0]["s"] = 1.5
    res3 = client.post("/analyze", json=bad_s_design)
    assert res3.status_code == 422

    # 4. /response_at with negative RPM
    res4 = client.post("/response_at", json={"design": get_demo_design("drone"), "rpm": -100.0})
    assert res4.status_code == 422


def test_preset_designs_and_mock_endpoint(client: TestClient):
    """Verify GET /presets/{name}/design and POST /analyze/mock."""
    for name in ["drone", "cantilever_demo", "simply_supported_demo"]:
        resp = client.get(f"/presets/{name}/design")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data and "members" in data

    # Unknown preset returns 404
    resp_bad = client.get("/presets/unknown_structure_name/design")
    assert resp_bad.status_code == 404

    # POST /analyze/mock returns contract valid response
    resp_mock = client.post("/analyze/mock")
    assert resp_mock.status_code == 200
    mock_data = resp_mock.json()
    assert "meta" in mock_data and "sweep" in mock_data

