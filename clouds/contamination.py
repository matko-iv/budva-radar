"""Suppress likely-FALSE cloud at sun-glint / coastal pixels (PDF Part A1).

EUMETSAT's CLM (and the NWC SAF CMa it descends from) is deliberately
"clear-conservative": when in doubt it flags a pixel cloudy, which structurally
over-detects cloud over two hard surfaces — the sun-glint sea and the coastline.
Budva is a coastal pixel right next to the Adriatic, the textbook worst case, and
this is the "90% cloud / blocks sun 67% while it is clearly sunny" false alarm.

Two mitigations the PDF prescribes, combined conservatively here:

  * sun-glint mask — compute the glint angle (between the satellite view direction
    and the specular reflection of the sun) per pixel and flag the glint zone
    (glint angle < glint_max_deg, ~25-30 deg), where specular sea reflectance
    trips the cloud tests;
  * retrieval consistency — a CLM-"cloudy" pixel with NO retrievable cloud top
    (CTTH) and NO optical thickness (OCA) is a strong false-alarm candidate; a
    real cloud has a retrievable top/COT, glint and coastline false-cloud do not.

A pixel is dropped ONLY when it is BOTH in the glint zone AND lacks any CTTH/OCA
corroboration. That is deliberately conservative: genuine cloud — including thin
cirrus, which still has a cloud top — is always kept; only the geometry-flagged,
unretrievable "cloud" over the glint sea/coast is removed. The geometry reuses the
already-unit-tested scalar helpers in clouds/solar.py + clouds/parallax.py, so
there is no new trigonometry to get wrong.
"""

import numpy as np

import config
from clouds import parallax, solar
from radar import calibration


def glint_angle_grid(lats, lons, sensing_time):
    """Per-pixel sun-glint angle (deg) over a regular lat/lon grid, for the MTG
    sub-satellite point (0 N, 0 E). Small angles mark the specular glint zone.

    Vectorized over the (cheap) ~150x170 subset by mapping the scalar solar /
    parallax helpers across the grid — same numbers as their unit tests, no
    duplicated math."""
    lon2d, lat2d = np.meshgrid(np.asarray(lons, float), np.asarray(lats, float))
    sza = np.vectorize(lambda la, lo: solar.solar_zenith_deg(sensing_time, la, lo))(lat2d, lon2d)
    saa = np.vectorize(lambda la, lo: solar.solar_azimuth_deg(sensing_time, la, lo))(lat2d, lon2d)
    vza = np.vectorize(parallax.satellite_zenith_deg)(lat2d, lon2d)              # satellite zenith
    vaa = np.vectorize(lambda la, lo: calibration.bearing_deg(                    # view azimuth to SSP
        la, lo, parallax.SSP_LAT, parallax.SSP_LON))(lat2d, lon2d)
    glint = np.vectorize(solar.glint_angle)(sza, vza, saa, vaa)
    return np.asarray(glint, dtype="float64")


def suppress_mask(cloudy, in_glint, has_retrieval):
    """The PDF A1 decision, as a pure boolean array: drop a CLM-cloudy pixel only
    where it is in the sun-glint zone AND has no corroborating CTTH/OCA retrieval.
    Returns the boolean `drop` array (same shape as the inputs)."""
    cloudy = np.asarray(cloudy, dtype=bool)
    in_glint = np.asarray(in_glint, dtype=bool)
    has_retrieval = np.asarray(has_retrieval, dtype=bool)
    return cloudy & in_glint & (~has_retrieval)


def clean_field(field, cfg=None):
    """Drop sun-glint / coastal false-cloud from a CloudField IN PLACE and return
    it (PDF A1). No-op when disabled, at night (no glint after dark), or with no
    sensing time. Records the number of dropped pixels in field.meta."""
    cfg = cfg or config.CLOUDS
    if not cfg.get("glint_suppress", True):
        return field
    st = field.sensing_time
    if not st:
        return field
    lats, lons = field.lats, field.lons
    if len(lats) == 0 or len(lons) == 0:
        return field

    # No specular sun glint once the sun is down — skip (presence is the only
    # usable signal at night anyway, per the module design).
    center_lat = float(0.5 * (lats[0] + lats[-1]))
    center_lon = float(0.5 * (lons[0] + lons[-1]))
    night_sza = float(cfg.get("sun_night_sza", solar.NIGHT_SZA_DEG))
    try:
        if solar.is_night(solar.solar_zenith_deg(st, center_lat, center_lon), night_sza):
            return field
    except Exception:
        return field

    L = field.layers
    mask = L.get("mask")
    if mask is None:
        return field

    glint = glint_angle_grid(lats, lons, st)
    in_glint = glint < float(cfg.get("glint_max_deg", 25.0))

    cloudy = np.nan_to_num(np.asarray(mask, dtype="float64"), nan=0.0) >= 0.5
    has_retrieval = np.zeros(cloudy.shape, dtype=bool)
    for k in ("ctt", "cth", "cot"):           # any valid cloud-top / COT retrieval
        a = L.get(k)
        if a is not None:
            has_retrieval |= np.isfinite(np.asarray(a, dtype="float64"))

    drop = suppress_mask(cloudy, in_glint, has_retrieval)
    n = int(drop.sum())
    if n:
        for k in ("mask", "frac", "opaque"):  # dropped -> clear
            if k in L:
                L[k] = np.where(drop, 0.0, L[k])
        for k in ("ctt", "cth", "cot", "phase"):  # dropped -> no cloud-only value
            if k in L:
                L[k] = np.where(drop, np.nan, L[k])
    field.meta["glint_dropped"] = n
    return field
