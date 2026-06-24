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


def _single_cloud_field():
    """Clear everywhere except ONE isolated cloudy cell — a small cloud, placed
    OFF the stride-subsample lattice so the old `a[::step,::step]` would drop it."""
    lats = np.arange(44.0, 41.99, -0.03)
    lons = np.arange(18.0, 21.01, 0.03)
    H, W = len(lats), len(lons)
    frac = np.zeros((H, W))
    frac[H // 2 + 1, W // 2 + 1] = 1.0
    nan = np.full((H, W), np.nan)
    return CloudField(lats, lons, {"mask": frac, "frac": frac, "opaque": frac,
                                   "ctt": nan, "cth": nan, "cot": nan, "phase": nan},
                      meta={"sensing_time": "2026-06-24T12:00:00"})


def test_downsample_preserves_small_cloud():
    # The per-point click read failed because the browser grid dropped small
    # clouds: a single cloudy cell must survive downsampling (not vanish).
    from clouds.grid import downsample_for_browser
    f = _single_cloud_field()
    ds = downsample_for_browser(f)
    fr = np.array([[0.0 if x is None else x for x in row] for row in ds["frac"]])
    assert fr.sum() > 0.0, "small cloud dropped from the browser grid"
    # frac shipped at (near) full resolution so the read is faithful to the picture
    assert len(ds["lons"]) >= f.shape[1] - 1, (len(ds["lons"]), f.shape[1])


def test_downsample_meanpool_keeps_small_cloud_when_coarsened():
    # Even when forced to coarsen, MEAN-pooling keeps a nonzero fraction in the
    # block (strided subsampling would drop the off-lattice cell).
    from clouds.grid import downsample_for_browser
    f = _single_cloud_field()
    ds = downsample_for_browser(f, max_dim=20)
    fr = np.array([[0.0 if x is None else x for x in row] for row in ds["frac"]])
    assert fr.sum() > 0.0, "mean-pool lost the small cloud under coarsening"


def test_downsample_omits_all_nan_layers():
    from clouds.grid import downsample_for_browser
    ds = downsample_for_browser(_single_cloud_field())
    assert ds["cot"] is None and ds["cth"] is None   # picture has no COT/height


def main():
    fails = []
    for fn in (test_value_at, test_cloud_fraction, test_sample_cloudy_and_phase,
               test_save_load_roundtrip, test_downsample_preserves_small_cloud,
               test_downsample_meanpool_keeps_small_cloud_when_coarsened,
               test_downsample_omits_all_nan_layers):
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
