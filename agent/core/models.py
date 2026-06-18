"""
Shared data models for the entire Samvit hardware design pipeline.

Every pipeline stage imports from here so contracts are defined once
and enforced everywhere.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    ERROR   = "ERROR"
    WARNING = "WARNING"
    INFO    = "INFO"


class PinType(str, Enum):
    POWER_IN    = "POWER_IN"
    POWER_OUT   = "POWER_OUT"
    DIGITAL_IN  = "DIGITAL_IN"
    DIGITAL_OUT = "DIGITAL_OUT"
    DIGITAL_BIDI = "DIGITAL_BIDI"
    ANALOG_IN   = "ANALOG_IN"
    ANALOG_OUT  = "ANALOG_OUT"
    PASSIVE     = "PASSIVE"


class InterfaceType(str, Enum):
    I2C     = "I2C"
    SPI     = "SPI"
    UART    = "UART"
    USB     = "USB"
    GPIO    = "GPIO"
    PWM     = "PWM"
    POWER   = "POWER"
    PASSIVE = "PASSIVE"


class StageStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    PASSED   = "PASSED"
    FAILED   = "FAILED"
    REPAIRED = "REPAIRED"
    SKIPPED  = "SKIPPED"


# ──────────────────────────────────────────────────────────────────────────────
# Low-level primitives
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    code:     str
    severity: Severity
    message:  str
    source:   str = ""
    objects:  List[str] = field(default_factory=list)

    def is_error(self) -> bool:
        return self.severity == Severity.ERROR

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    """Uniform result wrapper returned by every pipeline stage."""
    stage:    str
    status:   StageStatus
    data:     Dict[str, Any]          = field(default_factory=dict)
    issues:   List[Issue]             = field(default_factory=list)
    metrics:  Dict[str, float]        = field(default_factory=dict)
    duration: float                   = 0.0   # seconds

    def ok(self) -> bool:
        return self.status in (StageStatus.PASSED, StageStatus.REPAIRED, StageStatus.SKIPPED)

    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.is_error()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage":    self.stage,
            "status":   self.status.value,
            "data":     self.data,
            "issues":   [i.to_dict() for i in self.issues],
            "metrics":  self.metrics,
            "duration": self.duration,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Requirements (Stage 1)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class HardwareRequirements:
    """Structured requirements that seed the entire pipeline."""
    name:             str
    description:      str
    goals:            List[str]                = field(default_factory=list)
    constraints:      Dict[str, Any]           = field(default_factory=dict)
    budget_usd:       Optional[float]          = None
    form_factor:      Optional[str]            = None
    power_source:     Optional[str]            = None
    operating_voltage: Optional[float]         = None
    target_current_ma: Optional[float]         = None
    environment:      Optional[str]            = None
    success_criteria: List[str]               = field(default_factory=list)
    raw_text:         str                      = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# Architecture (Stage 3)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Subsystem:
    name:        str
    role:        str
    category:    str
    voltage_min: float = 0.0
    voltage_max: float = 5.0
    current_ma:  float = 100.0    # peak current draw of ONE unit
    interface:   str   = "GPIO"
    priority:    int   = 1        # 1 = must-have, 2 = nice-to-have
    notes:       str   = ""
    quantity:    int   = 1        # number of identical instances (e.g. 144 motors)


@dataclass
class SystemArchitecture:
    subsystems:    List[Subsystem]       = field(default_factory=list)
    power_budget_mw: float               = 0.0
    notes:         str                   = ""
    raw_plan:      str                   = ""


# ──────────────────────────────────────────────────────────────────────────────
# Component (Stages 4–8)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PinSpec:
    name:         str
    type:         PinType
    voltage_min:  float = 0.0
    voltage_max:  float = 5.0
    current_max:  float = 0.0
    current_draw: float = 0.0


@dataclass
class InterfaceSpec:
    name:  str
    type:  InterfaceType
    pins:  List[str] = field(default_factory=list)


@dataclass
class Component:
    """Fully resolved component record ready for the design stages."""
    part_number:  str
    manufacturer: str
    category:     str
    description:  str
    voltage_min:  float              = 0.0
    voltage_max:  float              = 5.0
    current_ma:   float              = 0.0
    package:      str                = ""
    footprint:    str                = ""
    cost_usd:     float              = 0.0
    datasheet_url: Optional[str]     = None
    source_url:   Optional[str]      = None
    pins:         Dict[str, PinSpec] = field(default_factory=dict)
    interfaces:   List[InterfaceSpec] = field(default_factory=list)
    dependencies: List[str]          = field(default_factory=list)
    notes:        str                = ""
    confidence:   float              = 0.0
    # Multiplier applied to the package θ_ja in thermal analysis.
    # 1.0 = no cooling; <1.0 models a heatsink / copper pour / fan.
    thermal_mitigation: float        = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# Schematic (Stages 10–11)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class NetNode:
    designator: str
    pin:        str


@dataclass
class Net:
    name:  str
    nodes: List[NetNode] = field(default_factory=list)


@dataclass
class SchematicComponent:
    designator: str
    part_number: str
    value:       str = ""
    position:    Tuple[float, float] = (0.0, 0.0)
    rotation:    float               = 0.0


@dataclass
class Schematic:
    components: List[SchematicComponent]  = field(default_factory=list)
    nets:       List[Net]                = field(default_factory=list)
    title:      str                       = "Untitled"
    revision:   str                       = "v0.1"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# PCB Layout (Stages 12–14)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PlacedComponent:
    designator: str
    footprint:  str
    x:          float
    y:          float
    rotation:   float = 0.0
    layer:      str   = "F.Cu"


@dataclass
class TraceSegment:
    net:   str
    x1:    float
    y1:    float
    x2:    float
    y2:    float
    width: float  = 0.25  # mm
    layer: str    = "F.Cu"


@dataclass
class PCBLayout:
    board_width:  float = 100.0   # mm
    board_height: float = 80.0
    placed:       List[PlacedComponent] = field(default_factory=list)
    traces:       List[TraceSegment]    = field(default_factory=list)
    via_count:    int                   = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# Design Rules (Stage 15)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DesignRules:
    min_trace_width:    float = 0.2     # mm
    min_clearance:      float = 0.2     # mm
    min_via_drill:      float = 0.3     # mm
    min_via_annular:    float = 0.15    # mm
    max_layers:         int   = 2
    copper_weight:      float = 1.0     # oz
    board_thickness:    float = 1.6     # mm
    keepout_margin:     float = 0.5     # mm


# ──────────────────────────────────────────────────────────────────────────────
# Simulation & Metrics (Stages 21–23)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SimulationResult:
    scenario:         str
    passed:           bool
    duration_ms:      float                = 0.0
    power_consumption_mw: float            = 0.0
    max_temperature_c:    float            = 0.0
    voltage_stability:    float            = 1.0   # 0–1
    signal_integrity:     float            = 1.0   # 0–1
    notes:            str                  = ""
    issues:           List[Issue]          = field(default_factory=list)


@dataclass
class DesignMetrics:
    pass_rate:            float = 0.0   # 0–1
    failure_rate:         float = 0.0
    erc_errors:           int   = 0
    drc_errors:           int   = 0
    power_draw_mw:        float = 0.0
    estimated_battery_h:  float = 0.0
    max_temp_c:           float = 0.0
    component_count:      int   = 0
    net_count:            int   = 0
    board_area_mm2:       float = 0.0
    bom_cost_usd:         float = 0.0
    sim_pass_rate:        float = 0.0
    # Power validation (p18_power) as a first-class convergence signal.
    # power_ok=False means p18 reported an error (e.g. PWR_OVER_BUDGET); the
    # design must not be considered "clean" while this is False.
    power_ok:             bool  = True
    power_over_budget_mw: float = 0.0   # how far over the architecture budget (0 if within)
    iteration:            int   = 0


# ──────────────────────────────────────────────────────────────────────────────
# Review & Repair (Stages 24–25)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RepairInstruction:
    target_stage:  str
    action:        str          # "replace_part" | "reroute_net" | "add_component" | "adjust_value" | "fix_erc" | "fix_thermal" | "reduce_power" | "fix_simulation" | "fix_placement" | "change_footprint"
    component:     str          = ""
    detail:        Dict[str, Any] = field(default_factory=dict)
    priority:      int          = 1


@dataclass
class ReviewReport:
    passed:        bool
    summary:       str
    root_causes:   List[str]            = field(default_factory=list)
    repairs:       List[RepairInstruction] = field(default_factory=list)
    iteration:     int                  = 0


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline state — carried through all stages
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DesignState:
    """
    Single object passed between every pipeline stage.
    Each stage reads what it needs and writes its outputs back.
    """
    requirements:   Optional[HardwareRequirements]  = None
    architecture:   Optional[SystemArchitecture]    = None
    components:     Dict[str, Component]            = field(default_factory=dict)
    schematic:      Optional[Schematic]             = None
    layout:         Optional[PCBLayout]             = None
    rules:          Optional[DesignRules]           = None
    sim_results:    List[SimulationResult]          = field(default_factory=list)
    metrics:        Optional[DesignMetrics]         = None
    review:         Optional[ReviewReport]          = None
    stage_results:  Dict[str, StageResult]          = field(default_factory=dict)
    stage_data:     Dict[str, Any]                   = field(default_factory=dict)
    iteration:      int                             = 0
    checkpoint_dir: str                             = "checkpoint"

    def record(self, result: StageResult) -> None:
        self.stage_results[result.stage] = result

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "iteration":   self.iteration,
            "stage_results": {k: v.to_dict() for k, v in self.stage_results.items()},
        }
        if self.requirements:
            d["requirements"] = self.requirements.to_dict()
        if self.architecture:
            d["architecture"] = asdict(self.architecture)
        d["components"] = {k: v.to_dict() for k, v in self.components.items()}
        if self.schematic:
            d["schematic"] = self.schematic.to_dict()
        if self.layout:
            d["layout"] = self.layout.to_dict()
        if self.metrics:
            d["metrics"] = asdict(self.metrics)
        if self.review:
            d["review"] = asdict(self.review)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    # ── Deserialisation (checkpoint resume) ──────────────────────────────────
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DesignState":
        """
        Rebuild a DesignState from a checkpoint dict (the inverse of to_dict).
        Defensive: missing/extra keys are tolerated so older or partial
        checkpoints can still be resumed.
        """
        st = cls()
        st.iteration = int(d.get("iteration", 0) or 0)

        if d.get("requirements"):
            st.requirements = _build_dc(HardwareRequirements, d["requirements"])
        if d.get("architecture"):
            arch = d["architecture"]
            st.architecture = SystemArchitecture(
                subsystems=[_build_dc(Subsystem, s) for s in arch.get("subsystems", [])],
                power_budget_mw=float(arch.get("power_budget_mw", 0.0) or 0.0),
                notes=arch.get("notes", ""),
                raw_plan=arch.get("raw_plan", ""),
            )

        for ref, cd in (d.get("components") or {}).items():
            st.components[ref] = _build_component(cd)

        if d.get("schematic"):
            sc = d["schematic"]
            st.schematic = Schematic(
                components=[_build_dc(SchematicComponent, c) for c in sc.get("components", [])],
                nets=[Net(name=n.get("name", ""),
                          nodes=[_build_dc(NetNode, nd) for nd in n.get("nodes", [])])
                      for n in sc.get("nets", [])],
                title=sc.get("title", "Untitled"),
                revision=sc.get("revision", "v0.1"),
            )
        if d.get("layout"):
            ly = d["layout"]
            st.layout = PCBLayout(
                board_width=float(ly.get("board_width", 100.0) or 100.0),
                board_height=float(ly.get("board_height", 80.0) or 80.0),
                placed=[_build_dc(PlacedComponent, p) for p in ly.get("placed", [])],
                traces=[_build_dc(TraceSegment, t) for t in ly.get("traces", [])],
                via_count=int(ly.get("via_count", 0) or 0),
            )
        if d.get("rules"):
            st.rules = _build_dc(DesignRules, d["rules"])
        if d.get("metrics"):
            st.metrics = _build_dc(DesignMetrics, d["metrics"])

        for stage, sr in (d.get("stage_results") or {}).items():
            st.stage_results[stage] = StageResult(
                stage=sr.get("stage", stage),
                status=StageStatus(sr.get("status", "PASSED")),
                data=sr.get("data", {}) or {},
                issues=[Issue(code=i.get("code", ""),
                              severity=Severity(i.get("severity", "INFO")),
                              message=i.get("message", ""),
                              source=i.get("source", ""),
                              objects=i.get("objects", []) or [])
                        for i in sr.get("issues", [])],
                metrics=sr.get("metrics", {}) or {},
                duration=float(sr.get("duration", 0.0) or 0.0),
            )
        return st

    @classmethod
    def from_json(cls, text: str) -> "DesignState":
        return cls.from_dict(json.loads(text))


def _field_names(dc_cls: type) -> set:
    return {f.name for f in dc_fields(dc_cls)}


def _build_dc(dc_cls: type, data: Dict[str, Any]):
    """Build a flat dataclass, ignoring keys it doesn't declare."""
    allowed = _field_names(dc_cls)
    return dc_cls(**{k: v for k, v in (data or {}).items() if k in allowed})


def _build_component(cd: Dict[str, Any]) -> "Component":
    """Rebuild a Component including its nested pins / interfaces."""
    allowed = _field_names(Component)
    kwargs = {k: v for k, v in cd.items() if k in allowed and k not in ("pins", "interfaces")}
    comp = Component(**kwargs)
    for pname, ps in (cd.get("pins") or {}).items():
        comp.pins[pname] = PinSpec(
            name=ps.get("name", pname),
            type=PinType(ps.get("type", "PASSIVE")),
            voltage_min=float(ps.get("voltage_min", 0.0) or 0.0),
            voltage_max=float(ps.get("voltage_max", 5.0) or 5.0),
            current_max=float(ps.get("current_max", 0.0) or 0.0),
            current_draw=float(ps.get("current_draw", 0.0) or 0.0),
        )
    for isp in (cd.get("interfaces") or []):
        comp.interfaces.append(InterfaceSpec(
            name=isp.get("name", ""),
            type=InterfaceType(isp.get("type", "PASSIVE")),
            pins=isp.get("pins", []) or [],
        ))
    return comp
