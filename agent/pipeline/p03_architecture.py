"""
Stage 3 — System Architecture Planner
======================================
Uses Gemini to decompose the HardwareRequirements into a set of
Subsystems (power, sensing, compute, feedback, comms, …).

This is one of the four Gemini call points in the pipeline.
The prompt is deliberately large so that the model can:
  1. Identify all required subsystems
  2. Estimate voltages, currents, and interfaces
  3. Flag any ambiguous or missing requirements
  4. Produce a short design rationale

All of this happens in a SINGLE Gemini call, minimising API usage.

Output
------
  state.architecture set
  StageResult.data["subsystems"] = list of subsystem dicts
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from agent.core.models import (
    DesignState, Issue, Severity, StageResult, StageStatus,
    Subsystem, SystemArchitecture,
)

SYSTEM_PROMPT = """You are a senior embedded-hardware systems architect with 20 years of experience
designing wearable and IoT devices. You communicate in precise, machine-readable JSON only.
Never output prose outside the JSON block."""

PLAN_PROMPT_TEMPLATE = """
=== HARDWARE ARCHITECTURE PLANNING REQUEST ===

Project name : {name}
Description  : {description}
Goals        : {goals}
Budget (USD) : {budget}
Form factor  : {form_factor}
Power source : {power_source}
Op. voltage  : {voltage}
Environment  : {environment}
Success criteria: {criteria}

=== YOUR TASK (all in one response) ===

1. Decompose the project into hardware subsystems. Create a SEPARATE subsystem
   entry for EACH distinct part type — never merge different parts into one
   entry (e.g. vibration motors, the motor-driver ICs that drive them, and an
   audio amplifier are THREE separate subsystems, not one "feedback" block).
2. For EACH subsystem specify:
   - name         : short identifier (snake_case)
   - role         : one-sentence description
   - category     : one of [MCU, SBC, POWER, SENSOR, ACTUATOR, COMMS, AUDIO,
                            DISPLAY, MEMORY, INTERFACE, PASSIVE, PROTECTION]
   - voltage_min  : minimum supply voltage (V, float, must be finite)
   - voltage_max  : maximum supply voltage (V, float, must be finite)
   - current_ma   : estimated peak current draw of ONE unit (mA, float, finite)
   - interface    : primary bus this subsystem exposes (I2C, SPI, UART, USB, GPIO, PWM, …)
   - priority     : 1 = must-have, 2 = nice-to-have
   - quantity     : how many identical units are required (int >= 1). If the
                    spec calls for repeated parts (e.g. "144 motors",
                    "9 driver boards", "two cameras"), put that count here
                    instead of inventing one subsystem per unit.
   - notes        : any design constraints or special requirements
3. Estimate total power budget (mW). It MUST account for quantity
   (sum of current_ma * voltage * quantity across subsystems, with headroom).
4. List any requirements that are ambiguous or missing.

=== OUTPUT FORMAT ===

Respond with ONLY this JSON (no markdown fences, no extra text):

