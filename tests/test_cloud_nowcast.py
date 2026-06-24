"""Field-motion + semi-Lagrangian nowcast tests (clouds/motion.py, nowcast.py).

Run from repo root:  python tests/test_cloud_nowcast.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds.grid import CloudField  # noqa: E402
from clouds import motion as cmotion, nowcast  # noqa: E402

LATS = np.arange(44.0, 41.99, -0.1)
LONS = np.arange(18.0, 20.01, 0.1)


def _field(cloudy_mask, t="2026-06-19T12:00:00"):
    mask = cloudy_mask.astype(float)
    nan = np.full(mask.shape, np.nan)
    return CloudField(LATS, LONS,
                      {"mask": mask, "frac": mask,
                       "ctt": np.where(cloudy_mask, 250.0, nan),
                       "cth": np.where(cloudy_mask, 8000.0, nan),
                       "cot": np.where(cloudy_mask, 12.0, nan),
                       "phase": np.where(cloudy_mask, 2.0, nan)},
                      meta={"sensing_time": t})


def _lon2d():
    return np.broadcast_to(LONS, (len(LATS), len(LONS)))


def _motion_east(speed=60.0, conf=0.6):
    return {"direction_deg": 90.0, "direction_cardinal": "E", "speed_kmh": speed,
            "dlat_per_min": 0.0, "dlon_per_min": 0.02, "confidence": conf, "dt_min": 10}


def test_compute_motion_detects_eastward_shift():
    lon2d = _lon2d()
    blob = (lon2d > 18.6) & (lon2d < 19.0)          # a north-south cloud band
    prev = _field(blob)
    curr = _field((lon2d > 18.9) & (lon2d < 19.3))   # same band, shifted ~0.3 deg E
    m = cmotion.compute_motion(prev, curr, 43.0, 19.0, dt_min=10.0)
    assert m is not None, "no motion detected"
    assert m["direction_cardinal"] == "E", f"dir {m['direction_cardinal']!r} != E"
    assert m["speed_kmh"] > 0


def test_approaching():
    lon2d = _lon2d()
    field = _field(lon2d < 19.0)          # cloudy WEST, clear EAST
    nc = nowcast.point_nowcast(field, _motion_east(), 43.0, 19.15)  # clear point
    assert nc["cloudFracNow"] is not None and nc["cloudFracNow"] <= 0.2
    assert nc["approaching"] is True, f"expected approaching, got {nc}"
    assert nc["clearing"] is False
    assert nc["etaMin"] is not None


def test_clearing():
    lon2d = _lon2d()
    field = _field(lon2d > 19.0)          # cloudy EAST, clear WEST; motion east
    nc = nowcast.point_nowcast(field, _motion_east(), 43.0, 19.4)   # cloudy point
    assert nc["cloudFracNow"] is not None and nc["cloudFracNow"] > 0.2
    assert nc["clearing"] is True, f"expected clearing, got {nc}"
    assert nc["approaching"] is False
    assert nc["etaMin"] is not None


def test_low_confidence_is_stationary():
    lon2d = _lon2d()
    field = _field(lon2d < 19.0)
    nc = nowcast.point_nowcast(field, _motion_east(conf=0.05), 43.0, 19.15)
    assert nc["approaching"] is False and nc["clearing"] is False


def test_unphysical_speed_not_shown_or_used():
    # A high-confidence but physically impossible vector (the "ka SW @ 408 km/h"
    # cross-correlation artifact, PDF Part B) must be neither displayed nor used.
    lon2d = _lon2d()
    field = _field(lon2d < 19.0)
    bogus = {"direction_deg": 225.0, "direction_cardinal": "SW", "speed_kmh": 500.0,
             "dlat_per_min": -0.2, "dlon_per_min": -0.2, "confidence": 0.9, "dt_min": 10}
    nc = nowcast.point_nowcast(field, bogus, 43.0, 19.15)
    assert nc["motionSpeedKmh"] is None, "absurd speed should not be displayed"
    assert nc["motionCardinal"] is None
    assert nc["approaching"] is False and nc["clearing"] is False


def test_compute_motion_flags_unphysical_speed():
    # Same clean shift as the eastward test but over dt=1 min -> ~2400 km/h, which
    # is unphysical for cloud advection -> compute_motion flags it confidence 0.
    lon2d = _lon2d()
    prev = _field((lon2d > 18.4) & (lon2d < 18.8))
    curr = _field((lon2d > 18.9) & (lon2d < 19.3))      # +0.5 deg in 1 minute
    m = cmotion.compute_motion(prev, curr, 43.0, 19.0, dt_min=1.0)
    assert m is not None
    assert m["speed_kmh"] > 250.0, f"setup should be fast, got {m['speed_kmh']}"
    assert m["confidence"] == 0.0, f"unphysical speed should be flagged, got {m['confidence']}"


def main():
    fails = []
    for fn in (test_compute_motion_detects_eastward_shift, test_approaching,
               test_clearing, test_low_confidence_is_stationary,
               test_unphysical_speed_not_shown_or_used,
               test_compute_motion_flags_unphysical_speed):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — cloud motion + nowcast OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
