"""Tests for clouds/highsight.py — the picture-reading source. Pure geometry /
brightness / reprojection only (no network), so they run offline.

Run from repo root:  python tests/test_cloud_highsight.py   (exit 0 = pass)
"""
import math
import sys
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
