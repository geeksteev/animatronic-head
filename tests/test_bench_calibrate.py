"""Tests for bench.calibrate against a simulated single-servo rig. No hardware."""

from types import SimpleNamespace

import pytest
import yaml

from bench.calibrate import REPO_CONFIG_DIR, CalibrationError, check_out_path, main, run_calibration
from bench.png import PNG_SIGNATURE
from tests.bench_fakes import KEY, ServoRig

LIMITS_FILE = REPO_CONFIG_DIR / "limits.yaml"
# eye_pan in config/limits.yaml: channel 0, neutral 90; limits are [60, 120] at the time of writing.
SERVO = "eye_pan"


def _factories(rig):
    return {
        "camera": lambda settings: SimpleNamespace(open=lambda: None, close=lambda: None),
        "meter": lambda port: rig.meter,
        "recorder": lambda cam, meter, settings: rig.recorder,
        "sock": lambda: rig.sock,
        "key": lambda: KEY,
        "waiter": rig.waiter,
    }


def _argv(tmp_path, *extra, hard_min="0", hard_max="180", out=None):
    return [SERVO, "--hard-min", hard_min, "--hard-max", hard_max, "--stall-amps", "1.0",
            "--meter-port", "/dev/ttyFAKE", "--host", "head.invalid",
            "--out", str(out or tmp_path / "proposal.yaml"), *extra]


def _run(rig, tmp_path, **kw):
    args = dict(hard_min=0.0, hard_max=180.0, stall_amps=1.0, out=tmp_path / "proposal.yaml",
                waiter=rig.waiter)
    args.update(kw)
    return run_calibration(rig.client, rig.recorder, rig.limits, SERVO, **args)


def _bound(rig, hard_min=0.0, hard_max=180.0):
    lim = rig.limits[SERVO]
    return max(hard_min, lim.min), min(hard_max, lim.max)


def test_image_stop_at_115_proposes_110(tmp_path):
    rig = ServoRig(max_stop=115, min_stop=70, gain=0.0)  # no current rise: image check only
    p = _run(rig, tmp_path, hard_max=140.0, hard_min=0.0)
    assert p["max"]["stop_found"] is True and p["max"]["reason"] == "image"
    assert p["max"]["stop_angle"] == 115
    assert p["proposed_max"] == 110
    assert p["min"]["stop_found"] is True and p["min"]["reason"] == "image"
    assert p["min"]["stop_angle"] == 70
    assert p["proposed_min"] == 75
    assert p["neutral"] == 90 and p["servo"] == SERVO and p["channel"] == 0
    assert rig.sock.closed


def test_current_spike_stops_early_and_backs_off(tmp_path):
    rig = ServoRig(max_stop=105, gain=0.5)  # 1 degree past the stop: 0.6 A
    p = _run(rig, tmp_path, spike_amps=0.3, stall_amps=5.0)
    assert p["max"]["reason"] == "current"
    assert p["max"]["stop_angle"] == 106  # the first angle past the stop; image check would say 105
    assert p["proposed_max"] == 101
    # The backoff command follows the tripping one immediately, before any further step.
    angles = [c.deg for c in rig.servo_commands]
    i = angles.index(106)
    assert angles[i + 1] == 101
    assert max(a for a in angles if a != 106) <= 106 and 107 not in angles


def test_stall_amps_alone_stops(tmp_path):
    rig = ServoRig(max_stop=105, gain=0.5)
    p = _run(rig, tmp_path, stall_amps=0.5)  # 0.1 + 0.5 * 2 = 1.1 A at 107; 0.6 at 106
    assert p["max"]["reason"] == "current" and p["max"]["stop_angle"] == 106


def test_no_stop_inside_bound_proposes_the_bound(tmp_path):
    rig = ServoRig()
    p = _run(rig, tmp_path, hard_min=80.0, hard_max=100.0)
    for d in ("min", "max"):
        assert p[d]["stop_found"] is False and p[d]["reason"] == "bound"
        assert p[d]["stop_angle"] is None
    assert (p["proposed_min"], p["proposed_max"]) == (80, 100)
    assert p["effective_bound"] == {"min": 80, "max": 100}


