"""Tests for clouds/contamination.py — sun-glint / coastal false-cloud
suppression (PDF Part A1). The decision logic is pure and exhaustively tested;
the geometry reuses the (separately tested) scalar solar/parallax helpers, so we
check parity with them plus the field-cleaning wiring.

Run from repo root:  python tests/test_cloud_contamination.py   (exit 0 = pass)
Also discoverable by pytest.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import contamination, parallax, solar  # noqa: E402
from clouds.grid import CloudField  # noqa: E402
from radar import calibration  # noqa: E402

LATS = np.array([3.0, 2.5, 2.0, 1.5, 1.0])      # near the MTG sub-satellite point,
LONS = np.array([1.0, 1.5, 2.0, 2.5, 3.0])      # so a daytime glint zone exists
DAY = "2026-06-21T11:00:00"
NIGHT = "2026-06-21T00:00:00"


def _field(time=DAY, cloudy=True, retrieval=False):
    H, W = len(LATS), len(LONS)
    mask = np.ones((H, W)) if cloudy else np.zeros((H, W))
    nan = np.full((H, W), np.nan)
    val = (lambda v: np.full((H, W), v)) if retrieval else (lambda v: nan.copy())
    return CloudField(
        LATS, LONS,
        {"mask": mask, "frac": mask.copy(), "opaque": np.where(mask >= 0.5, 1.0, 0.0),
         "ctt": val(250.0), "cth": val(9000.0), "cot": val(5.0),
         "phase": np.where(mask >= 0.5, 2.0, nan)},
        meta={"sensing_time": time})


# --- pure decision logic ------------------------------------------------------
def test_suppress_mask_truth_table():
    # drop only when cloudy AND in glint AND NOT corroborated by a retrieval
    cloudy        = np.array([1, 1, 1, 1, 0], dtype=bool)
    in_glint      = np.array([1, 1, 0, 0, 1], dtype=bool)
    has_retrieval = np.array([0, 1, 0, 1, 0], dtype=bool)
    drop = contamination.suppress_mask(cloudy, in_glint, has_retrieval)
    assert list(drop) == [True, False, False, False, False], list(drop)


def test_suppress_keeps_thin_cirrus_with_a_top():
    # real cirrus in the glint zone HAS a cloud top -> kept (not a false alarm)
    drop = contamination.suppress_mask([True], [True], [True])
    assert bool(drop[0]) is False


# --- geometry parity with the scalar helpers ----------------------------------
def test_glint_grid_matches_scalar():
    grid = contamination.glint_angle_grid(LATS, LONS, DAY)
    assert grid.shape == (len(LATS), len(LONS))
    assert np.all((grid >= 0.0) & (grid <= 180.0))
    for (la, lo) in [(2.0, 2.0), (3.0, 1.0)]:
        sza = solar.solar_zenith_deg(DAY, la, lo)
        saa = solar.solar_azimuth_deg(DAY, la, lo)
        vza = parallax.satellite_zenith_deg(la, lo)
        vaa = calibration.bearing_deg(la, lo, parallax.SSP_LAT, parallax.SSP_LON)
        want = solar.glint_angle(sza, vza, saa, vaa)
        i, j = list(LATS).index(la), list(LONS).index(lo)
        assert abs(grid[i, j] - want) < 1e-9, f"{grid[i, j]} != {want}"


# --- field cleaning wiring ----------------------------------------------------
def test_clean_drops_uncorroborated_glint_cloud():
    # glint_max 180 => whole daytime scene is "in glint"; cloudy + no retrieval
    # => every such pixel is dropped to clear.
    f = _field(cloudy=True, retrieval=False)
    contamination.clean_field(f, {"glint_suppress": True, "glint_max_deg": 180.0})
    assert f.meta["glint_dropped"] > 0
    assert np.allclose(f.layers["mask"], 0.0)
    assert np.allclose(f.layers["frac"], 0.0)
    assert np.allclose(f.layers["opaque"], 0.0)
    assert np.all(np.isnan(f.layers["cot"]))


def test_clean_keeps_cloud_with_retrieval():
    # same broad glint, but every pixel has a CTTH/COT retrieval -> real cloud, kept
    f = _field(cloudy=True, retrieval=True)
    contamination.clean_field(f, {"glint_suppress": True, "glint_max_deg": 180.0})
    assert f.meta["glint_dropped"] == 0
    assert np.allclose(f.layers["mask"], 1.0)


def test_clean_no_glint_zone_keeps_everything():
    f = _field(cloudy=True, retrieval=False)
    contamination.clean_field(f, {"glint_suppress": True, "glint_max_deg": 0.0})
    assert f.meta["glint_dropped"] == 0
    assert np.allclose(f.layers["mask"], 1.0)


def test_clean_disabled_is_noop():
    f = _field(cloudy=True, retrieval=False)
    before = f.layers["mask"].copy()
    contamination.clean_field(f, {"glint_suppress": False, "glint_max_deg": 180.0})
    assert np.allclose(f.layers["mask"], before)


def test_clean_at_night_is_noop():
    # no specular glint after dark -> early return, nothing dropped
    f = _field(time=NIGHT, cloudy=True, retrieval=False)
    contamination.clean_field(f, {"glint_suppress": True, "glint_max_deg": 180.0})
    assert np.allclose(f.layers["mask"], 1.0)


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
    print("\nPASS — sun-glint / coastal false-cloud suppression OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
