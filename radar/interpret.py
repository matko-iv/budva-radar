"""Top-level interpretation: combines sampling + motion -> human-readable status.

Algorithm:
  1) For each source and its latest frame: sample concentric rings around the
     location.
  2) If we have >= 2 frames, compute a motion vector.
  3) Identify the radius where precipitation is strongest, and the bearing of
     that sector.
  4) If the motion direction matches the reverse-bearing toward the strongest
     sector, the rain is moving toward us.
  5) ETA = distance / speed (if approaching).
"""

import datetime
from pathlib import Path
from PIL import Image
import numpy as np

import config
from radar import fetch, calibration, colormap, sampling, motion

def _angular_diff(a, b):
    """Minimum angular distance between two angles in degrees (range [0, 180])."""
    if a is None or b is None:
        return None
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _load_rgb(path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.array(img)


def _classify_center_pixel(rgb_array, source_id: str, lat: float, lon: float):
    """Return (dBZ, wet_neighbour_count) for the exact pixel covering
    (lat, lon). dBZ is None if the pixel is background/out-of-image.
    wet_neighbour_count is the number of pixels in the 3x3 neighbourhood
    (8 pixels, centre excluded) that are >= RAIN_DBZ_THRESHOLD — used to
    enforce a speckle test on the strict "rain at location" signal.
    """
    cal = calibration.get_calibration(source_id)
    px, py = cal.latlon_to_pixel(lat, lon)
    H, W = rgb_array.shape[:2]
    xi, yi = int(round(px)), int(round(py))
    if not (0 <= xi < W and 0 <= yi < H):
        return None, 0
    arr = colormap.pixels_to_dbz(rgb_array[yi, xi].reshape(1, 3), source_id)
    centre_dbz = (None if arr.size == 0 or np.isnan(arr[0])
                  else float(arr[0]))
    # 3x3 neighbourhood (excluding the centre pixel itself)
    x0, x1 = max(0, xi - 1), min(W, xi + 2)
    y0, y1 = max(0, yi - 1), min(H, yi + 2)
    block = rgb_array[y0:y1, x0:x1].reshape(-1, 3)
    block_dbz = colormap.pixels_to_dbz(block, source_id)
    # Count wet neighbours (the centre is in this block — subtract its
    # contribution if it itself is wet so we get neighbours only).
    wet_total = int(np.nansum(block_dbz >= config.RAIN_DBZ_THRESHOLD))
    if centre_dbz is not None and centre_dbz >= config.RAIN_DBZ_THRESHOLD:
        wet_neighbours = max(0, wet_total - 1)
    else:
        wet_neighbours = wet_total
    return centre_dbz, wet_neighbours


def interpret_source(source_id: str, location: dict, radii_km: list) -> dict:
    """Interpret the latest frame (and the previous one, if present) for one source."""
    frames = fetch.list_cached_frames(source_id)
    if not frames:
        return {"source": source_id, "ok": False, "reason": "no_frames"}

    latest_path = frames[-1]
    latest_rgb = _load_rgb(latest_path)
    rings = sampling.sample_concentric(
        latest_rgb, source_id, location["lat"], location["lon"], radii_km
    )
    center_dbz, center_wet_neighbours = _classify_center_pixel(
        latest_rgb, source_id, location["lat"], location["lon"]
    )

    # Motion vector vs the previous frame + persistence check (rings & centre
    # are "confirmed" only when the same signal showed up in the previous
    # scan too — the SCIT/TITAN/KONRAD persistence rule).
    motion_info = None
    prev_rings = None
    prev_center_dbz = None
    prev_center_neighbours = 0
    if len(frames) >= 2:
        prev_path = frames[-2]
        try:
            prev_rgb = _load_rgb(prev_path)
            prev_rings = sampling.sample_concentric(
                prev_rgb, source_id, location["lat"], location["lon"], radii_km
            )
            prev_center_dbz, prev_center_neighbours = _classify_center_pixel(
                prev_rgb, source_id, location["lat"], location["lon"]
            )
            motion_info = motion.compute_motion_vector(
                prev_rgb, latest_rgb, source_id, location["lat"], location["lon"]
            )
            if motion_info is not None:
                try:
                    t_prev = motion._frame_timestamp(prev_path)
                    t_latest = motion._frame_timestamp(latest_path)
                    dt_min = (t_latest - t_prev).total_seconds() / 60.0
                    kmh = motion.estimate_kmh_from_motion(motion_info, dt_min)
                    motion_info["dt_minutes"] = round(dt_min, 1)
                    motion_info["speed_kmh"] = kmh
                except Exception:
                    pass
                # Tag confidence band so the UI / downstream can distinguish
                # "use as primary" from "use as hint only".
                conf = motion_info.get("confidence", 0) or 0
                if conf >= config.MOTION_MIN_CORRELATION:
                    motion_info["confidence_band"] = "high"
                elif conf >= config.MOTION_LOW_CONFIDENCE_MIN:
                    motion_info["confidence_band"] = "low"
                else:
                    motion_info["confidence_band"] = "noise"
        except Exception as e:
            motion_info = {"error": str(e)}

    # Annotate each ring with persistence info (was the same ring wet in
    # the previous scan too?). prev_rings may be None when only one frame
    # is cached — in that case no ring can be "confirmed".
    _annotate_persistence(rings, prev_rings)

    centre_confirmed = _check_centre_persistence(
        center_dbz, center_wet_neighbours,
        prev_center_dbz, prev_center_neighbours,
    )
    persistence_available = prev_rings is not None
    approaching = _is_approaching(
        rings, motion_info, center_dbz,
        centre_confirmed=centre_confirmed,
        persistence_available=persistence_available,
    )
    scenario = classify_scenario(rings, approaching, persistence_available)

    # Store the path RELATIVE to the repo root so the JSON is reproducible
    # across machines. Previously this was an absolute Windows path and
    # caused diff churn whenever a different clone pushed.
    try:
        _repo_root = Path(__file__).resolve().parent.parent
        frame_path_rel = str(latest_path.resolve().relative_to(_repo_root))
    except (ValueError, OSError):
        frame_path_rel = str(latest_path.name)

    return {
        "source": source_id,
        "ok": True,
        "frame_path": frame_path_rel.replace("\\", "/"),
        "frame_timestamp": motion._frame_timestamp(latest_path).isoformat()
            if "_" in latest_path.stem else None,
        "center_dbz": round(center_dbz, 1) if center_dbz is not None else None,
        "rings": rings,
        "motion": motion_info,
        "approaching": approaching,
        "scenario": scenario,
    }


# Max distance at which we will say "kisa se primice" (is_approaching=True).
# Anything farther only fires "kisa postoji u okolini", even when motion is
# aligned with us. Rationale: small isolated pulse storms live 20-30 min and
# travel 10-25 km/h (research doc 2026-05) so realistic range is ~10 km; a
# cell at 15+ km that "points at us" often dissipates before arrival.
# Large organized systems will be inside this radius soon enough.
APPROACHING_MAX_KM = 15.0

# Maximum angular deviation between the radar motion vector and the
# reverse-bearing toward the cell before we still call it "approaching".
# Previously ±45° (too loose — cells passing tangentially fired alarms);
# now ±15° so the cell must be heading more-or-less straight at us.
# At our 15 km max distance, ±15° = ~4 km of lateral spread.
APPROACH_TOLERANCE_DEG = 10.0


def _min_wet_for_ring(ring):
    """Threshold for treating a ring as containing actionable rain.

    The threshold is published by sampling.sample_concentric in the ring
    dict itself (`min_wet_threshold`), already scaled for the source's
    pixel size. We fall back to the old 5-pixel floor only if a ring lacks
    that field (e.g., out-of-image annulus).
    """
    if not ring:
        return sampling.MIN_WET_PIXELS_PER_ANNULUS
    t = ring.get("min_wet_threshold")
    return t if t is not None else sampling.MIN_WET_PIXELS_PER_ANNULUS


def _annotate_persistence(rings, prev_rings):
    """Mark each ring with `confirmed` (true iff this ring AND the matching
    ring in the previous scan both cleared their wet-pixel threshold).

    Without prev_rings we can't confirm anything — every ring stays as
    `confirmed=False, persistence_scans=1`, and downstream logic treats
    them as candidates only.
    """
    by_radius = {r.get("radius_km"): r for r in (prev_rings or [])}
    for r in rings:
        threshold = _min_wet_for_ring(r)
        n_now = r.get("n_wet", 0) or 0
        wet_now = n_now >= threshold
        prev = by_radius.get(r.get("radius_km"))
        if prev is None:
            r["confirmed"] = False
            r["persistence_scans"] = 1 if wet_now else 0
            continue
        prev_n = prev.get("n_wet", 0) or 0
        wet_prev = prev_n >= _min_wet_for_ring(prev)
        if wet_now and wet_prev:
            r["confirmed"] = True
            r["persistence_scans"] = 2
        elif wet_now:
            r["confirmed"] = False
            r["persistence_scans"] = 1  # candidate this scan only
        else:
            r["confirmed"] = False
            r["persistence_scans"] = 0


def _check_centre_persistence(center_dbz, center_neighbours,
                              prev_center_dbz, prev_center_neighbours):
    """Strict "rain at location" requires speckle-clean wet pixels on the
    marker in TWO consecutive scans. Returns True only if both scans had
    centre dBZ above the rain threshold AND >= SPECKLE_MIN_NEIGHBOURS
    wet neighbours."""
    def _wet(dbz, neighbours):
        return (dbz is not None
                and dbz >= config.RAIN_DBZ_THRESHOLD
                and neighbours >= sampling.SPECKLE_MIN_NEIGHBOURS)
    return _wet(center_dbz, center_neighbours) and _wet(prev_center_dbz,
                                                         prev_center_neighbours)


# --------------------------------------------------------------------------
# Scenario state machine (PDF section "Klasifikacija scenarija"):
#   CLEAR     -> nothing on the radar
#   BIO_NOISE -> only sub-rain widespread echo (insects, virga, bright band)
#   LIKELY_NO_RAIN -> wet echo present but not heading toward us
#   POSSIBLE  -> confirmed >= 25 dBZ within 150 km, approaching
#   LIKELY    -> confirmed >= 30 dBZ within 100 km, approaching
#   IMMINENT  -> confirmed >= 40 dBZ within 25 km, ETA <= 30 min
#   RAINING   -> rain at location (or confirmed echo within 10 km)
#   SEVERE    -> >= 50 dBZ core anywhere visible (hail / severe warning)
# Higher-severity states override lower-severity ones.
# --------------------------------------------------------------------------
SCENARIO_STATES = [
    "SEVERE", "RAINING", "IMMINENT", "LIKELY", "POSSIBLE",
    "LIKELY_NO_RAIN", "BIO_NOISE", "CLEAR",
]
SCENARIO_PRIORITY = {s: i for i, s in enumerate(SCENARIO_STATES)}  # lower = worse

SCENARIO_ACTION = {
    "SEVERE":         "Severe weather warning",
    "RAINING":        "Rain now",
    "IMMINENT":       "Alarm: rain imminent",
    "LIKELY":         "Notify (sat/lightning verify)",
    "POSSIBLE":       "Soft notify",
    "LIKELY_NO_RAIN": "Status only",
    "BIO_NOISE":      "Log only",
    "CLEAR":          "No action",
}

# A severe (>= 50 dBZ) core is treated as a direct threat to Budva only when
# it is this close, OR when it is approaching. Beyond this, it is mentioned as
# context but does not become the headline state (a hail core 150 km away
# moving inland is not a Budva warning).
SEVERE_THREAT_KM = 50.0

# Rain that is not approaching is only described as "nearby" within this
# range. Beyond it, non-approaching rain is reported as distant and not a
# threat — so we never claim "rain nearby" for a cell 50+ km away.
VICINITY_KM = 20.0


def _cell_phrase(km, card, dbz, intensity):
    """One consistent description of a single cell, e.g.
    'light rain (28 dBZ) at 5.3 km E'. `intensity` and `card` are already
    English (from colormap.classify_intensity / calibration.bearing_to_cardinal)."""
    txt = intensity or "precipitation"
    if dbz is not None:
        txt += f" ({dbz:.0f} dBZ)"
    if km is not None:
        txt += f" at {km:.1f} km"
        if card:
            txt += f" {card}"
    return txt


def classify_scenario(rings, approaching, persistence_available):
    """Single source of truth for one radar's interpretation.

    Produces ONE coherent state + ONE English narrative sentence in which
    every number (distance, bearing, intensity, motion, ETA) refers to the
    same cell — the closest confirmed cell from `approaching` — plus an
    optional secondary note if a far-off severe core also exists.

    rings:                 sampling rings, annotated with `confirmed`.
    approaching:           the dict returned by _is_approaching — the coherent
                           closest-cell data set (km, bearing, dBZ, ETA, ...).
    persistence_available: False when only one frame is cached.

    Returns {state, narrative, action, severe_present, severe_detail, eta_min}.
    """
    a = approaching or {}

    # --- The one cell every number refers to (closest confirmed rain) ---
    has_rain = bool(a.get("any_rain_within_radii"))
    rain_at_loc = bool(a.get("rain_at_location"))
    km = a.get("closest_rain_km")
    card = a.get("closest_rain_bearing_cardinal")
    dbz = a.get("closest_rain_intensity_dbz")
    intensity = a.get("closest_rain_intensity_label")
    is_appr = bool(a.get("is_approaching"))
    eta = a.get("eta_minutes")
    motion_card = a.get("motion_direction_cardinal")
    speed = a.get("motion_speed_kmh")
    cell = _cell_phrase(km, card, dbz, intensity)

    # --- Severe core scan (may be a different, farther cell) ---
    severe_ring = None
    if persistence_available:
        severe_ring = next(
            (r for r in rings
             if r.get("confirmed")
             and (r.get("max_dbz") or 0) >= config.SEVERE_DBZ),
            None,
        )
    severe_km = severe_card = severe_dbz = None
    if severe_ring is not None:
        severe_km = severe_ring.get("closest_wet_km") or severe_ring.get("radius_km")
        severe_card = (severe_ring.get("closest_wet_bearing_cardinal")
                       or severe_ring.get("strongest_bearing_cardinal"))
        severe_dbz = severe_ring.get("max_dbz")
    severe_present = severe_ring is not None
    # A severe core is a direct threat only if close, or if it is the cell
    # we're already tracking as approaching.
    severe_is_threat = severe_present and (
        (severe_km is not None and severe_km <= SEVERE_THREAT_KM)
        or (is_appr and dbz is not None and dbz >= config.SEVERE_DBZ)
    )
    severe_detail = None
    severe_note = ""
    if severe_present and not severe_is_threat:
        sev_loc = ""
        if severe_km is not None:
            sev_loc = f" at {severe_km:.0f} km"
            if severe_card:
                sev_loc += f" {severe_card}"
        sev_dbz_txt = f"~{severe_dbz:.0f} dBZ" if severe_dbz is not None else "strong"
        severe_detail = f"strong core ({sev_dbz_txt}){sev_loc}"
        # Only add a separate note when the severe core is at a clearly
        # different place than the cell we're already describing — otherwise
        # the primary sentence already covers that location.
        same_place = (km is not None and severe_km is not None
                      and abs(km - severe_km) <= 15)
        if not same_place:
            severe_note = f" Plus a {severe_detail} — too far to threaten Budva."

    def _result(state, narrative, eta_min=None):
        return {
            "state": state,
            "narrative": narrative + severe_note,
            "action": SCENARIO_ACTION[state],
            "severe_present": severe_present,
            "severe_detail": severe_detail,
            "eta_min": eta_min,
        }

    # ---- SEVERE: hail core that actually threatens Budva ----
    if severe_is_threat:
        sk = severe_km if severe_km is not None else km
        sc = severe_card if severe_card is not None else card
        sd = severe_dbz if severe_dbz is not None else dbz
        loc = ""
        if sk is not None:
            loc = f" at {sk:.1f} km"
            if sc:
                loc += f" {sc}"
        dbz_txt = f"{sd:.0f} dBZ" if sd is not None else "strong"
        if is_appr and eta is not None:
            tail = f", approaching, ETA ~{eta:.0f} min"
        elif sk is not None and sk <= SEVERE_THREAT_KM:
            tail = ", in the immediate vicinity"
        else:
            tail = ""
        return _result("SEVERE",
                       f"Severe core ({dbz_txt}){loc}, hail possible{tail}.",
                       eta_min=eta if is_appr else None)

    # ---- No confirmed rain at all: CLEAR / BIO_NOISE / unconfirmed ----
    if not has_rain:
        any_echo = any((r.get("n_echo", 0) or 0) > 0 for r in rings)
        any_wet_raw = any((r.get("n_wet_raw", 0) or 0) > 0 for r in rings)
        candidate = any(
            (r.get("n_wet", 0) or 0) >= _min_wet_for_ring(r)
            and not r.get("confirmed")
            for r in rings
        )
        if candidate and not persistence_available:
            return _result("LIKELY_NO_RAIN",
                           "Rain pixels in this scan, but no previous scan to "
                           "confirm them (waiting for the next one).")
        if candidate or any_wet_raw:
            return _result("LIKELY_NO_RAIN",
                           "Scattered rain pixels on the radar, below the "
                           "confirmation threshold (cluster too small).")
        if any_echo:
            return _result("BIO_NOISE",
                           "Only weak echo (<20 dBZ) on the radar — likely "
                           "insects, mist or bright-band; not rain.")
        return _result("CLEAR", "No echo on the radar within 150 km.")

    # ---- There IS confirmed rain. State from the single closest cell. ----
    if rain_at_loc:
        return _result("RAINING", f"Rain at the location: {cell}.", eta_min=0)

    if km is not None and km <= 10:
        move = ""
        if motion_card:
            move = (f" Cell is moving {motion_card}"
                    + (" (toward us)." if is_appr else " (will pass by)."))
        return _result("RAINING", f"Rain falling nearby: {cell}.{move}", eta_min=0)

    if is_appr:
        eta_txt = f", ETA ~{eta:.0f} min" if eta is not None else ""
        spd_txt = f" (~{speed:.0f} km/h)" if speed else ""
        if (dbz or 0) >= config.HEAVY_DBZ_THRESHOLD and km <= 25:
            return _result("IMMINENT",
                           f"Rain imminent: {cell}, approaching"
                           f"{spd_txt}{eta_txt}.", eta_min=eta)
        if (dbz or 0) >= config.MODERATE_DBZ and km <= 100:
            return _result("LIKELY",
                           f"Rain likely: {cell}, approaching"
                           f"{spd_txt}{eta_txt}.", eta_min=eta)
        return _result("POSSIBLE",
                       f"Rain possible: {cell}, approaching{spd_txt}{eta_txt}.",
                       eta_min=eta)

    # Confirmed rain present but not approaching us — phrase the WHY
    # accurately from the approaching reason so we never claim to know the
    # motion when we don't, and never call a 50+ km cell "nearby".
    reason = a.get("reason", "")
    if reason == "aligned_but_too_far":
        why = (f"heading our way but too far ({km:.0f} km) to reliably arrive"
               if km is not None else "heading our way but too far to reliably arrive")
    elif reason == "aligned_but_unconfirmed":
        why = "heading our way but not yet confirmed (waiting for the next scan)"
    elif reason in ("no_reliable_motion", "no_motion_data"):
        why = "motion can't be reliably estimated yet"
    elif motion_card:
        why = f"not heading toward us (moving {motion_card})"
    else:
        why = "not heading toward us"
    return _result("LIKELY_NO_RAIN", f"Rain present: {cell}, {why}.")




def _is_approaching(rings, motion_info, center_dbz=None,
                    centre_confirmed=False, persistence_available=False):
    """Heuristic: rain is approaching if:
      (a) there is a 'wet' sector within 100 km that is *confirmed*
          (>= threshold in the latest AND the previous scan),
      (b) the motion direction is opposite of the strongest-sector bearing.

    `center_dbz` is the dBZ value of the single pixel under the marker
    (None if background/out-of-image). `centre_confirmed` requires the
    marker pixel to be wet (with a clean 3x3 neighbourhood) in both
    consecutive scans before we set rain_at_location=True.

    `persistence_available` is False when only one frame is cached — in
    that case we degrade gracefully: wet rings are reported but flagged
    as `persistence_available=False` and never raise an "approaching"
    alarm. The data is shown, the user is not yet warned.

    Returns a dict with the qualitative assessment, or None.
    """
    if not rings:
        return None

    # Strict "rain at location" requires speckle-clean wet pixels on the
    # marker in TWO consecutive scans. Without persistence we cannot make
    # that claim — fall back to single-scan check.
    if persistence_available:
        rain_at_location = bool(centre_confirmed)
    else:
        rain_at_location = (center_dbz is not None
                            and center_dbz >= config.RAIN_DBZ_THRESHOLD)

    # A ring counts as actionable rain only when it cleared the distance-
    # aware threshold in BOTH the current and the previous scan. Operational
    # standard across SCIT / TITAN / KONRAD. Without a previous frame, we
    # report the rings as candidates (no alarms raised downstream).
    def _ring_counts(r):
        if persistence_available:
            return bool(r.get("confirmed"))
        return (r.get("n_wet", 0) or 0) >= _min_wet_for_ring(r)
    wet_rings = [r for r in rings if _ring_counts(r)]
    # Candidate rings: passed the wet threshold in the latest scan but were
    # not confirmed by the previous scan. Reported separately so the UI can
    # render "echo detected, awaiting next scan for confirmation".
    candidate_count = sum(
        1 for r in rings
        if (r.get("n_wet", 0) or 0) >= _min_wet_for_ring(r)
        and not _ring_counts(r)
    )
    if not wet_rings:
        return {"any_rain_within_radii": False,
                "rain_at_location": rain_at_location,
                "persistence_available": persistence_available,
                "candidate_unconfirmed_rings": candidate_count,
                "center_dbz": round(center_dbz, 1) if center_dbz is not None else None}

    closest = min(wet_rings, key=lambda r: r["radius_km"])
    bearing_to_rain = closest.get("strongest_bearing")

    # Exact closest-wet-pixel distance + bearing across all rings (not the
    # coarse ring radius). Used for precise distance reporting.
    closest_exact_km = None
    closest_exact_bearing = None
    closest_exact_dbz = None
    for r in wet_rings:
        ck = r.get("closest_wet_km")
        if ck is None:
            continue
        if closest_exact_km is None or ck < closest_exact_km:
            closest_exact_km = ck
            closest_exact_bearing = r.get("closest_wet_bearing")
            closest_exact_dbz = r.get("closest_wet_dbz")

    # Motion may be unavailable even if rain is detected (only 1 frame cached,
    # or correlation failed). Still report the rain detection.
    if motion_info is None or motion_info.get("error"):
        return {
            "any_rain_within_radii": True,
            "rain_at_location": rain_at_location,
            "persistence_available": persistence_available,
            "candidate_unconfirmed_rings": candidate_count,
            "closest_rain_km": round(closest_exact_km, 2) if closest_exact_km is not None else closest["radius_km"],
            "closest_rain_ring_km": closest["radius_km"],
            "closest_rain_bearing_deg": (
                closest_exact_bearing if closest_exact_bearing is not None else bearing_to_rain
            ),
            "closest_rain_bearing_cardinal": (
                calibration.bearing_to_cardinal(
                    closest_exact_bearing if closest_exact_bearing is not None else bearing_to_rain
                ) if (closest_exact_bearing or bearing_to_rain) is not None else None
            ),
            "closest_rain_intensity_dbz": (
                closest_exact_dbz if closest_exact_dbz is not None else closest.get("strongest_dbz")
            ),
            "closest_rain_intensity_label": colormap.classify_intensity(
                closest_exact_dbz if closest_exact_dbz is not None else closest.get("strongest_dbz")
            ),
            "is_approaching": False,
            "reason": "no_motion_data",
        }

    motion_dir = motion_info.get("direction_deg")
    motion_conf = motion_info.get("confidence", 0)

    base = {
        "any_rain_within_radii": True,
        "rain_at_location": rain_at_location,
        "persistence_available": persistence_available,
        "candidate_unconfirmed_rings": candidate_count,
        "closest_rain_km": round(closest_exact_km, 2) if closest_exact_km is not None else closest["radius_km"],
        "closest_rain_ring_km": closest["radius_km"],
        "closest_rain_bearing_deg": (
            closest_exact_bearing if closest_exact_bearing is not None else bearing_to_rain
        ),
        "closest_rain_bearing_cardinal": (
            calibration.bearing_to_cardinal(
                closest_exact_bearing if closest_exact_bearing is not None else bearing_to_rain
            ) if (closest_exact_bearing or bearing_to_rain) is not None else None
        ),
        "closest_rain_intensity_dbz": (
            closest_exact_dbz if closest_exact_dbz is not None else closest.get("strongest_dbz")
        ),
        "closest_rain_intensity_label": colormap.classify_intensity(
            closest_exact_dbz if closest_exact_dbz is not None else closest.get("strongest_dbz")
        ),
    }
    if bearing_to_rain is None or motion_dir is None or motion_conf < config.MOTION_MIN_CORRELATION:
        base.update({
            "is_approaching": False,
            "reason": "no_reliable_motion",
            "motion_confidence": motion_conf,
        })
        return base

    # Reverse bearing = direction from the rain toward us (where it's moving
    # if approaching).
    reverse = (bearing_to_rain + 180) % 360
    diff = _angular_diff(motion_dir, reverse)
    direction_aligned = diff is not None and diff < APPROACH_TOLERANCE_DEG

    # Distance gate: "primice se" only fires when the nearest wet pixel is
    # inside APPROACHING_MAX_KM. Beyond that, we keep is_approaching=False and
    # let the rendering use the lighter "kisa postoji u okolini" wording.
    dist_for_check = closest_exact_km if closest_exact_km is not None else closest["radius_km"]
    within_approach_range = (dist_for_check is not None
                             and dist_for_check <= APPROACHING_MAX_KM)
    # Persistence gate: an "approaching" alarm requires a previous-scan
    # confirmation. SCIT/TITAN/KONRAD all enforce >= 2 consecutive scans.
    is_appr = direction_aligned and within_approach_range and persistence_available

    eta_min = None
    if is_appr and motion_info.get("speed_kmh"):
        spd = motion_info["speed_kmh"]
        if spd > 1:  # ignore noise-level motion
            eta_min = round(dist_for_check / spd * 60, 1)

    if is_appr:
        reason = "approaching"
    elif direction_aligned and not within_approach_range:
        reason = "aligned_but_too_far"  # rain heading toward us but unlikely to arrive
    elif direction_aligned and not persistence_available:
        reason = "aligned_but_unconfirmed"  # need a second scan before alarming
    else:
        reason = "motion_not_aligned"

    base.update({
        "motion_direction_deg": motion_dir,
        "motion_direction_cardinal": calibration.bearing_to_cardinal(motion_dir),
        "motion_speed_kmh": motion_info.get("speed_kmh"),
        "motion_confidence": motion_conf,
        "motion_confidence_band": motion_info.get("confidence_band"),
        "is_approaching": bool(is_appr),
        "eta_minutes": eta_min,
        "angular_alignment_deg": diff,
        "reason": reason,
    })
    return base


def interpret_all() -> dict:
    """Main entry: returns interpretation from both sources + composite summary."""
    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": config.LOCATION,
        "radii_km": config.SAMPLE_RADII_KM,
        "sources": {},
    }
    for src_id in ["dhmz", "opera"]:
        out["sources"][src_id] = interpret_source(src_id, config.LOCATION,
                                                  config.SAMPLE_RADII_KM)

    # ------------------------------------------------------------------
    # ONE coherent interpretation per radar, driven entirely by that
    # radar's scenario. No competing text systems: the scenario narrative
    # is the single source of truth, and the composite picks the worst
    # state across radars for the headline.
    # ------------------------------------------------------------------
    SRC_LABEL = {"dhmz": "DHMZ Uljenje", "opera": "OPERA composite"}
    per_radar = {}
    summary_lines = []
    composite_state = None
    composite_narrative = None
    composite_action = None
    composite_src = None
    composite_severe = False
    composite_eta = None
    closest_eta = None

    for src_id, info in out["sources"].items():
        label = SRC_LABEL.get(src_id, src_id.upper())
        if not info.get("ok"):
            reason = info.get("reason", "?")
            per_radar[src_id] = {"state": "UNAVAILABLE",
                                 "narrative": f"nedostupno ({reason})"}
            summary_lines.append(f"{label}: nedostupno ({reason}).")
            continue

        sc = info.get("scenario") or {}
        state = sc.get("state", "CLEAR")
        narrative = sc.get("narrative", "")
        per_radar[src_id] = {
            "state": state,
            "narrative": narrative,
            "action": sc.get("action"),
            "severe_present": sc.get("severe_present", False),
            "eta_min": sc.get("eta_min"),
        }
        # ONE line per radar — the coherent narrative.
        summary_lines.append(f"{label}: [{state}] {narrative}")

        if sc.get("severe_present"):
            composite_severe = True
        eta_min = sc.get("eta_min")
        if eta_min is not None and (closest_eta is None or eta_min < closest_eta):
            closest_eta = eta_min
        if composite_state is None or (
            SCENARIO_PRIORITY.get(state, 99)
            < SCENARIO_PRIORITY.get(composite_state, 99)
        ):
            composite_state = state
            composite_narrative = narrative
            composite_action = sc.get("action")
            composite_eta = eta_min
            composite_src = src_id

    # Legacy booleans kept so existing consumers (the alert banner, the
    # weather-forecast card) still work — now derived from the scenario.
    rain_at_location = composite_state == "RAINING"
    rain_approaching = composite_state in ("IMMINENT", "LIKELY", "POSSIBLE")
    rain_in_vicinity = composite_state in (
        "IMMINENT", "LIKELY", "POSSIBLE", "LIKELY_NO_RAIN", "SEVERE",
    )

    out["summary"] = {
        # Single headline (worst state across radars)
        "scenario_state": composite_state,
        "scenario_narrative": composite_narrative,
        "scenario_action": composite_action,
        "scenario_source": composite_src,
        "severe_present": composite_severe,
        "scenario_eta_minutes": composite_eta,
        # Per-radar coherent interpretation (DHMZ and OPERA separately)
        "per_radar": per_radar,
        # Legacy compatibility flags (derived from scenario_state)
        "rain_approaching": rain_approaching,
        "rain_at_location": rain_at_location,
        "rain_in_vicinity": rain_in_vicinity,
        "closest_eta_minutes": closest_eta,
        "lines": summary_lines,
    }
    return out