def test_hard_bound_wider_than_limits_is_clamped_to_limits(tmp_path):
    rig = ServoRig()
    lim = rig.limits[SERVO]
    p = _run(rig, tmp_path, hard_min=0.0, hard_max=180.0)
    assert (p["proposed_min"], p["proposed_max"]) == (lim.min, lim.max)
    assert p["effective_bound"] == {"min": lim.min, "max": lim.max}
    assert p["max"]["stop_found"] is False


def test_empty_intersection_exits_2_and_sends_nothing(tmp_path):
    rig = ServoRig()
    lim = rig.limits[SERVO]
    out = tmp_path / "proposal.yaml"
    argv = _argv(tmp_path, hard_min=str(lim.max + 10), hard_max=str(lim.max + 20))
    assert main(argv, _factories(rig)) == 2
    assert rig.servo_commands == [] and rig.sock.datagrams == []
    assert not out.exists()


def test_bound_not_containing_neutral_exits_2(tmp_path):
    rig = ServoRig()
    assert main(_argv(tmp_path, hard_min="100", hard_max="110"), _factories(rig)) == 2
    assert rig.sock.datagrams == []


def test_unknown_servo_exits_2(tmp_path):
    rig = ServoRig()
    assert main(["nope", *_argv(tmp_path)[1:]], _factories(rig)) == 2
    assert rig.sock.datagrams == []


def test_every_servo_datagram_stays_inside_the_bound(tmp_path):
    rig = ServoRig(max_stop=115, gain=0.1)  # stops beyond the bound: it must never get there
    _run(rig, tmp_path, hard_min=65.0, hard_max=112.5, step=1.5)
    lo, hi = _bound(rig, 65.0, 112.5)
    cmds = rig.servo_commands
    assert cmds and all(lo <= c.deg <= hi for c in cmds)
    assert all(c.ch == 0 for c in cmds)
    assert cmds[-1].deg == 90  # back at neutral
    assert hi in [c.deg for c in cmds] and lo in [c.deg for c in cmds]


def test_step_is_capped_by_max_speed_times_dwell(tmp_path):
    rig = ServoRig()
    _run(rig, tmp_path, hard_min=85.0, hard_max=95.0, step=10.0, dwell=0.01)  # 300 dps * 0.01 = 3
    angles = [c.deg for c in rig.servo_commands]
    assert max(abs(b - a) for a, b in zip(angles, angles[1:])) <= 5.0 + 1e-9  # neutral jump back <= bound


def test_writes_proposal_and_evidence(tmp_path):
    rig = ServoRig(max_stop=115, min_stop=70, gain=0.0)
    out = tmp_path / "sub" / "proposal.yaml"
    _run(rig, tmp_path, out=out)
    text = out.read_text()
    assert text.startswith("# PROPOSAL ONLY - a human must review it before editing config/limits.yaml\n")
    data = yaml.safe_load(text)
    assert set(data) == {"servo", "channel", "proposed_min", "proposed_max", "neutral", "max", "min",
                         "effective_bound", "step", "backoff", "generated_at"}
    assert set(data["max"]) == {"stop_found", "reason", "stop_angle", "current_trace", "evidence"}
    assert data["max"]["current_trace"][0].keys() == {"angle", "max_amps"}
    assert data["step"] == 1.0 and data["backoff"] == 5.0
    for d in ("max", "min"):
        for kind in ("stop", "backoff"):
            name = f"{SERVO}_{d}_{kind}.png"
            assert data[d]["evidence"][kind] == name
            assert (out.parent / name).read_bytes().startswith(PNG_SIGNATURE)


def test_exception_midrun_sends_neutral_and_closes_client(tmp_path):
    rig = ServoRig(max_stop=115, gain=0.0)
    rig.recorder.fail_at_poll = 150  # well into the first sweep
    with pytest.raises(RuntimeError, match="unplugged"):
        _run(rig, tmp_path)
    cmds = rig.servo_commands
    assert max(c.deg for c in cmds) > 90  # it had started moving
    assert cmds[-1].deg == 90
    assert rig.sock.closed
    assert not (tmp_path / "proposal.yaml").exists()


