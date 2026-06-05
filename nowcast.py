"""Probabilistic arrival nowcast + storm-mode classification (PDF Stage 2).

arrival_nowcast() replaces the hard 15 km "approaching" cap and the +-10 deg
alignment gate with a lead-time-dependent confidence CONE plus a growth/decay
SURVIVAL model:

  * Each tracked cell's centre is advected forward over a deterministic
    perturbation grid (speed x direction = an "unscented" mini-ensemble). It is
    deterministic on purpose — no RNG — so status.json stays byte-reproducible
    and doesn't churn git (the repo's stated constraint).
  * The directional spread WIDENS with lead time and is wider for convective
    cells (erratic) than stratiform (steady) — the cone.
  * A member "hits" the location if the advected centre passes within
    (cell radius + buffer) of the point before NOWCAST_LEAD_MAX_MIN, weighted
    by the cell's SURVIVAL probability at the hit time (a decaying core that
    needs 90 min to arrive probably dies first).
  * P(rain) = weighted hit fraction per cell, combined across cells as
    1 - prod(1 - p_i). Reported per lead bucket (15/30/60/120 min) so the UI
    can show a confidence curve and the 60-min number lines up with
    verification.py's horizon.

classify_storm_mode() names the scene morphology from REFLECTIVITY ALONE.
Severe-convective modes (squall line/QLCS, bow echo, supercell) are flagged as
SUSPECTED only — confirming rotation needs Doppler velocity, which these image
composites do not carry.

numpy / math only.
"""

import math
import numpy as np

import config
from radar import calibration, colormap


LEAD_BUCKETS = (15, 30, 60, 120)


# ---------------------------------------------------------------------------
# Arrival probability
# ---------------------------------------------------------------------------
def _lifetime_min(summary, latest):
    """Survival timescale (min). None means 'steady/growing -> survives the
    lead window'. For a decaying core: time for max_dBZ to fall to the rain
    threshold at the observed decay rate (floored)."""
    slope = summary.get("dbz_trend_per_min")
    if slope is None or slope >= -1e-3:
        return None
    head = max(latest["max_dbz"] - config.RAIN_DBZ_THRESHOLD, 0.0)
    return max(head / abs(slope), config.NOWCAST_MIN_LIFETIME_MIN)


