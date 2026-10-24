/**
 * ResonAI shared data contract (master §6)
 * Units: SI (m, kg, N, Pa, s). Frequencies in Hz, rotation in RPM.
 */

// ─── Materials & Sections ────────────────────────────────────────────────────

export type MaterialKey =
  | "steel"
  | "aluminium_6061"
  | "titanium"
  | "cfrp"
  | "abs_plastic";

export type SectionType = "rect" | "circ" | "tube" | "box";

export interface RectSection {
  type: "rect";
  w: number; // m
  h: number; // m (in-plane bending depth)
}

export interface CircSection {
  type: "circ";
  d: number;
}

export interface TubeSection {
  type: "tube";
  d: number;
  t: number;
}

export interface BoxSection {
  type: "box";
  w: number;
  h: number;
  t: number;
}

export type Section = RectSection | CircSection | TubeSection | BoxSection;

// ─── Design (frontend → backend) ─────────────────────────────────────────────

export interface Node {
  id: string;
  x: number;
  y: number;
  ui?: Record<string, unknown>;
}

export interface Member {
  id: string;
  n1: string;
  n2: string;
  material: MaterialKey;
  section: Section;
  ui?: Record<string, unknown>;
}

export type SupportType = "fixed" | "pinned" | "roller" | "spring" | "free";

export interface Support {
  node: string;
  type: SupportType;
  kx?: number;
  ky?: number;
  kr?: number;
}

export type ComponentKind =
  | "motor"
  | "fan"
  | "pump"
  | "compressor"
  | "camera"
  | "gyro"
  | "fc"
  | "battery"
  | "generic";

export type MountPreset =
  | "rubber_soft"
  | "rubber_medium"
  | "silicone_gel"
  | "foam"
  | "steel_spring"
  | "wire_rope"
  | "rigid"
  | "custom";

export interface Mount {
  preset: MountPreset;
  kv: number; // N/m
  zeta: number;
}

export interface Excitation {
  type: "unbalance";
  rpm_min: number;
  rpm_max: number;
  me: number; // kg·m
  orders: number[];
  blades?: number;
}

export interface Component {
  id: string;
  kind: ComponentKind;
  member: string;
  s: number; // 0..1 along member
  mass: number; // kg
  mount: Mount;
  excitation?: Excitation;
  limit_g?: number;
  ui?: Record<string, unknown>;
}

export interface AnalysisSettings {
  rpm_min: number;
  rpm_max: number;
  n_points: number;
  beam_zeta: number;
  n_elements_per_member: number;
}

export interface Design {
  name: string;
  plane: "xy";
  nodes: Node[];
  members: Member[];
  supports: Support[];
  components: Component[];
  analysis: AnalysisSettings;
  ui?: Record<string, unknown>;
}

// ─── Analysis response (backend → frontend) ──────────────────────────────────

export type ComponentStatus = "ok" | "warning" | "critical";

export interface ModeShape {
  i: number;
  f_hz: number;
  shape: number[][]; // [[ux, uy], ...] per mesh node
  note?: string;
}

export interface SweepData {
  rpm: number[];
  freq_hz: number[];
  node_amp_mm: number[][]; // [n_rpm_coarse][n_mesh_nodes]
  component_accel_g: Record<string, number[]>;
  component_disp_mm: Record<string, number[]>;
  transmitted_force_n: Record<string, number[]>;
}

export interface CampbellData {
  mode_freqs_hz: number[];
  lines: { label: string; order: number }[];
}

export interface ComponentResult {
  id: string;
  peak_accel_g: number;
  at_rpm: number;
  limit_g?: number;
  status: ComponentStatus;
}

export type FindingSeverity = "ok" | "warning" | "critical" | "info";
export type FindingType =
  | "resonance"
  | "limit_exceeded"
  | "stress"
  | "mount_amplification"
  | "displacement";

export interface Finding {
  id: string;
  severity: FindingSeverity;
  before_severity?: FindingSeverity;
  type: FindingType;
  component?: string;
  rpm?: number;
  value?: number;
  limit?: number;
  mode?: number;
  mode_f_hz?: number;
  position?: { member: string; s: number };
  detail: string;
}

export interface AnalysisResult {
  meta: {
    n_dof: number;
    n_modes_kept: number;
    solve_ms: number;
    method: "rom" | "full";
  };
  mesh_nodes: {
    id: string;
    x: number;
    y: number;
    member?: string;
    s?: number;
  }[];
  modes: ModeShape[];
  sweep: SweepData;
  campbell: CampbellData;
  components: ComponentResult[];
  findings: Finding[];
  max_stress_mpa?: {
    value: number;
    member: string;
    at_rpm: number;
    yield_mpa: number;
    ratio: number;
  };
}

