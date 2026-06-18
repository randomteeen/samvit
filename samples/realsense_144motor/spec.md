# Samvit — Hardware Spec

## What You're Building

A body-worn device that takes in live depth data from two sensors, builds a spatial heatmap of the environment, and communicates it to the user through a 144-motor vibration grid and audio tones — replacing vision for navigation in real time.

---

## Sensors

**Use: Intel RealSense D435i (x2)**
- One forward-facing, one angled upward ~30°
- Forward sensor covers ground to chest height
- Angled sensor covers chest to top of user's head
- Both connect via USB to the processing unit
- Python library: `pyrealsense2` — gives depth frames directly, well documented, widely used

---

## Processing Unit

**Use: Raspberry Pi 4 (4GB)**
- Runs Python natively
- Has enough USB ports for both RealSense sensors
- Supports PWM control, I2C, and WiFi all on one board
- This is where the heatmap processing and all signal logic runs
- No additional compute needed

---

## Vibration Grid (144 Motors)

**Motors: ERM (Eccentric Rotating Mass) coin vibration motors**
- Small, flat, lightweight — suitable for body-worn use
- Arranged in a 12×12 grid worn on the torso (front or vest-style)
- The grid maps directly to the heatmap — each motor corresponds to a zone in the user's environment
- Closer obstacle = stronger vibration in that motor's position on the grid

**Driver: PCA9685 PWM Driver boards (x9, chained via I2C)**
- Each PCA9685 drives 16 motors
- 9 boards chained together = 144 motors
- All controlled over a single I2C connection from the Raspberry Pi
- Python library: `adafruit-circuitpython-pca9685` — lets you set any motor's intensity in one line

---

## Audio Output

**Use: Any small speaker via MAX98357A I2S amplifier**
- Handles directional audio tones for destination guidance
- Connects directly to Raspberry Pi GPIO
- Python library: `sounddevice` — generate and play tones programmatically in real time

---

## Communication with Software Layer

**Use: Raspberry Pi's built-in WiFi**
- The Jarvis voice AI sends destination coordinates over local WiFi to the Pi
- No extra hardware needed — Pi handles this natively
- On the software side, a simple socket connection receives the coordinates

---

## Success Bar

- Full 144-motor grid responds to environment in under 50ms
- Both sensor zones (lower + upper) detected and mapped independently
- User can distinguish obstacle direction and height from vibration position alone
- Device runs for 4+ hours on battery
