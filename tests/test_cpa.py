"""Tests for nowcast.closest_point_of_approach — the geometric closest-point-of-
approach (CPA) test the PDF's Part E asks for: classify a moving cell relative to
a fixed point as HIT / BYPASS / RECEDING from the relative position + velocity,
instead of the cruder instantaneous range-rate sign.

  t_cpa = -(r0 . v) / (v . v)        (min; 0 if v.v ~ 0)
  d_min = | r0 + v * t_cpa |         (km; the miss distance at closest approach)

r0 = cell position relative to the point (km, east/north), v = cell velocity
(km/min, east/north). t_cpa <= 0 => the cell is already receding (CPA in the
past). This mirrors TCAS tau / storm-track projection.

Run from repo root:  python tests/test_cpa.py   (exit 0 = pass)
Also discoverable by pytest (test_* functions).
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nowcast  # noqa: E402


def test_head_on_hits_with_zero_miss():
    # Cell 10 km north, moving due south at 1 km/min -> straight at the point.
    t_cpa, d_min = nowcast.closest_point_of_approach(0.0, 10.0, 0.0, -1.0)
    assert abs(t_cpa - 10.0) < 1e-9, f"t_cpa {t_cpa} != 10"
    assert d_min < 1e-9, f"head-on miss distance {d_min} should be ~0"


def test_tangential_pass_has_large_miss_in_future():
    # Cell at (10 E, -5 N), moving due north: it slides PAST 10 km to the east.
    # Closest approach is in the future (t_cpa > 0) but misses by 10 km.
    t_cpa, d_min = nowcast.closest_point_of_approach(10.0, -5.0, 0.0, 1.0)
    assert t_cpa > 0.0, f"tangential CPA should be in the future, got {t_cpa}"
    assert abs(d_min - 10.0) < 1e-9, f"tangential miss {d_min} != 10"


def test_receding_cell_has_negative_t_cpa():
    # Cell 5 km north, moving further north (away): CPA already happened.
    t_cpa, _ = nowcast.closest_point_of_approach(0.0, 5.0, 0.0, 1.0)
    assert t_cpa < 0.0, f"receding cell should have t_cpa < 0, got {t_cpa}"


def test_stationary_cell_cpa_is_now_at_current_distance():
    # No velocity -> closest approach is now, at the current distance.
    t_cpa, d_min = nowcast.closest_point_of_approach(3.0, 4.0, 0.0, 0.0)
    assert t_cpa == 0.0, f"stationary t_cpa {t_cpa} should be 0"
    assert abs(d_min - 5.0) < 1e-9, f"stationary d_min {d_min} != 5 (=|3,4|)"


def test_oblique_approach_misses_by_offset():
    # Cell at (4 E, 12 N) moving due south at 2 km/min. It passes 4 km to the
    # east of the point (x never changes), so d_min = 4 and t_cpa = 6 min.
    t_cpa, d_min = nowcast.closest_point_of_approach(4.0, 12.0, 0.0, -2.0)
    assert abs(t_cpa - 6.0) < 1e-9, f"oblique t_cpa {t_cpa} != 6"
    assert abs(d_min - 4.0) < 1e-9, f"oblique d_min {d_min} != 4"


BUDVA_LAT, BUDVA_LON = 42.2864, 18.8400


def _summary(cid, dx_km, dy_km, equiv, dbz, speed, direction, lat_c, lon_c,
             cell_type="convective", trend="steady", dbz_trend=0.0):
    """Build a track summary shaped like the pipeline's, positioned dx/dy km
    (east/north) from the assessment point."""
    import math as _m
    kx = 111.32 * _m.cos(_m.radians(lat_c))
    ky = 110.57
    clat = lat_c + dy_km / ky
    clon = lon_c + dx_km / kx
    dist = _m.hypot(dx_km, dy_km)
    edge = max(0.0, dist - equiv / 2.0)
    bearing = (_m.degrees(_m.atan2(dx_km, dy_km)) + 360) % 360
    latest = {"lat": clat, "lon": clon, "equiv_diam_km": equiv, "max_dbz": dbz,
              "cell_type": cell_type, "edge_km": edge, "contains_location": edge <= 0,
              "bearing_deg": bearing, "bearing_cardinal": None}
    return {"id": cid, "latest": latest, "speed_kmh": speed,
            "direction_deg": direction, "direction_cardinal": None,
            "dbz_trend_per_min": dbz_trend, "trend": trend}


def test_dominant_tangential_pass_classified_bypass():
    # Cell 40 km east, 8 km south of Budva, moving due north (0 deg): its centre
    # slides ~40 km to the east -> miss distance >> reach -> BYPASS, not approaching.
    summ = [_summary("byp", 40.0, -8.0, 10.0, 46.0, 55.0, 0.0,
                     BUDVA_LAT, BUDVA_LON)]
    res = nowcast.arrival_nowcast(summ, BUDVA_LAT, BUDVA_LON)
    # The deterministic central trajectory misses by ~40 km -> BYPASS, regardless
    # of whether the probabilistic cone still clips the point at +-2 sigma.
    assert res["dominant"]["classification"] == "BYPASS", res["dominant"]["classification"]
    assert res["bypassing"] is True


def test_dominant_head_on_classified_hit():
    # Cell 40 km north, moving due south (180 deg) straight at Budva -> HIT.
    summ = [_summary("hit", 0.0, 40.0, 12.0, 44.0, 50.0, 180.0,
                     BUDVA_LAT, BUDVA_LON)]
    res = nowcast.arrival_nowcast(summ, BUDVA_LAT, BUDVA_LON)
    assert res["dominant"]["classification"] == "HIT", res["dominant"]["classification"]
    assert res["bypassing"] is False


def test_dominant_receding_classified_receding():
    # Cell 12 km north, moving further north (0 deg) -> already receding.
    summ = [_summary("rec", 0.0, 12.0, 10.0, 40.0, 45.0, 0.0,
                     BUDVA_LAT, BUDVA_LON)]
    res = nowcast.arrival_nowcast(summ, BUDVA_LAT, BUDVA_LON)
    assert res["dominant"]["classification"] == "RECEDING", res["dominant"]["classification"]
    assert res["bypassing"] is False


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
        except Exception as e:  # e.g. AttributeError before the fn exists
            fails.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — closest-point-of-approach geometry OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