def _cell_arrival(summary, lat_c, lon_c):
    """Per-cell arrival probability + ETA + per-lead-bucket cumulative prob.
    None if the cell has no usable velocity."""
    latest = summary["latest"]

    # Already raining at the point: the location is inside the cell, or the
    # nearest rain pixel is essentially on top of us (within the buffer). NOTE
    # this is a tighter test than the advection reach below (which is
    # radius+buffer, i.e. the cell BODY covering the point) — a cell whose
    # nearest edge is still tens of km away is approaching, not overhead.
    if latest.get("contains_location") or latest["edge_km"] <= config.NOWCAST_REACH_BUFFER_KM:
        return {"p": 1.0, "eta_min": 0.0,
                "p_by_lead": {b: 1.0 for b in LEAD_BUCKETS},
                "tau_min": None, "stationary": False, "on_location": True}

    if "speed_kmh" not in summary or summary.get("direction_deg") is None:
        return None
    # Cap absurd speeds (e.g. a far cell that inherited a bad Europe-wide
    # composite motion vector) to a physical storm maximum.
    speed = min(float(summary["speed_kmh"] or 0.0), config.NOWCAST_MAX_SPEED_KMH)
    direction = summary["direction_deg"]
    if speed < 1.0:                       # near-stationary and not on us -> won't arrive
        return {"p": 0.0, "eta_min": None,
                "p_by_lead": {b: 0.0 for b in LEAD_BUCKETS},
                "tau_min": None, "stationary": True, "on_location": False}

    # Physical reach gate: at the capped max speed a cell can travel at most
    # (NOWCAST_MAX_SPEED_KMH * lead window) km. If its nearest edge is farther
    # than that it CANNOT arrive in time, so it is not "approaching" — this
    # kills absurd "hail 983 km away, ETA 103 min" cases.
    max_reach_km = config.NOWCAST_MAX_SPEED_KMH * (config.NOWCAST_LEAD_MAX_MIN / 60.0)
    if latest["edge_km"] > max_reach_km:
        return {"p": 0.0, "eta_min": None,
                "p_by_lead": {b: 0.0 for b in LEAD_BUCKETS},
                "tau_min": None, "stationary": False, "on_location": False}

    # local km plane centred on the location (east +, north +)
    kx = 111.32 * math.cos(math.radians(lat_c))
    ky = 110.57
    px = (latest["lon"] - lon_c) * kx
    py = (latest["lat"] - lat_c) * ky
    # A member "hits" when the advected CENTRE passes within the cell's
    # equivalent radius + buffer of the point (i.e. the cell body covers it).
    reach = latest["equiv_diam_km"] / 2.0 + config.NOWCAST_REACH_BUFFER_KM

    convective = latest["cell_type"] == "convective"
    base_spread = (config.NOWCAST_DIR_SPREAD_CONVECTIVE_DEG if convective
                   else config.NOWCAST_DIR_SPREAD_STRATIFORM_DEG)
    tau = _lifetime_min(summary, latest)

    # deterministic 5x5 unscented grid: speed factors x direction sigmas,
    # Gaussian weights (separable, each normalised so the product sums to 1).
    sf = np.array(config.NOWCAST_SPEED_FACTORS, dtype=float)
    sw = np.exp(-0.5 * ((sf - 1.0) / 0.2) ** 2)
    sw /= sw.sum()
    doff = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
    dw = np.exp(-0.5 * (doff / 1.0) ** 2)
    dw /= dw.sum()

    dt = config.NOWCAST_LEAD_STEPS_MIN
    Tmax = config.NOWCAST_LEAD_MAX_MIN
    buckets = {b: 0.0 for b in LEAD_BUCKETS}
    hit_w = 0.0
    eta_acc = 0.0
    eta_wsum = 0.0

    for i, fac in enumerate(sf):
        v = speed * fac / 60.0                       # km/min
        for j, off in enumerate(doff):
            w = float(sw[i] * dw[j])
            t = dt
            hit_t = None
            while t <= Tmax:
                spread = base_spread + config.NOWCAST_DIR_GROWTH_DEG_PER_MIN * t
                ang = math.radians(direction + off * spread)
                ex = px + v * t * math.sin(ang)      # east position of centre
                ny = py + v * t * math.cos(ang)      # north position of centre
                if math.hypot(ex, ny) <= reach:
                    hit_t = t
                    break
                t += dt
            if hit_t is None:
                continue
            surv = 1.0 if tau is None else math.exp(-hit_t / tau)
            if summary.get("trend") == "growing":
                surv = max(surv, 0.8)
            hit_w += w * surv
            eta_acc += w * surv * hit_t
            eta_wsum += w * surv
            for b in LEAD_BUCKETS:
                if hit_t <= b:
                    buckets[b] += w * surv

    p = float(min(max(hit_w, 0.0), 1.0))
    eta = (eta_acc / eta_wsum) if eta_wsum > 1e-6 else None
    return {
        "p": round(p, 3),
        "eta_min": round(eta, 1) if eta is not None else None,
        "p_by_lead": {b: round(min(buckets[b], 1.0), 3) for b in LEAD_BUCKETS},
        "tau_min": round(tau, 1) if tau is not None else None,
        "stationary": False,
        "on_location": False,
    }


def arrival_nowcast(summaries, lat_c, lon_c):
    """Combine per-cell arrival into a single P(rain) + ETA + confidence curve."""
    per = []
    for s in summaries:
        a = _cell_arrival(s, lat_c, lon_c)
        if a is not None:
            per.append((s, a))

    if not per:
        return {"p_rain": 0.0, "eta_minutes": None, "n_cells_considered": 0,
                "approaching": False, "dominant": None,
                "p_by_lead": {str(b): 0.0 for b in LEAD_BUCKETS}}

    # combine independent cell probabilities, per lead bucket
    agg = {}
    for b in LEAD_BUCKETS:
        prod = 1.0
        for _s, a in per:
            pb = a["p_by_lead"].get(b, a["p"] if b == LEAD_BUCKETS[-1] else 0.0)
            prod *= (1.0 - pb)
        agg[str(b)] = round(float(1.0 - prod), 3)
    p_rain = agg[str(LEAD_BUCKETS[-1])]

    # dominant cell = highest p, tie-break earliest ETA
    per.sort(key=lambda pa: (-pa[1]["p"],
                             pa[1]["eta_min"] if pa[1]["eta_min"] is not None else 1e9))
    dom_s, dom_a = per[0]
    c = dom_s["latest"]
    dominant = {
        "track_id": dom_s["id"],
        "p": dom_a["p"],
        "eta_minutes": dom_a["eta_min"],
        "dist_km": c["edge_km"],
        "bearing_deg": c["bearing_deg"],
        "bearing_cardinal": c["bearing_cardinal"],
        "max_dbz": c["max_dbz"],
        "cell_type": c["cell_type"],
        "trend": dom_s.get("trend"),
        "speed_kmh": dom_s.get("speed_kmh"),
        "direction_cardinal": dom_s.get("direction_cardinal"),
        "intensity_label": colormap.classify_intensity(c["max_dbz"]),
        "on_location": dom_a.get("on_location", False),
    }
    return {
        "p_rain": p_rain,
        "eta_minutes": dom_a["eta_min"],
        "n_cells_considered": len(per),
        "approaching": bool(p_rain >= config.P_APPROACH_THRESHOLD),
        "dominant": dominant,
        "p_by_lead": agg,
    }


