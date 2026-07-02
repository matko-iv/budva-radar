"""Coastal-arrival score: will an inland convective cell reach the coast at
Budva, or dissipate over the Dinaric barrier?

Three controls: the base arrival probability from the nowcast/CPA
classification, the growth/decay trend (rising VIL or dBZ intensifies,
falling collapses), and a ridge filter — a cell that is inland and steered
seaward must descend the coastal ridge, where subsidence warming/drying
promotes dissipation, so its arrival is down-weighted.

Montenegro convective-lifecycle literature is thin; tune these against the
verification log rather than treating them as climatology.
"""

import config


def _ang_diff(a, b):
    """Smallest absolute angle (deg) between two bearings."""
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def descends_seaward(cell_bearing_from_target_deg, motion_direction_deg,
                     seaward_azimuth_deg=None, *, inland_halfwidth=90.0,
                     motion_halfwidth=60.0):
    """True when a cell sits inland of the target and is steered seaward, so it
    must descend the coastal ridge to arrive. The bearing is as seen from the
    target; inland means within inland_halfwidth of the landward normal,
    seaward-steered within motion_halfwidth of the seaward azimuth."""
    if motion_direction_deg is None:
        return False
    seaward = (config.COASTAL_SEAWARD_AZIMUTH_DEG if seaward_azimuth_deg is None
               else seaward_azimuth_deg)
    landward = (seaward + 180.0) % 360.0
    inland = _ang_diff(cell_bearing_from_target_deg, landward) <= inland_halfwidth
    seaward_motion = _ang_diff(motion_direction_deg, seaward) <= motion_halfwidth
    return bool(inland and seaward_motion)


def coastal_arrival_score(base_prob, vil_trend_per_min=None,
                          dbz_trend_per_min=None, descends_ridge=False,
                          ridge_dissipation=None):
    """Arrival score in [0, 1] plus a label. Prefers the VIL trend over the
    dBZ trend for the growth/decay sign; applies the ridge multiplier when the
    cell must descend the seaward slope."""
    diss = config.COASTAL_RIDGE_DISSIPATION if ridge_dissipation is None else ridge_dissipation
    score = float(base_prob)

    trend = vil_trend_per_min if vil_trend_per_min is not None else dbz_trend_per_min
    if trend is None:
        gf = 1.0
    elif trend > 1e-4:
        gf = 1.15
    elif trend < -1e-4:
        gf = 0.6
    else:
        gf = 1.0
    score *= gf
    if descends_ridge:
        score *= diss

    score = max(0.0, min(1.0, score))
    label = "likely" if score >= 0.6 else ("possible" if score >= 0.3 else "unlikely")
    return {"score": round(score, 3), "label": label,
            "growth_factor": gf, "ridge_applied": bool(descends_ridge)}
