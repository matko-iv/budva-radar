"""Extract the facts the cloud verdict consumes, at a point + concentric rings.

Combines clouds/grid.py sampling (cloud fraction, top temp/height, optical
thickness, phase) with clouds/nowcast.py (approaching / clearing / ETA) into one
facts dict, plus a near-term sun outlook and per-ring fractions for the UI table.
"""

import config
from clouds import nowcast

_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E",
             "NE": "SW", "SW": "NE", "SE": "NW", "NW": "SE"}

_COMMON_NAME = {
    ("high", "thin"): "cirrus",        ("high", "thick"): "cirrostratus",
    ("mid", "thin"): "altocumulus",    ("mid", "thick"): "altostratus",
    ("low", "thin"): "stratocumulus",  ("low", "thick"): "stratus",
}


def _height_band(cth_m, cfg):
    if cth_m is None:
        return None
    if cth_m < cfg["height_low_max_m"]:
        return "low"
    if cth_m < cfg["height_mid_max_m"]:
        return "mid"
    return "high"


def _thickness(cot, cfg):
    if cot is None:
        return None
    return "thin" if cot <= cfg["cot_thin_max"] else "thick"


def _type_label(band, thickness):
    if not band:
        return None
    base = f"{band} {thickness} cloud" if thickness else f"{band} cloud"
    name = _COMMON_NAME.get((band, thickness))
    return f"{base} ({name})" if name else base


def _sun_outlook(now_clear, approaching, clearing, overcast, eta):
    if now_clear:
        if approaching and eta is not None:
            return f"Sunny now; clouds in ~{round(eta)} min."
        return "Sunny — sky stays clear for the next ~2 h."
    if clearing and eta is not None:
        return f"Sun in ~{round(eta)} min."
    if overcast:
        return "Sky stays closed for the next ~2 h."
    return "Variable — some sun likely."


def cloud_facts(field, motion, lat, lon, loc_name="Budva", cfg=None):
    """Full facts dict for one point (verdict contract + descriptors + rings)."""
    cfg = cfg or config.CLOUDS
    nc = nowcast.point_nowcast(field, motion, lat, lon, cfg)
    frac = nc["cloudFracNow"]

    now_radius = config.SAMPLE_RADII_KM[0]
    desc_radius = config.SAMPLE_RADII_KM[1] if len(config.SAMPLE_RADII_KM) > 1 else 25
    cloudy = frac is not None and frac > cfg["frac_clear_max"]
    ctt_k = field.sample_cloudy("ctt", lat, lon, desc_radius) if cloudy else None
    cth_m = field.sample_cloudy("cth", lat, lon, desc_radius) if cloudy else None
    cot = field.sample_cloudy("cot", lat, lon, desc_radius) if cloudy else None
    phase = field.dominant_phase(lat, lon, desc_radius) if cloudy else None

    band = _height_band(cth_m, cfg)
    thick = _thickness(cot, cfg)

    # Effective sky cover for the clear/partly/overcast decision: OPAQUE cloud
    # blocks the sun fully; CONTAMINATED (semitransparent) cloud counts only a
    # little (sun gets through). So sky_cover = opaque + semi_weight*(total-opaque).
    # This uses the satellite's own CLM classification (frac=total, opaque=code 3).
    opaque_cover = field.cloud_fraction(lat, lon, now_radius, layer="opaque")
    if frac is None:
        sky_cover = None
    else:
        opq = opaque_cover or 0.0
        sky_cover = opq + cfg["semi_sky_weight"] * max(frac - opq, 0.0)
    thin_veil = bool(frac is not None and frac > cfg["frac_clear_max"]
                     and sky_cover is not None and sky_cover <= cfg["frac_clear_max"])

    now_clear = sky_cover is not None and sky_cover <= cfg["frac_clear_max"]
    overcast = sky_cover is not None and sky_cover >= cfg["frac_overcast_min"]

    rings = []
    for r in config.SAMPLE_RADII_KM:
        rf = field.cloud_fraction(lat, lon, r)
        ro = field.cloud_fraction(lat, lon, r, layer="opaque")
        rings.append({"radius_km": r,
                      "cloud_fraction": None if rf is None else round(rf, 3),
                      "opaque_fraction": None if ro is None else round(ro, 3)})

    return {
        "locationName": loc_name,
        "cloudFracNow": frac,
        "opaqueFracNow": None if opaque_cover is None else round(opaque_cover, 3),
        "skyCoverEff": None if sky_cover is None else round(sky_cover, 3),
        "thinVeil": thin_veil,
        "cloudAtLocation": nc["cloudAtLocation"],
        "approaching": nc["approaching"],
        "clearing": nc["clearing"],
        "etaMin": nc["etaMin"],
        "motionCardinal": nc["motionCardinal"],
        "fromCardinal": _OPPOSITE.get(nc["motionCardinal"]),
        "motionSpeedKmh": nc["motionSpeedKmh"],
        "cloudTopHeightM": None if cth_m is None else round(cth_m, 0),
        "cloudTopTempC": None if ctt_k is None else round(ctt_k - 273.15, 1),
        "heightBand": band,
        "opticalThickness": None if cot is None else round(cot, 1),
        "thickness": thick,
        "phase": phase,
        "cloudTypeLabel": _type_label(band, thick),
        "sunOutlook": _sun_outlook(now_clear, nc["approaching"], nc["clearing"],
                                   overcast, nc["etaMin"]),
        "rings": rings,
        "series": nc["series"],
    }
