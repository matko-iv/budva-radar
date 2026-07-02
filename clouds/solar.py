"""Solar geometry + direct-beam transmittance — the sun/shade axis.

"Is there cloud" (CLM presence) and "is the sun blocked" are different
questions. Presence comes from the CLM mask; whether the sun gets through is
set by optical thickness and solar geometry, since a low sun makes the same
cloud block more. This module owns the second question:

  T_direct = exp(-COT / cos(SZA))                 (Beer-Lambert direct beam)

The solar zenith angle uses the NOAA solar-position algorithm (~0.5 deg
accuracy, ample for thresholding) rather than pulling in pyorbital.

Beer-Lambert is a lower bound on transmission — thin ice cloud forward-
scatters, so ice phase gets a higher blocking threshold (ice_factor). At
night (SZA >= night_sza) OCA COT is unreliable, so no sun verdict is
returned and the caller falls back to CLM presence + CTTH.
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


def solar_azimuth_deg(dt, lat, lon):
    """Solar azimuth (degrees, 0 = N, 90 = E, clockwise), for the sun-glint
    geometry. NOAA solar-position algorithm."""
    t = _to_utc(dt)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60.0 + t.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    tst = hour * 60.0 + eqtime + 4.0 * lon
    ha = math.radians(tst / 4.0 - 180.0)
    latr = math.radians(lat)
    # Azimuth from SOUTH (positive west), then rotate to from-north clockwise.
    az = math.atan2(math.sin(ha),
                    math.cos(ha) * math.sin(latr) - math.tan(decl) * math.cos(latr))
    return (math.degrees(az) + 180.0) % 360.0


def glint_angle(sza_deg, vza_deg, saa_deg, vaa_deg):
    """Sun-glint angle (degrees) between the satellite view direction and the
    specular reflection of the sun. Small angles (< ~25-30 deg) mark the
    glint zone over sea, where specular reflectance trips cloud tests and
    inflates cloud presence. `*_aa` are azimuths (deg).

        cos(glint) = cos(VZA)cos(SZA) - sin(VZA)sin(SZA)cos(VAA - SAA)
    """
    sza, vza = math.radians(sza_deg), math.radians(vza_deg)
    draa = math.radians(float(vaa_deg) - float(saa_deg))
    c = math.cos(vza) * math.cos(sza) - math.sin(vza) * math.sin(sza) * math.cos(draa)
    c = max(-1.0, min(1.0, c))
    return math.degrees(math.acos(c))


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


def cmf(cot, sza_deg, *, phase=None, ice_factor=ICE_FORWARD_SCATTER):
    """Cloud Modification Factor CMF = GHI_cloudy / GHI_clear, the "is it
    actually sunny" quantity (0 overcast -> 1 clear). Unlike
    direct_transmittance it credits diffuse forward-scattered light, so thin
    cloud stays bright even when its direct beam is a few percent. The
    sun/shade verdict runs on this via cmf_sun_state.

    Functional form: Papachristopoulou et al. (2024, AMT 17, 1851-1877),
    Eq. 2: CMF = 1 - tanh(b * COT**a). The paper's published a, b polynomials
    are degenerate as transcribed (they drive CMF -> 0 for every COT >= ~0.1;
    verified numerically), so the coefficients here are a re-fit to the
    paper's own unambiguous anchors: the COT -> 0 / inf limits, the SENSE2
    sky-state thresholds (clear ~ COT < 1, overcast ~ COT > 13), and the
    worked example (COT ~ 2.1 -> CMF ~ 0.8). a = 0.75 with b0 = 0.10
    reproduces all three; a mild air-mass term raises b toward low sun.
    Accuracy is anchor-level, not the paper's quoted 1.5%.

    Ice cloud forward-scatters more than the liquid cloud the fit assumes,
    so for phase == "ice" the optical thickness is divided by ice_factor.
    Clear (COT 0 or None) -> 1.0.
    """
    if cot is None or cot <= 0.0:
        return 1.0
    tau = float(cot)
    if phase == "ice" and ice_factor:
        tau /= float(ice_factor)
    sza = 0.0 if sza_deg is None else float(sza_deg)
    mu0 = max(math.cos(math.radians(min(abs(sza), 89.0))), 0.10)
    a = 0.75
    b = 0.10 * (1.0 + 0.10 * (1.0 / mu0 - 1.0))
    val = 1.0 - math.tanh(b * tau ** a)
    return max(0.0, min(1.0, val))


def cmf_sun_state(cmf_value, *, sunny_min=0.8, blocked_max=0.4, night=False):
    """Map a CMF to the sun/shade word:
        CMF >= sunny_min               -> "sunny"
        blocked_max < CMF < sunny_min  -> "dimmed"
        CMF <= blocked_max             -> "blocked"
    Returns None at night or when CMF is unavailable.
    """
    if night or cmf_value is None:
        return None
    if cmf_value >= sunny_min:
        return "sunny"
    if cmf_value <= blocked_max:
        return "blocked"
    return "dimmed"


def sun_state(cot, sza_deg, phase=None, *, cot_thin=3.0, cot_block=5.0,
              night_sza=NIGHT_SZA_DEG, ice_factor=ICE_FORWARD_SCATTER):
    """Legacy direct-beam sun/shade classifier: "sunny" / "dimmed" /
    "blocked", or None at night. Slant-corrected and relaxed for ice cloud.
    It under-reads thin forward-scattering cloud as dimmed/blocked when the
    scene is in fact sunny, so the verdict now uses cmf + cmf_sun_state;
    this stays for the sunTransmittance diagnostic and its unit tests."""
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
