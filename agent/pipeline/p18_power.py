"""
Stage 18 — Power Analysis Module
===================================
Estimates:
  • Total current draw per rail
  • Power consumption (mW) for each subsystem
  • Estimated battery life (hours) given capacity
  • Voltage drop on power traces
  • Thermal power dissipation

Deterministic — no Gemini call.

Output
------
  StageResult.data["total_power_mw"]
  StageResult.data["battery_life_h"]
  StageResult.data["per_rail"]
  StageResult.metrics["power_draw_mw", "battery_life_h"]
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agent.core.models import (
    Component, DesignRules, DesignState, Issue, Severity,
    StageResult, StageStatus,
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Copper resistivity: ρ = 1.72e-8 Ω·m; typical trace resistance per mm
# for 0.25mm wide, 35µm thick (1oz) trace: ~2.1 mΩ/mm
_TRACE_RESISTANCE_PER_MM = 0.0021  # Ω/mm


@dataclass
class RailAnalysis:
    name:          str
    voltage:       float    # V
    total_draw_ma: float    # mA
    power_mw:      float    # mW
    sources:       List[str]
    consumers:     List[str]


def _estimate_battery_life(
    capacity_mah: float,
    total_draw_ma: float,
    efficiency: float = 0.85,
) -> float:
    """Return estimated battery life in hours."""
    if total_draw_ma <= 0:
        return float("inf")
    return (capacity_mah * efficiency) / total_draw_ma


def _find_battery_capacity(components: Dict[str, Component]) -> float:
    """Extract battery capacity in mAh from component notes/description."""
    for comp in components.values():
        if "battery" in comp.category.lower() or "battery" in comp.description.lower():
            import re
            m = re.search(r"(\d+)\s*mAh", comp.notes + " " + comp.description, re.IGNORECASE)
            if m:
                return float(m.group(1))
    return 3000.0  # default: assume 3000mAh 18650


def _voltage_drop(current_a: float, trace_length_mm: float) -> float:
    """Estimate voltage drop across a trace in mV."""
    return current_a * trace_length_mm * _TRACE_RESISTANCE_PER_MM * 1000.0


# ──────────────────────────────────────────────────────────────────────────────
# Stage entry point
# ──────────────────────────────────────────────────────────────────────────────

def run(state: DesignState) -> StageResult:
    t0 = time.monotonic()
    issues: List[Issue] = []

    sel_result = state.stage_results.get("p08_part_selection")
    selected_map: Dict[str, str] = (
        sel_result.data.get("selected", {}) if sel_result else {}
    )
    quantities_map: Dict[str, int] = (
        sel_result.data.get("quantities", {}) if sel_result else {}
    )
    selected_pns: List[str] = list(
        selected_map.values()
    ) if sel_result else list(state.components.keys())

    # part_number → number of identical units (e.g. 144 motors all share one pn).
    pn_qty: Dict[str, int] = {}
    for sub_name, pn in selected_map.items():
        pn_qty[pn] = pn_qty.get(pn, 0) + max(1, int(quantities_map.get(sub_name, 1)))

    selected_comps = {pn: state.components[pn] for pn in selected_pns if pn in state.components}

    # Group by voltage rail
    rails: Dict[float, RailAnalysis] = {}
    for pn, comp in selected_comps.items():
        qty = max(1, pn_qty.get(pn, 1))
        v = round(comp.voltage_max, 1)
        if v not in rails:
            rails[v] = RailAnalysis(
                name=f"VDD_{v:.1f}V".replace(".", "_"),
                voltage=v,
                total_draw_ma=0.0,
                power_mw=0.0,
                sources=[],
                consumers=[],
            )
        rail = rails[v]
        if comp.category in ("POWER", "Charger IC", "Buck-Boost", "LDO", "Boost Converter"):
            rail.sources.append(pn)
        else:
            rail.total_draw_ma += comp.current_ma * qty
            rail.consumers.append(pn)

    for rail in rails.values():
        rail.power_mw = rail.total_draw_ma * rail.voltage

    total_power_mw = sum(r.power_mw for r in rails.values())
    total_draw_ma  = sum(r.total_draw_ma for r in rails.values())

    # Fallback: component DB may store current_ma as "50mA" strings; float() strips
    # the "mA" suffix → all current_ma = 0 → total_draw_ma = 0 → battery = inf.
    # Use architecture subsystem current estimates so battery life is always realistic.
    if total_draw_ma <= 0 and state.architecture:
        for _sub in state.architecture.subsystems:
            if _sub.category not in ("POWER",) and _sub.current_ma > 0:
                total_draw_ma += _sub.current_ma * max(1, int(getattr(_sub, "quantity", 1) or 1))
        total_power_mw = total_draw_ma * 3.3  # 3.3 V rail average

    # Battery life estimate
    battery_cap_mah = _find_battery_capacity(state.components)
    battery_life_h  = _estimate_battery_life(battery_cap_mah, total_draw_ma)

    # Voltage drop check on power traces
    if state.layout:
        avg_trace_mm = 50.0  # assume 50mm average power trace
        i_max_a = total_draw_ma / 1000.0
        v_drop_mv = _voltage_drop(i_max_a, avg_trace_mm)
        if v_drop_mv > 100:
            issues.append(Issue(
                code="PWR_HIGH_VDROP",
                severity=Severity.WARNING,
                message=(
                    f"Estimated voltage drop on power trace: {v_drop_mv:.1f}mV "
                    f"({i_max_a*1000:.0f}mA, {avg_trace_mm}mm avg). "
                    "Consider widening power traces or using wider copper pours."
                ),
                source="power",
            ))

    # Architecture budget comparison
    if state.architecture and state.architecture.power_budget_mw > 0:
        budget = state.architecture.power_budget_mw
        if total_power_mw > budget * 1.1:
            issues.append(Issue(
                code="PWR_OVER_BUDGET",
                severity=Severity.ERROR,
                message=(
                    f"Total power {total_power_mw:.0f}mW exceeds architecture "
                    f"budget {budget:.0f}mW by {total_power_mw - budget:.0f}mW."
                ),
                source="power",
            ))

    # Battery life validation
    import math
    if not math.isfinite(battery_life_h) or battery_life_h > 87600:
        # If battery life is Infinity or > 10 years, something is wrong with the data
        issues.append(Issue(
            code="PWR_UNREALISTIC_BATTERY",
            severity=Severity.ERROR,
            message=(
                f"Estimated battery life ({battery_life_h}) is unrealistic. "
                "This usually means total current draw is zero or negative, "
                "or component current specs were parsed incorrectly. "
                "Check part selection and datasheet parameters."
            ),
            source="power",
        ))
    elif battery_life_h < 2.0:
        issues.append(Issue(
            code="PWR_SHORT_BATTERY",
            severity=Severity.WARNING,
            message=(
                f"Estimated battery life {battery_life_h:.1f}h is below 2h. "
                "Consider reducing standby current or increasing battery capacity."
            ),
            source="power",
        ))

    # Sanitize battery_life_h for the data payload to prevent JSON issues
    safe_battery_h = battery_life_h
    if not math.isfinite(battery_life_h):
        safe_battery_h = 87600.0

    has_errors = any(i.is_error() for i in issues)
    return StageResult(
        stage="p18_power",
        status=StageStatus.PASSED if not has_errors else StageStatus.FAILED,
        data={
            "total_power_mw":  round(total_power_mw, 2),
            "total_draw_ma":   round(total_draw_ma, 2),
            "battery_cap_mah": battery_cap_mah,
            "battery_life_h":  round(safe_battery_h, 2),
            "per_rail":        {r.name: {"draw_ma": r.total_draw_ma, "power_mw": r.power_mw}
                                for r in rails.values()},
        },
        issues=issues,
        metrics={
            "power_draw_mw":    total_power_mw,
            "battery_life_h":   safe_battery_h,   # use sanitized value — raw may be Infinity
            "total_draw_ma":    total_draw_ma,
        },
        duration=time.monotonic() - t0,
    )
