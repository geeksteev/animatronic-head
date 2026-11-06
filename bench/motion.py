"""Deterministic frame-differencing motion detector for the bench camera.

numpy only: no OpenCV, no I/O, no randomness. The same frames always give the same result.

Pipeline:

1. :func:`preprocess` turns a BGR (or grayscale) image into a float grayscale array, optionally
   cropped to a region of interest and downscaled by an integer block mean (reduces sensor noise).
2. :func:`diff_score` is the mean absolute difference of two preprocessed frames (0..255).
3. :class:`NoiseFloor` measures scores on a still scene and derives a motion threshold.
4. :func:`analyze` scores consecutive frames around a command and reports when motion started,
   when it settled and how large it was.
5. :func:`matches_reference` compares one image to a stored reference (e.g. "back at neutral").

Timing conventions used by :func:`analyze`:

- The score of the pair (frame ``i-1``, frame ``i``) is stamped with frame ``i``'s ``t``,
  i.e. the first frame in which the change is visible.
- ``settled_t`` is the ``t`` of the first frame of a still run: every consecutive score from
  that frame onwards stays ``<= threshold`` for at least ``settle_window_s``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple, Union

import numpy as np

from bench.camera import Frame

__all__ = [
    "MotionResult",
    "NoiseFloor",
    "analyze",
    "diff_score",
    "matches_reference",
    "preprocess",
]

Roi = Tuple[int, int, int, int]
ImageLike = Union[np.ndarray, Frame]

# ITU-R BT.601 luma weights scaled to sum to 256, in B, G, R order (0.114, 0.587, 0.299).
_GRAY_WEIGHTS_BGR = (29, 150, 77)
_GRAY_SHIFT = 8

# Slack for comparing float timestamps (e.g. 6 frames at 1/30 s against a 0.2 s window).
_T_EPS = 1e-9


def _image_of(image: ImageLike) -> np.ndarray:
    return image.image if isinstance(image, Frame) else np.asarray(image)


def preprocess(image: ImageLike, roi: Optional[Roi] = None, downscale: int = 2) -> np.ndarray:
    """Grayscale, crop and downscale an image. Returns a 2-D ``float64`` array.

    - ``image``: ``H x W x 3`` BGR or ``H x W`` grayscale (a :class:`Frame` is accepted too).
      BGR is converted with fixed integer weights ``(29 B + 150 G + 77 R + 128) >> 8``.
    - ``roi``: optional ``(x, y, w, h)`` crop in full-resolution pixels; must lie inside the image.
    - ``downscale``: integer factor >= 1. Each ``downscale x downscale`` block is averaged;
      rows/columns that do not fill a whole block are dropped.
    """
    img = _image_of(image)
    if img.ndim == 3:
        if img.shape[2] != 3:
            raise ValueError(f"expected H x W x 3 BGR image, got shape {img.shape}")
    elif img.ndim != 2:
        raise ValueError(f"expected a 2-D or H x W x 3 image, got shape {img.shape}")

    if isinstance(downscale, bool) or not isinstance(downscale, (int, np.integer)) or downscale < 1:
        raise ValueError(f"downscale must be an integer >= 1, got {downscale!r}")
    downscale = int(downscale)

    if roi is not None:
        if len(roi) != 4:
            raise ValueError(f"roi must be (x, y, w, h), got {roi!r}")
        x, y, w, h = (int(v) for v in roi)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > img.shape[1] or y + h > img.shape[0]:
            raise ValueError(f"roi {roi!r} is empty or outside image of shape {img.shape}")
        img = img[y : y + h, x : x + w]

    if img.ndim == 3:
        b = img[:, :, 0].astype(np.int64)
        g = img[:, :, 1].astype(np.int64)
        r = img[:, :, 2].astype(np.int64)
        wb, wg, wr = _GRAY_WEIGHTS_BGR
        gray = ((wb * b + wg * g + wr * r + (1 << (_GRAY_SHIFT - 1))) >> _GRAY_SHIFT).astype(
            np.float64
        )
    else:
        gray = img.astype(np.float64)

    if downscale == 1:
        return gray
    hh = gray.shape[0] // downscale
    ww = gray.shape[1] // downscale
    if hh == 0 or ww == 0:
        raise ValueError(f"image of shape {gray.shape} is smaller than downscale {downscale}")
    gray = gray[: hh * downscale, : ww * downscale]
    return gray.reshape(hh, downscale, ww, downscale).mean(axis=(1, 3))


def diff_score(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference of two preprocessed frames of the same shape (0..255)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    if a.size == 0:
        raise ValueError("cannot score empty frames")
    return float(np.mean(np.abs(a - b)))


@dataclass(frozen=True)
class NoiseFloor:
    """Scores between consecutive frames of a still scene, and the threshold derived from them.

    ``threshold = max(min_threshold, mean + k * std)`` (population std).
    """

    mean: float
    std: float
    threshold: float
    k: float
    min_threshold: float
    scores: Tuple[float, ...] = field(default=())

    @classmethod
    def from_frames(
        cls,
        frames: Sequence[ImageLike],
        k: float = 4.0,
        min_threshold: float = 1.0,
        roi: Optional[Roi] = None,
        downscale: int = 2,
    ) -> "NoiseFloor":
        """Measure the noise floor from at least two frames (or images) of a still scene.

        Use the same ``roi`` and ``downscale`` as the later :func:`analyze` call.
        """
        if len(frames) < 2:
            raise ValueError("need at least two frames to measure a noise floor")
        pre = [preprocess(f, roi=roi, downscale=downscale) for f in frames]
        scores = tuple(diff_score(pre[i - 1], pre[i]) for i in range(1, len(pre)))
        arr = np.asarray(scores, dtype=np.float64)
        mean = float(arr.mean())
        std = float(arr.std())
        threshold = max(float(min_threshold), mean + float(k) * std)
        return cls(
            mean=mean,
            std=std,
            threshold=threshold,
            k=float(k),
            min_threshold=float(min_threshold),
            scores=scores,
        )


@dataclass(frozen=True)
class MotionResult:
    """Outcome of :func:`analyze`. Times are camera-clock seconds; ``None`` when not applicable.

    - ``moved``: some consecutive score at ``t >= command_t`` is above the threshold.
    - ``motion_start_t``: ``t`` of the first such score; ``latency_s`` is it minus ``command_t``.
    - ``settled`` / ``settled_t``: see the module docstring; ``settle_time_s`` is
      ``settled_t - command_t``. ``settled`` is False if the still run starts after
      ``command_t + timeout_s`` or the frames end before the window is complete.
    - ``peak_score`` / ``peak_t``: largest score at ``t >= command_t`` (0.0 / None if none).
    - ``scores``: every consecutive ``(t, score)``, including those before ``command_t``.
    """

    moved: bool
    motion_start_t: Optional[float]
    settled: bool
    settled_t: Optional[float]
    latency_s: Optional[float]
    settle_time_s: Optional[float]
    peak_score: float
    peak_t: Optional[float]
    scores: list = field(default_factory=list)


def analyze(
    frames: Sequence[Frame],
    command_t: float,
    threshold: float,
    settle_window_s: float = 0.2,
    timeout_s: float = 2.0,
    roi: Optional[Roi] = None,
    downscale: int = 2,
) -> MotionResult:
    """Detect motion after a command in a list of :class:`bench.camera.Frame`.

    ``frames`` must be in non-decreasing ``t`` order. Motion before ``command_t`` is ignored.
    """
    if settle_window_s < 0 or timeout_s < 0:
        raise ValueError("settle_window_s and timeout_s must be >= 0")
    ts = [float(f.t) for f in frames]
    if any(ts[i] < ts[i - 1] for i in range(1, len(ts))):
        raise ValueError("frames must be in non-decreasing t order")

    pre = [preprocess(f.image, roi=roi, downscale=downscale) for f in frames]
    scores = [(ts[i], diff_score(pre[i - 1], pre[i])) for i in range(1, len(pre))]

    # Score j belongs to the pair (frame j, frame j+1) and is stamped ts[j+1].
    after = [j for j, (t, _) in enumerate(scores) if t >= command_t]

    peak_score = 0.0
    peak_t: Optional[float] = None
    for j in after:
        t, s = scores[j]
        if peak_t is None or s > peak_score:
            peak_score, peak_t = s, t

    start_j = next((j for j in after if scores[j][1] > threshold), None)
    if start_j is None:
        return MotionResult(
            moved=False,
            motion_start_t=None,
            settled=False,
            settled_t=None,
            latency_s=None,
            settle_time_s=None,
            peak_score=peak_score,
            peak_t=peak_t,
            scores=scores,
        )

    motion_start_t = scores[start_j][0]
    deadline = command_t + timeout_s
    settled_t: Optional[float] = None
    run_start: Optional[int] = None  # frame index where the current still run begins
    for j in range(start_j + 1, len(scores)):
        if scores[j][1] > threshold:
            run_start = None
            continue
        if run_start is None:
            run_start = j  # first frame of the quiet pair (frame j, frame j+1)
            if ts[run_start] > deadline + _T_EPS:
                break
        if ts[j + 1] - ts[run_start] >= settle_window_s - _T_EPS:
            settled_t = ts[run_start]
            break

    settled = settled_t is not None
    return MotionResult(
        moved=True,
        motion_start_t=motion_start_t,
        settled=settled,
        settled_t=settled_t,
        latency_s=motion_start_t - command_t,
        settle_time_s=(settled_t - command_t) if settled else None,
        peak_score=peak_score,
        peak_t=peak_t,
        scores=scores,
    )


def matches_reference(
    image: ImageLike,
    reference: ImageLike,
    threshold: float,
    roi: Optional[Roi] = None,
    downscale: int = 2,
) -> Tuple[bool, float]:
    """Return ``(score <= threshold, score)`` for ``image`` against ``reference``."""
    score = diff_score(
        preprocess(image, roi=roi, downscale=downscale),
        preprocess(reference, roi=roi, downscale=downscale),
    )
    return score <= threshold, score