# ---------------------------------------------------------------------------
# Storm-mode classification (reflectivity only)
# ---------------------------------------------------------------------------
def _signed_angle(ref_deg, test_deg):
    """test - ref wrapped to (-180, 180]. Positive = clockwise of ref."""
    return ((test_deg - ref_deg + 180.0) % 360.0) - 180.0


def _summary_for_cell(summaries, cell):
    """Find the track summary whose latest cell is (closest to) `cell`."""
    best, bestd = None, 1e9
    for s in summaries:
        lc = s["latest"]
        d = abs(lc["lat"] - cell["lat"]) + abs(lc["lon"] - cell["lon"])
        if d < bestd:
            bestd, best = d, s
    return best


def _training_suspected(summaries):
    """Training/back-building proxy: several DISTINCT, fast-moving cells whose
    tracks repeatedly pass close to the location -> cells cross the same axis
    while the system stays put. Conservative (>=3) to avoid false alarms."""
    near = sum(1 for s in summaries
               if s.get("n_frames", 0) >= 2
               and s.get("speed_kmh", 0) >= 25.0
               and s.get("path_min_edge_km", 1e9) <= 25.0)
    return near >= 3


def classify_storm_mode(cells, summaries, scene_mot):
    """Scene-level morphology from reflectivity alone. Returns
    {mode, confidence, flags, n_cells, n_convective, max_dbz, largest_area_km2}.
    `flags` lists SUSPECTED severe signatures (each notes it needs velocity)."""
    if not cells:
        return {"mode": "none", "confidence": "n/a", "flags": [],
                "n_cells": 0, "n_convective": 0, "max_dbz": None,
                "largest_area_km2": None}

    big = max(cells, key=lambda c: c["area_km2"])
    strong = max(cells, key=lambda c: c["max_dbz"])
    n_cells = len(cells)
    n_conv = sum(1 for c in cells if c["cell_type"] == "convective")
    n_cores = sum(c["n_cores"] for c in cells)
    flags = []

    if strong["max_dbz"] < config.CELL_CORE_DBZ and big["area_km2"] > 1500.0:
        mode, conf = "widespread stratiform", "high"
    elif (strong["eccentricity"] >= 0.95 and strong["major_km"] >= 80.0
          and strong["max_dbz"] >= 40.0):
        mode, conf = "squall line / QLCS (suspected)", "medium"
        flags.append("linear convective system \u2014 straight-line wind risk; "
                     "needs Doppler velocity to confirm")
    elif big["area_km2"] > 1500.0 and n_cores >= 1:
        mode, conf = "embedded convection", "medium"
    elif n_conv >= 3:
        mode, conf = "multicell cluster", "medium"
    elif n_cells <= 2 and strong["area_km2"] < 400.0 and strong["max_dbz"] >= 40.0:
        mode, conf = "isolated pulse / airmass cell", "medium"
    elif strong["max_dbz"] >= 40.0:
        mode, conf = "scattered convection", "low"
    else:
        mode, conf = "light / scattered echo", "low"

    # right/left-deviant motion (supercell proxy) on the strongest cell
    ds = _summary_for_cell(summaries, strong)
    if (ds and scene_mot and ds.get("direction_deg") is not None
            and scene_mot.get("direction_deg") is not None
            and strong["max_dbz"] >= 45.0):
        dev = _signed_angle(scene_mot["direction_deg"], ds["direction_deg"])
        if abs(dev) >= 25.0:
            side = "right" if dev > 0 else "left"
            flags.append(f"strong cell deviating {abs(dev):.0f}\u00b0 to the {side} "
                         "of mean flow \u2014 SUSPECTED supercell; needs Doppler "
                         "velocity to confirm rotation")

    if _training_suspected(summaries):
        flags.append("cells repeatedly crossing a similar axis \u2014 training / "
                     "back-building; flash-flood risk for steep coastal catchments")

    return {"mode": mode, "confidence": conf, "flags": flags,
            "n_cells": n_cells, "n_convective": n_conv,
            "max_dbz": strong["max_dbz"], "largest_area_km2": round(big["area_km2"], 1)}