// ─── /response_at ────────────────────────────────────────────────────────────

export interface ResponseAtNode {
  id: string;
  ux: [number, number]; // [re, im]
  uy: [number, number];
  rot: [number, number];
}

export interface ResponseAtComponent {
  id: string;
  uy: [number, number];
}

export interface ResponseAt {
  rpm: number;
  omega: number;
  nodes: ResponseAtNode[];
  components: ResponseAtComponent[];
}

// ─── /optimize ───────────────────────────────────────────────────────────────

export interface OptimizeChange {
  path: string;
  from: number | string;
  to: number | string;
  label: string;
}

export interface ComponentMetrics {
  peak_accel_g: number;
  at_rpm: number;
  limit_g?: number;
  status: string;
}

export interface MetricsBlock {
  components?: Record<string, ComponentMetrics>;
  max_stress_ratio?: number;
  n_resonance_findings?: number;
  robust_worst_case?: number;
  rom_vs_full_discrepancy_pct?: number;
  mass_added_kg?: number;
  camera_peak_g?: number;
  stress_ratio?: number;
  mass_kg?: number;
  [key: string]: unknown;
}

export interface OptimizeCandidate {
  rank: number;
  label?: string;
  changes: OptimizeChange[];
  design: Design;
  metrics_before: MetricsBlock;
  metrics_after: MetricsBlock;
  reduction_pct: number;
  robust_worst_case_after?: number;
  verified?: boolean;
  findings?: Finding[];
  findings_before_counts?: Record<string, number>;
  findings_after_counts?: Record<string, number>;
  findings_diff?: {
    resolved: Finding[];
    remaining: Finding[];
    new: Finding[];
  };
}

export interface OptimizeResult {
  candidates: OptimizeCandidate[];
  best_index: number;
  solve_ms: number;
}

// ─── /agent/chat ─────────────────────────────────────────────────────────────

export type AgentMode = "build" | "continue" | "fix" | "explain";

export type OpName =
  | "load_preset"
  | "add_node"
  | "add_member"
  | "remove_member"
  | "set_section"
  | "set_material"
  | "set_support"
  | "add_component"
  | "move_component"
  | "set_component"
  | "remove_component"
  | "set_mount"
  | "set_excitation"
  | "set_analysis";

export interface AgentOp {
  op: OpName;
  args: Record<string, unknown>;
}

export interface AgentMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AgentRequest {
  mode: AgentMode;
  design: Design;
  analysis?: AnalysisResult;
  messages: AgentMessage[];
}

export interface AgentResponse {
  assistant_message: string;
  ops: AgentOp[];
  design: Design;
  warnings: string[];
  action?: "run_analysis" | "run_optimization" | null;
  clarifying_question?: string | null;
  op_descriptions?: string[];
  source?: "llm" | "fallback" | string;
}

// ─── /validate ───────────────────────────────────────────────────────────────

export interface ValidationCase {
  id?: string;
  name: string;
  reference: string | number;
  ours: string | number;
  unit?: string;
  error_pct: number;
  tolerance_pct?: number;
  pass: boolean;
  detail?: string;
}

export interface ValidationResult {
  cases: ValidationCase[];
  summary?: {
    passed: number;
    total: number;
  };
  mesh_convergence_pass?: boolean;
  benchmark?: {
    min_speedup_dense?: number;
    max_speedup_dense?: number;
    min_speedup_sparse?: number;
    max_speedup_sparse?: number;
    max_error_pct?: number;
    claim_line?: string;
    full_ms?: number;
    rom_ms?: number;
    speedup?: number;
    error_pct?: number;
    [key: string]: unknown;
  };
  benchmark_live?: {
    n_dof: number;
    dense_ms: number;
    sparse_ms: number;
    rom_setup_ms: number;
    rom_sweep_ms: number;
    rom_total_ms: number;
    speedup_vs_dense: number;
    speedup_vs_sparse: number;
    max_error_pct: number;
  };
  environment?: {
    os: string;
    cpu: string;
    cores: number;
    python: string;
    numpy: string;
    scipy: string;
  };
}

// ─── Presets ─────────────────────────────────────────────────────────────────

export interface MaterialPreset {
  key: MaterialKey;
  label: string;
  E: number; // Pa
  rho: number; // kg/m³
  yield_mpa: number;
}

export interface MountPresetInfo {
  key: MountPreset;
  label: string;
  kv: number;
  zeta: number;
}

export interface ComponentPreset {
  kind: ComponentKind;
  label: string;
  mass: number;
  limit_g?: number;
  me?: number;
  rpm_min?: number;
  rpm_max?: number;
}

export interface StructurePreset {
  id: string;
  name: string;
  description: string;
  design: Design;
}