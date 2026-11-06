"""Tests for bench.motion on synthetic frames: a dark 'eye' rectangle on a light background.

All noise is seeded, so every run sees identical frames.
"""

import numpy as np
import pytest

from bench.camera import Frame
from bench.motion import (
    MotionResult,
    NoiseFloor,
    analyze,
    diff_score,
    matches_reference,
    preprocess,
)

H, W = 96, 128
BG, EYE = 200, 40
EYE_W, EYE_H = 24, 30
EYE_Y = 32
X0 = 40
NOISE_SIGMA = 4.0
FPS = 30
DT = 1.0 / FPS
N = 90                 # 3 s of frames
COMMAND_T = 1.0        # frame 30


def scene(rng, rects):
    """rects: list of (x, y, w, h) dark rectangles. Returns uint8 H x W x 3 BGR with noise."""
    img = np.full((H, W, 3), BG, dtype=np.float64)
    for x, y, w, h in rects:
        img[y : y + h, x : x + w, :] = EYE
    img += rng.normal(0.0, NOISE_SIGMA, size=img.shape)
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)


def eye(x):
    return (x, EYE_Y, EYE_W, EYE_H)


def make_frames(x_of_i, seed, extra=None):
    """x_of_i(i) -> eye x for frame i; extra(i) -> list of more rectangles."""
    rng = np.random.default_rng(seed)
    frames = []
    for i in range(N):
        rects = [eye(x_of_i(i))] + (extra(i) if extra else [])
        frames.append(Frame(index=i, t=i * DT, image=scene(rng, rects)))
    return frames


def noise_floor(roi=None, seed=1000):
    rng = np.random.default_rng(seed)
    still = [scene(rng, [eye(X0)]) for _ in range(40)]
    return NoiseFloor.from_frames(still, roi=roi)


def move_once(start, stop, step=2):
    """Eye still at X0, moves `step` px per frame on frames start..stop, then still."""
    return lambda i: X0 + step * int(np.clip(i - start + 1, 0, stop - start + 1))


def near(a, b):
    return a is not None and abs(a - b) <= DT + 1e-9


# --- preprocess / diff_score ---------------------------------------------------------------


def test_preprocess_grayscale_fixed_weights():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[..., 0] = 255  # pure blue
    out = preprocess(img, downscale=1)
    assert out.shape == (2, 2)
    assert np.all(out == (29 * 255 + 128) >> 8)
    grey = np.full((2, 2, 3), 123, dtype=np.uint8)
    assert np.all(preprocess(grey, downscale=1) == 123)


def test_preprocess_accepts_2d_roi_and_block_mean():
    img = np.arange(6 * 8, dtype=np.uint8).reshape(6, 8)
    out = preprocess(img, roi=(2, 1, 5, 4), downscale=2)
    assert out.shape == (2, 2)   # 4 x 5 crop -> 2 x 2 (odd column dropped)
    crop = img[1:5, 2:7].astype(float)
    assert out[0, 0] == crop[0:2, 0:2].mean()
    assert out[1, 1] == crop[2:4, 2:4].mean()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"downscale": 0},
        {"downscale": 1.5},
        {"roi": (0, 0, 0, 4)},
        {"roi": (5, 0, 4, 4)},
        {"roi": (-1, 0, 2, 2)},
    ],
)
def test_preprocess_rejects_bad_args(kwargs):
    with pytest.raises(ValueError):
        preprocess(np.zeros((4, 6, 3), dtype=np.uint8), **kwargs)


def test_diff_score_range_and_shape_check():
    a = np.zeros((3, 3))
    assert diff_score(a, a) == 0.0
    assert diff_score(a, np.full((3, 3), 255.0)) == 255.0
    with pytest.raises(ValueError):
        diff_score(a, np.zeros((3, 4)))


# --- noise floor ---------------------------------------------------------------------------


def test_noise_floor_between_noise_and_one_pixel_shift():
    nf = noise_floor()
    assert nf.threshold == max(1.0, nf.mean + 4.0 * nf.std)
    assert nf.threshold > nf.mean > 0

    # Fresh still frames stay below the threshold.
    still = make_frames(lambda i: X0, seed=7)
    pre = [preprocess(f.image) for f in still]
    still_scores = [diff_score(pre[i - 1], pre[i]) for i in range(1, len(pre))]
    assert max(still_scores) < nf.threshold

    # A 1-pixel shift of the rectangle at the default downscale is above it.
    rng = np.random.default_rng(8)
    for _ in range(20):
        a = preprocess(scene(rng, [eye(X0)]))
        b = preprocess(scene(rng, [eye(X0 + 1)]))
        assert diff_score(a, b) > nf.threshold


