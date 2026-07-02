"""Tests for clouds/parallax.py — geostationary parallax over Budva.

MTG sits at 0 N, 0 E; the satellite zenith over Budva is
~52 deg, so a cloud at height H is displaced in the nadir image by ~H*tan(52)
~= 1.3*H (up to ~13 km = ~4 FCI pixels for a 10 km top), radially AWAY from the
sub-satellite point. A cloud truly OVER Budva therefore appears shifted toward
the NE in the image, so to read what is really overhead we sample shifted that
way (and/or use a larger disc to absorb the shift).

Run from repo root:  python tests/test_cloud_parallax.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import parallax  # noqa: E402
from radar import calibration  # noqa: E402

BUDVA_LAT, BUDVA_LON = 42.2864, 18.8400


def test_satellite_zenith_zero_at_subpoint():
    assert parallax.satellite_zenith_deg(0.0, 0.0) < 0.01


def test_satellite_zenith_over_budva_is_about_52():
    z = parallax.satellite_zenith_deg(BUDVA_LAT, BUDVA_LON)
    assert 49.0 < z < 56.0, f"Budva satellite zenith {z:.1f} not ~52 deg"


def test_parallax_offset_magnitude_for_high_cloud():
    dlat, dlon = parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, 10000.0)
    d_km = calibration.haversine_km(BUDVA_LAT, BUDVA_LON,
                                    BUDVA_LAT + dlat, BUDVA_LON + dlon)
    assert 9.0 < d_km < 16.0, f"10 km-top parallax shift {d_km:.1f} km not ~13 km"


def test_parallax_offset_points_away_from_subpoint():
    # Away from SSP (0,0) at Budva is toward the NE: both components positive.
    dlat, dlon = parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, 8000.0)
    assert dlat > 0.0 and dlon > 0.0, (dlat, dlon)


def test_parallax_offset_scales_with_height():
    d_lo = parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, 2000.0)
    d_hi = parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, 10000.0)
    assert abs(d_hi[0]) > abs(d_lo[0]) and abs(d_hi[1]) > abs(d_lo[1])


def test_parallax_offset_zero_for_no_height():
    assert parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, 0.0) == (0.0, 0.0)
    assert parallax.parallax_offset(BUDVA_LAT, BUDVA_LON, None) == (0.0, 0.0)


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
    print("\nPASS — geostationary parallax OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