{{
  "subsystems": [
    {{
      "name":        "power_management",
      "role":        "Regulate and distribute power from battery to all rails",
      "category":    "POWER",
      "voltage_min": 3.0,
      "voltage_max": 5.5,
      "current_ma":  500.0,
      "interface":   "GPIO",
      "priority":    1,
      "quantity":    1,
      "notes":       "Must support LiPo single-cell charging"
    }}
  ],
  "power_budget_mw": 1500.0,
  "notes": "Free-form architect notes",
  "ambiguities": ["list", "of", "unclear", "requirements"]
}}
"""


# ──────────────────────────────────────────────────────────────────────────────
# JSON parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Dict[str, Any]:
    """Extract JSON from a model response that may contain extra text."""
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    # Find outermost { … }
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in model response.")
    return json.loads(text[start:end])


def _parse_architecture(raw: Dict[str, Any]) -> SystemArchitecture:
    subsystems: List[Subsystem] = []
    for s in raw.get("subsystems", []):
        subsystems.append(Subsystem(
            name=s.get("name", "unknown"),
            role=s.get("role", ""),
            category=s.get("category", "PASSIVE"),
            voltage_min=float(s.get("voltage_min", 0.0)),
            voltage_max=float(s.get("voltage_max", 5.0)),
            current_ma=float(s.get("current_ma", 100.0)),
            interface=s.get("interface", "GPIO"),
            priority=int(s.get("priority", 1)),
            notes=s.get("notes", ""),
            quantity=max(1, int(s.get("quantity", 1) or 1)),
        ))
    return SystemArchitecture(
        subsystems=subsystems,
        power_budget_mw=float(raw.get("power_budget_mw", 0.0)),
        notes=raw.get("notes", ""),
        raw_plan=json.dumps(raw, indent=2),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Fallback: heuristic planner (no Gemini)
#
# When Gemini is unavailable we must NOT collapse the spec into a fixed generic
# template — that throws the user's actual requirements away (and produces an
# "empty" design with none of the requested parts). Instead we read the
# requirements text and derive subsystems + quantities from it, only falling
# back to a minimal generic skeleton if nothing recognisable is found.
# ──────────────────────────────────────────────────────────────────────────────

# Last-resort skeleton when the spec text yields nothing recognisable.
_GENERIC_SUBSYSTEMS = [
    Subsystem("power_management", "Power regulation and charging", "POWER",
              3.0, 5.5, 500, "GPIO", 1, "LiPo single-cell"),
    Subsystem("microcontroller", "Main control logic", "MCU",
              3.0, 3.6, 150, "GPIO", 1, ""),
    Subsystem("sensing", "Environmental / spatial sensing", "SENSOR",
              1.8, 5.0, 200, "I2C", 1, ""),
    Subsystem("feedback_output", "Haptic or audio feedback", "ACTUATOR",
              3.0, 5.0, 300, "I2C", 1, ""),
    Subsystem("wireless_comms", "Bluetooth / BLE link", "COMMS",
              1.7, 3.6, 50, "UART", 2, ""),
]

# Each family: keywords to look for, the subsystem template to emit, and the
# counting nouns that legitimately denote a quantity OF THIS family (so e.g.
# "drives 16 motors" near a PWM driver counts toward motors, not drivers).
# (kw_list, name, role, category, vmin, vmax, current_ma_per_unit, interface,
#  priority, nouns_regex)
_SPEC_FAMILIES = [
    (["realsense", "depth camera", "depth sensor", "stereo camera", "rgb-d", "lidar"],
     "depth_sensor", "Spatial / depth sensing", "SENSOR", 4.75, 5.25, 700.0, "USB", 1,
     r"cameras?|sensors?|modules?"),
    (["raspberry pi", "rpi", "jetson", "single board", "single-board", "sbc", "compute module"],
     "compute", "Main compute / processing unit", "SBC", 4.75, 5.25, 2500.0, "GPIO", 1,
     r"boards?|units?"),
    (["esp32", "stm32", "atmega", "arduino", "microcontroller", "mcu"],
     "microcontroller", "Microcontroller / control logic", "MCU", 3.0, 3.6, 150.0, "GPIO", 1,
     r"mcus?|units?"),
    (["pca9685", "pwm driver", "servo driver", "motor driver", "driver board", "driver ic"],
     "motor_driver", "PWM / motor driver", "ACTUATOR", 3.0, 5.5, 25.0, "I2C", 1,
     r"boards?|drivers?|ics?|modules?"),
    (["erm", "vibration motor", "coin motor", "haptic motor", "lra", "vibration grid", "motor"],
     "vibration_motors", "Haptic vibration actuators", "ACTUATOR", 2.5, 5.0, 80.0, "PWM", 1,
     r"motors?"),
    (["max98357", "i2s amp", "audio amp", "speaker amp", "class-d", "amplifier"],
     "audio_amp", "Audio amplifier", "AUDIO", 2.5, 5.5, 300.0, "I2S", 1,
     r"amplifiers?|amps?"),
    (["speaker", "buzzer"],
     "speaker", "Audio output transducer", "AUDIO", 2.5, 5.0, 200.0, "GPIO", 2,
     r"speakers?|buzzers?"),
    (["microphone", "i2s mic", "inmp441"],
     "microphone", "Audio input", "SENSOR", 1.6, 3.6, 2.0, "I2S", 2,
     r"microphones?|mics?"),
    (["imu", "accelerometer", "gyroscope", "mpu-6050", "mpu6050", "icm-"],
     "imu", "Inertial measurement", "SENSOR", 1.8, 3.6, 5.0, "I2C", 2,
     r"imus?|sensors?"),
    (["wifi", "wi-fi", "bluetooth", "ble", "lora", "lte", "wireless", "socket"],
     "wireless_comms", "Wireless connectivity", "COMMS", 1.7, 3.6, 250.0, "UART", 1,
     r"modules?|radios?"),
    (["tp4056", "bq24", "charger", "charging", "charge ic"],
     "battery_charger", "Battery charging", "POWER", 4.0, 5.5, 500.0, "GPIO", 1,
     r"chargers?|ics?"),
    (["buck", "boost", "ldo", "regulator", "pmic", "power distribution", "power management", "power supply"],
     "power_management", "Power regulation and distribution", "POWER", 3.0, 5.5, 500.0, "GPIO", 1,
     r"regulators?|rails?|ics?"),
    (["18650", "li-ion", "lithium", "lipo", "battery pack", "battery", "cell"],
     "battery", "Energy storage", "Battery", 3.0, 4.2, 0.0, "GPIO", 1,
     r"batteries|cells?|packs?"),
]

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
}

_WORDNUM_RE = "|".join(_WORD_NUMBERS)


def _count_near(text: str, keyword: str, nouns: str, default: int = 1) -> int:
    """Find an explicit quantity associated with `keyword`, conservatively.

    Only counts STRONG signals so digits inside part numbers (D435i, PCA9685,
    MAX98357A, "Pi 4") are never mistaken for quantities:
      * an explicit multiplier marker:  "x9", "(x9)", "×2"
      * a number/word immediately before the keyword:  "144-motor", "9 PCA9685"
      * a number/word directly attached to one of this family's counting
        `nouns`:  "144 motors" for the motor family
    Returns the largest such count found, or `default` if none.
    """
    best = 0
    for m in re.finditer(re.escape(keyword), text):
        lo = max(0, m.start() - 30)
        hi = min(len(text), m.end() + 30)
        window = text[lo:hi]
        before = text[lo:m.start()]
        cands: List[int] = []

        # a. explicit multiplier marker, not glued inside a word/number
        cands += [int(n) for n in re.findall(r"(?<![a-z0-9])[x×]\s?(\d{1,4})", window)]
        # b. number or number-word immediately before the keyword
        mb = re.search(r"(\d{1,4})[\s-]{0,3}$", before)
        if mb:
            cands.append(int(mb.group(1)))
        mw = re.search(rf"\b({_WORDNUM_RE})\s+$", before)
        if mw:
            cands.append(_WORD_NUMBERS[mw.group(1)])
        # c. number (or word) directly attached to one of THIS family's nouns
        cands += [int(n) for n in re.findall(rf"(\d{{1,4}})[\s-]{{0,3}}(?:{nouns})", window)]
        for wd, n in _WORD_NUMBERS.items():
            if re.search(rf"\b{wd}\s+(?:{nouns})", window):
                cands.append(n)

        if cands:
            best = max(best, max(cands))
    return best if best > 0 else default


def _spec_derived_fallback(req: "HardwareRequirements") -> SystemArchitecture:
    """Derive subsystems + quantities directly from the requirements text."""
    parts: List[str] = [req.description or "", req.raw_text or ""]
    parts.extend(req.goals or [])
    parts.extend(req.success_criteria or [])
    text = "\n".join(parts).lower()

    subsystems: List[Subsystem] = []
    used_names: set = set()
    for kws, name, role, cat, vmin, vmax, cur, iface, prio, nouns in _SPEC_FAMILIES:
        hit = next((kw for kw in kws if kw in text), None)
        if hit is None or name in used_names:
            continue
        qty = _count_near(text, hit, nouns, default=1)
        # Power rails, compute boards, MCUs and the battery are treated as
        # singular in the heuristic plan — only the explicitly arrayed parts
        # (motor drivers, motors, sensors, amps) scale up. A real Gemini plan
        # can override this when it is available.
        if cat in ("POWER", "SBC", "Battery", "MCU"):
            qty = 1
        subsystems.append(Subsystem(
            name=name, role=role, category=cat,
            voltage_min=vmin, voltage_max=vmax, current_ma=cur,
            interface=iface, priority=prio, notes="spec-derived", quantity=qty,
        ))
        used_names.add(name)

    if not subsystems:
        # Nothing recognisable — fall back to the minimal generic skeleton, but
        # still compute a sensible budget below.
        subsystems = [Subsystem(**vars(s)) for s in _GENERIC_SUBSYSTEMS]
        note = ("Generic fallback skeleton (Gemini unavailable and no recognisable "
                "parts in the spec).")
    else:
        note = ("Spec-derived heuristic plan (Gemini unavailable). Subsystems and "
                "quantities were extracted from the requirements text.")

    budget = sum(s.current_ma * s.voltage_max * max(1, s.quantity)
                 for s in subsystems if s.category not in ("POWER", "Battery"))
    budget = max(budget * 1.3, 1000.0)  # 30% headroom, floor 1 W

    return SystemArchitecture(
        subsystems=subsystems,
        power_budget_mw=round(budget, 1),
        notes=note,
        raw_plan="",
    )


def _fallback_architecture(req: "HardwareRequirements") -> SystemArchitecture:
    return _spec_derived_fallback(req)


# ──────────────────────────────────────────────────────────────────────────────
# Stage entry point (async because it calls Gemini)
# ──────────────────────────────────────────────────────────────────────────────

async def run_async(
    state: DesignState,
    gemini_manager: Any,
) -> StageResult:
    t0 = time.monotonic()
    issues: List[Issue] = []

    if state.requirements is None:
        return StageResult(
            stage="p03_architecture",
            status=StageStatus.FAILED,
            issues=[Issue("ARCH_NO_REQ", Severity.ERROR,
                          "Requirements not set before architecture stage.", "architecture")],
            duration=time.monotonic() - t0,
        )

    req = state.requirements

    prompt = PLAN_PROMPT_TEMPLATE.format(
        name=req.name,
        description=req.description,
        goals="\n  - " + "\n  - ".join(req.goals) if req.goals else "(none specified)",
        budget=f"${req.budget_usd:.2f}" if req.budget_usd else "unspecified",
        form_factor=req.form_factor or "unspecified",
        power_source=req.power_source or "unspecified",
        voltage=f"{req.operating_voltage}V" if req.operating_voltage else "unspecified",
        environment=req.environment or "unspecified",
        criteria="\n  - " + "\n  - ".join(req.success_criteria) if req.success_criteria else "(none)",
    )

    raw_response = ""
    arch: Optional[SystemArchitecture] = None

    try:
        raw_response = await gemini_manager.call_gemini(
            prompt=prompt,
            task="heavy",
            system_instruction=SYSTEM_PROMPT,
            temperature=0.15,
        )
        raw_dict = _extract_json(raw_response)
        arch = _parse_architecture(raw_dict)

        ambiguities = raw_dict.get("ambiguities", [])
        for amb in ambiguities:
            issues.append(Issue(
                code="ARCH_AMBIGUITY",
                severity=Severity.WARNING,
                message=f"Ambiguous requirement: {amb}",
                source="architecture",
            ))

    except Exception as exc:
        issues.append(Issue(
            code="ARCH_GEMINI_ERROR",
            severity=Severity.WARNING,
            message=f"Gemini call failed ({exc}). Using fallback heuristic plan.",
            source="architecture",
        ))
        arch = _fallback_architecture(req)

    if not arch.subsystems:
        issues.append(Issue(
            code="ARCH_NO_SUBSYSTEMS",
            severity=Severity.ERROR,
            message="Architecture planner produced no subsystems.",
            source="architecture",
        ))
        return StageResult(
            stage="p03_architecture",
            status=StageStatus.FAILED,
            issues=issues,
            duration=time.monotonic() - t0,
        )

    state.architecture = arch
    has_errors = any(i.is_error() for i in issues)

    return StageResult(
        stage="p03_architecture",
        status=StageStatus.FAILED if has_errors else StageStatus.PASSED,
        data={
            "subsystems":     [vars(s) for s in arch.subsystems],
            "power_budget_mw": arch.power_budget_mw,
            "notes":          arch.notes,
        },
        issues=issues,
        metrics={"subsystem_count": float(len(arch.subsystems))},
        duration=time.monotonic() - t0,
    )


def run(state: DesignState, gemini_manager: Any) -> StageResult:
    """Synchronous wrapper for use outside an event loop."""
    return asyncio.run(run_async(state, gemini_manager))
