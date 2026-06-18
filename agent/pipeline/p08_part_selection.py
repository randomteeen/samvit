"""
Stage 8 — Part Selection Engine
==================================
Selects the single best part for each subsystem from Stage 7 candidates.

Primary:  hardware_builder.part_selection_engine.PartSelectionEngine
          (runs offline DB query + voltage/current/cost scoring)
Fallback: simple pick-first-by-score from candidates list

No Gemini call — fully deterministic.

Output
------
  StageResult.data["selected"] = {subsystem_name: part_number}
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from agent.core.models import (
    Component, DesignState, Issue, Severity,
    StageResult, StageStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# PartSelectionEngine bridge
# ─────────────────────────────────────────────────────────────────────────────

# Maps generic architecture categories → specific DB category names
_CATEGORY_DB_ALIASES: Dict[str, List[str]] = {
    "POWER":      ["Charger IC", "Buck-Boost", "Boost Converter", "LDO", "POWER"],
    "PROTECTION": ["Diode", "MOSFET", "PROTECTION"],
    "Battery":    ["Battery"],
    "MCU":        ["MCU", "Microcontroller"],
    "SBC":        ["SBC"],
    "MEMORY":     ["Flash", "Storage", "MEMORY"],
    "SENSOR":     ["Depth Sensor", "ToF Sensor", "LiDAR", "Camera", "IMU",
                   "Barometer", "Microphone", "SENSOR"],
    "ACTUATOR":   ["Actuator", "Haptic Driver", "PWM Driver", "ACTUATOR"],
    "COMMS":      ["BT Module", "BLE SoC", "LoRa", "LTE Module", "COMMS"],
    "AUDIO":      ["Audio Amp", "Mic Amp", "Microphone", "AUDIO"],
    "DISPLAY":    ["Display", "LED", "DISPLAY"],
    "INTERFACE":  ["Level Shifter", "IO Expander", "Buffer", "Connector", "INTERFACE"],
    "PASSIVE":    ["Capacitor", "Resistor", "Diode", "MOSFET", "Connector", "PASSIVE"],
}


def _expand_category(category: str) -> List[str]:
    """Return DB-specific category names for a generic architecture category."""
    aliases = _CATEGORY_DB_ALIASES.get(category, [])
    return aliases if aliases else [category]


def _select_via_engine(
    category: str,
    voltage_min: float,
    voltage_max: float,
    current_ma: float,
    budget_usd: Optional[float],
) -> Optional[str]:
    """
    Ask PartSelectionEngine to search the offline DB for the best match.
    Returns part_number string or None.
    """
    try:
        import os, sys
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        from hardware_builder.part_selection_engine import (  # type: ignore[import]
            PartSelectionEngine, ComponentRequirements,
        )

        db_path = os.path.join(repo_root, "hardware_builder", "samvit_parts.db")
        engine = PartSelectionEngine(db_path)

        reqs = ComponentRequirements(
            category=category,
            voltage_min=voltage_min if voltage_min > 0 else None,
            voltage_max=voltage_max if voltage_max > 0 else None,
            current_min_ma=current_ma if current_ma > 0 else None,
            max_cost_usd=budget_usd,
        )

        # Expand generic category to DB-specific names and try each
        for db_cat in _expand_category(category):
            _reqs = ComponentRequirements(
                category=db_cat,
                voltage_min=voltage_min if voltage_min > 0 else None,
                voltage_max=voltage_max if voltage_max > 0 else None,
                current_min_ma=current_ma if current_ma > 0 else None,
                max_cost_usd=budget_usd,
            )
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                best = pool.submit(asyncio.run, engine.select_best_part(_reqs)).result(timeout=30)
            if best:
                return best.part_number
        return None

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: pick best from Stage 7 candidate list (already scored)
# ─────────────────────────────────────────────────────────────────────────────

def _pick_from_candidates(
    candidates: List[Dict[str, Any]],
    components: Dict[str, Component],
    budget_usd: Optional[float],
) -> Optional[str]:
    for c in candidates:
        pn = c["part_number"]
        comp = components.get(pn)
        if comp is None:
            continue
        if budget_usd is not None and comp.cost_usd > budget_usd:
            continue
        return pn
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(state: DesignState) -> StageResult:
    t0 = time.monotonic()
    issues: List[Issue] = []

    if state.architecture is None:
        return StageResult(
            stage="p08_part_selection",
            status=StageStatus.FAILED,
            issues=[Issue("SELECT_NO_ARCH", Severity.ERROR,
                          "Architecture not set.", "part_selection")],
            duration=time.monotonic() - t0,
        )

    # p07 stores candidates in stage_results (state.stage_data is always None)
    _p07 = state.stage_results.get("p07_component_search")
    candidates_data: Dict[str, List[Dict[str, Any]]] = (
        _p07.data.get("candidates", {}) if _p07 else {}
    )

    budget_usd: Optional[float] = getattr(state.requirements, "budget_usd", None) if state.requirements else None
    selected: Dict[str, str] = {}
    quantities: Dict[str, int] = {}   # subsystem_name → unit count

    for sub in state.architecture.subsystems:
        # 1. Try PartSelectionEngine (offline DB scoring)
        pn = _select_via_engine(
            category=sub.category,
            voltage_min=sub.voltage_min,
            voltage_max=sub.voltage_max,
            current_ma=sub.current_ma,
            budget_usd=budget_usd,
        )

        # 2. Fall back to picking best from Stage 7 candidates
        if pn is None:
            cands = candidates_data.get(sub.name, [])
            pn = _pick_from_candidates(cands, state.components, budget_usd)

        if pn is None:
            issues.append(Issue(
                code="SELECT_NO_PART",
                severity=Severity.WARNING if sub.priority == 2 else Severity.ERROR,
                message=f"Could not select a part for subsystem '{sub.name}' ({sub.category}).",
                source="part_selection",
                objects=[sub.name],
            ))
        else:
            selected[sub.name] = pn
            quantities[sub.name] = max(1, int(getattr(sub, "quantity", 1) or 1))

    # Extended BOM cost = sum(unit_cost * quantity) over selected parts.
    bom_cost_usd = 0.0
    for sub_name, pn in selected.items():
        comp = state.components.get(pn)
        if comp is not None:
            bom_cost_usd += comp.cost_usd * quantities.get(sub_name, 1)

    # Persist selection into state for downstream stages
    if not hasattr(state, "stage_data") or state.stage_data is None:
        state.stage_data = {}
    _sd = state.stage_data.setdefault("p08_part_selection", {})
    _sd["selected"] = selected
    _sd["quantities"] = quantities

    has_errors = any(i.is_error() for i in issues)
    return StageResult(
        stage="p08_part_selection",
        status=StageStatus.FAILED if has_errors else StageStatus.PASSED,
        data={
            "selected": selected,
            "quantities": quantities,
            "bom_cost_usd": round(bom_cost_usd, 2),
        },
        issues=issues,
        duration=time.monotonic() - t0,
    )
