"""Geostationary parallax over a fixed ground point.

MTG sits at the sub-satellite point (0 N, 0 E). The satellite zenith over
Budva is ~52 deg, so an elevated cloud is displaced in the nadir-projected
image away from the SSP by ~height * tan(satellite_zenith) — ~1.3x height, up
to ~13 km (~4 FCI pixels) for a 10 km top. A cloud truly over Budva appears
shifted toward the NE.

To read "what is really over the point" we either (a) sample a disc large enough
to absorb the shift (the cheap, robust default already in use), or (b) move the
sample centre by `parallax_offset` using a representative cloud-top height.

Geometry is dependency-free; bearings/distances reuse radar.calibration, matching
clouds/grid.py.
"""

import math

from radar import calibration

SSP_LAT = 0.0           # MTG sub-satellite point latitude
SSP_LON = 0.0           # MTG sub-satellite point longitude
EARTH_R_KM = 6371.0
GEO_ALT_KM = 35786.0    # geostationary altitude above the surface
_KM_PER_DEG = 111.32


def _central_angle_deg(lat, lon, ssp_lat, ssp_lon):
    a, b = math.radians(lat), math.radians(lon)
    c, d = math.radians(ssp_lat), math.radians(ssp_lon)
    cos_psi = math.sin(a) * math.sin(c) + math.cos(a) * math.cos(c) * math.cos(b - d)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_psi))))


def satellite_zenith_deg(lat, lon, ssp_lat=SSP_LAT, ssp_lon=SSP_LON,
                         sat_alt_km=GEO_ALT_KM, earth_r_km=EARTH_R_KM):
    """Angle between the local vertical and the satellite at a ground point.

    0 at the sub-satellite point, ~52 deg over Budva, ->90 near the disc edge."""
    psi = math.radians(_central_angle_deg(lat, lon, ssp_lat, ssp_lon))
    r_s = earth_r_km + sat_alt_km
    num = r_s * math.cos(psi) - earth_r_km
    den = math.sqrt(r_s * r_s + earth_r_km * earth_r_km
                    - 2.0 * earth_r_km * r_s * math.cos(psi))
    if den <= 0.0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, num / den))))


def parallax_offset(lat, lon, cth_m, ssp_lat=SSP_LAT, ssp_lon=SSP_LON):
    """(dlat, dlon) degrees to ADD to a ground point to land on where a cloud of
    top height `cth_m` truly over that point APPEARS in the nadir image — i.e. the
    radial shift AWAY from the sub-satellite point of magnitude
    cth * tan(satellite_zenith). Zero/None height -> no shift."""
    if not cth_m or cth_m <= 0.0:
        return (0.0, 0.0)
    satzen = satellite_zenith_deg(lat, lon, ssp_lat, ssp_lon)
    shift_km = (cth_m / 1000.0) * math.tan(math.radians(satzen))
    # Direction away from the SSP = opposite the initial bearing toward the SSP.
    bearing_to_ssp = calibration.bearing_deg(lat, lon, ssp_lat, ssp_lon)
    away = math.radians((bearing_to_ssp + 180.0) % 360.0)
    dlat = (shift_km * math.cos(away)) / _KM_PER_DEG
    dlon = (shift_km * math.sin(away)) / (_KM_PER_DEG * math.cos(math.radians(lat)))
    return (dlat, dlon)
