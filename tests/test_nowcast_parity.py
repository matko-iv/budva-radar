"""Parity test: docs/nowcast-browser.js (JS port) must match nowcast.py (Python
authoritative) for the same cell catalog. Guards Python<->JS drift.

Part A: real pipeline output on the cached DHMZ frames, assessed at Budva AND at
        an offset point (exercises per-point geometry).
Part B: synthetic cells that exercise the decay/growth/stationary/on-location/
        far-gate branches the current real frames may not hit.

Run from repo root:  python tests/test_nowcast_parity.py
Exit 0 = parity holds; exit 1 = mismatch (prints the offending fields).
"""
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image

import config
import nowcast
import tracking
from radar import fetch, motion, calibration

LAT = config.LOCATION["lat"]
LON = config.LOCATION["lon"]
RUNNER = Path(__file__).parent / "_run_port.js"

# Tolerances: absorb Python banker's-round vs JS half-up at the 3rd/1st decimal.
TOL_P = 2e-3
TOL_ETA = 0.15
TOL_KM = 0.1


def load_rgb(p):
    return np.array(Image.open(p).convert("RGB"))


def run_js(cells, lat, lon):
    payload = {"cells": cells, "lat": lat, "lon": lon}
    fd, name = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        out = subprocess.run(["node", str(RUNNER), name],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError("node runner failed:\n" + out.stderr)
        return json.loads(out.stdout)
    finally:
        os.unlink(name)


def catalog_from_summaries(summaries):
    cat = []
    for s in summaries:
        c = s.get("latest") or {}
        cat.append({
            "id": s["id"], "lat": c["lat"], "lon": c["lon"],
            "equiv_diam_km": c["equiv_diam_km"], "max_dbz": c["max_dbz"],
            "cell_type": c["cell_type"], "speed_kmh": s.get("speed_kmh"),
            "direction_deg": s.get("direction_deg"),
            "dbz_trend_per_min": s.get("dbz_trend_per_min"), "trend": s.get("trend"),
        })
    return cat


def _approx(a, b, tol):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def compare(py, js, label, fails, check_geometry=True, dist_rel_tol=0.01):
    """Compare Python (authoritative) vs JS port.

    The probabilistic model (p_rain, p_by_lead, eta, approaching, dominant p/id)
    is always compared. Descriptive geometry (dom.dist_km, dom.bearing_cardinal)
    is only meaningful when the assessed point == the cell-extraction point, since
    Python's dominant.dist_km/bearing are extraction-relative (not recomputed per
    assessed point) — the JS port recomputes them per clicked point, which is the
    intended behavior. `dist_rel_tol` absorbs the pixel-calibration vs
    equirectangular projection gap for real frames (~1%).
    """
    def bad(msg):
        fails.append(f"[{label}] {msg}")
    if not _approx(py["p_rain"], js["p_rain"], TOL_P):
        bad(f"p_rain {py['p_rain']} vs {js['p_rain']}")
    for b in ("15", "30", "60", "120"):
        if not _approx(py["p_by_lead"].get(b), js["p_by_lead"].get(b), TOL_P):
            bad(f"p_by_lead[{b}] {py['p_by_lead'].get(b)} vs {js['p_by_lead'].get(b)}")
    if not _approx(py["eta_minutes"], js["eta_minutes"], TOL_ETA):
        bad(f"eta {py['eta_minutes']} vs {js['eta_minutes']}")
    if bool(py["approaching"]) != bool(js["approaching"]):
        bad(f"approaching {py['approaching']} vs {js['approaching']}")
    if py["n_cells_considered"] != js["n_cells_considered"]:
        bad(f"n_cells {py['n_cells_considered']} vs {js['n_cells_considered']}")
    dpy, djs = py.get("dominant"), js.get("dominant")
    if (dpy is None) != (djs is None):
        bad(f"dominant presence py={dpy is None} js={djs is None}")
        return
    if dpy is None:
        return
    if dpy["track_id"] != djs["track_id"]:
        bad(f"dom.track_id {dpy['track_id']} vs {djs['track_id']}")
    if not _approx(dpy["p"], djs["p"], TOL_P):
        bad(f"dom.p {dpy['p']} vs {djs['p']}")
    if not _approx(dpy["eta_minutes"], djs["eta_minutes"], TOL_ETA):
        bad(f"dom.eta {dpy['eta_minutes']} vs {djs['eta_minutes']}")
    # cell-intrinsic properties (independent of assessed point)
    for key in ("intensity_label", "cell_type", "trend", "direction_cardinal"):
        if dpy.get(key) != djs.get(key):
            bad(f"dom.{key} {dpy.get(key)!r} vs {djs.get(key)!r}")
    if check_geometry:
        a, b2 = dpy["dist_km"], djs["dist_km"]
        if abs(a - b2) > dist_rel_tol * max(abs(a), abs(b2), 1.0):
            bad(f"dom.dist_km {a} vs {b2} (rel>{dist_rel_tol})")
        if dpy.get("bearing_cardinal") != djs.get("bearing_cardinal"):
            bad(f"dom.bearing_cardinal {dpy.get('bearing_cardinal')!r} vs {djs.get('bearing_cardinal')!r}")
        if bool(dpy["on_location"]) != bool(djs["on_location"]):
            bad(f"dom.on_location {dpy['on_location']} vs {djs['on_location']}")


def synth(cid, clat, clon, equiv, max_dbz, cell_type, speed, direction,
          dbz_trend, trend, lat_c, lon_c):
    """Build a (summary, catalog) pair consistent with how the pipeline shapes
    cells, so Python arrival_nowcast and the JS catalog see identical geometry."""
    kx = 111.32 * math.cos(math.radians(lat_c))
    ky = 110.57
    px = (clon - lon_c) * kx
    py = (clat - lat_c) * ky
    dist = math.hypot(px, py)
    edge = max(0.0, dist - equiv / 2.0)
    bearing = (math.degrees(math.atan2(px, py)) + 360) % 360
    dir_card = None if direction is None else calibration.bearing_to_cardinal(direction)
    latest = {"lat": clat, "lon": clon, "equiv_diam_km": equiv, "max_dbz": max_dbz,
              "cell_type": cell_type, "edge_km": edge, "contains_location": edge <= 0,
              "bearing_deg": bearing, "bearing_cardinal": calibration.bearing_to_cardinal(bearing)}
    summ = {"id": cid, "latest": latest, "speed_kmh": speed, "direction_deg": direction,
            "direction_cardinal": dir_card, "dbz_trend_per_min": dbz_trend, "trend": trend}
    cat = {"id": cid, "lat": clat, "lon": clon, "equiv_diam_km": equiv,
           "max_dbz": max_dbz, "cell_type": cell_type, "speed_kmh": speed,
           "direction_deg": direction, "dbz_trend_per_min": dbz_trend, "trend": trend}
    return summ, cat


def main():
    fails = []

    # ---- Part A: real pipeline output on cached frames -----------------------
    frames = fetch.list_cached_frames("dhmz")
    if len(frames) < 2:
        print("SKIP Part A: need >=2 cached dhmz frames")
    else:
        latest = load_rgb(frames[-1])
        prev = load_rgb(frames[-2])
        mi = motion.compute_motion_vector(prev, latest, "dhmz", LAT, LON)
        if mi is not None:
            t_prev = motion._frame_timestamp(frames[-2])
            t_latest = motion._frame_timestamp(frames[-1])
            dt_min = (t_latest - t_prev).total_seconds() / 60.0
            mi["speed_kmh"] = motion.estimate_kmh_from_motion(mi, dt_min)
        cells = tracking.extract_cells(latest, "dhmz", LAT, LON)
        summaries = tracking.update_summaries(cells, [], mi)
        cat = catalog_from_summaries(summaries)
        print(f"Part A: {len(summaries)} real cells")

        # Assess at the extraction point (Budva). dist_km tolerance 3% absorbs the
        # pixel-calibration vs equirectangular projection gap on real frames.
        py = nowcast.arrival_nowcast(summaries, LAT, LON)
        compare(py, run_js(cat, LAT, LON), "real@budva", fails, dist_rel_tol=0.03)

    # ---- Part B: synthetic branch coverage -----------------------------------
    # Assessment point = Budva. Cells crafted to hit each model branch.
    summ, cat = [], []
    specs = [
        # convective, inbound from the north, decaying (finite tau)
        ("decay01", LAT + 0.55, LON, 12.0, 47.0, "convective", 60.0, 180.0, -2.0, "decaying"),
        # stratiform, inbound from the east, growing (survival floored at 0.8)
        ("grow0001", LAT, LON + 0.70, 20.0, 33.0, "stratiform", 45.0, 270.0, 1.2, "growing"),
        # near-stationary (speed < 1) -> p 0
        ("stat0001", LAT + 0.30, LON + 0.30, 8.0, 41.0, "convective", 0.3, 225.0, 0.0, "steady"),
        # on-location (edge within buffer) -> p 1
        ("onloc001", LAT + 0.01, LON, 10.0, 38.0, "convective", 30.0, 180.0, 0.0, "steady"),
        # far beyond physical reach -> p 0
        ("far00001", LAT + 3.0, LON, 15.0, 52.0, "convective", 50.0, 180.0, 0.0, "steady"),
        # just PASSED: trailing edge ~3 km E of the point, moving E (receding)
        # -> p 0 (the "approaching, ETA 0 after the rain has gone by" bug)
        ("passed01", LAT, LON + 0.0971, 10.0, 36.0, "convective", 40.0, 90.0, -0.5, "decaying"),
        # mirror-image control: edge ~3 km W, moving E TOWARD the point -> p 1
        ("immin001", LAT, LON - 0.0971, 10.0, 36.0, "convective", 40.0, 90.0, 0.0, "steady"),
    ]
    for (cid, clat, clon, eq, dbz, ct, sp, di, tr, trend) in specs:
        s, c = synth(cid, clat, clon, eq, dbz, ct, sp, di, tr, trend, LAT, LON)
        summ.append(s)
        cat.append(c)
    print(f"Part B: {len(summ)} synthetic cells")
    py_b = nowcast.arrival_nowcast(summ, LAT, LON)
    compare(py_b, run_js(cat, LAT, LON), "synth@budva", fails)

    # Part B offset: build cells relative to an off-Budva assessment point so
    # Python is self-consistent there (extraction point == assessment point).
    # This is the apples-to-apples test of per-point geometry the JS port does.
    pLat, pLon = LAT + 0.40, LON - 0.50
    summ2, cat2 = [], []
    for (cid, dlat, dlon, eq, dbz, ct, sp, di, tr, trend) in [
        ("offc0001", pLat + 0.45, pLon, 12.0, 46.0, "convective", 55.0, 180.0, 0.0, "steady"),
        ("offc0002", pLat, pLon + 0.40, 16.0, 34.0, "stratiform", 40.0, 270.0, -1.5, "decaying"),
    ]:
        s, c = synth(cid, dlat, dlon, eq, dbz, ct, sp, di, tr, trend, pLat, pLon)
        summ2.append(s)
        cat2.append(c)
    py_b2 = nowcast.arrival_nowcast(summ2, pLat, pLon)
    compare(py_b2, run_js(cat2, pLat, pLon), "synth@offset", fails)

    # ---- Result --------------------------------------------------------------
    if fails:
        print("\nFAIL — parity mismatches:")
        for f in fails:
            print("  " + f)
        return 1
    print("\nPASS — JS port matches Python nowcast on all checked fields.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