def test_keyboard_interrupt_sends_neutral_and_closes_client(tmp_path):
    rig = ServoRig(max_stop=115, gain=0.0)
    calls = []

    def waiter(seconds):
        calls.append(seconds)
        if len(calls) > 40:
            raise KeyboardInterrupt
        rig.waiter(seconds)

    with pytest.raises(KeyboardInterrupt):
        _run(rig, tmp_path, waiter=waiter)
    assert rig.servo_commands[-1].deg == 90 and rig.sock.closed


def test_main_run_succeeds_and_exits_0(tmp_path):
    rig = ServoRig(max_stop=115, min_stop=70, gain=0.0)
    assert main(_argv(tmp_path), _factories(rig)) == 0
    assert yaml.safe_load((tmp_path / "proposal.yaml").read_text())["proposed_max"] == 110
    assert rig.sock.closed


def test_main_failure_exits_1_with_neutral(tmp_path):
    rig = ServoRig(max_stop=115)
    rig.recorder.fail_at_poll = 150
    assert main(_argv(tmp_path), _factories(rig)) == 1
    assert rig.servo_commands[-1].deg == 90 and rig.sock.closed


@pytest.mark.parametrize("argv_extra", [
    [],  # nothing: servo, --hard-min, ... missing
])
def test_missing_arguments_fail_parsing(argv_extra):
    with pytest.raises(SystemExit) as e:
        main(argv_extra, {})
    assert e.value.code == 2


def test_meter_is_mandatory():
    with pytest.raises(SystemExit) as e:
        main([SERVO, "--hard-min", "0", "--hard-max", "180", "--stall-amps", "1", "--out", "x"], {})
    assert e.value.code == 2


# -- the output path can never reach config/limits.yaml ---------------------------------------


def _assert_refused(tmp_path, out):
    rig = ServoRig()
    before = LIMITS_FILE.read_bytes()
    assert main(_argv(tmp_path, out=out), _factories(rig)) == 2
    assert rig.sock.datagrams == []
    assert LIMITS_FILE.read_bytes() == before


def test_out_config_limits_yaml_refused(tmp_path):
    _assert_refused(tmp_path, LIMITS_FILE)


def test_out_relative_config_limits_yaml_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_CONFIG_DIR.parent)
    _assert_refused(tmp_path, "config/limits.yaml")


def test_out_through_dotdot_refused(tmp_path):
    _assert_refused(tmp_path, REPO_CONFIG_DIR / ".." / "config" / "limits.yaml")
    _assert_refused(tmp_path, REPO_CONFIG_DIR / "sub" / ".." / "proposal.yaml")


def test_out_symlink_into_config_refused(tmp_path):
    link_dir = tmp_path / "linkdir"
    link_dir.symlink_to(REPO_CONFIG_DIR, target_is_directory=True)
    link_file = tmp_path / "innocent.yaml"
    link_file.symlink_to(LIMITS_FILE)
    _assert_refused(tmp_path, link_dir / "proposal.yaml")
    _assert_refused(tmp_path, link_file)


def test_out_named_limits_dot_anything_refused(tmp_path):
    _assert_refused(tmp_path, tmp_path / "limits.proposed.yaml")


def test_out_equal_to_the_limits_file_argument_refused(tmp_path):
    copy = tmp_path / "mylimits.yaml"
    copy.write_bytes(LIMITS_FILE.read_bytes())
    with pytest.raises(CalibrationError):
        check_out_path(copy, copy)


def test_limits_bytes_unchanged_after_a_run(tmp_path):
    before = LIMITS_FILE.read_bytes()
    rig = ServoRig(max_stop=115, min_stop=70, gain=0.0)
    assert main(_argv(tmp_path), _factories(rig)) == 0
    assert LIMITS_FILE.read_bytes() == before
