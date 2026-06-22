"""Solar geometry + direct-beam transmittance — the SUN/SHADE axis.

The PDF's core correction: "is there cloud" (CLM presence) and "is the sun
blocked" (direct beam) are two different questions. Presence comes from the CLM
mask; whether the sun gets through is set by optical thickness AND solar geometry
(the low sun in the morning/evening makes the same cloud block more). This module
owns the second question.

  T_direct = exp(-COT / cos(SZA))                       (Beer-Lambert direct beam)

Dependency-free: the solar zenith angle is computed with the NOAA solar-position
algorithm (accurate to ~0.5 deg, ample for thresholding) rather than pulling in
pyorbital, mirroring how radar/calibration.py reimplements geometry in-house.

Caveats baked in (from the PDF):
  * Beer-Lambert is a LOWER bound on transmission; thin ice cloud forward-scatters
    so the sun's disk stays visible to higher COT -> ice phase gets a higher
    blocking threshold (ice_factor).
  * At night (SZA >= night_sza) OCA COT is unreliable (no solar channels), so we
    return NO sun verdict and the caller falls back to CLM presence + CTTH.
"""

import datetime
import math

# Thresholds default to config.CLOUDS but are passed explicitly so this module
# stays pure and unit-testable without importing config.
NIGHT_SZA_DEG = 80.0       # at/after this the sun is too low to call sun/shade
ICE_FORWARD_SCATTER = 1.5  # ice cloud needs ~50% more COT to actually block the sun


def _to_utc(dt):
    """Accept a datetime (naive treated as UTC) or an ISO-8601 string."""
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def solar_zenith_deg(dt, lat, lon):
    """Solar zenith angle (degrees, 0 = sun overhead, >90 = below horizon) for a
    point at the given UTC time. NOAA solar-position algorithm."""
    t = _to_utc(dt)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60.0 + t.second / 3600.0

    # Fractional year (radians).
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)

    # Equation of time (minutes) and solar declination (radians).
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))

    # True solar time -> hour angle (degrees), longitude positive east, UTC.
    time_offset = eqtime + 4.0 * lon
    tst = hour * 60.0 + time_offset
    ha = math.radians(tst / 4.0 - 180.0)

    latr = math.radians(lat)
    cos_z = (math.sin(latr) * math.sin(decl)
             + math.cos(latr) * math.cos(decl) * math.cos(ha))
    cos_z = max(-1.0, min(1.0, cos_z))
    return math.degrees(math.acos(cos_z))


def cos_zenith(dt, lat, lon):
    """cos(SZA) = the air-mass factor mu0 used in the Beer-Lambert slant path."""
    return math.cos(math.radians(solar_zenith_deg(dt, lat, lon)))


def is_night(sza_deg, night_sza=NIGHT_SZA_DEG):
    """True when the sun is too low for a reliable sun/shade (OCA) verdict."""
    return sza_deg is not None and sza_deg >= night_sza


def direct_transmittance(cot, sza_deg):
    """Beer-Lambert direct-beam transmittance T = exp(-COT / cos(SZA)).

    Clear (COT 0 or None) -> 1.0. Below the horizon -> 0.0 (no direct beam)."""
    if cot is None or cot <= 0.0:
        return 1.0
    mu0 = math.cos(math.radians(sza_deg))
    if mu0 <= 0.0:
        return 0.0
    return math.exp(-float(cot) / mu0)


def slant_cot(cot, sza_deg):
    """Optical thickness along the slant path to the sun = COT / cos(SZA).
    The same cloud blocks more when the sun is low (the /cos(SZA) correction)."""
    mu0 = math.cos(math.radians(sza_deg))
    if mu0 <= 0.0:
        return float("inf")
    return float(cot) / mu0


def sun_state(cot, sza_deg, phase=None, *, cot_thin=3.0, cot_block=5.0,
              night_sza=NIGHT_SZA_DEG, ice_factor=ICE_FORWARD_SCATTER):
    """Sun/shade state at a point from optical thickness + solar geometry.

    Returns one of "sunny" / "dimmed" / "blocked", or None at night (no usable
    sun verdict). Thresholds are slant-corrected for the sun angle and relaxed for
    ice cloud (forward scattering keeps the disk visible to higher COT).
    """
    if sza_deg is not None and sza_deg >= night_sza:
        return None
    if cot is None or cot <= 0.0:
        return "sunny"
    thin, block = cot_thin, cot_block
    if phase == "ice":
        thin *= ice_factor
        block *= ice_factor
    eff = slant_cot(cot, sza_deg if sza_deg is not None else 0.0)
    if eff <= thin:
        return "sunny"
    if eff <= block:
        return "dimmed"
    return "blocked"
