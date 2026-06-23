"""Coastal-arrival score (PDF Part C3): will an inland convective cell actually
reach the Adriatic coast at Budva, or dissipate over the Dinaric barrier?

The score combines three physically-motivated controls (lightning, Part D, is
deliberately omitted):

  * STEERING + CPA base probability — is the cell heading here at all
    (`base_prob`, from the nowcast / CPA classification);
  * GROWTH/DECAY — rising VIL/echo-top (or dBZ) intensifies, falling collapses;
  * RIDGE DISSIPATION FILTER — a cell that is inland AND steered seaward must
    descend the coastal ridge, where subsidence warming/drying promotes
    dissipation (the bora downslope mechanism), so its arrival is down-weighted.

Montenegro/Adriatic convective-lifecycle literature is thin; these are starting
points to be TUNED against the verification log, not validated climatology.
"""

import config


def _ang_diff(a, b):
    """Smallest absolute angle (deg) between two bearings."""
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def descends_seaward(cell_bearing_from_target_deg, motion_direction_deg,
                     seaward_azimuth_deg=None, *, inland_halfwidth=90.0,
                     motion_halfwidth=60.0):
    """True when a cell is INLAND of the target and STEERED seaward — i.e. it
    must descend the coastal ridge to arrive. `cell_bearing_from_target_deg` is
    the cell's bearing as seen FROM the target (Budva); `motion_direction_deg`
    is where the cell is heading. Inland = bearing within `inland_halfwidth` of
    the landward normal (opposite the seaward azimuth); seaward-steered = motion
    within `motion_halfwidth` of the seaward azimuth."""
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
    """Combine the base arrival probability with the growth/decay trend and the
    ridge dissipation filter into a single arrival score in [0, 1] + a label.

    Prefers the VIL trend (3-D, PDF C2) over the dBZ trend for the growth/decay
    sign; applies the ridge multiplier when the cell must descend the seaward
    slope. Returns {score, label, growth_factor, ridge_applied}.
    """
    diss = config.COASTAL_RIDGE_DISSIPATION if ridge_dissipation is None else ridge_dissipation
    score = float(base_prob)

    trend = vil_trend_per_min if vil_trend_per_min is not None else dbz_trend_per_min
    if trend is None:
        gf = 1.0
    elif trend > 1e-4:
        gf = 1.15                       # intensifying -> more likely to arrive
    elif trend < -1e-4:
        gf = 0.6                        # collapsing -> less likely
    else:
        gf = 1.0
    score *= gf
    if descends_ridge:
        score *= diss                   # dissipation filter

    score = max(0.0, min(1.0, score))
    label = "likely" if score >= 0.6 else ("possible" if score >= 0.3 else "unlikely")
    return {"score": round(score, 3), "label": label,
            "growth_factor": gf, "ridge_applied": bool(descends_ridge)}
