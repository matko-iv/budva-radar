"""Tests for clouds/highsight.py — the picture-reading source. Pure geometry /
brightness / reprojection only (no network), so they run offline.

Run from repo root:  python tests/test_cloud_highsight.py   (exit 0 = pass)
"""
import datetime
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import highsight as hs  # noqa: E402

BUDVA = (42.2864, 18.8400)
BBOX = {"lat_min": 40.3, "lat_max": 44.3, "lon_min": 16.4, "lon_max": 21.4}


def _ref_tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def test_tile_index_matches_web_mercator_reference():
    for z in (5, 6, 7, 8):
        assert hs.tile_index(*BUDVA, z) == _ref_tile(*BUDVA, z), z
    assert hs.tile_index(*BUDVA, 7) == (70, 47)


def test_tile_range_covers_bbox_and_budva():
    z = 7
    x0, x1, y0, y1 = hs.tile_range(BBOX, z)
    assert x0 <= x1 and y0 <= y1
    bx, by = hs.tile_index(*BUDVA, z)
    assert x0 <= bx <= x1 and y0 <= by <= y1
    # corners fall inside the inclusive range
    for lat in (BBOX["lat_min"], BBOX["lat_max"]):
        for lon in (BBOX["lon_min"], BBOX["lon_max"]):
            tx, ty = hs.tile_index(lat, lon, z)
            assert x0 <= tx <= x1 and y0 <= ty <= y1, (lat, lon)


def test_merc_norm_monotonic():
    # x grows east, y grows south (lat down).
    x_w, _ = hs.merc_norm(42.0, 16.0)
    x_e, _ = hs.merc_norm(42.0, 21.0)
    assert x_e > x_w
    _, y_n = hs.merc_norm(44.0, 18.0)
    _, y_s = hs.merc_norm(40.0, 18.0)
    assert y_s > y_n


def test_cloud_fields_brightness_thresholds():
    white = np.full((1, 1, 3), 250, dtype="uint8")     # bright neutral -> thick cloud
    grey = np.full((1, 1, 3), 170, dtype="uint8")      # mid neutral -> cloud, not thick
    sea = np.array([[[10, 30, 60]]], dtype="uint8")    # dark blue -> clear
    land = np.array([[[60, 110, 40]]], dtype="uint8")  # green, saturated -> clear
    cloud, thick = hs.cloud_fields(np.concatenate([white, grey, sea, land], axis=1))
    assert list(cloud[0]) == [1.0, 1.0, 0.0, 0.0], cloud
    assert list(thick[0]) == [1.0, 0.0, 0.0, 0.0], thick


