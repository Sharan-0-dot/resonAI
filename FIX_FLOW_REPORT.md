# FIX-FLOW CHECK: Reproduction, Diagnosis, and Flow Verification Report

**Date:** 2026-10-24  
**Project:** ResonAI Solver & Frontend Integration  
**Scope:** Drone preset (`quad_drone_diagonal`), `/analyze`, `/optimize`, and candidate application flow.

---

## 1. Executive Summary & Root Cause Findings

During the Phase A diagnosis, we evaluated the drone triage pipeline across the four potential failure causes:
- **(a) APPLY-PATH BUG:** **Not real.** Changes in `changes[]` were already completely and identically reflected in `candidate[0].design`. The frontend `applyCandidate` in `useStore.ts` replaces the design in full, invalidates the client `/response_at` cache, resets old analysis state, and dispatches a fresh `/analyze` call.
- **(b) GRID MISMATCH:** **Minor effect (0.12%), now completely resolved.** Previously, `/analyze` evaluated 400 uniform points while optimizer verification evaluated 1,000 points. In Phase B1, mode-crossing RPM refinement points were added to `/analyze`'s frequency grid (union of uniform grid and crossing points), reducing any discrepancy between `/analyze` and `/optimize` verification to under 0.20% (0.00% on IMU, 0.20% on camera).
- **(c) LEGITIMATE FINDINGS:** **Real.** The downgraded `limit_exceeded: camera` (0.49 g RMS, 97% of 0.50 g limit) and `limit_exceeded: imu` (0.80 g RMS, 80% of 1.00 g limit) remain present as **warning** findings because both stay above the 70% threshold. The `mount_amplification` warning also remains physically valid because motor and camera mounts have isolation boundaries within the 1,000–12,000 RPM range.
- **(d) SEVERITY TOO COARSE:** **Primary root cause.** In `post.py`, any resonance crossing within 5% was unconditionally flagged as `critical`, even when all sensitive payloads were under their limits (camera 0.49 g / 0.50 g, IMU 0.80 g / 1.00 g), stress was at 0.8% of yield, and clearance was completely safe. Under Phase B2 consequence-aware resonance grading, resonances without harmful consequences are truthfully categorized as `warning`, eliminating false alarms while preserving full mechanical transparency.

---

## 2. Findings Tables (Post-Fix Verification)

Generated via `scripts/fix_flow_check.py` on the fixed codebase:

### Table 1: Findings BEFORE (Baseline Drone)

| ID | Severity | Type | Component / Mode | RPM | Value | Detail |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `f1` | **critical** | `limit_exceeded` | camera | 3192 | 3.74 g | Component 'camera' peak acceleration 3.74 g RMS = 748% of limit (limit 0.50 g RMS) at 3192 RPM (excitation is 1.00x of mode 4 at 53.1 Hz) |
| `f2` | **critical** | `limit_exceeded` | imu | 3192 | 2.39 g | Component 'imu' peak acceleration 2.39 g RMS = 239% of limit (limit 1.00 g RMS) at 3192 RPM (excitation is 1.00x of mode 4 at 53.1 Hz) |
| `f3` | **critical** | `resonance` | motor_left, motor_right | 3185 | 0.00% | 1x excitation from 'motor_left' and 'motor_right' at 3185 RPM (53.1 Hz) is within 0.0% of mode 4 (53.1 Hz) |
| `f4` | **critical** | `resonance` | motor_left, motor_right | 6067 | 0.00% | 1x excitation from 'motor_left' and 'motor_right' at 6067 RPM (101.1 Hz) is within 0.0% of mode 5 (101.1 Hz) |
| `f5` | **warning** | `mount_amplification` | motor_left, motor_right, camera, imu | — | 82.2 Hz | Mount frequencies for motor_left (214.6 Hz), motor_right (214.6 Hz), camera (82.2 Hz), imu (225.1 Hz) sit in vibration amplification zones (r < sqrt(2)) within operating range |

---

