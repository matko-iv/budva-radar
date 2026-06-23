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


def solar_azimuth_deg(dt, lat, lon):
    """Solar azimuth (degrees, 0 = N, 90 = E, clockwise) — needed for the
    sun-glint geometry (PDF Part A1). NOAA solar-position algorithm."""
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
    SPECULAR reflection of the sun (PDF Part A1). Small angles (< ~25-30 deg)
    mark the glint zone over sea, where specular reflectance trips cloud tests
    and inflates the cloud-presence number. `*_aa` are azimuths (deg).

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


def cmf(cot, sza_deg):
    """Cloud Modification Factor CMF = GHI_cloudy / GHI_clear (PDF Part A2).

    Papachristopoulou et al. (2024, AMT 17, 1851-1877, doi:10.5194/amt-17-1851-
    2024), Eq. 2:  CMF = 1 - tanh(b * COT**a), with a, b 4th-order polynomials in
    SZA (degrees). Ranges 0 (overcast) -> 1 (clear). This is the physically
    correct "is it actually sunny" quantity (global irradiance), unlike the
    direct-beam transmittance, because it keeps thin/forward-scattering cloud
    bright.

    !!! VERIFICATION PENDING — DIAGNOSTIC ONLY, NOT USED FOR THE VERDICT !!!
    The coefficients below are transcribed verbatim from the paper, but with the
    literal `tanh(b * COT**a)` grouping they are degenerate (CMF ~ 0 for every
    COT >= ~0.1 at all SZA — the opposite of the paper's stated behaviour). The
    grouping/coefficients must be confirmed against the TYPESET Eq. 2 / Fig. 2a
    before this can replace `sun_state()`. Until then `cloud_facts` reports it as
    a labelled diagnostic and the sun/shade verdict stays on `sun_state()`.

    Clear (COT 0 or None) -> 1.0, which is the one reliable, unambiguous limit.
    """
    if cot is None or cot <= 0.0:
        return 1.0
    sza = 0.0 if sza_deg is None else float(sza_deg)
    a = (2.24e-1 + 2.81e-4 * sza - 2.18e-5 * sza**2
         + 3.71e-7 * sza**3 - 2.65e-9 * sza**4)
    b = (12.2 + 5.27e-3 * sza - 2.24e-3 * sza**2
         + 8.33e-6 * sza**3 + 3.94e-8 * sza**4)
    val = 1.0 - math.tanh(b * float(cot) ** a)
    return max(0.0, min(1.0, val))


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
