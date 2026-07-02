"""Tests for clouds/clm.py — classify the MTG FCI CLM `cloud_state` by MEANING
read from the netCDF enum, not by a hardcoded integer map.

The integer<->category mapping is written as a netCDF4 ENUM inside the
file and is NOT publicly hardcoded; dust/ash are SEPARATE flags, not cloud_state
4-7. The five official categories are cloud-free, cloud contaminated, cloud
filled, snow/ice contaminated, undefined/non-processed. So read the enum and map
by name; only fall back to the documented heritage integers when no enum exists.

Run from repo root:  python tests/test_cloud_clm.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import clm  # noqa: E402

# A plausible file enum (values deliberately NOT the heritage 1/2/3 so the test
# proves we map by meaning, not by assumed integers).
FILE_ENUM = {
    "no_data": 0,
    "cloud_free": 10,
    "cloud_contaminated": 20,
    "cloud_filled": 30,
    "snow_ice_contaminated": 40,
    "undefined": 50,
}


def test_classifies_from_file_enum_by_meaning():
    codes = np.array([0, 10, 20, 30, 40, 50], dtype="float64")
    cat = clm.categorize(codes, FILE_ENUM)
    assert list(cat["nodata"]) == [True, False, False, False, False, True]
    assert list(cat["clear"]) == [False, True, False, False, False, False]
    assert list(cat["contaminated"]) == [False, False, True, False, False, False]
    assert list(cat["filled"]) == [False, False, False, True, False, False]
    assert list(cat["snow_ice"]) == [False, False, False, False, True, False]


def test_cloud_any_is_contaminated_or_filled():
    # Thin cirrus (contaminated) IS cloud — presence must include it.
    codes = np.array([10, 20, 30, 40], dtype="float64")
    cat = clm.categorize(codes, FILE_ENUM)
    assert list(cat["cloud_any"]) == [False, True, True, False]


def test_nan_is_nodata():
    codes = np.array([np.nan, 20], dtype="float64")
    cat = clm.categorize(codes, FILE_ENUM)
    assert cat["nodata"][0] and not cat["nodata"][1]


def test_falls_back_to_heritage_map_without_enum():
    # Documented FCI heritage integers: 1 free, 2 contaminated, 3 filled, 8 snow.
    codes = np.array([0, 1, 2, 3, 8, 9], dtype="float64")
    cat = clm.categorize(codes, None)
    assert list(cat["clear"]) == [False, True, False, False, False, False]
    assert list(cat["contaminated"]) == [False, False, True, False, False, False]
    assert list(cat["filled"]) == [False, False, False, True, False, False]
    assert list(cat["snow_ice"]) == [False, False, False, False, True, False]
    assert cat["nodata"][0] and cat["nodata"][5]


def test_enum_from_flag_attrs():
    # satpy/xarray expose the enum as flag_values + flag_meanings on the variable.
    enum = clm.enum_from_attrs({
        "flag_values": [0, 10, 20, 30, 40, 50],
        "flag_meanings": ("no_data cloud_free cloud_contaminated "
                          "cloud_filled snow_ice_contaminated undefined"),
    })
    assert enum == FILE_ENUM, enum


def test_spatial_coherence_drops_isolated_pixel():
    # A lone cloudy pixel (the textbook coastline false alarm) has 0 cloudy
    # neighbours -> dropped by the N-adjacent test.
    m = np.zeros((5, 5), dtype=bool)
    m[2, 2] = True
    out = clm.spatial_coherence(m, min_neighbors=2)
    assert not out.any(), "isolated cloudy pixel should be dropped"


def test_spatial_coherence_keeps_solid_block():
    m = np.zeros((6, 6), dtype=bool)
    m[1:4, 1:4] = True                      # 3x3 solid block
    out = clm.spatial_coherence(m, min_neighbors=2)
    assert out[2, 2], "centre of a solid block must survive"
    assert out.sum() >= 5


def test_categorize_coherence_despeckles_cloud_any():
    codes = np.full((6, 6), 1.0)            # all cloud_free (heritage 1)
    codes[0, 0] = 2.0                       # isolated contaminated (coast false alarm)
    codes[2:4, 2:4] = 3.0                   # solid 2x2 filled block
    base = clm.categorize(codes)
    assert bool(base["cloud_any"][0, 0]) is True   # counted before coherence
    out = clm.categorize(codes, coherence_min_neighbors=2)
    assert bool(out["cloud_any"][0, 0]) is False, "isolated coastal pixel kept"
    assert bool(out["clear"][0, 0]) is True, "dropped pixel should become clear"
    assert bool(out["cloud_any"][2, 2]) is True, "solid block must survive"


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
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — CLM cloud_state classification OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