### Table 2: Findings AFTER (`/analyze` on `candidate[0].design`)

| ID | Severity | Type | Component / Mode | RPM | Value | Detail |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `f1` | **warning** | `mount_amplification` | motor_left, motor_right, camera, imu | — | 32.9 Hz | Mount frequencies for motor_left (214.6 Hz), motor_right (214.6 Hz), camera (32.9 Hz), imu (490.9 Hz) sit in vibration amplification zones (r < sqrt(2)) within operating range |
| `f2` | **warning** | `limit_exceeded` | imu | 3481 | 0.80 g | Component 'imu' peak acceleration 0.80 g RMS = 80% of limit (limit 1.00 g RMS) at 3481 RPM (excitation is 1.00x of mode 5 at 59.7 Hz) |
| `f3` | **warning** | `limit_exceeded` | camera | 3454 | 0.49 g | Component 'camera' peak acceleration 0.49 g RMS = 97% of limit (limit 0.50 g RMS) at 3454 RPM (excitation is 1.00x of mode 5 at 59.7 Hz) |
| `f4` | **warning** | `resonance` | mode 3 (`motor_left`, `motor_right`) | 2251 | 0.00% | Resonance remains at 2251 RPM (37.5 Hz) but no component exceeds its limit: camera 0.49 g RMS of 0.50 g, imu 0.80 g RMS of 1.00 g, stress 0.8% of yield. Avoid dwelling at this speed. |
| `f5` | **warning** | `resonance` | mode 5 (`motor_left`, `motor_right`) | 3583 | 0.00% | Resonance remains at 3583 RPM (59.7 Hz) but no component exceeds its limit: camera 0.49 g RMS of 0.50 g, imu 0.80 g RMS of 1.00 g, stress 0.8% of yield. Avoid dwelling at this speed. |

**Result:** **0 Critical findings** remaining on the optimized drone! All physical findings are faithfully retained as warnings with quantitative engineering context.

---

### Table 3: Findings Diff (Matching Key: `(type, component)` or `(type, mode)`)

| Status | Finding Key | Severity Shift | Detail / Explanation |
| :--- | :--- | :--- | :--- |
| **REMAINING** | `limit_exceeded: camera` | `critical` -> `warning` | Camera peak acceleration dropped from 3.74 g to 0.487 g (safely below 0.50 g limit). Degraded to warning because 0.487 g > 0.35 g (70% warning threshold). |
| **REMAINING** | `limit_exceeded: imu` | `critical` -> `warning` | IMU peak dropped from 2.39 g to 0.803 g (safely below 1.00 g limit). Degraded to warning because 0.803 g > 0.70 g (70% warning threshold). |
| **RESOLVED** | `resonance: mode 4` | `critical` -> None | Mode 4 was shifted by the mount stiffness change and is no longer resonant in that band. |
| **REMAINING** | `resonance: mode 5` | `critical` -> `warning` | Mode 5 crossing shifted from 6067 RPM (101.1 Hz) to 3583 RPM (59.7 Hz). Because camera/IMU are safe and stress is 0.8%, it is truthfully downgraded to warning. |
| **REMAINING** | `mount_amplification` | `warning` (unchanged) | Physical mount frequencies still have $\sqrt{2} f_m$ within the 1,000–12,000 RPM range. |
| **NEW** | `resonance: mode 3` | None -> `warning` | Softening camera mount lowered mode 3 to 37.5 Hz (2251 RPM), creating an in-band crossing. Because consequences are safe, it is truthfully classified as warning. |

---

## 3. Sensitive Component Metrics & Grid Alignment

| Component | Limit | Peak Before | Peak After (`/analyze`) | Peak After (Opt Verif) | Discrepancy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **camera** | 0.50 g RMS | 3.7410 g | 0.4870 g | 0.4880 g | **0.20%** |
| **imu** | 1.00 g RMS | 2.3940 g | 0.8030 g | 0.8030 g | **0.00%** |

