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
    approaching = _is_approaching(
        rings, motion_info, center_dbz,
        centre_confirmed=centre_confirmed,
        persistence_available=(prev_rings is not None),
    )

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

    # Composite summary: which sources report approaching rain + descriptive lines
    summary_lines = []
    any_approaching = False
    any_at_location = False
    any_in_vicinity = False  # rain detected but neither approaching nor at-location
    closest_eta = None
    for src_id, info in out["sources"].items():
        if not info.get("ok"):
            summary_lines.append(f"[{src_id}] unavailable: {info.get('reason', '?')}")
            continue
        appr = info.get("approaching") or {}
        if not appr.get("any_rain_within_radii"):
            # No ring qualified as "actionable rain", but there may still be
            # sub-threshold wet pixels somewhere in the 150 km horizon. Surface
            # the nearest one so the user knows what's actually on the radar
            # instead of a false "no precipitation" claim.
            nearest_any_km = None
            nearest_any_card = None
            nearest_any_dbz = None
            for r in info.get("rings", []):
                ck = r.get("closest_wet_km")
                if ck is None:
                    continue
                if nearest_any_km is None or ck < nearest_any_km:
                    nearest_any_km = ck
                    nearest_any_card = r.get("closest_wet_bearing_cardinal")
                    nearest_any_dbz = r.get("closest_wet_dbz")
            if nearest_any_km is not None:
                intensity_label = colormap.classify_intensity(nearest_any_dbz)
                summary_lines.append(
                    f"[{src_id}] nearest echo (ignored — sub-threshold or > {APPROACHING_MAX_KM:.0f} km): "
                    f"{intensity_label} at {nearest_any_km} km {nearest_any_card or '?'}"
                )
            else:
                summary_lines.append(
                    f"[{src_id}] no precipitation within {max(config.SAMPLE_RADII_KM)} km"
                )
            continue
        km = appr.get("closest_rain_km")
        card = appr.get("closest_rain_bearing_cardinal", "?")
        intensity = appr.get("closest_rain_intensity_label", "?")
        if appr.get("rain_at_location"):
            any_at_location = True
            summary_lines.append(
                f"[{src_id}] RAIN AT LOCATION: {intensity} ({km} km {card})"
            )
        elif appr.get("is_approaching"):
            any_approaching = True
            eta = appr.get("eta_minutes")
            spd = appr.get("motion_speed_kmh")
            summary_lines.append(
                f"[{src_id}] APPROACHING: {intensity} at {km} km {card}, "
                f"moving toward us at {spd} km/h, ETA ~{eta} min"
            )
            if eta is not None and (closest_eta is None or eta < closest_eta):
                closest_eta = eta
        else:
            # Rain is present but not "approaching" — either motion not aligned,
            # or cell is too far to be a reliable predictor.
            # "rain_in_vicinity" only fires for echoes inside APPROACHING_MAX_KM
            # — beyond that we don't bother the user (consistent with the
            # approaching gate, both at 15 km).
            in_vicinity = (isinstance(km, (int, float))
                           and km <= APPROACHING_MAX_KM)
            if in_vicinity:
                any_in_vicinity = True
            reason = appr.get("reason", "")
            if reason == "aligned_but_too_far":
                why = f"aligned but too far ({km} km > {APPROACHING_MAX_KM:.0f} km — likely to dissipate)"
            else:
                why = f"not heading toward us (motion: {appr.get('motion_direction_cardinal','?')})"
            label = "precipitation present" if in_vicinity else "distant precipitation (>15 km, ignored)"
            summary_lines.append(
                f"[{src_id}] {label}: {intensity} at {km} km {card}, "
                f"{why}"
            )

    out["summary"] = {
        "rain_approaching": any_approaching,
        "rain_at_location": any_at_location,
        "rain_in_vicinity": any_in_vicinity,
        "closest_eta_minutes": closest_eta,
        "lines": summary_lines,
    }
    return out
