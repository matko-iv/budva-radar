"""Tests for the ORD-H5 nowcast comparison stack: the grids-based rain-rate
builder, the DGMR adapter (gated + plumbing via a mock), and the verification
harness (FSS/CSI). Self-contained: writes tiny synthetic ODIM PVOL HDF5 files to
a temp dir, so it needs no network and no real radar archive.

    psenv/bin/python tests/test_compare_nowcast.py      (exit 0 = pass)
"""
import datetime
import math
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

try:
    import h5py  # noqa: F401
    import pysteps  # noqa: F401
except Exception as e:                                   # pragma: no cover
    print(f"SKIP — deps missing ({e})")
    sys.exit(0)

import compare_nowcast as cn  # noqa: E402
import config  # noqa: E402
from radar import ord as ordmod, pysteps_nowcast as pn, dgmr_adapter as dg  # noqa: E402

SITE_LAT, SITE_LON = 42.55, 18.20


def _budva_polar():
    kx = 111.32 * math.cos(math.radians(SITE_LAT))
    dx = (config.LOCATION["lon"] - SITE_LON) * kx
    dy = (config.LOCATION["lat"] - SITE_LAT) * 110.57
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360, math.hypot(dx, dy)


def _write_pvol(path, ts, az_c, rng_c, peak_dbz, nrays=360, nbins=250):
    gain, offset, undetect = 0.5, -32.0, 0.0
    rays = np.arange(nrays)[:, None]; bins = np.arange(nbins)[None, :]
    daz = np.minimum((rays - az_c) % 360, (az_c - rays) % 360)
    dbz = peak_dbz * np.exp(-((daz / 6.0) ** 2 + ((bins - rng_c) / 8.0) ** 2))
    val = np.where(dbz < 5, np.nan, dbz)
    raw = np.where(np.isnan(val), undetect, np.clip((val - offset) / gain, 1, 254)).astype(np.uint8)
    with h5py.File(path, "w") as f:
        f.create_group("what").attrs["object"] = np.bytes_(b"PVOL")
        wg = f.create_group("where")
        wg.attrs["lat"] = SITE_LAT; wg.attrs["lon"] = SITE_LON; wg.attrs["height"] = 100.0
        ds = f.create_group("dataset1"); dw = ds.create_group("where")
        dw.attrs["elangle"] = 0.5; dw.attrs["nbins"] = nbins; dw.attrs["nrays"] = nrays
        dw.attrs["rscale"] = 1000.0; dw.attrs["rstart"] = 0.0
        d1 = ds.create_group("data1"); d1.create_dataset("data", data=raw)
        w = d1.create_group("what")
        w.attrs["quantity"] = np.bytes_(b"DBZH"); w.attrs["gain"] = gain
        w.attrs["offset"] = offset; w.attrs["nodata"] = 255.0; w.attrs["undetect"] = undetect


def _archive(d, n=14):
    import datetime
    az0, rng0 = _budva_polar()
    base = datetime.datetime(2022, 7, 15, 12, 0)
    paths = []
    for k in range(n):
        ts = (base + datetime.timedelta(minutes=5 * k)).strftime("%Y%m%dT%H%M")
        p = Path(d) / f"pvol_@{ts}@_hrulj.h5"
        _write_pvol(p, ts, az0 + 16 - 2.4 * k, rng0 + 60 - 8 * k,
                    28 + 20 * math.exp(-((k - 7) / 4.0) ** 2))
        paths.append(str(p))
    return paths


# --- ORD H5 -> rain-rate stack ------------------------------------------------
def test_load_grid_and_grids_stack():
    with tempfile.TemporaryDirectory() as d:
        paths = _archive(d, 5)
        grids = [ordmod.load_grid(p) for p in paths]
        assert grids[-1]["km_per_px"] == 1.0 and grids[-1]["dbz"].ndim == 2
        R, info = pn.build_rainrate_stack_from_grids(
            [g["dbz"] for g in grids], grids[-1]["cal"], 1.0,
            config.LOCATION["lat"], config.LOCATION["lon"], half_km=120.0)
        assert R.shape[0] == 5 and info["km_per_px"] == 1.0
        assert info["scenario"] in ("convective", "stratiform")
        assert "cal" in info, "grids info must carry the GridCal for nowcast_product"


