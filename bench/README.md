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
