"""Sampling + persistence test for clouds/grid.py CloudField.

Run from repo root:  python tests/test_cloud_grid.py   (exit 0 = pass)
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds.grid import CloudField  # noqa: E402


def _synthetic():
    """West half (lon < 19) cloudy at 250 K / 8000 m / ice; east half clear."""
    lats = np.arange(44.0, 41.99, -0.1)   # descending (north-up)
    lons = np.arange(18.0, 20.01, 0.1)
    lon2d = np.broadcast_to(lons, (len(lats), len(lons)))
    cloudy = lon2d < 19.0
    mask = cloudy.astype(float)
    nan = np.full(mask.shape, np.nan)
    ctt = np.where(cloudy, 250.0, nan)
    cth = np.where(cloudy, 8000.0, nan)
    cot = np.where(cloudy, 12.0, nan)
    phase = np.where(cloudy, 2.0, nan)   # ice
    return CloudField(lats, lons, {"mask": mask, "frac": mask, "ctt": ctt,
                                   "cth": cth, "cot": cot, "phase": phase},
                      meta={"sensing_time": "2026-06-19T12:00:00", "source": "synthetic"})


def test_value_at():
    f = _synthetic()
    assert f.value_at("mask", 43.0, 18.3) == 1.0
    assert f.value_at("mask", 43.0, 19.7) == 0.0
    assert abs(f.value_at("ctt", 43.0, 18.3) - 250.0) < 1e-6
    assert f.value_at("ctt", 43.0, 19.7) is None  # clear -> NaN -> None


def test_cloud_fraction():
    f = _synthetic()
    assert f.cloud_fraction(43.0, 18.3, 20) > 0.95
    assert f.cloud_fraction(43.0, 19.7, 20) < 0.05
    mid = f.cloud_fraction(43.0, 19.0, 60)  # straddles the boundary
    assert 0.2 < mid < 0.8


def test_sample_cloudy_and_phase():
    f = _synthetic()
    assert abs(f.sample_cloudy("cth", 43.0, 18.3, 30) - 8000.0) < 1e-6
    assert f.sample_cloudy("cth", 43.0, 19.7, 10) is None  # no cloudy cells
    assert f.dominant_phase(43.0, 18.3, 30) == "ice"


def test_save_load_roundtrip():
    f = _synthetic()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "frame.npz"
        f.save(p)
        g = CloudField.load(p)
        assert g.shape == f.shape
        assert g.sensing_time == "2026-06-19T12:00:00"
        assert g.value_at("mask", 43.0, 18.3) == 1.0
        assert abs(g.value_at("ctt", 43.0, 18.3) - 250.0) < 1e-3


def main():
    fails = []
    for fn in (test_value_at, test_cloud_fraction, test_sample_cloudy_and_phase,
               test_save_load_roundtrip):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — CloudField sampling + persistence OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
