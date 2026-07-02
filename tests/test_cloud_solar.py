"""Tests for clouds/solar.py — dependency-free solar zenith angle + Beer-Lambert
direct-beam transmittance + sun/shade state. These encode the
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


# --- Cloud Modification Factor (Papachristopoulou et al. 2024) ----
# The CMF (all-sky / clear-sky GLOBAL irradiance) is now what the sun/shade
# verdict runs on (via cmf_sun_state), replacing the direct-beam metric that
# under-read thin forward-scattering cloud as "blocked". The paper's transcribed
# a,b polynomials are degenerate (CMF~0 for all cloud), so cmf() is re-fit to the
# paper's own published anchors: clear COT<1 -> CMF>=0.9, overcast COT>13 ->
# CMF<=0.4, thin COT~2.1 stays bright (CMF~0.8). These tests pin the reliable
# LIMITS and that anchor behaviour.

def test_cmf_clear_sky_is_one():
    # COT 0 (or None) -> CMF 1 (full clear-sky GHI). A reliable limit.
    assert solar.cmf(0.0, 46.0) == 1.0
    assert solar.cmf(None, 46.0) == 1.0


def test_cmf_is_bounded_unit_interval():
    for cot in (0.0, 0.5, 2.0, 5.0, 20.0, 100.0):
        for sza in (0.0, 46.0, 70.0):
            v = solar.cmf(cot, sza)
            assert 0.0 <= v <= 1.0, f"CMF({cot},{sza})={v} out of [0,1]"


def test_cmf_monotone_non_increasing_in_cot():
    # Thicker cloud never lets MORE global irradiance through (CMF->0 as COT->inf).
    sza = 46.0
    prev = solar.cmf(0.0, sza)
    for cot in (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0):
        cur = solar.cmf(cot, sza)
        assert cur <= prev + 1e-9, f"CMF not monotone at COT {cot}: {cur} > {prev}"
        prev = cur


def test_cmf_thin_cloud_stays_bright():
    # The paper's worked example: thin altocumulus COT~2.1 is visibly sunny -> high
    # CMF (NOT the direct-beam ~4.8% that wrongly reads "sun blocked").
    assert solar.cmf(2.1, 46.0) >= 0.8, solar.cmf(2.1, 46.0)
    assert solar.cmf(1.0, 46.0) >= 0.85
    assert solar.direct_transmittance(2.1, 46.0) < 0.1  # the metric we moved off


def test_cmf_overcast_is_low():
    # SENSE2 sky-state anchor the paper cites: overcast COT>13 -> CMF<=0.4.
    assert solar.cmf(13.0, 46.0) <= 0.42
    assert solar.cmf(20.0, 46.0) <= 0.4
    assert solar.cmf(50.0, 46.0) < 0.1


def test_cmf_ice_is_brighter_than_water():
    # Ice forward-scatters more, so the disc stays brighter at the same COT.
    for cot in (3.0, 5.0, 8.0):
        assert solar.cmf(cot, 40.0, phase="ice") >= solar.cmf(cot, 40.0, phase="water")


def test_cmf_lower_sun_dims_a_little_more():
    # Same cloud, lower sun -> slightly more attenuation (mild air-mass term).
    assert solar.cmf(5.0, 75.0) <= solar.cmf(5.0, 20.0)


def test_cmf_real_budva_cirrostratus_is_sunny():
    # The actual reported bug: high cirrostratus over Budva, COT median 3.7, ice,
    # SZA 37.4. Beer-Lambert called it "dimmed" (T=0.01); CMF says the sun gets
    # through. This is the case the fix must flip.
    v = solar.cmf(3.7, 37.4, phase="ice")
    assert v >= 0.8, f"Budva cirrostratus CMF {v} should read sunny"
    assert solar.cmf_sun_state(v) == "sunny"


def test_cmf_sun_state_bands():
    assert solar.cmf_sun_state(0.95) == "sunny"
    assert solar.cmf_sun_state(0.80) == "sunny"
    assert solar.cmf_sun_state(0.65) == "dimmed"
    assert solar.cmf_sun_state(0.40) == "blocked"
    assert solar.cmf_sun_state(0.10) == "blocked"
    assert solar.cmf_sun_state(0.95, night=True) is None
    assert solar.cmf_sun_state(None) is None


# --- Sun-glint geometry  -----------------------------------------

def test_solar_azimuth_noon_is_south():
    # Summer-solstice ~solar noon over Budva: the sun is due south -> az ~180.
    az = solar.solar_azimuth_deg(
        datetime.datetime(2026, 6, 21, 10, 45, tzinfo=datetime.timezone.utc),
        BUDVA_LAT, BUDVA_LON)
    assert 170.0 < az < 190.0, f"solar azimuth {az:.1f} not ~south"


def test_glint_angle_specular_is_small():
    # Sensor on the specular reflection of the sun (VZA=SZA, view azimuth
    # opposite the solar azimuth) -> glint angle ~0 (strong glint zone).
    g = solar.glint_angle(40.0, 40.0, saa_deg=150.0, vaa_deg=330.0)
    assert g < 5.0, f"specular glint angle {g:.1f} should be ~0"


def test_glint_angle_away_from_specular_is_large():
    g = solar.glint_angle(40.0, 40.0, saa_deg=150.0, vaa_deg=150.0)
    assert g > 60.0, f"non-glint angle {g:.1f} should be large"


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
