"""Tests for radar/volume.py — full-volume radar products:
Vertically Integrated Liquid (Greene-Clark), 18-dBZ echo-top, and VIL density,
plus the 4/3-earth beam-height geometry. Pure numpy/math; no HDF5 needed here
(the polar-volume I/O is exercised separately against a cached ORD file).

Run from repo root:  python tests/test_volume.py   (exit 0 = pass)
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from radar import volume  # noqa: E402


def test_beam_height_increases_with_range_and_elevation():
    # 4/3-earth beam height grows with slant range and with elevation angle.
    h50 = volume.beam_height_m(50_000, 0.5)
    h130 = volume.beam_height_m(130_000, 0.5)
    assert h130 > h50 > 0
    # Anchor: 0.5deg beam over Budva at ~130 km is ~2.5 km AGL.
    assert 2000 < h130 < 3200, f"130 km / 0.5deg beam height {h130:.0f} m not ~2.5 km"
    # Higher elevation -> higher beam at the same range.
    assert volume.beam_height_m(130_000, 1.5) > h130


def test_vil_zero_below_floor():
    # All gates below the 18 dBZ floor -> no integrated liquid.
    assert volume.vil_from_profile([1000.0, 3000.0], [10.0, 15.0]) == 0.0


def test_vil_matches_greene_clark_hand_value():
    # Two gates of 45 dBZ, 2 km apart: VIL = 3.44e-6 * Z^(4/7) * dh.
    vil = volume.vil_from_profile([2000.0, 4000.0], [45.0, 45.0])
    z = 10.0 ** (45.0 / 10.0)
    expect = 3.44e-6 * z ** (4.0 / 7.0) * 2000.0
    assert abs(vil - expect) < 1e-6, f"VIL {vil} != {expect}"
    assert abs(vil - 2.56) < 0.05, f"VIL {vil} not ~2.56 kg/m2"


def test_vil_caps_hail_reflectivity_at_56():
    # >56 dBZ is capped at 56 to suppress hail contamination, so a 70 dBZ
    # profile integrates the SAME as a 56 dBZ profile.
    hot = volume.vil_from_profile([2000.0, 4000.0], [70.0, 70.0])
    capped = volume.vil_from_profile([2000.0, 4000.0], [56.0, 56.0])
    assert abs(hot - capped) < 1e-9, f"hail cap failed: {hot} vs {capped}"


def test_echo_top_interpolates_to_18_dbz():
    heights = [1000.0, 3000.0, 5000.0, 7000.0]
    dbz = [30.0, 25.0, 20.0, 10.0]
    # 18 dBZ crossing between 5000 m (20) and 7000 m (10): 5000 + 2000*(20-18)/(20-10).
    et = volume.echo_top_m(heights, dbz, threshold=18.0)
    assert abs(et - 5400.0) < 1.0, f"echo top {et} != 5400"


def test_echo_top_none_when_no_gate_reaches_threshold():
    assert volume.echo_top_m([1000.0, 3000.0], [10.0, 12.0], threshold=18.0) is None


def test_vil_density_normalizes_by_echo_top():
    # VIL density (g/m3) = 1000 * VIL(kg/m2) / echo_top(m).
    d = volume.vil_density_g_m3(2.56, 5400.0)
    assert abs(d - 1000.0 * 2.56 / 5400.0) < 1e-9
    assert volume.vil_density_g_m3(2.56, None) is None
    assert volume.vil_density_g_m3(0.0, 5400.0) == 0.0


def test_column_products_bundles_vil_eth_density():
    # Convenience wrapper over a (heights, dbz) column.
    heights = [2000.0, 4000.0, 6000.0]
    dbz = [50.0, 45.0, 22.0]
    p = volume.column_products(heights, dbz)
    assert p["vil_kg_m2"] > 0
    assert p["echo_top_m"] is not None
    assert p["vil_density_g_m3"] is not None
    # density is derived from the UNrounded vil/eth; the bundle rounds each field
    # independently for JSON stability, so allow a rounding-scale tolerance.
    assert math.isclose(p["vil_density_g_m3"],
                        1000.0 * p["vil_kg_m2"] / p["echo_top_m"], rel_tol=1e-3)


def test_zdr_column_above_freezing_level():
    # ZDR >= 1 dB in real echo (>= 20 dBZ) extending above the 0C level marks an
    # updraft. Freezing level 3500 m; ZDR>=1 up to 6000 m -> a
    # 2500 m deep column.
    heights = [2000.0, 3000.0, 4000.0, 5000.0, 6000.0, 7000.0]
    zdr =     [0.3,    0.8,    1.5,    2.0,    1.2,    0.4]
    dbz =     [45.0,   44.0,   42.0,   40.0,   35.0,   28.0]
    col = volume.zdr_column(heights, zdr, dbz, freezing_level_m=3500.0)
    assert col["present"] is True
    assert abs(col["top_m"] - 6000.0) < 1.0, col["top_m"]
    assert abs(col["depth_m"] - 2500.0) < 1.0, col["depth_m"]


def test_zdr_column_absent_when_only_below_freezing():
    # ZDR>=1 only BELOW the freezing level (warm-rain ZDR) -> not an updraft column.
    heights = [1000.0, 2000.0, 3000.0]
    zdr =     [2.0,    1.5,    0.5]
    dbz =     [48.0,   46.0,   40.0]
    col = volume.zdr_column(heights, zdr, dbz, freezing_level_m=3500.0)
    assert col["present"] is False
    assert col["depth_m"] == 0.0


def test_surface_rain_confidence_low_when_beam_high():
    # Beam ~2.5 km up over Budva -> aloft echo does NOT guarantee surface rain.
    c = volume.surface_rain_confidence(2540.0)
    assert c["confidence"] == "low"
    assert volume.surface_rain_confidence(800.0)["confidence"] == "high"
    assert volume.surface_rain_confidence(None)["confidence"] == "low"


import glob  # noqa: E402

BUDVA_LAT, BUDVA_LON = 42.2864, 18.8400


def _newest_volume():
    fs = sorted(glob.glob(str(ROOT / "data" / "frames" / "ord" / "*.h5")))
    return fs[-1] if fs else None


def test_real_volume_column_over_budva_overshoots():
    p = _newest_volume()
    if not p:
        print("  SKIP: no cached ORD volume")
        return
    vol = volume.read_volume(p)
    assert len(vol["sweeps"]) >= 5, "expected a multi-sweep volume"
    prof = volume.column_profile_at(vol, BUDVA_LAT, BUDVA_LON)
    # Budva is ~130 km from the Uljenje site -> the lowest beam is ~2.5 km up
    # (beam overshoot). Geometry is echo-independent: the 0.5deg
    # beam height over Budva must be ~2.5 km regardless of whether it rained.
    import math as _m
    s_km, _ = volume._ground_range_az(vol["site"], BUDVA_LAT, BUDVA_LON)
    assert 120 < s_km < 140, f"Budva range {s_km:.0f} km not ~130"
    low_el = min(sw["elangle"] for sw in vol["sweeps"])
    h_low = volume.beam_height_m(s_km / _m.cos(_m.radians(low_el)) * 1000.0, low_el)
    assert 2000 < h_low < 3000, f"0.5deg beam over Budva {h_low:.0f} m not ~2.5 km"
    # products bundle is well-formed (values depend on live weather)
    prods = volume.column_products_at(vol, BUDVA_LAT, BUDVA_LON)
    assert prods["vil_kg_m2"] >= 0.0
    assert 0 <= prods["n_levels"] <= len(vol["sweeps"])
    assert prods["echo_top_m"] is None or prods["echo_top_m"] > 0


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
    print("\nPASS — VIL / echo-top / VIL-density column math OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
