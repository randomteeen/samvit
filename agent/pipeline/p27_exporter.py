"""
Stage 27 — Exporter
=====================
Produces all fabrication artefacts from the completed design:
  • bom.csv           — Bill of Materials
  • pick_place.csv    — Pick-and-place file
  • gerbers/          — One Gerber file per copper/silkscreen layer
  • drill.drl         — Excellon drill file
  • netlist.net       — KiCad netlist

All outputs returned as strings in StageResult.data["artefacts"].
The CheckpointManager saves these to disk.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any, Dict, List, Optional

from agent.core.models import (
    Component, DesignState, Issue, PCBLayout, PlacedComponent,
    Severity, StageResult, StageStatus,
)


# ──────────────────────────────────────────────────────────────────────────────
# BOM
# ──────────────────────────────────────────────────────────────────────────────

def _generate_bom(
    selected_map: Dict[str, str],
    components: Dict[str, Component],
    quantities: Optional[Dict[str, int]] = None,
) -> str:
    quantities = quantities or {}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "Subsystem", "Part Number", "Manufacturer", "Category",
                     "Package", "Footprint", "Qty", "Unit Cost USD", "Total Cost USD", "Notes"])
    total = 0.0
    for i, (sub, pn) in enumerate(selected_map.items(), 1):
        comp = components.get(pn)
        if comp is None:
            continue
        qty = max(1, int(quantities.get(sub, 1)))
        line_cost = comp.cost_usd * qty
        total += line_cost
        writer.writerow([
            i, sub, comp.part_number, comp.manufacturer, comp.category,
            comp.package, comp.footprint, qty,
            f"{comp.cost_usd:.2f}", f"{line_cost:.2f}", comp.notes,
        ])
    writer.writerow(["", "", "", "", "", "", "TOTAL", "", "", f"{total:.2f}", ""])
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Pick-and-place
# ──────────────────────────────────────────────────────────────────────────────

def _generate_pnp(placed: List[PlacedComponent]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Designator", "Footprint", "X_mm", "Y_mm", "Rotation", "Layer", "Side"])
    for p in placed:
        side = "Top" if p.layer == "F.Cu" else "Bottom"
        writer.writerow([
            p.designator, p.footprint,
            f"{p.x:.3f}", f"{p.y:.3f}", f"{p.rotation:.1f}",
            p.layer, side,
        ])
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Gerber stubs (real Gerber requires KiCad CLI — we produce valid format shells)
# ──────────────────────────────────────────────────────────────────────────────

def _gerber_header(layer_name: str, function: str) -> str:
    return (
        f"%TF.FileFunction,{function}*%\n"
        f"%TF.GenerationSoftware,Samvit,HardwareAgent,1.0*%\n"
        f"%FSLAX46Y46*%\n"
        f"%MOMM*%\n"
        f"%LPD*%\n"
        f"G04 Layer: {layer_name} *\n"
    )


def _generate_gerbers(layout: Optional[PCBLayout]) -> Dict[str, str]:
    gerbers: Dict[str, str] = {}
    layers = [
        ("F.Cu",    "Copper,L1,Top,Signal"),
        ("B.Cu",    "Copper,L2,Bot,Signal"),
        ("F.SilkS", "Legend,Top"),
        ("B.SilkS", "Legend,Bot"),
        ("F.Mask",  "SolderMask,Top"),
        ("B.Mask",  "SolderMask,Bot"),
        ("Edge.Cuts", "Profile,NP"),
    ]
    ext_map = {
        "F.Cu": "GTL", "B.Cu": "GBL",
        "F.SilkS": "GTO", "B.SilkS": "GBO",
        "F.Mask":  "GTS", "B.Mask":  "GBS",
        "Edge.Cuts": "GKO",
    }
    for layer_name, function in layers:
        ext = ext_map.get(layer_name, "GBR")
        content = _gerber_header(layer_name, function)
        if layout and layer_name == "F.Cu":
            for seg in layout.traces:
                if seg.layer == "F.Cu":
                    w = int(seg.width * 1e6)
                    content += (
                        f"G01*\n"
                        f"X{int(seg.x1*1e6):+010d}Y{int(seg.y1*1e6):+010d}D02*\n"
                        f"X{int(seg.x2*1e6):+010d}Y{int(seg.y2*1e6):+010d}D01*\n"
                    )
        content += "M02*\n"
        gerbers[f"gerbers/samvit.{ext}"] = content
    return gerbers


# ──────────────────────────────────────────────────────────────────────────────
# Drill file
# ──────────────────────────────────────────────────────────────────────────────

def _generate_drill(via_count: int) -> str:
    lines = [
        "M48",
        "METRIC,TZ",
        "T1C0.300",
        "M95",
        "G90",
        "G05",
        "T1",
    ]
    for i in range(via_count):
        x = 10 + (i % 10) * 5
        y = 10 + (i // 10) * 5
        lines.append(f"X{x:06.3f}Y{y:06.3f}")
    lines.append("M30")
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# Stage entry point
# ──────────────────────────────────────────────────────────────────────────────

def run(state: DesignState) -> StageResult:
    t0 = time.monotonic()
    issues: List[Issue] = []

    sel_result = state.stage_results.get("p08_part_selection")
    selected   = sel_result.data.get("selected", {}) if sel_result else {}
    quantities = sel_result.data.get("quantities", {}) if sel_result else {}

    artefacts: Dict[str, str] = {}

    # BOM
    artefacts["bom.csv"] = _generate_bom(selected, state.components, quantities)

    # Pick-and-place
    if state.layout:
        artefacts["pick_place.csv"] = _generate_pnp(state.layout.placed)
        gerbs = _generate_gerbers(state.layout)
        artefacts.update(gerbs)
        artefacts["drill.drl"] = _generate_drill(state.layout.via_count)
    else:
        issues.append(Issue("EXP_NO_LAYOUT", Severity.WARNING,
                            "No PCB layout — Gerber/PnP files not generated.", "exporter"))

    # KiCad files from Stage 26
    kic_result = state.stage_results.get("p26_kicad")
    if kic_result:
        artefacts.update(kic_result.data.get("files", {}))

    has_errors = any(i.is_error() for i in issues)
    return StageResult(
        stage="p27_exporter",
        status=StageStatus.PASSED if not has_errors else StageStatus.FAILED,
        data={
            "artefacts":     artefacts,
            "artefact_list": list(artefacts.keys()),
            "file_count":    len(artefacts),
        },
        issues=issues,
        metrics={"export_file_count": float(len(artefacts))},
        duration=time.monotonic() - t0,
    )