def test_nowcast_product_accepts_gridcal():
    # nowcast_product must use the supplied GridCal (not get_calibration('ord'))
    cal = ordmod.GridCal(SITE_LAT, SITE_LON, 100, 1.0)
    stack = np.zeros((4, 60, 60)); stack[:, 28:34, 28:34] = 8.0
    info = {"km_per_px": 1.0, "budva_crop_xy": (30, 30), "budva_full_xy": (30, 30),
            "shape": (60, 60), "scenario": "convective", "cal": cal}
    velocity = pn.motion_field(stack)
    fc, _, m = pn.nowcast_fields(stack, 4, velocity=velocity, method="extrapolation")
    prod = pn.nowcast_product(stack, info, "ord", n_leadtimes=4, fc=fc,
                              velocity=velocity, method=m, cal=cal, scenario="convective")
    assert prod["method"] == "extrapolation" and len(prod["series"]) == 4


# --- DGMR adapter (gated + plumbing) -----------------------------------------
def test_dgmr_center_tile_centers_budva():
    fld = np.zeros((241, 241)); fld[120, 120] = 9.0
    tile, c = dg.center_tile(fld, 120, 120)
    assert tile.shape == (256, 256) and c == (128, 128) and tile[128, 128] == 9.0


def test_dgmr_gated_without_plugin():
    if dg.available():
        print("  (skip: DGMR plugin installed on this machine)")
        return
    fc, meta = dg.forecast(np.zeros((4, 241, 241)),
                           {"km_per_px": 1.0, "budva_crop_xy": (120, 120)}, 12)
    assert fc is None and "reason" in meta            # plugin not installed -> graceful


def test_dgmr_rejects_coarse_resolution():
    fc, meta = dg.forecast(np.zeros((4, 80, 80)),
                           {"km_per_px": 4.0, "budva_crop_xy": (40, 40)}, 6)
    assert fc is None and "1 km" in meta["reason"]


def test_dgmr_plumbing_with_mock_model():
    stack = np.zeros((4, 200, 200)); stack[:, 96:104, 96:104] = 10.0
    info = {"km_per_px": 1.0, "budva_crop_xy": (100, 100)}

    def mock(inp, num_samples=1):
        assert inp.shape == (4, 256, 256, 1)
        last = inp[-1, :, :, 0]
        return np.stack([last for _ in range(18)], 0)[None, ..., None]   # (1,18,256,256,1)

    fc, meta = dg.forecast(stack, info, 12, _forecast_fn=mock)
    assert fc is not None and fc.shape == (12, 256, 256)
    assert meta["native_leads"] == 18 and meta["budva_tile_xy"] == [128, 128]


