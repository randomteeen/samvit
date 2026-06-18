# RealSense 144-motor wearable — verified pipeline output

Generated output of a full `main.py` run on the body-worn depth-navigation spec
(`spec.md`): 2× Intel RealSense D435i, Raspberry Pi 4, a 12×12 (144) ERM
vibration grid driven by 9× PCA9685 over I2C, a MAX98357A audio amp, and battery
power. The run used a live Gemini architecture plan (no heuristic fallback).

This is the output produced *after* the architecture-fidelity fix (spec-derived
fallback + per-subsystem quantities). It demonstrates that the design now
captures the real spec rather than collapsing to a 5-block / 7-part stub.

## What the design contains

| Subsystem | Part | Qty |
|---|---|---|
| central_processing_unit | Raspberry Pi 4B | 1 |
| depth_sensor_array | Intel RealSense D435 | 2 |
| pwm_driver_array | (I2C interface part) | 9 |
| haptic_actuators | ERM Vibration Motor 10mm | 144 |
| audio_amplifier / transducer | PAM8403 | 2 |
| power_distribution_network | AMS1117-3.3 | 1 |

Full parts list with quantities and costs is in `bom.csv` (159 units across the
real subsystems; the architecture also recorded the correct quantities
144 / 9 / 2 / 1). Compare to the earlier broken run, which produced 7 parts, an
ESP32 instead of a Pi, and zero motors.

## Known limitations (not slop, but not fab-ready either)

- **PCB layout is subsystem-level**, not 144 placed footprints: each subsystem is
  one designator and the motor row is annotated `… x144` in the netlist/BOM. The
  gerbers/drill therefore represent the interconnect skeleton, not 144 routed
  motor sites.
- **The driver resolved to an I2C level-shifter part**, not a PCA9685 — the
  offline parts DB has no PCA9685 entry (parts-coverage gap, not an architecture
  bug).
- **Several repair-added parts are `[repair-ghost]` passive placeholders** where
  the part search could not resolve a real component.
- **The design does not meet its own power/thermal/battery targets**
  (≈44 W, 0.18 h battery, 223 °C). 144 ERM motors at ~80 mA each is ≈11.5 A — a
  4-hour battery runtime at that draw is physically implausible. The repair loop
  correctly reports it "cannot move the remaining metrics".

Treat this as a complete architecture + BOM that an engineer must review and
re-power-budget before fabrication.