- **`/analyze` Grid:** 409 points (400 uniform points unioned with 9 mode-crossing refinement points).
- **Optimizer Search Grid:** 200 points (coarse surrogate).
- **Optimizer Verification Grid:** 1,000 points (direct full solve).
- **Stress Ratio Before:** `0.0447` (26.85 MPa, 4.5% of yield).
- **Stress Ratio After:** `0.0083` (4.99 MPa, 0.8% of yield).
- **Camera Peak Discrepancy:** `0.20%` (far below the 2.0% threshold).
- **IMU Peak Discrepancy:** `0.00%`.

---

## 4. Phase B Fixes Implemented

1. **B1: RPM Grid Refinement (`backend/app/solver/pipeline.py`)**:
   - Added mode-crossing RPM refinement points ($\pm 0.2\%$ flanking bands) for all non-rigid modes across all harmonic orders.
   - Unioned with uniform grid, sorted, and deduplicated.
2. **B2: Consequence-Aware Resonance Severity (`backend/app/solver/post.py`)**:
   - Added named thresholds at top of `post.py`:
     - `RESONANCE_CROSSING_CRITICAL_BAND = 0.05`
     - `STRESS_RATIO_CRITICAL_THRESHOLD = 0.50`
     - `CLEARANCE_MM = 5.0`
   - Resonances within 5% are `critical` if any sensitive payload exceeds its limit, max stress ratio > 0.50, peak displacement > 5.0 mm, or no limits are defined.
   - Otherwise, they are graded `warning` with explicit component values and stress percentages in the detail string.
3. **B3: Limit Exceeded Detail Format (`backend/app/solver/post.py`)**:
   - Detail string explicitly reports percentage of limit and margin (e.g. `0.49 g RMS = 97% of limit`).
4. **B4: Optimizer Objective Margin Penalty (`backend/app/optimizer/optimize.py`)**:
   - Added `0.5 * max(0.0, worst_accel_ratio - 0.7)` term to differential evolution objective $J$.
   - Appends informative margin note when bounds prevent achieving a 30% margin.
5. **B5: Findings Diff Schema (`backend/app/schemas.py`, `optimize.py`, `optimize_response.json`, `contract.ts`)**:
   - Added additive fields to `CandidateBlock`: `findings_before_counts`, `findings_after_counts`, and `findings_diff` (`resolved`, `remaining`, `new`).
   - Added `before_severity` to `Finding`.
6. **B6: Frontend Diff & Before -> After Strip**:
   - `FindingsPanel.tsx`: Added compact `Before -> After` strip ("4 critical, 1 warning -> 0 critical, 5 warning"), renders RESOLVED items with green check and strikethrough, REMAINING items with severity transition pills, and NEW items with a "NEW" badge.
   - `ComparePanel.tsx`: Added matching Before -> After findings strip.
   - `useStore.ts`: In `applyCandidate`, invalidates old analysis state, clears `/response_at` cache, and updates design.

---

## 5. Verification Gate Results

### Backend Fast Test Suite (`pytest -m "not slow"`)
```
69 passed, 2 skipped, 11 deselected, 1 warning in 14.55s
```

### Backend Slow Test Suite (`pytest -m "slow"`)
```
11 passed, 71 deselected, 1 warning in 163.20s (2m 43s)
```

### Frontend Typecheck (`npx tsc --noEmit`)
```
Passed: 0 errors
```

### Frontend ESLint (`npm run lint`)
```
Passed: 0 errors, 0 warnings
```

### Contract Verification (`npm run check:contract`)
```
✓ Mock file: All 8 contract blocks strictly verified.
✓ Live /analyze endpoint: All 8 contract blocks strictly verified.
✓ Live /validate: Verified 22 analytical test cases.
Contract Check Passed: JSON contracts verified.
```

### Production Build (`npm run build`)
```
▲ Next.js 16.4.2 (Turbopack)
✓ Compiled successfully in 11.3s
✓ Prerendered static pages (4/4) in 1.6s
```
