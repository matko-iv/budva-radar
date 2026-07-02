"""Extract the facts the cloud verdict consumes, at a point + concentric rings.

Combines clouds/grid.py sampling (cloud fraction, top temp/height, optical
thickness, phase) with clouds/nowcast.py (approaching / clearing / ETA) into one
facts dict, plus a near-term sun outlook and per-ring fractions for the UI table.
"""

import config
from clouds import nowcast, parallax, solar
from radar import calibration

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


def _sun_outlook(now_clear, approaching, clearing, overcast, eta, night=False):
    # At night cloud is IR-detected only, so never promise "sun".
    if night:
        if now_clear:
            return "Clear night sky."
        if clearing and eta is not None:
            return f"Clearing in ~{round(eta)} min."
        if overcast:
            return "Overcast (IR) — stays closed for the next ~2 h."
        return "Cloudy night (IR detection)."
    if now_clear:
        if approaching and eta is not None:
            return f"Sunny now; clouds in ~{round(eta)} min."
        return "Sunny — sky stays clear for the next ~2 h."
    if clearing and eta is not None:
        return f"Sun in ~{round(eta)} min."
    if overcast:
        return "Sky stays closed for the next ~2 h."
    return "Variable — some sun likely."


def cloud_facts(field, motion, lat, lon, loc_name="Budva", cfg=None, gc_sky=None):
    """Full facts dict for one point (verdict contract + descriptors + rings).

    Two independent axes: cloud presence (cloudFracNow, from the CLM mask —
    thin cirrus counts) and sun/shade (sunState/transmittance, from OCA COT +
    solar zenith).

    `gc_sky` is an optional GeoColour read at the point ({cloudFrac,
    blockFrac}) used as a daytime cross-check that vetoes the OCA COT
    over-read — OCA sometimes retrieves a phantom thick ice shield where the
    visible picture is clear. The picture caps L2 cloud downward only (it can
    shrink a phantom, never add cloud, since sun-glint can falsely brighten
    it). Pass it only when GeoColour is usable (day, sun high enough);
    otherwise it must be None.
    """
    cfg = cfg or config.CLOUDS

    # GeoColour daytime caps (downward only). gc_not_thick = the picture shows no
    # optically-thick cloud over the point, so any high OCA COT there is a phantom.
    gc_cloud_cap = gc_block_cap = None
    gc_not_thick = False
    if gc_sky is not None:
        gc_cloud_cap = gc_sky.get("cloudFrac")
        gc_block_cap = gc_sky.get("blockFrac")
        gc_not_thick = (gc_block_cap is not None
                        and gc_block_cap <= cfg["frac_clear_max"])

    nc = nowcast.point_nowcast(field, motion, lat, lon, cfg)
    frac = nc["cloudFracNow"]
    if gc_cloud_cap is not None and frac is not None:
        frac = min(frac, float(gc_cloud_cap))

    now_radius = cfg.get("point_read_radius_km", config.SAMPLE_RADII_KM[0])
    desc_radius = config.SAMPLE_RADII_KM[1] if len(config.SAMPLE_RADII_KM) > 1 else 25

    night_sza = float(cfg.get("sun_night_sza", solar.NIGHT_SZA_DEG))
    sza = None
    if field.sensing_time:
        try:
            sza = solar.solar_zenith_deg(field.sensing_time, lat, lon)
        except Exception:
            sza = field.meta.get("sza_deg")
    is_night = bool(sza is not None and solar.is_night(sza, night_sza))
    sat_zen = round(parallax.satellite_zenith_deg(lat, lon), 1)

    # The clear/partly/overcast state runs on the sun-blocking "opaque" layer,
    # not raw CLM presence:
    # sky_cover = opaque + semi_weight*(total - opaque), semi_weight default 0.
    opaque_cover = field.cloud_fraction(lat, lon, now_radius, layer="opaque")
    if gc_block_cap is not None and opaque_cover is not None:
        opaque_cover = min(opaque_cover, float(gc_block_cap))
    if frac is None:
        sky_cover = None
    else:
        opq = opaque_cover or 0.0
        sky_cover = opq + cfg["semi_sky_weight"] * max(frac - opq, 0.0)

    # Describe whatever cloud is present, so a thin veil is labelled "cirrus"
    # rather than left blank; a truly clear sky still gets no type.
    has_cloud = frac is not None and frac > cfg["frac_clear_max"]
    ctt_k = field.sample_cloudy("ctt", lat, lon, desc_radius) if has_cloud else None
    cth_m = field.sample_cloudy("cth", lat, lon, desc_radius) if has_cloud else None
    cot = field.sample_cloudy("cot", lat, lon, desc_radius) if has_cloud else None
    phase = field.dominant_phase(lat, lon, desc_radius) if has_cloud else None
    # When the picture shows nothing optically thick, the OCA COT is the
    # over-read; cap the descriptor so the label reads "thin (cirrus)"
    # consistently with the sun verdict, not "thick (COT 256)".
    if gc_not_thick and cot is not None:
        cot = min(cot, cfg["cot_thin_max"])

    band = _height_band(cth_m, cfg)
    thick = _thickness(cot, cfg)

    # Sun/shade at the point: median COT over cloudy cells (robust to noise
    # and parallax). Optionally sample where overhead cloud actually appears
    # (the parallax shift); off by default since the disc already absorbs it
    # and shifting would de-sync the read from the on-map marker.
    s_lat, s_lon, parallax_km = lat, lon, None
    if cfg.get("parallax_correct", False) and cth_m:
        dlat, dlon = parallax.parallax_offset(lat, lon, cth_m)
        s_lat, s_lon = lat + dlat, lon + dlon
        parallax_km = round(calibration.haversine_km(lat, lon, s_lat, s_lon), 1)
    cot_med = field.sample_cloudy("cot", s_lat, s_lon, now_radius, reducer="median")
    # Same veto on the sun-axis COT: a phantom thick reading must not drive
    # CMF -> 0 when the picture shows the sun getting through. Picture clear:
    # 0 COT (sunny); picture thin: cap to the thin range.
    if gc_sky is not None and cot_med is not None:
        if frac is not None and frac <= cfg["frac_clear_max"]:
            cot_med = 0.0
        elif gc_not_thick:
            cot_med = min(cot_med, cfg["cot_thin_max"])

    sun_state = transmittance = cmf_val = None
    if not is_night and cot_med is not None:
        ice_factor = float(cfg.get("sun_ice_factor", solar.ICE_FORWARD_SCATTER))
        # The sun verdict runs on the global-irradiance CMF, not the direct
        # beam: thin forward-scattering cloud keeps the sky bright, so a
        # cirrostratus reads "sunny" despite a few-percent direct beam.
        cmf_val = solar.cmf(cot_med, sza if sza is not None else 0.0,
                            phase=phase, ice_factor=ice_factor)
        sun_state = solar.cmf_sun_state(
            cmf_val, sunny_min=float(cfg.get("cmf_sunny_min", 0.80)),
            blocked_max=float(cfg.get("cmf_blocked_max", 0.40)))
        # Direct-beam transmittance kept as a labelled diagnostic only.
        transmittance = solar.direct_transmittance(
            cot_med, sza if sza is not None else 0.0)
    elif not is_night and sky_cover is not None:
        # Picture-only field (HighSight / GeoColour): no OCA COT, so the
        # brightness sun-blocking cover is the signal, mapped with the same
        # clear/overcast thresholds as the cloud level.
        sun_state = ("blocked" if sky_cover >= cfg["frac_overcast_min"]
                     else "sunny" if sky_cover <= cfg["frac_clear_max"]
                     else "dimmed")

    thin_veil = bool(frac is not None and frac > cfg["frac_clear_max"]
                     and sky_cover is not None and sky_cover <= cfg["frac_clear_max"])

    now_clear = sky_cover is not None and sky_cover <= cfg["frac_clear_max"]
    overcast = sky_cover is not None and sky_cover >= cfg["frac_overcast_min"]

    # Keep "cloud at location" consistent with the (possibly GeoColour-capped)
    # presence — downward only, so a vetoed phantom no longer reads as cloud.
    cloud_at_location = bool(nc["cloudAtLocation"]) and (
        frac is not None and frac > cfg["frac_clear_max"])
    gc_capped = bool(gc_sky is not None and (
        (gc_cloud_cap is not None and nc["cloudFracNow"] is not None
         and gc_cloud_cap < nc["cloudFracNow"]) or gc_not_thick))

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
        "cloudAtLocation": cloud_at_location,
        "geocolourCapped": gc_capped,
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
        # --- sun / shade axis (separate from presence above) ---
        "szaDeg": None if sza is None else round(sza, 1),
        "isNight": is_night,
        "sunState": sun_state,
        "sunTransmittance": None if transmittance is None else round(transmittance, 3),
        "cmf": None if cmf_val is None else round(cmf_val, 3),
        "cotMedian": None if cot_med is None else round(cot_med, 1),
        "satelliteZenithDeg": sat_zen,
        "parallaxShiftKm": parallax_km,
        "sunOutlook": _sun_outlook(now_clear, nc["approaching"], nc["clearing"],
                                   overcast, nc["etaMin"], night=is_night),
        "rings": rings,
        "series": nc["series"],
    }
