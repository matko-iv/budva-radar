"""Tests for the block/TREC dense motion field (PDF Part B1): instead of ONE
global cross-correlation vector (which assumes the whole scene moves as a rigid
block), tile the field and cross-correlate each block -> a local motion vector
per tile, so differential motion / growth / rotation is captured.

Pure numpy/scipy on intensity grids (no colormap/calibration needed here).

Run from repo root:  python tests/test_motion_field.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from radar import motion  # noqa: E402


def _blob(grid, r, c, size=10, val=5.0):
    grid[r:r + size, c:c + size] = val


def test_best_shift_recovers_known_shift():
    prev = np.zeros((64, 64))
    _blob(prev, 20, 20)
    curr = np.zeros((64, 64))
    _blob(curr, 25, 28)            # moved +5 rows (south), +8 cols (east)
    dx, dy, conf = motion._best_shift(prev, curr, max_shift_px=15)
    assert dx == 8 and dy == 5, f"shift ({dx},{dy}) != (8,5)"
    assert conf > 0.3


def test_trec_field_recovers_uniform_shift():
    prev = np.zeros((128, 128))
    curr = np.zeros((128, 128))
    # several blobs all translated by the same (dx=6, dy=-4)
    for (r, c) in [(20, 20), (20, 80), (80, 20), (80, 80)]:
        _blob(prev, r, c)
        _blob(curr, r - 4, c + 6)
    field = motion.trec_field(prev, curr, block_px=64, max_shift_px=12)
    assert len(field) >= 3, f"expected several block vectors, got {len(field)}"
    dxs = [v["dx"] for v in field]
    dys = [v["dy"] for v in field]
    assert abs(float(np.median(dxs)) - 6) <= 1
    assert abs(float(np.median(dys)) - (-4)) <= 1


def test_trec_field_captures_differential_motion():
    prev = np.zeros((128, 128))
    curr = np.zeros((128, 128))
    # left blob moves east, right blob moves west — a single global vector
    # cannot represent this; the field must.
    _blob(prev, 60, 20); _blob(curr, 60, 28)     # +8 cols (east)
    _blob(prev, 60, 90); _blob(curr, 60, 82)     # -8 cols (west)
    field = motion.trec_field(prev, curr, block_px=48, max_shift_px=12)
    left = [v for v in field if v["col"] < 64]
    right = [v for v in field if v["col"] >= 64]
    assert left and right
    assert float(np.median([v["dx"] for v in left])) > 3
    assert float(np.median([v["dx"] for v in right])) < -3


def test_field_median_is_robust_to_one_outlier():
    vecs = [{"dx": 5, "dy": 2, "confidence": 0.9},
            {"dx": 6, "dy": 3, "confidence": 0.8},
            {"dx": 5, "dy": 2, "confidence": 0.7},
            {"dx": 40, "dy": -30, "confidence": 0.3}]  # outlier
    m = motion.field_median(vecs)
    assert abs(m["dx"] - 5) <= 1 and abs(m["dy"] - 2) <= 1


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = []
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            fails.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — block/TREC motion field OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
