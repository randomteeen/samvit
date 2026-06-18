"""
Stage 10 — Schematic Graph Builder
=====================================
Converts selected parts + subsystem interfaces into a normalized
circuit graph: nodes = component pins, edges = electrical nets.

This is deterministic — no Gemini call. It applies standard
interface wiring rules (I2C bus, SPI bus, UART, PWM, power rails).

Output
------
  state.schematic is populated (components + nets).
  StageResult.data["net_count"], ["component_count"]
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.core.models import (
    Component, DesignState, Issue, Net, NetNode,
    Schematic, SchematicComponent, Severity,
    StageResult, StageStatus, Subsystem,
)


# ──────────────────────────────────────────────────────────────────────────────
# Standard net templates per interface type
# ──────────────────────────────────────────────────────────────────────────────

def _designator(sub_name: str, idx: int) -> str:
    prefix_map = {
        "power": "U", "regulator": "U", "charger": "U",
        "mcu": "U", "sbc": "U", "compute": "U",
        "sensor": "S", "camera": "S", "depth": "S", "lidar": "S", "imu": "S",
        "haptic": "M", "actuator": "M", "motor": "M", "feedback": "M",
        "audio": "AMP", "speaker": "AMP", "mic": "MIC",
        "bluetooth": "RF", "comms": "RF", "wireless": "RF",
        "battery": "BAT", "display": "DSP",
    }
    for kw, prefix in prefix_map.items():
        if kw in sub_name.lower():
            return f"{prefix}{idx}"
    return f"U{idx}"


def _make_power_nets(
    power_des: str,
    consumers: List[str],
    v_rail: float = 3.3,
) -> List[Net]:
    """Create VDD and GND nets connecting power component to all consumers."""
    vdd_name = f"VDD_{v_rail:.1f}V".replace(".", "_")
    gnd_name = "GND"

    vdd_net = Net(name=vdd_name, nodes=[NetNode(power_des, "VOUT")])
    gnd_net = Net(name=gnd_name, nodes=[NetNode(power_des, "GND")])

    for des in consumers:
        vdd_net.nodes.append(NetNode(des, "VDD"))
        gnd_net.nodes.append(NetNode(des, "GND"))

    return [vdd_net, gnd_net]


def _make_i2c_nets(master_des: str, slave_des_list: List[str]) -> List[Net]:
    sda = Net(name="I2C_SDA", nodes=[NetNode(master_des, "SDA")])
    scl = Net(name="I2C_SCL", nodes=[NetNode(master_des, "SCL")])
    for des in slave_des_list:
        sda.nodes.append(NetNode(des, "SDA"))
        scl.nodes.append(NetNode(des, "SCL"))
    return [sda, scl]


def _make_spi_nets(master_des: str, slave_des: str, idx: int) -> List[Net]:
    return [
        Net(f"SPI_MOSI", [NetNode(master_des, "MOSI"), NetNode(slave_des, "MOSI")]),
        Net(f"SPI_MISO", [NetNode(master_des, "MISO"), NetNode(slave_des, "MISO")]),
        Net(f"SPI_SCK",  [NetNode(master_des, "SCK"),  NetNode(slave_des, "SCK")]),
        Net(f"SPI_CS{idx}", [NetNode(master_des, f"CS{idx}"), NetNode(slave_des, "CS")]),
    ]


def _make_uart_nets(master_des: str, slave_des: str) -> List[Net]:
    return [
        Net("UART_TX", [NetNode(master_des, "TX"), NetNode(slave_des, "RX")]),
        Net("UART_RX", [NetNode(master_des, "RX"), NetNode(slave_des, "TX")]),
    ]


def _make_pwm_net(master_des: str, slave_des: str, ch: int) -> List[Net]:
    return [Net(f"PWM_CH{ch}", [NetNode(master_des, f"PWM{ch}"), NetNode(slave_des, "PWM_IN")])]


# ──────────────────────────────────────────────────────────────────────────────
# Graph builder
# ──────────────────────────────────────────────────────────────────────────────

def build_graph(
    selected_map: Dict[str, str],
    subsystems: List[Subsystem],
    components: Dict[str, Component],
) -> Tuple[List[SchematicComponent], List[Net]]:
    sch_comps: List[SchematicComponent] = []
    nets:       List[Net]               = []
    des_map:    Dict[str, str]          = {}   # subsystem_name → designator

    # Assign designators
    for i, sub in enumerate(subsystems, 1):
        pn = selected_map.get(sub.name)
        if not pn:
            continue
        des = _designator(sub.name, i)
        des_map[sub.name] = des
        comp = components.get(pn)
        qty = max(1, int(getattr(sub, "quantity", 1) or 1))
        value = pn
        if comp:
            value = f"{comp.part_number} ({comp.voltage_max}V)"
        if qty > 1:
            value = f"{value} x{qty}"
        sch_comps.append(SchematicComponent(
            designator=des,
            part_number=pn,
            value=value,
            position=(float((i % 5) * 30), float((i // 5) * 30)),
        ))

    # Find MCU/SBC (bus master)
    master_sub = next(
        (s for s in subsystems if s.category in ("MCU", "SBC") and s.name in des_map),
        None,
    )
    master_des = des_map.get(master_sub.name) if master_sub else None

    # Find power sub
    power_sub = next(
        (s for s in subsystems if s.category == "POWER" and s.name in des_map), None
    )
    power_des = des_map.get(power_sub.name) if power_sub else None

    # Power nets: connect power component to everything else
    if power_des:
        consumers = [des for name, des in des_map.items() if des != power_des]
        v_rail = 3.3
        if power_sub:
            v_rail = power_sub.voltage_max
        nets.extend(_make_power_nets(power_des, consumers, v_rail))

    # Signal nets: connect each subsystem to bus master by its declared interface
    if master_des:
        i2c_slaves:  List[str] = []
        spi_idx = 0
        uart_idx = 0
        pwm_idx  = 0

        for sub in subsystems:
            if sub.name not in des_map:
                continue
            des = des_map[sub.name]
            if des == master_des or des == power_des:
                continue

            iface = sub.interface.upper()
            if iface == "I2C":
                i2c_slaves.append(des)
            elif iface == "SPI":
                nets.extend(_make_spi_nets(master_des, des, spi_idx))
                spi_idx += 1
            elif iface == "UART":
                nets.extend(_make_uart_nets(master_des, des))
                uart_idx += 1
            elif iface == "PWM":
                nets.extend(_make_pwm_net(master_des, des, pwm_idx))
                pwm_idx += 1
            else:
                # GPIO fallback
                nets.append(Net(
                    f"GPIO_{des}",
                    [NetNode(master_des, f"IO{pwm_idx}"), NetNode(des, "IO0")],
                ))
                pwm_idx += 1

        if i2c_slaves:
            nets.extend(_make_i2c_nets(master_des, i2c_slaves))

    # Deduplicate net names
    seen_names: Set[str] = set()
    unique_nets: List[Net] = []
    for n in nets:
        name = n.name
        if name in seen_names:
            name = f"{name}_{len(unique_nets)}"
        seen_names.add(name)
        unique_nets.append(Net(name=name, nodes=n.nodes))

    return sch_comps, unique_nets


# ──────────────────────────────────────────────────────────────────────────────
# Stage entry point
# ──────────────────────────────────────────────────────────────────────────────

def run(state: DesignState) -> StageResult:
    t0 = time.monotonic()
    issues: List[Issue] = []

    if state.architecture is None:
        return StageResult(
            stage="p10_schematic_graph",
            status=StageStatus.FAILED,
            issues=[Issue("SCH_NO_ARCH", Severity.ERROR,
                          "Architecture not available.", "schematic_graph")],
            duration=time.monotonic() - t0,
        )

    sel_result = state.stage_results.get("p08_part_selection")
    if sel_result is None:
        return StageResult(
            stage="p10_schematic_graph",
            status=StageStatus.FAILED,
            issues=[Issue("SCH_NO_PARTS", Severity.ERROR,
                          "Part selection must run first.", "schematic_graph")],
            duration=time.monotonic() - t0,
        )

    selected_map: Dict[str, str] = sel_result.data.get("selected", {})

    try:
        sch_comps, nets = build_graph(
            selected_map,
            state.architecture.subsystems,
            state.components,
        )

        req_name = state.requirements.name if state.requirements else "Untitled"
        schematic = Schematic(
            components=sch_comps,
            nets=nets,
            title=req_name,
            revision=f"v{state.iteration + 1}.0",
        )
        state.schematic = schematic

        if not sch_comps:
            issues.append(Issue("SCH_EMPTY", Severity.ERROR,
                                "Schematic graph has no components.", "schematic_graph"))

        has_errors = any(i.is_error() for i in issues)
        return StageResult(
            stage="p10_schematic_graph",
            status=StageStatus.FAILED if has_errors else StageStatus.PASSED,
            data={
                "component_count": len(sch_comps),
                "net_count":       len(nets),
            },
            metrics={
                "component_count": float(len(sch_comps)),
                "net_count":       float(len(nets)),
            },
            issues=issues,
            duration=time.monotonic() - t0,
        )

    except Exception as exc:
        return StageResult(
            stage="p10_schematic_graph",
            status=StageStatus.FAILED,
            issues=[Issue("SCH_EXCEPTION", Severity.ERROR,
                          f"Schematic graph builder crashed: {exc}", "schematic_graph")],
            duration=time.monotonic() - t0,
        )
