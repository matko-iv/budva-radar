"""Tests for clouds/solar.py — dependency-free solar zenith angle + Beer-Lambert
direct-beam transmittance + sun/shade state. These encode the PDF's core
correction: "is the sun blocked" is a function of optical thickness AND solar
geometry, separate from "is there cloud" (CLM presence).

Run from repo root:  python tests/test_cloud_solar.py   (exit 0 = pass)
Also discoverable by pytest (test_* functions).
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import solar  # noqa: E402

BUDVA_LAT, BUDVA_LON = 42.2864, 18.8400


def test_solar_noon_summer_budva():
    # Summer-solstice solar noon over Budva (lon 18.84 -> ~10:45 UTC). Sun is
    # high: SZA ~= lat - declination = 42.29 - 23.44 ~= 18.9 deg.
    dt = datetime.datetime(2026, 6, 21, 10, 45, tzinfo=datetime.timezone.utc)
    sza = solar.solar_zenith_deg(dt, BUDVA_LAT, BUDVA_LON)
    assert 16.0 < sza < 22.0, f"summer-noon SZA {sza:.2f} not ~18.9 deg"


def test_night_is_night():
    dt = datetime.datetime(2026, 6, 21, 23, 30, tzinfo=datetime.timezone.utc)
    sza = solar.solar_zenith_deg(dt, BUDVA_LAT, BUDVA_LON)
    assert sza > 90.0, f"midnight SZA {sza:.2f} should be below horizon"
    assert solar.is_night(sza) is True


def test_day_is_not_night():
    sza = solar.solar_zenith_deg(
        datetime.datetime(2026, 6, 21, 10, 45, tzinfo=datetime.timezone.utc),
        BUDVA_LAT, BUDVA_LON)
    assert solar.is_night(sza) is False


def test_accepts_iso_string():
    a = solar.solar_zenith_deg("2026-06-21T10:45:00", BUDVA_LAT, BUDVA_LON)
    b = solar.solar_zenith_deg(
        datetime.datetime(2026, 6, 21, 10, 45, tzinfo=datetime.timezone.utc),
        BUDVA_LAT, BUDVA_LON)
    assert abs(a - b) < 1e-6, f"ISO string {a} != datetime {b}"


def test_transmittance_clear_is_full_sun():
    assert solar.direct_transmittance(0.0, 30.0) == 1.0


def test_transmittance_thick_blocks():
    assert solar.direct_transmittance(10.0, 0.0) < 0.001


def test_transmittance_lower_sun_blocks_more():
    # Same cloud, lower sun (bigger SZA) -> longer slant path -> less direct beam.
    assert solar.direct_transmittance(2.0, 70.0) < solar.direct_transmittance(2.0, 10.0)


def test_sun_state_thin_is_sunny():
    assert solar.sun_state(1.0, 30.0, phase="water") == "sunny"


def test_sun_state_thick_is_blocked():
    assert solar.sun_state(20.0, 30.0, phase="water") == "blocked"


def test_sun_state_no_cloud_is_sunny():
    assert solar.sun_state(None, 30.0) == "sunny"


def test_sun_state_ice_is_more_forgiving():
    # COT 3.5 at SZA 30: slant-corrected ~4.0. For water that is "dimmed"
    # (>thin 3); ice forward-scatters so the same cloud still reads "sunny".
    assert solar.sun_state(3.5, 30.0, phase="water") == "dimmed"
    assert solar.sun_state(3.5, 30.0, phase="ice") == "sunny"


def test_sun_state_night_returns_none():
    # At night OCA COT is unreliable (no solar channels) -> no sun verdict.
    assert solar.sun_state(20.0, 85.0, phase="water") is None


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
    print("\nPASS — solar zenith + transmittance + sun/shade OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
