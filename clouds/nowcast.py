"""Semi-Lagrangian cloud-field nowcast.

For a point P, the cloud that WILL be over P in t minutes is the cloud currently
UPSTREAM at P - velocity*t. So we sample the latest cloud field upstream along
the motion vector at increasing lead times and read off:

  * cloud fraction now over P,
  * whether clouds are APPROACHING (clear now, cloudy field arriving) with ETA,
  * whether it is CLEARING (cloudy now, clear gap arriving) with ETA.

A small directional fan (cone) that widens with lead time makes the estimate
robust to motion-vector error — the cloud analogue of the radar nowcast cone.
"""

import math

import config

_MIN_CONF = 0.12        # below this the field correlation is unreliable
_MIN_SPEED_KMH = 2.0    # slower than this == effectively stationary
_SAMPLE_RADIUS_KM = 8.0  # disc radius when reading a fan member
_KM_PER_DEG = 111.32


def _dest(lat, lon, bearing_deg, dist_km):
    """Equirectangular destination (good for the <300 km nowcast reach)."""
    b = math.radians(bearing_deg)
    dlat = (dist_km * math.cos(b)) / _KM_PER_DEG
    dlon = (dist_km * math.sin(b)) / (_KM_PER_DEG * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _fan_fraction(field, lat, lon, motion, t, cfg):
    """Cone-averaged cloud fraction upstream of (lat,lon) at lead t minutes,
    or None if the whole cone falls outside the grid."""
    speed = motion["speed_kmh"]
    dist_km = speed * (t / 60.0)
    up_bearing = (motion["direction_deg"] + 180.0) % 360.0
    spread = cfg["nowcast_dir_spread_deg"] + cfg["nowcast_dir_growth_deg_per_min"] * t
    members = [(-spread, 0.25), (0.0, 0.5), (spread, 0.25)]
    num = den = 0.0
    for da, w in members:
        ulat, ulon = _dest(lat, lon, up_bearing + da, dist_km)
        if not field.contains(ulat, ulon):
            continue
        fr = field.cloud_fraction(ulat, ulon, _SAMPLE_RADIUS_KM)
        if fr is None:
            continue
        num += w * fr
        den += w
    return (num / den) if den > 0 else None


def point_nowcast(field, motion, lat, lon, cfg=None):
    """Run the field-advection nowcast for one point. Returns a dict with
    cloudFracNow / approaching / clearing / etaMin / series / motion summary."""
    cfg = cfg or config.CLOUDS
    # Tight point read so a small cloud at (lat,lon) isn't averaged into clear sky
    # (the "clicked small cloud reads clear" bug); falls back to the innermost ring.
    now_radius = cfg.get("point_read_radius_km", config.SAMPLE_RADII_KM[0])
    frac_now = field.cloud_fraction(lat, lon, now_radius)

    # A motion vector is usable only if confident, non-trivial, AND physically
    # plausible. The same gate decides both whether to advect and whether to
    # SHOW the vector, so an unreliable estimate (e.g. a spurious 408 km/h
    # cross-correlation peak) is neither used nor displayed (PDF Part B).
    max_speed = float(cfg.get("motion_max_speed_kmh", 250.0))
    usable = bool(motion and motion.get("direction_deg") is not None
                  and (motion.get("confidence") or 0) >= _MIN_CONF
                  and _MIN_SPEED_KMH <= (motion.get("speed_kmh") or 0) <= max_speed)

    out = {
        "cloudFracNow": None if frac_now is None else round(frac_now, 3),
        "cloudAtLocation": bool(frac_now is not None and frac_now > cfg["frac_clear_max"]),
        "approaching": False,
        "clearing": False,
        "etaMin": None,
        "motionCardinal": motion.get("direction_cardinal") if usable else None,
        "motionSpeedKmh": motion.get("speed_kmh") if usable else None,
        "series": [],
    }
    if frac_now is None:
        return out

    series = [{"t": 0, "frac": round(frac_now, 3)}]
    if not usable:
        out["series"] = series
        return out

    step = cfg["nowcast_lead_step_min"]
    lead_max = cfg["nowcast_lead_max_min"]
    now_clear = frac_now <= cfg["frac_clear_max"]
    eta_appr = eta_clear = None

    for t in range(step, lead_max + 1, step):
        fr = _fan_fraction(field, lat, lon, motion, t, cfg)
        if fr is None:
            break   # cone left the domain — nothing more to say upstream
        series.append({"t": t, "frac": round(fr, 3)})
        if now_clear and eta_appr is None and fr > cfg["frac_clear_max"]:
            eta_appr = t
        if (not now_clear) and eta_clear is None and fr <= cfg["frac_clear_max"]:
            eta_clear = t

    out["series"] = series
    if now_clear:
        out["approaching"] = eta_appr is not None
        out["etaMin"] = eta_appr
    else:
        out["clearing"] = eta_clear is not None
        out["etaMin"] = eta_clear
    return out
