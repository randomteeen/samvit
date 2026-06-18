"""
Stage 23 — Metrics Engine
===========================
Aggregates all stage results into a single DesignMetrics snapshot:
  • ERC/DRC error counts
  • Simulation pass rate
  • Power draw + battery life
  • Thermal max temperature
  • Component count, net count, BOM cost
  • Board area

Deterministic — no Gemini call.
"""

from __future__ import annotations

import time
from dataclasses import asdict

from agent.core.models import (
    DesignMetrics, DesignState, StageResult, StageStatus,
)


def run(state: DesignState) -> StageResult:
    t0 = time.monotonic()

    def _get(stage: str, key: str, default: float = 0.0) -> float:
        r = state.stage_results.get(stage)
        if r is None:
            return default
        val = r.data.get(key, default)
        try:
            fval = float(val)
            import math
            if not math.isfinite(fval):
                return default
            return fval
        except (ValueError, TypeError):
            return default

    def _iget(stage: str, key: str, default: int = 0) -> int:
        r = state.stage_results.get(stage)
        if r is None:
            return default
        return int(r.data.get(key, default))

    erc_result = state.stage_results.get("p16_erc")
    drc_result = state.stage_results.get("p17_drc")
    pwr_result = state.stage_results.get("p18_power")
    thr_result = state.stage_results.get("p19_thermal")
    sim_result = state.stage_results.get("p21_simulation")
    sel_result = state.stage_results.get("p08_part_selection")

    erc_errors = len(erc_result.data.get("erc_errors", [])) if erc_result else 0
    drc_errors = len(drc_result.data.get("drc_errors", [])) if drc_result else 0

    power_mw      = _get("p18_power", "total_power_mw")
    battery_life  = _get("p18_power", "battery_life_h")
    max_temp      = _get("p19_thermal", "max_temp_c")
    sim_pass_rate = _get("p21_simulation", "pass_rate")

    # Real placed-part count = sum of selected-subsystem quantities (e.g. 144
    # motors + 9 drivers + …), NOT len(state.components) which counts the whole
    # candidate catalogue and wildly overstates the board.
    if sel_result and sel_result.data.get("selected"):
        selected_map = sel_result.data.get("selected", {})
        quantities_map = sel_result.data.get("quantities", {})
        comp_count = sum(max(1, int(quantities_map.get(name, 1)))
                         for name in selected_map)
    else:
        comp_count = len(state.components)
    net_count  = len(state.schematic.nets) if state.schematic else 0
    board_area = (state.layout.board_width * state.layout.board_height
                  if state.layout else 0.0)
    bom_cost   = _get("p08_part_selection", "bom_cost_usd")

    n_total = _iget("p21_simulation", "total_count", 1)
    n_pass  = _iget("p21_simulation", "passed_count", 0)
    pass_rate   = n_pass / n_total if n_total > 0 else 0.0
    fail_rate   = 1.0 - pass_rate

    # Power validation as a convergence signal. power_ok mirrors p18_power's
    # own pass/fail (any ERROR-severity power issue → not ok). If p18 has not
    # run yet, treat as ok so we don't spuriously block. Also surface how far
    # over the architecture power budget we are, to give the repair loop's
    # improvement guard a gradient on power.
    power_ok = pwr_result is None or pwr_result.status == StageStatus.PASSED
    budget_mw = (state.architecture.power_budget_mw
                 if state.architecture and state.architecture.power_budget_mw > 0
                 else 0.0)
    power_over_budget_mw = (
        max(0.0, power_mw - budget_mw * 1.1) if budget_mw > 0 else 0.0
    )

    metrics = DesignMetrics(
        pass_rate=round(pass_rate, 3),
        failure_rate=round(fail_rate, 3),
        erc_errors=erc_errors,
        drc_errors=drc_errors,
        power_draw_mw=round(power_mw, 2),
        estimated_battery_h=round(battery_life, 2),
        max_temp_c=round(max_temp, 1),
        component_count=comp_count,
        net_count=net_count,
        board_area_mm2=round(board_area, 1),
        bom_cost_usd=round(bom_cost, 2),
        sim_pass_rate=round(sim_pass_rate, 3),
        power_ok=power_ok,
        power_over_budget_mw=round(power_over_budget_mw, 2),
        iteration=state.iteration,
    )
    state.metrics = metrics

    return StageResult(
        stage="p23_metrics",
        status=StageStatus.PASSED,
        data=asdict(metrics),
        metrics={
            "erc_errors":    float(erc_errors),
            "drc_errors":    float(drc_errors),
            "sim_pass_rate": sim_pass_rate,
            "power_mw":      power_mw,
            "battery_h":     battery_life,
            "max_temp_c":    max_temp,
            "bom_cost_usd":  bom_cost,
        },
        duration=time.monotonic() - t0,
    )
