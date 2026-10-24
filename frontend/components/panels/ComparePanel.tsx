"use client";

import { useState } from "react";
import { useStore } from "@/lib/store/useStore";
import { analyze } from "@/lib/api/client";
import type { MetricsBlock } from "@/lib/types/contract";
import {
  ArrowRight,
  Check,
  Activity,
  TrendingDown,
  Layers,
  Loader2,
} from "lucide-react";
import clsx from "clsx";

export default function ComparePanel() {
  const optimizeResult = useStore((s) => s.optimizeResult);
  const activeCandidate = useStore((s) => s.activeCandidate);
  const setActiveCandidate = useStore((s) => s.setActiveCandidate);
  const applyCandidate = useStore((s) => s.applyCandidate);
  const setAnalysis = useStore((s) => s.setAnalysis);
  const setUi = useStore((s) => s.setUi);
  const showToast = useStore((s) => s.showToast);

  const [applying, setApplying] = useState(false);

  if (!optimizeResult || optimizeResult.candidates.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted p-3">
        Run Auto-fix in the top bar to generate optimized candidates.
      </div>
    );
  }

  const cand = activeCandidate ?? optimizeResult.candidates[0];

  /** Worst peak acceleration among vibration-sensitive parts (those with a limit). */
  const getSensitivePeak = (m?: MetricsBlock): number => {
    if (!m) return 0.5;
    if (m.components) {
      const all = Object.values(m.components);
      const limited = all.filter((c) => c?.limit_g != null);
      const pool = limited.length > 0 ? limited : all;
      if (pool.length > 0) {
        return Math.max(...pool.map((c) => c?.peak_accel_g ?? 0));
      }
    }
    if (typeof m.camera_peak_g === "number") return m.camera_peak_g;
    return 0.5;
  };

  const getStressRatio = (m?: MetricsBlock): number => {
    if (!m) return 0.15;
    if (typeof m.max_stress_ratio === "number") return m.max_stress_ratio;
    if (typeof m.stress_ratio === "number") return m.stress_ratio;
    return 0.15;
  };

  const camBefore = getSensitivePeak(cand.metrics_before);
  const camAfter = getSensitivePeak(cand.metrics_after);
  const reduction =
    camBefore > 0 ? ((1 - camAfter / camBefore) * 100).toFixed(1) : "0.0";
  const stressBefore = getStressRatio(cand.metrics_before);
  const stressAfter = getStressRatio(cand.metrics_after);

  /**
   * Apply the candidate to the live design, leave compare mode, and re-run the
   * analysis on the applied design so Findings never sit empty.
   */
  const handleApplyFix = async () => {
    if (applying) return;
    setApplying(true);

    // Capture the exact design being applied before the store changes.
    const appliedDesign = structuredClone(cand.design);

    applyCandidate(cand);
    setUi({ viewMode: "single", isAnalyzing: true, error: null });

    try {
      const result = await analyze(appliedDesign);
      setAnalysis(result);
      showToast("Fix applied. Findings updated.", "success");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Analysis failed";
      setUi({ error: msg });
      showToast(`Fix applied, but re-analysis failed: ${msg}`, "warning");
    } finally {
      setUi({ isAnalyzing: false });
      setApplying(false);
    }
  };

  return (
    <div className="flex flex-col gap-2 p-3 text-xs bg-surface border-t border-line">
      {/* Top Row: Candidates Selection + Solve Time */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-xs text-ink uppercase tracking-wider text-[11px]">
            Candidate:
          </span>
          <div className="flex gap-1.5">
            {optimizeResult.candidates.map((c) => (
              <button
                key={c.rank}
                type="button"
                className={clsx(
                  "rounded px-2.5 py-1 text-xs font-medium transition-colors font-mono",
                  cand.rank === c.rank
                    ? "bg-accent text-white font-semibold shadow-2xs"
                    : "bg-panel text-ink-2 hover:bg-line/40 border border-line"
                )}
                onClick={() => setActiveCandidate(c)}
              >
                #{c.rank} (−{c.reduction_pct.toFixed(0)}% g RMS)
              </button>
            ))}
          </div>
        </div>

        <span className="font-mono text-[11px] text-muted">
          Solver converged in {optimizeResult.solve_ms.toFixed(0)} ms
        </span>
      </div>

      {/* Slim Summary Strip: Metrics & Changes & Action Buttons */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-panel/40 px-3 py-2">
        {/* Metrics comparison */}
        <div className="flex items-center gap-6">
          {/* Sensitive-part acceleration */}
          <div className="flex items-center gap-2 font-mono">
            <div className="flex items-center gap-1 text-muted text-[11px]">
              <Activity size={13} className="text-accent" />
              <span>Worst Payload Accel:</span>
            </div>
            <span className="text-danger font-medium line-through">
              {camBefore.toFixed(2)} g RMS
            </span>
            <ArrowRight size={12} className="text-muted" />
            <span className="text-ok font-bold">
              {camAfter.toFixed(2)} g RMS
            </span>
            <span className="rounded bg-ok-soft px-1.5 py-0.2 text-[10px] font-bold text-ok font-mono">
              −{reduction}%
            </span>
          </div>

          {/* Stress Ratio */}
          <div className="hidden sm:flex items-center gap-2 font-mono">
            <div className="flex items-center gap-1 text-muted text-[11px]">
              <TrendingDown size={13} className="text-accent" />
              <span>Max Stress:</span>
            </div>
            <span className="text-muted font-medium">
              {(stressBefore * 100).toFixed(0)}%
            </span>
            <ArrowRight size={12} className="text-muted" />
            <span className="text-ink font-semibold">
              {(stressAfter * 100).toFixed(0)}%
            </span>
          </div>

          {/* Before -> After findings strip */}
          {cand.findings_before_counts && cand.findings_after_counts && (
            <div className="hidden md:flex items-center gap-1.5 font-mono text-[11px] bg-surface px-2 py-0.5 rounded border border-line">
              <span className="text-danger font-medium">
                {cand.findings_before_counts.critical ?? 0} critical, {cand.findings_before_counts.warning ?? 0} warning
              </span>
              <ArrowRight size={11} className="text-muted" />
              <span className="text-ok font-semibold">
                {cand.findings_after_counts.critical ?? 0} critical, {cand.findings_after_counts.warning ?? 0} warning
              </span>
            </div>
          )}
        </div>

        {/* Changes Made List */}
        <div className="hidden lg:flex items-center gap-1.5 flex-1 max-w-sm truncate">
          <Layers size={13} className="text-muted shrink-0" />
          <span className="text-[11px] text-muted shrink-0">Tuned:</span>
          <div className="flex items-center gap-1 truncate">
            {cand.changes.map((ch, i) => (
              <span
                key={i}
                className="rounded bg-surface px-1.5 py-0.5 font-mono text-[10px] text-ink border border-line truncate"
                title={ch.label}
              >
                {ch.label}
              </span>
            ))}
          </div>
        </div>

        {/* Buttons */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            className="btn btn-outline h-7 px-2.5 text-xs text-ink-2"
            onClick={() => setUi({ viewMode: "single" })}
            disabled={applying}
          >
            Exit Compare
          </button>
          <button
            type="button"
            className="btn btn-primary h-7 px-3 text-xs shadow-xs"
            onClick={handleApplyFix}
            disabled={applying}
          >
            {applying ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                <span>Applying...</span>
              </>
            ) : (
              <>
                <Check size={13} />
                <span>Apply Fix</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}