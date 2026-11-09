# bench

Readers for lab instruments, so agents can log measurements during hardware work.
Nothing here runs on the head or in the brain; it is tooling for the bench.

## Motion test (CLI)

Flashes the head, drives it through a fixed sequence (center, look left/right/up/down, blink,
center, fail-safe) and judges it from the camera and the supply current.

### Rig setup

- **Camera:** Logitech C922 looking at the eyes, fixed in place, with steady lighting.
- **ESP32:** connected over USB (flashing and serial log). The head must have Wi-Fi configured
  in `firmware/eyes_esp32/include/secrets.h` (gitignored).
- **Extech 383273:** in series with the **servo supply**, set to a DC current range (`DCA`),
  RS-232 to USB adapter to the PC. Use `--no-meter` only when there is no meter: without it
  nothing stops the test when a servo stalls.
- `pip install -e '.[bench]'`, and your user in the `video` and `dialout` groups
  (`sudo usermod -aG video,dialout $USER`, then log in again).

### Run

```sh
python -m bench.motion_test --port /dev/ttyUSB0 --meter-port /dev/ttyUSB1 --stall-amps 0.8
python -m bench.motion_test --port /dev/ttyUSB0 --no-meter --stall-amps 0.8 --skip-flash
```

- `--port` ESP32 serial port (required). `--stall-amps` abort threshold in amps (required, no
  default; pick a value just above the normal running current of your supply).
- Exactly one of `--meter-port` or `--no-meter` is required.
- `--host` head address. Default: `$EYES_HOST`, else the IP printed on the serial log
  (`[wifi] connected, IP ...`). With `--skip-flash` the board has already booted, so the IP line
  may be missed: reset the board, or pass `--host`.
- `--camera` (default `/dev/video0`), `--width --height --fps --fourcc --exposure --gain
  --focus --wb-temperature --warmup-frames`: see `bench.camera.CameraSettings`.
- `--skip-flash`, `--limits` (default `config/limits.yaml`), `--out`
  (default `bench/out/<UTC timestamp>/`, gitignored).

**`config/limits.yaml` is human-owned.** The test only reads it. Never edit it to make a test
pass; calibrate on the bench and let a human change it.

Exit codes: `0` passed, `1` failed or aborted (including overcurrent and meter errors),
`2` setup error (bad arguments, flash failure, no IP, camera or meter not usable).
A current over `--stall-amps` sends `center`, stops sending and aborts the remaining steps.

### Report

`<out>/report.json` (the head's address is never written) and key frames in `<out>/frames/`.
Top level: `schema_version`, `started_at`, `camera_settings`, `command_timeout_ms`,
`stall_amps`, `noise_threshold`, `steps`, `passed`, `aborted` (`null` or a reason such as
`overcurrent`). Each step has `name`, `command`, `t_sent`, `status` (`passed`, `failed` or
`skipped`), `checks`, `latency_s`, `settle_time_s`, `max_amps`, `key_frames`.

### Reading a failure

1. Look at `aborted` first. `overcurrent` means a servo stalled or the stall limit is too low;
   later steps are `skipped`.
2. Find the first step with `status: "failed"` and the `false` entries in its `checks`:
   `moved` (the image did not change: wiring, power, wrong host or key), `settled` (still moving
   at the end of the window), `current_ok` (max current above `--stall-amps`),
   `returned_to_reference` (blink did not return to the starting picture) and
   `returned_to_neutral` (the fail-safe did not bring the eyes back to neutral).
3. Open the PNGs listed in `key_frames` to see what the camera saw.
4. The `failsafe` step may also have `evidence.serial_failsafe_enter`: whether the serial log
   showed `[failsafe] ENTER`.

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

## Firmware flasher and serial log

`bench/flash.py` uploads `firmware/eyes_esp32` with `pio run -d <dir> -t upload`
(`--upload-port <port>` when given). It refuses to run unless `include/secrets.h` exists (copy
`include/secrets.h.example`); it never reads that file. Failures and timeouts (180 s) raise
`FlashError` carrying the last 50 output lines.

`bench/serial_log.py` reads the ESP32 log on a background thread (needs pyserial,
`pip install -e '.[bench]'`; `import bench.serial_log` does not).

```python
from bench.flash import flash
from bench.serial_log import SerialLog

flash(port="/dev/ttyUSB0")                      # close any SerialLog on this port first
with SerialLog("/dev/ttyUSB0") as log:          # then open a new one
    log.wait_for(r"\[wifi\] connected", timeout_s=20)
    print(log.ip_address(), log.failsafe_events())
```

**The USB port is shared:** close `SerialLog` before `flash()` and reopen it afterwards.
