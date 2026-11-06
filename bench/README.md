# bench

Readers for lab instruments, so agents can log measurements during hardware work.
Nothing here runs on the head or in the brain; it is tooling for the bench.

## Extech 383273 multimeter

`bench/instruments/extech_383273.py` polls the meter over RS-232 and prints readings.
First use: spotting servo stall current during limit calibration (a servo pushed against
its mechanical stop draws a current spike).

### Install

```sh
pip install -e '.[bench]'     # adds pyserial; it is not a core dependency
```

### Hardware

- USB-RS232 adapter with an **FTDI** chip and a **male DB9** connector.
  The meter needs RTS held low (negative RS-232 voltage) to transmit; the reader does this,
  but some cheap adapters don't drive RTS properly.
- **Enable COMM on the meter.** Older units power up with the serial port off; turn on the
  COMM function (see the meter manual) or the meter stays silent.

### Run

```sh
python3 -m bench.instruments.extech_383273 /dev/ttyUSB0            # human-readable
python3 -m bench.instruments.extech_383273 /dev/ttyUSB0 --jsonl    # one JSON object per line
python3 -m bench.instruments.extech_383273 /dev/ttyUSB0 --rate 4 --count 100
```

`--jsonl` prints one object per reading on stdout:

```json
{"t": 1762400000.12, "function": "DCA 200mA", "unit": "mA", "value": 18.4, "display": "018.4", "raw": "02 22 ..."}
```

- `value` is `null` for overrange or any undecoded special code (`display` is `"OL/special"`).
- Errors (timeouts, bad frames) go to stderr as `{"error": "..."}`, so stdout stays parseable.

### Protocol notes

9600 8N1, polled by sending a single SPACE (0x20). The bytes `9 4 2 1 E G N R W U S T X`
are reserved (they change baud rate or drive the data-logger memory) and are never sent.
Full frame layout is in the module docstring.

### Known gaps

- The function/range table was transcribed from a photo of the manual and needs
  verification (pages 11-12).
- Byte-3 special codes (manual section 3.5) are not decoded; they report `value = null`.

## Camera (Logitech C922)

`bench/camera.py` captures frames of the eye mechanism with auto exposure, autofocus and
auto white balance off, so frames are comparable between runs. Needs OpenCV
(`pip install -e '.[bench]'`); `import bench.camera` itself does not.

```python
from bench.camera import CameraSettings, OpenCVCamera

with OpenCVCamera(CameraSettings(exposure=200, focus=40)) as cam:
    print(cam.applied_settings())   # values read back from the driver
    frame = cam.read()              # Frame(index, t, image: uint8 HxWx3 BGR)
```

- Opens `/dev/video0` with the V4L2 backend at 640x480 MJPG @ 30 fps by default; the manual
  defaults (exposure, gain, focus, white balance) are documented on `CameraSettings`.
- Every property is read back after setting. Any that did not take raise `CameraError`
  (`strict=False` prints a warning instead). `v4l2-ctl -d /dev/video0 -L` shows valid ranges.
- The first `warmup_frames` (30) frames are discarded while the sensor settles.
- `FakeCamera(source, clock)` is a deterministic stand-in for tests; `source` is a list of
  images or a callable `(t) -> image`.

## Motion detector

`bench/motion.py` decides from camera frames whether the eye moved after a command, when it
started and when it settled. It uses numpy only (no OpenCV, no I/O, no randomness), so it is
deterministic and tested on synthetic frames.

```python
from bench.motion import NoiseFloor, analyze, matches_reference

nf = NoiseFloor.from_frames(still_frames)             # threshold = max(1.0, mean + 4 * std)
r = analyze(frames, command_t, nf.threshold)          # frames: list of bench.camera.Frame
r.moved, r.latency_s, r.settled, r.settle_time_s, r.peak_score
ok, score = matches_reference(image, neutral_image, nf.threshold)
```

- Frames are converted to grayscale with fixed integer weights, optionally cropped to
  `roi=(x, y, w, h)` and downscaled by block mean (2x by default). Measure the noise floor
  with the same `roi` and `downscale` that you pass to `analyze`.
- Each score is stamped with the later frame's `t`. Motion before `command_t` is ignored.
- `settled_t` is the first frame of a still run lasting `settle_window_s` (0.2 s). If that run
  starts after `command_t + timeout_s` (2 s), `settled` is False.
