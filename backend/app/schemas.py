"""Pydantic v2 schemas for the ResonAI data contract.

All models adhere strictly to 00_MASTER_CONTEXT.md section 6.
Conventions:
- All physical units in JSON are SI (m, kg, N, Pa, s, Hz, rad/s).
- Accelerations in g.
- Stresses in MPa.
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


# ==========================================
# 6.1 Design Models (Frontend -> Backend)
# ==========================================


class NodeModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    x: float
    y: float
    z: Optional[float] = 0.0


class RectSection(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["rect"]
    w: float = Field(gt=0.0, description="Width in m")
    h: float = Field(gt=0.0, description="Height / in-plane depth in m")


class CircSection(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["circ"]
    d: float = Field(gt=0.0, description="Diameter in m")


class TubeSection(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tube"]
    d: float = Field(gt=0.0, description="Outer diameter in m")
    t: float = Field(gt=0.0, description="Wall thickness in m")


class BoxSection(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["box"]
    w: float = Field(gt=0.0, description="Outer width in m")
    h: float = Field(gt=0.0, description="Outer height in m")
    t: float = Field(gt=0.0, description="Wall thickness in m")


SectionType = Union[RectSection, CircSection, TubeSection, BoxSection]


class MemberModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    n1: str
    n2: str
    material: str
    section: SectionType


class SupportModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    node: str
    type: Literal["fixed", "pinned", "roller", "spring", "free"]
    kx: Optional[float] = Field(default=None, ge=0.0)
    ky: Optional[float] = Field(default=None, ge=0.0)
    kr: Optional[float] = Field(default=None, ge=0.0)


class MountModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    preset: Optional[str] = None
    kv: float = Field(gt=0.0, description="Vertical stiffness in N/m")
    zeta: float = Field(ge=0.0, description="Damping ratio")


class ExcitationModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["unbalance", "harmonic"] = "unbalance"
    rpm_min: Optional[float] = Field(default=None, gt=0.0)
    rpm_max: Optional[float] = Field(default=None, gt=0.0)
    me: Optional[float] = Field(default=None, ge=0.0, description="Unbalance in kg*m")
    orders: Optional[List[float]] = [1.0]
    blades: Optional[int] = Field(default=None, ge=1)


class ComponentModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    member: str
    s: float = Field(ge=0.0, le=1.0, description="Fraction along member from n1 to n2")
    mass: float = Field(gt=0.0, description="Lumped mass in kg")
    mount: Optional[MountModel] = None
    excitation: Optional[ExcitationModel] = None
    limit_g: Optional[float] = Field(default=None, gt=0.0, description="Acceleration limit in g")


class AnalysisConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    rpm_min: float = Field(default=500.0, ge=0.0)
    rpm_max: float = Field(default=12000.0, gt=0.0)
    n_points: int = Field(default=400, ge=10)
    beam_zeta: float = Field(default=0.02, ge=0.0, le=1.0)
    n_elements_per_member: int = Field(default=20, ge=1)


class DesignModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "ResonAI Structure"
    plane: Optional[str] = "xy"
    nodes: List[NodeModel]
    members: List[MemberModel]
    supports: List[SupportModel] = Field(default_factory=list)
    components: List[ComponentModel] = Field(default_factory=list)
    analysis: Optional[AnalysisConfigModel] = Field(default_factory=AnalysisConfigModel)

    @model_validator(mode="after")
    def validate_references(self):
        node_ids = {n.id for n in self.nodes}
        for m in self.members:
            if m.n1 not in node_ids:
                raise ValueError(f"Member '{m.id}' references undefined node n1='{m.n1}'")
            if m.n2 not in node_ids:
                raise ValueError(f"Member '{m.id}' references undefined node n2='{m.n2}'")
            if m.n1 == m.n2:
                raise ValueError(f"Member '{m.id}' has identical start and end node '{m.n1}'")

        member_ids = {m.id for m in self.members}
        for comp in self.components:
            if comp.member not in member_ids:
                raise ValueError(f"Component '{comp.id}' references undefined member '{comp.member}'")

        for supp in self.supports:
            if supp.node not in node_ids:
                raise ValueError(f"Support references undefined node '{supp.node}'")

        return self


# ==========================================
# 6.2 Analyze Response Models (Backend -> Frontend)
# ==========================================


class AnalysisMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    n_dof: int
    n_modes_kept: int
    solve_ms: float
    method: str = "rom"


class MeshNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    x: float
    y: float
    member: Optional[str] = None
    s: Optional[float] = None


class ModeInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    i: int
    f_hz: float
    shape: List[List[float]]
    note: Optional[str] = None


class SweepResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    rpm: List[float]
    freq_hz: List[float]
    node_amp_mm: List[List[float]]
    component_accel_g: Dict[str, List[float]]
    component_disp_mm: Dict[str, List[float]]
    transmitted_force_n: Dict[str, List[float]]


class CampbellLine(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    order: float


class CampbellBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode_freqs_hz: List[float]
    lines: List[CampbellLine]


class ComponentSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    peak_accel_g: float
    at_rpm: float
    limit_g: Optional[float] = None
    status: Literal["ok", "warning", "critical"]


class PositionRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    member: str
    s: float


class Finding(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    severity: Literal["ok", "warning", "critical", "info"]
    before_severity: Optional[str] = None
    type: str
    component: Optional[str] = None
    rpm: Optional[float] = None
    value: Optional[float] = None
    limit: Optional[float] = None
    mode: Optional[int] = None
    mode_f_hz: Optional[float] = None
    position: Optional[PositionRef] = None
    detail: str


class MaxStress(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: float
    member: str
    at_rpm: float
    yield_mpa: float
    ratio: float


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    meta: AnalysisMeta
    mesh_nodes: List[MeshNode]
    modes: List[ModeInfo]
    sweep: SweepResult
    campbell: CampbellBlock
    components: List[ComponentSummary]
    findings: List[Finding]
    max_stress_mpa: MaxStress


# ==========================================
# 6.3 Response At RPM Models (Backend -> Frontend)
# ==========================================


class NodeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    ux: List[float]  # [re, im] in metres
    uy: List[float]  # [re, im] in metres
    rot: List[float]  # [re, im] in radians


class ComponentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    uy: List[float]  # [re, im] in metres


class ResponseAtResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    rpm: float
    omega: float
    order: Optional[float] = None
    nodes: List[NodeResponse]
    components: List[ComponentResponse]


class ResponseAtRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    design: DesignModel
    rpm: float = Field(gt=0.0, description="Rotational speed in RPM")


# ==========================================
# 6.4 Optimize Models (Frontend <-> Backend)
# ==========================================


class LocksModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    positions: bool = False
    mounts: bool = False
    damping: bool = False


class BudgetModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_evals: int = Field(default=1200, ge=50)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    design: DesignModel
    locks: Optional[LocksModel] = Field(default_factory=LocksModel)
    objective: str = "min_sensor_accel"
    budget: Optional[BudgetModel] = Field(default_factory=BudgetModel)
    seed: Optional[int] = 1
    movable: Optional[List[str]] = None


class ChangeItem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    path: str
    from_val: Any = Field(alias="from")
    to_val: Any = Field(alias="to")
    label: str


class ComponentMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    peak_accel_g: float
    at_rpm: float
    limit_g: Optional[float] = None
    status: str


class MetricsBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    components: Dict[str, ComponentMetrics]
    max_stress_ratio: float
    n_resonance_findings: int
    robust_worst_case: Optional[float] = None
    rom_vs_full_discrepancy_pct: Optional[float] = None
    mass_added_kg: float = 0.0


class CandidateBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    rank: int
    label: str
    changes: List[ChangeItem]
    design: DesignModel
    metrics_before: MetricsBlock
    metrics_after: MetricsBlock
    reduction_pct: float
    robust_worst_case_after: float
    verified: bool = True
    findings: Optional[List[Finding]] = None
    findings_before_counts: Optional[Dict[str, int]] = None
    findings_after_counts: Optional[Dict[str, int]] = None
    findings_diff: Optional[Dict[str, List[Finding]]] = None


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidates: List[CandidateBlock]
    best_index: Optional[int] = None
    solve_ms: float
    evaluations: int
    note: Optional[str] = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str = Field(default="continue", description="One of build, continue, fix, explain")
    design: Optional[Dict[str, Any]] = Field(default=None, description="Current structure design")
    analysis: Optional[Dict[str, Any]] = Field(default=None, description="Latest analysis result")
    candidates: Optional[List[Dict[str, Any]]] = Field(default=None, description="Latest optimization candidates")
    messages: List[ChatMessage] = Field(default_factory=list, description="Chat messages history")


class AgentChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    assistant_message: str
    ops: List[Dict[str, Any]] = Field(default_factory=list)
    op_descriptions: List[str] = Field(default_factory=list)
    design: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)
    action: Optional[str] = None
    clarifying_question: Optional[str] = None
    source: str = Field(description="'llm', 'cache', or 'fallback'")


class AgentHealthResponse(BaseModel):
    configured: bool
    model_configured: bool