# --- _hourly_mm bucketing ------------------------------------------------------
def _base_ms(y, mo, d, h, mi):
    return int(datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def test_hourly_mm_mid_hour_base_splits_into_two_hours():
    # base at 13:40Z, dt=5, leads 5..80 -> each step's midpoint (lead - dt/2) falls
    # in 13:00Z for leads 5..20 and in 14:00Z for leads 25..80.
    base_ms = _base_ms(2026, 7, 12, 13, 40)
    point = {5: 1.0, 10: 2.0, 15: 3.0, 20: 2.0,
             25: 10.0, 30: 9.0, 35: 8.0, 40: 7.0, 45: 6.0, 50: 5.0,
             55: 4.0, 60: 3.0, 65: 2.0, 70: 1.0, 75: 0.5, 80: 0.5}
    series = [{"lead_min": lead, "point_mmh": p, "disc_max_mmh": p + 1.0}
              for lead, p in point.items()]
    out = cn._hourly_mm(base_ms, series, 5.0)
    assert [b["hour"] for b in out] == ["2026-07-12T13:00:00Z", "2026-07-12T14:00:00Z"]
    h13, h14 = out
    assert h13["covered_min"] == 20 and h14["covered_min"] == 60
    assert h13["mm"] == 0.67 and h14["mm"] == 4.67
    assert h13["peak_point_mmh"] == 3.0 and h13["peak_disc_mmh"] == 4.0
    assert h14["peak_point_mmh"] == 10.0 and h14["peak_disc_mmh"] == 11.0


def test_hourly_mm_peak_is_max_not_sum():
    # base on-the-hour, 12 steps all inside 13:00Z; point_mmh rises then falls.
    base_ms = _base_ms(2026, 7, 12, 13, 0)
    vals = [1.0, 3.0, 5.0, 8.0, 10.0, 7.0, 4.0, 2.0, 1.0, 0.5, 0.2, 0.1]
    series = [{"lead_min": 5 * (i + 1), "point_mmh": v, "disc_max_mmh": 0.0}
              for i, v in enumerate(vals)]
    out = cn._hourly_mm(base_ms, series, 5.0)
    assert len(out) == 1
    assert out[0]["hour"] == "2026-07-12T13:00:00Z"
    assert out[0]["mm"] == 3.48                  # sum(v * 5/60), not the peak
    assert out[0]["peak_point_mmh"] == 10.0       # the max, not the sum


def test_hourly_mm_peak_disc_independent_of_point():
    # dry at Budva all hour (point_mmh == 0) while a cell sits ~8 km away
    # (disc_max_mmh large) -> peak_point_mmh stays 0, peak_disc_mmh reports the cell.
    base_ms = _base_ms(2026, 7, 12, 13, 0)
    disc = [0.0, 0.0, 0.0, 5.0, 12.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    series = [{"lead_min": 5 * (i + 1), "point_mmh": 0.0, "disc_max_mmh": d}
              for i, d in enumerate(disc)]
    out = cn._hourly_mm(base_ms, series, 5.0)
    assert len(out) == 1
    assert out[0]["mm"] == 0.0
    assert out[0]["peak_point_mmh"] == 0.0
    assert out[0]["peak_disc_mmh"] == 12.0


def test_hourly_mm_backward_compatible_on_the_hour():
    base_ms = _base_ms(2026, 7, 12, 13, 0)
    series = [{"lead_min": lead, "point_mmh": 2.0, "disc_max_mmh": 3.0}
              for lead in (5, 10, 15, 20)]
    out = cn._hourly_mm(base_ms, series, 5.0)
    assert len(out) == 1
    bucket = out[0]
    assert bucket["hour"] == "2026-07-12T13:00:00Z"
    assert bucket["mm"] == 0.67
    assert bucket["covered_min"] == 20
    assert set(bucket.keys()) == {"hour", "mm", "covered_min",
                                  "peak_point_mmh", "peak_disc_mmh"}


# --- verification harness -----------------------------------------------------
def test_verify_harness_scores_models():
    import verify_nowcast as vn
    with tempfile.TemporaryDirectory() as d:
        paths = _archive(d, 14)
        data = vn.verify(paths, n_leads=4, stride=1)
    assert data["ok"] and data["n_cases"] >= 1
    assert data["scales"] == [2, 4, 8, 16, 32]
    keys = {m["key"] for m in data["models"]}
    assert {"linda", "extrapolation"} <= keys, f"missing models: {keys}"
    for m in data["models"]:
        assert len(m["by_lead"]) == 4
        first = m["by_lead"][0]
        assert len(first["fss"]) == 5
        # FSS is non-decreasing with neighbourhood scale (a defining property)
        fvals = [x for x in first["fss"] if x is not None]
        assert fvals == sorted(fvals), f"FSS must rise with scale, got {first['fss']}"
        assert first["csi"] is None or 0.0 <= first["csi"] <= 1.0


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    fails = []
    for fn in fns:
        try:
            fn(); print(f"PASS  {fn.__name__}")
        except Exception as e:
            fails.append(fn.__name__); print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — ORD-H5 + DGMR adapter + verification OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