def test_reproject_uniform_and_orientation():
    z = 7
    x0, x1, y0, y1 = hs.tile_range(BBOX, z)
    ny, nx = (y1 - y0 + 1), (x1 - x0 + 1)
    mosaic = np.zeros((ny * hs.TILE_PX, nx * hs.TILE_PX, 3), dtype="uint8")
    # north half white, south half dark -> the reprojected grid's top rows (north)
    # must be the white ones (orientation sanity).
    mosaic[: (ny * hs.TILE_PX) // 2] = 255
    lats = np.linspace(BBOX["lat_max"], BBOX["lat_min"], 40)
    lons = np.linspace(BBOX["lon_min"], BBOX["lon_max"], 50)
    out = hs.reproject(mosaic, (x0 * hs.TILE_PX, y0 * hs.TILE_PX), z, lats, lons)
    assert out.shape == (40, 50, 3)
    assert out[0].mean() > out[-1].mean()           # north brighter than south


def test_build_field_is_picture_only():
    z = 7
    x0, x1, y0, y1 = hs.tile_range(BBOX, z)
    ny, nx = (y1 - y0 + 1), (x1 - x0 + 1)
    mosaic = np.full((ny * hs.TILE_PX, nx * hs.TILE_PX, 3), 255, dtype="uint8")
    lats = np.linspace(BBOX["lat_max"], BBOX["lat_min"], 30)
    lons = np.linspace(BBOX["lon_min"], BBOX["lon_max"], 30)
    rgb = hs.reproject(mosaic, (x0 * hs.TILE_PX, y0 * hs.TILE_PX), z, lats, lons)
    field = hs.build_field(rgb, lats, lons, "2026-06-24T11:00:00")
    assert np.nanmean(field.layers["frac"]) == 1.0           # all-white -> all cloud
    assert np.isnan(field.layers["cot"]).all()               # picture has no COT
    assert field.meta["source"] == "HighSight"


# --- frame pinning: date / slot / sensing_time (the fix) ----------------------
def test_date_param_format():
    # matches the spec example: .../satellite/3/6/4?date=2025/01/04/0710
    assert hs._date_param(datetime.datetime(2025, 1, 4, 7, 10)) == "2025/01/04/0710"


def test_slot_iso_is_utc_z():
    # sensing_time carries a 'Z' so the page reads age in UTC, not local
    assert hs._slot_iso(datetime.datetime(2026, 6, 24, 8, 10, 0)) == "2026-06-24T08:10:00Z"


def test_freshest_slot_floored_and_lagged():
    slot = hs._freshest_slot({"highsight_lag_min": 30})
    assert slot.minute % 10 == 0 and slot.second == 0 and slot.microsecond == 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    age_min = (now - slot).total_seconds() / 60.0
    assert 29.0 <= age_min < 41.0, f"slot age {age_min:.1f} min outside [30,40) window"


def test_ts_sha_handles_trailing_z():
    ts, sha = hs._ts_sha("2026-06-24T08:10:00Z")
    assert ts == "20260624_081000" and len(sha) == 12
    assert hs._ts_sha("2026-06-24T08:10:00Z") == (ts, sha)   # stable


def test_cache_roundtrip_lets_fetch_skip_download():
    # save a frame, then _load_cached returns it -> fetch_field can skip the
    # network for a slot we already hold (the HighSight tile-quota guard).
    saved_dir = hs.FRAMES_DIR
    with tempfile.TemporaryDirectory() as d:
        hs.FRAMES_DIR = Path(d)
        try:
            t = "2026-06-24T08:10:00Z"
            lats = np.linspace(44.0, 41.0, 8)
            lons = np.linspace(16.0, 21.0, 8)
            nan = np.full((8, 8), np.nan)
            field = hs.CloudField(lats, lons, {
                "mask": np.ones((8, 8)), "frac": np.ones((8, 8)),
                "opaque": np.zeros((8, 8)), "ctt": nan, "cth": nan,
                "cot": nan, "phase": nan},
                meta={"sensing_time": t, "source": "HighSight"})
            assert hs.save_frame(field, t)["fetched"] is True
            assert hs.save_frame(field, t)["fetched"] is False        # deduped
            cached = hs._load_cached(t)
            assert cached is not None
            f2, rgb = cached
            assert rgb is None and np.nanmean(f2.layers["frac"]) == 1.0
            assert hs._load_cached("2026-06-24T07:00:00Z") is None     # uncached slot
        finally:
            hs.FRAMES_DIR = saved_dir


# --- tile-quota throttle ------------------------------------------------------
def test_within_interval_throttle():
    n = datetime.datetime(2026, 6, 24, 12, 0)
    assert hs._within_interval(n, datetime.datetime(2026, 6, 24, 11, 30), 100) is True   # 30<100 skip
    assert hs._within_interval(n, datetime.datetime(2026, 6, 24, 10, 0), 100) is False   # 120>100 fetch
    assert hs._within_interval(n, None, 100) is False                                     # no cache
    assert hs._within_interval(n, datetime.datetime(2026, 6, 24, 11, 30), 0) is False     # disabled


def test_throttle_skips_download_for_recent_frame():
    # A frame cached 20 min before the nominal slot, with a 100-min throttle, must
    # be REUSED with zero tile downloads (this is what fits the monthly quota).
    saved = (hs.FRAMES_DIR, hs._fetch_tile, hs._freshest_slot)
    with tempfile.TemporaryDirectory() as d:
        hs.FRAMES_DIR = Path(d)
        calls = []
        hs._freshest_slot = lambda cfg=None: datetime.datetime(2026, 6, 24, 12, 0)
        hs._fetch_tile = lambda *a, **k: (calls.append(1) or
                                          np.full((hs.TILE_PX, hs.TILE_PX, 3), 200, "uint8"))
        try:
            t_old = "2026-06-24T11:40:00Z"
            lats = np.linspace(44, 41, 8); lons = np.linspace(16, 21, 8)
            nan = np.full((8, 8), np.nan)
            field = hs.CloudField(lats, lons, {
                "mask": np.ones((8, 8)), "frac": np.ones((8, 8)), "opaque": np.zeros((8, 8)),
                "ctt": nan, "cth": nan, "cot": nan, "phase": nan},
                meta={"sensing_time": t_old, "source": "HighSight"})
            hs.save_frame(field, t_old)
            cfg = dict(__import__("config").CLOUDS); cfg["highsight_min_interval_min"] = 100
            f, rgb, st = hs.fetch_field(cfg, key="K")
            assert len(calls) == 0, f"throttle should skip download, got {len(calls)} fetches"
            assert st == t_old, f"should reuse cached slot, got {st}"
        finally:
            hs.FRAMES_DIR, hs._fetch_tile, hs._freshest_slot = saved


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
    print("\nPASS — HighSight tile/brightness/reproject OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