def test_noise_floor_min_threshold_and_validation():
    img = np.full((8, 8, 3), 100, dtype=np.uint8)
    nf = NoiseFloor.from_frames([img, img, img])
    assert nf.mean == 0.0 and nf.std == 0.0 and nf.threshold == 1.0
    with pytest.raises(ValueError):
        NoiseFloor.from_frames([img])


# --- analyze -------------------------------------------------------------------------------


def test_still_sequence_does_not_move():
    nf = noise_floor()
    r = analyze(make_frames(lambda i: X0, seed=1), COMMAND_T, nf.threshold)
    assert r.moved is False
    assert r.motion_start_t is None and r.latency_s is None
    assert r.settled is False and r.settled_t is None
    assert len(r.scores) == N - 1
    assert r.scores[0][0] == DT


def test_motion_after_command_start_and_settle():
    nf = noise_floor()
    start, stop = 36, 45   # first and last frame in which the eye has a new position
    r = analyze(make_frames(move_once(start, stop), seed=2), COMMAND_T, nf.threshold)
    assert r.moved is True
    assert near(r.motion_start_t, start * DT)
    assert r.latency_s == pytest.approx(r.motion_start_t - COMMAND_T)
    assert r.settled is True
    assert near(r.settled_t, stop * DT)
    assert r.settle_time_s == pytest.approx(r.settled_t - COMMAND_T)
    assert r.peak_score > nf.threshold
    assert start * DT - 1e-9 <= r.peak_t <= stop * DT + 1e-9


def test_motion_that_keeps_going_never_settles():
    nf = noise_floor()
    # Eye jitters by 2 px every frame from frame 36 to the end.
    jitter = lambda i: X0 + (2 * ((i - 35) % 2) if i > 35 else 0)
    r = analyze(make_frames(jitter, seed=3), COMMAND_T, nf.threshold)
    assert r.moved is True
    assert near(r.motion_start_t, 36 * DT)
    assert r.settled is False
    assert r.settled_t is None and r.settle_time_s is None


def test_settle_after_timeout_is_not_settled():
    nf = noise_floor()
    # Jitters on frames 36..79; frame 79 (t ~ 2.63 s) is the last new position,
    # past command_t + timeout_s = 2.0 s.
    jitter = lambda i: X0 + (2 * ((i - 35) % 2) if 35 < i < 80 else 0)
    r = analyze(make_frames(jitter, seed=4), COMMAND_T, nf.threshold, timeout_s=1.0)
    assert r.moved is True
    assert r.settled is False
    # With a longer timeout the same frames do settle.
    r2 = analyze(make_frames(jitter, seed=4), COMMAND_T, nf.threshold, timeout_s=2.0)
    assert r2.settled is True
    assert near(r2.settled_t, 79 * DT)


def test_motion_before_command_is_ignored():
    nf = noise_floor()
    r = analyze(make_frames(move_once(6, 15), seed=5), COMMAND_T, nf.threshold)
    assert r.moved is False
    # ...but it is still visible in the scores.
    assert max(s for t, s in r.scores if t < COMMAND_T) > nf.threshold


def test_motion_outside_roi_is_ignored():
    roi = (0, 0, 64, H)   # left half; the eye is at x = 40..63
    nf = noise_floor(roi=roi)
    distractor = lambda i: [(80 + 2 * int(np.clip(i - 35, 0, 10)), 10, 24, 30)]
    frames = make_frames(lambda i: X0, seed=6, extra=distractor)
    assert analyze(frames, COMMAND_T, nf.threshold, roi=roi).moved is False
    assert analyze(frames, COMMAND_T, noise_floor().threshold).moved is True


def test_analyze_rejects_unordered_frames():
    frames = make_frames(lambda i: X0, seed=1)[:3]
    with pytest.raises(ValueError):
        analyze(frames[::-1], 0.0, 1.0)


def test_analyze_is_deterministic():
    nf1, nf2 = noise_floor(), noise_floor()
    assert nf1 == nf2
    r1 = analyze(make_frames(move_once(36, 45), seed=9), COMMAND_T, nf1.threshold)
    r2 = analyze(make_frames(move_once(36, 45), seed=9), COMMAND_T, nf2.threshold)
    assert isinstance(r1, MotionResult)
    assert r1 == r2
    frames = make_frames(move_once(36, 45), seed=9)
    assert analyze(frames, COMMAND_T, nf1.threshold) == analyze(frames, COMMAND_T, nf1.threshold)


# --- matches_reference ---------------------------------------------------------------------


def test_matches_reference():
    nf = noise_floor()
    rng = np.random.default_rng(11)
    reference = scene(rng, [eye(X0)])
    ok, score = matches_reference(scene(rng, [eye(X0)]), reference, nf.threshold)
    assert ok is True and score <= nf.threshold
    ok, score = matches_reference(scene(rng, [eye(X0 + 1)]), reference, nf.threshold)
    assert ok is False and score > nf.threshold
