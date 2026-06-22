"""Tests for clouds/clm.py — classify the MTG FCI CLM `cloud_state` by MEANING
read from the netCDF enum, not by a hardcoded integer map.

The PDF: the integer<->category mapping is written as a netCDF4 ENUM inside the
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
