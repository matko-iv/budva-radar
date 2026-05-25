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

    # Motion vector vs the previous frame
    motion_info = None
    if len(frames) >= 2:
        prev_path = frames[-2]
        try:
            prev_rgb = _load_rgb(prev_path)
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
        except Exception as e:
            motion_info = {"error": str(e)}

    approaching = _is_approaching(rings, motion_info)

    return {
        "source": source_id,
        "ok": True,
        "frame_path": str(latest_path),
        "frame_timestamp": motion._frame_timestamp(latest_path).isoformat()
            if "_" in latest_path.stem else None,
        "rings": rings,
        "motion": motion_info,
        "approaching": approaching,
    }


def _is_approaching(rings, motion_info):
    """Heuristic: rain is approaching if:
      (a) there is a 'wet' sector within 100 km,
      (b) the motion direction is opposite of the strongest-sector bearing.

    Returns a dict with the qualitative assessment, or None.
    """
    if not rings:
        return None

    # First check: is there any rain at all?
    # Require a small cluster of wet pixels (not just one stray legend match)
    # before reporting precipitation. sampling.MIN_WET_PIXELS_PER_ANNULUS sets
    # the floor - tuned so real cells always pass and lone false positives don't.
    min_wet = sampling.MIN_WET_PIXELS_PER_ANNULUS
    wet_rings = [r for r in rings if r.get("n_wet", 0) >= min_wet]
    if not wet_rings:
        return {"any_rain_within_radii": False}

    closest = min(wet_rings, key=lambda r: r["radius_km"])
    bearing_to_rain = closest.get("strongest_bearing")

    # Motion may be unavailable even if rain is detected (only 1 frame cached,
    # or correlation failed). Still report the rain detection.
    if motion_info is None or motion_info.get("error"):
        return {
            "any_rain_within_radii": True,
            "closest_rain_km": closest["radius_km"],
            "closest_rain_bearing_deg": bearing_to_rain,
            "closest_rain_bearing_cardinal": (
                calibration.bearing_to_cardinal(bearing_to_rain) if bearing_to_rain is not None else None
            ),
            "closest_rain_intensity_dbz": closest.get("strongest_dbz"),
            "closest_rain_intensity_label": colormap.classify_intensity(closest.get("strongest_dbz")),
            "is_approaching": False,
            "reason": "no_motion_data",
        }

    motion_dir = motion_info.get("direction_deg")
    motion_conf = motion_info.get("confidence", 0)

    base = {
        "any_rain_within_radii": True,
        "closest_rain_km": closest["radius_km"],
        "closest_rain_bearing_deg": bearing_to_rain,
        "closest_rain_bearing_cardinal": (
            calibration.bearing_to_cardinal(bearing_to_rain) if bearing_to_rain is not None else None
        ),
        "closest_rain_intensity_dbz": closest.get("strongest_dbz"),
        "closest_rain_intensity_label": colormap.classify_intensity(closest.get("strongest_dbz")),
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
    is_appr = diff is not None and diff < 45  # within +/- 45 deg tolerance
    eta_min = None
    if is_appr and motion_info.get("speed_kmh"):
        spd = motion_info["speed_kmh"]
        if spd > 1:  # ignore noise-level motion
            eta_min = round(closest["radius_km"] / spd * 60, 1)

    base.update({
        "motion_direction_deg": motion_dir,
        "motion_direction_cardinal": calibration.bearing_to_cardinal(motion_dir),
        "motion_speed_kmh": motion_info.get("speed_kmh"),
        "motion_confidence": motion_conf,
        "is_approaching": bool(is_appr),
        "eta_minutes": eta_min,
        "angular_alignment_deg": diff,
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
    closest_eta = None
    for src_id, info in out["sources"].items():
        if not info.get("ok"):
            summary_lines.append(f"[{src_id}] unavailable: {info.get('reason', '?')}")
            continue
        appr = info.get("approaching") or {}
        if not appr.get("any_rain_within_radii"):
            summary_lines.append(f"[{src_id}] no precipitation within {max(config.SAMPLE_RADII_KM)} km")
            continue
        km = appr.get("closest_rain_km")
        card = appr.get("closest_rain_bearing_cardinal", "?")
        intensity = appr.get("closest_rain_intensity_label", "?")
        if appr.get("is_approaching"):
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
            summary_lines.append(
                f"[{src_id}] precipitation present: {intensity} at {km} km {card}, "
                f"but not heading toward us (motion: {appr.get('motion_direction_cardinal','?')})"
            )

    out["summary"] = {
        "rain_approaching": any_approaching,
        "closest_eta_minutes": closest_eta,
        "lines": summary_lines,
    }
    return out
