"""Top-level interpretation: combines sampling + motion + tracking -> human-readable status."""

import datetime
from pathlib import Path
from PIL import Image
import numpy as np

import config
from radar import fetch, calibration, colormap, sampling, motion
import nowcast
import tracking

# Memory cache for track histories between runs
_TRACK_CACHE = {}

def _load_rgb(path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.array(img)


# Range-dependent confidence taper (beam overshoot at long range). Distance-
# from-point proxy; full confidence within ~60 km, declining to a floor by
# 150 km. Used to annotate the per-source ring table.
RANGE_TAPER_FULL_KM = 60.0
RANGE_TAPER_MIN_CONF = 0.3


def _range_confidence(km):
    if km is None:
        return 1.0
    if km <= RANGE_TAPER_FULL_KM:
        return 1.0
    frac = (km - RANGE_TAPER_FULL_KM) / (150.0 - RANGE_TAPER_FULL_KM)
    return round(max(RANGE_TAPER_MIN_CONF, 1.0 - (1.0 - RANGE_TAPER_MIN_CONF) * min(frac, 1.0)), 2)


def interpret_source(source_id: str, location: dict, radii_km: list) -> dict:
    """Interpret the latest frame using Stage 2 object tracking and probabilistic nowcasting."""
    frames = fetch.list_cached_frames(source_id)
    if not frames:
        return {"source": source_id, "ok": False, "reason": "no_frames"}

    latest_path = frames[-1]
    latest_rgb = _load_rgb(latest_path)
    
    # 1. Global motion vector (from motion.py)
    motion_info = None
    if len(frames) >= 2:
        prev_path = frames[-2]
        try:
            prev_rgb = _load_rgb(prev_path)
            motion_info = motion.compute_motion_vector(
                prev_rgb, latest_rgb, source_id, location["lat"], location["lon"]
            )
            if motion_info is not None:
                t_prev = motion._frame_timestamp(prev_path)
                t_latest = motion._frame_timestamp(latest_path)
                dt_min = (t_latest - t_prev).total_seconds() / 60.0
                motion_info["speed_kmh"] = motion.estimate_kmh_from_motion(motion_info, dt_min)
                conf = motion_info.get("confidence", 0) or 0
                motion_info["confidence_band"] = "high" if conf >= config.MOTION_MIN_CORRELATION else "low"
        except Exception as e:
            motion_info = {"error": str(e)}

    # 2. STAGE 2: Object Extraction & Tracking
    current_cells = tracking.extract_cells(latest_rgb, source_id, location["lat"], location["lon"])

    # Concentric-ring sampling for the transparent per-source detail table.
    # (The cell/nowcast path above is the brain; these rings are the raw view
    # the UI table shows.) Annotate the beam-overshoot range-confidence taper.
    try:
        rings = sampling.sample_concentric(latest_rgb, source_id, location["lat"], location["lon"], radii_km)
        for r in rings:
            r["confidence"] = _range_confidence(r.get("radius_km"))
    except Exception as e:
        rings = []
        print(f"  [{source_id}] ring sampling failed: {e}")
    
    # Load history, update, and save back to cache
    prev_summaries = _TRACK_CACHE.get(source_id, [])
    cell_summaries = tracking.update_summaries(current_cells, prev_summaries, motion_info)
    _TRACK_CACHE[source_id] = cell_summaries

    # 3. STAGE 2: Probabilistic Nowcast & Storm Mode Morphology
    nowcast_results = nowcast.arrival_nowcast(cell_summaries, location["lat"], location["lon"])
    storm_mode = nowcast.classify_storm_mode(current_cells, cell_summaries, motion_info)

    # Convert probabilistic nowcast into Legacy states to satisfy the UI/Verification.
    # IMPORTANT: the cell detector finds cells across the WHOLE image — for the
    # OPERA composite that is all of Europe — so a storm 3000 km away must NOT
    # be reported as "rain nearby". Bound the "in the area" / "closest rain"
    # fields to the monitored vicinity (the outermost sampling ring). The
    # probabilistic nowcast already discounts distant cells, so is_approaching
    # and P_rain are unaffected by this bound.
    VICINITY_MAX_KM = max(config.SAMPLE_RADII_KM)
    nearby = [c for c in current_cells
              if c.get("edge_km") is not None and c["edge_km"] <= VICINITY_MAX_KM]
    nearest = min(nearby, key=lambda c: c["edge_km"]) if nearby else None
    dom = nowcast_results["dominant"]

    if nowcast_results["approaching"] and dom:
        # the arriving cell drives the headline numbers
        ck, cb, cd, cl = dom["dist_km"], dom["bearing_cardinal"], dom["max_dbz"], dom["intensity_label"]
    elif nearest:
        ck = round(nearest["edge_km"], 1)
        cb = nearest.get("bearing_cardinal")
        cd = nearest.get("max_dbz")
        cl = colormap.classify_intensity(nearest.get("max_dbz"))
    else:
        ck = cb = cd = None
        cl = "none"

    approaching_legacy = {
        "is_approaching": nowcast_results["approaching"],
        "eta_minutes": nowcast_results["eta_minutes"],
        "any_rain_within_radii": len(nearby) > 0,
        "rain_at_location": any(c["contains_location"] for c in current_cells),
        "closest_rain_km": ck,
        "closest_rain_intensity_dbz": cd,
        "closest_rain_bearing_cardinal": cb,
        "closest_rain_intensity_label": cl,
        "motion_speed_kmh": dom["speed_kmh"] if dom else None,
        "reason": f"Probabilistic Nowcast P_rain: {nowcast_results['p_rain']}" if nowcast_results["approaching"] else "P_rain below threshold",
        "nowcast_details": nowcast_results
    }

    scenario = classify_scenario(approaching_legacy, storm_mode)

    try:
        _repo_root = Path(__file__).resolve().parent.parent
        frame_path_rel = str(latest_path.resolve().relative_to(_repo_root))
    except (ValueError, OSError):
        frame_path_rel = str(latest_path.name)

    # Browser-side per-point nowcast (radar-map.html): ship the tracked cell
    # catalog so docs/nowcast-browser.js can replay arrival_nowcast for ANY
    # clicked point. Absolute lat/lon (location-independent) + each cell's own
    # track velocity is all the JS port needs; it recomputes edge/bearing per
    # point. ALL cells are shipped (NOT bounded to Budva's vicinity): a point
    # clicked near the frame edge can have a relevant cell that is far from Budva
    # but close to that point, and the nowcast's own reach gate (~240 km) handles
    # distance. Only for DHMZ — radar-map is DHMZ-based and its cells are few and
    # local; the OPERA composite is Europe-wide. Purely additive.
    cells_catalog = []
    if source_id == "dhmz":
        for s in cell_summaries:
            c = s.get("latest") or {}
            if c.get("lat") is None or c.get("lon") is None:
                continue
            cells_catalog.append({
                "id": s["id"],
                "lat": round(c["lat"], 4),
                "lon": round(c["lon"], 4),
                "equiv_diam_km": round(c["equiv_diam_km"], 2),
                "max_dbz": c["max_dbz"],
                "cell_type": c["cell_type"],
                "speed_kmh": s.get("speed_kmh"),
                "direction_deg": s.get("direction_deg"),
                "dbz_trend_per_min": s.get("dbz_trend_per_min"),
                "trend": s.get("trend"),
            })

    return {
        "source": source_id,
        "ok": True,
        "frame_path": frame_path_rel.replace("\\", "/"),
        "frame_timestamp": motion._frame_timestamp(latest_path).isoformat() if "_" in latest_path.stem else None,
        "motion": motion_info,
        "approaching": approaching_legacy,
        "storm_mode": storm_mode,
        "scenario": scenario,
        "zr_scenario": "convective" if storm_mode["n_convective"] > 0 else "stratiform",
        "rings": rings,
        "cells": cells_catalog,
    }


def classify_scenario(approaching, storm_mode):
    """Generates human-readable state merging the deterministic closest cell and severe flags."""
    a = approaching or {}
    
    has_rain = a.get("any_rain_within_radii")
    rain_at_loc = a.get("rain_at_location")
    is_appr = a.get("is_approaching")
    km = a.get("closest_rain_km")
    dbz = a.get("closest_rain_intensity_dbz")
    card = a.get("closest_rain_bearing_cardinal")
    eta = a.get("eta_minutes")
    intensity = a.get("closest_rain_intensity_label")
    
    # Parse Storm Mode Flags
    severe_present = False
    severe_detail = None
    if storm_mode and storm_mode.get("flags"):
        severe_present = True
        severe_detail = " | ".join(storm_mode["flags"])

    def _result(state, narrative):
        return {
            "state": state,
            "narrative": narrative + (f" (Caution: {severe_detail})" if severe_detail else ""),
            "action": state,
            "severe_present": severe_present,
            "severe_detail": severe_detail,
            "eta_min": eta if is_appr else None,
        }

    txt = intensity or "precipitation"
    if dbz is not None: txt += f" ({dbz:.0f} dBZ)"
    if km is not None: txt += f" at {km:.1f} km"
    if card: txt += f" {card}"

    if rain_at_loc:
        return _result("RAINING", f"Rain at the location: {txt}.")
        
    if is_appr:
        if (dbz or 0) >= config.SEVERE_DBZ:
            return _result("SEVERE", f"Severe core approaching: {txt}, ETA ~{eta:.0f} min.")
        if (dbz or 0) >= config.HEAVY_DBZ_THRESHOLD and km <= 25:
            return _result("IMMINENT", f"Rain imminent: {txt}, ETA ~{eta:.0f} min.")
        if (dbz or 0) >= config.MODERATE_DBZ:
            return _result("LIKELY", f"Rain likely: {txt}, ETA ~{eta:.0f} min.")
        return _result("POSSIBLE", f"Rain possible: {txt}, ETA ~{eta:.0f} min.")
        
    if has_rain:
        return _result("LIKELY_NO_RAIN", f"Rain present: {txt}, {a.get('reason')}.")
        
    return _result("CLEAR", "No echo on the radar within target area.")

def interpret_all() -> dict:
    """Main entry: returns interpretation from both sources + composite summary."""
    out = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": config.LOCATION,
        "sources": {},
    }
    for src_id in ["dhmz", "opera"]:
        out["sources"][src_id] = interpret_source(src_id, config.LOCATION, config.SAMPLE_RADII_KM)

    SRC_LABEL = {"dhmz": "DHMZ Uljenje", "opera": "OPERA composite"}
    per_radar = {}
    summary_lines = []
    
    # Priority scaling
    SCENARIO_PRIORITY = {
        "SEVERE": 0, "RAINING": 1, "IMMINENT": 2, "LIKELY": 3, 
        "POSSIBLE": 4, "LIKELY_NO_RAIN": 5, "BIO_NOISE": 6, "CLEAR": 7, "UNAVAILABLE": 99
    }
    
    composite_state = None
    composite_narrative = None
    composite_src = None
    composite_severe = False
    closest_eta = None

    for src_id, info in out["sources"].items():
        label = SRC_LABEL.get(src_id, src_id.upper())
        if not info.get("ok"):
            per_radar[src_id] = {"state": "UNAVAILABLE", "narrative": "nedostupno"}
            continue

        sc = info.get("scenario") or {}
        state = sc.get("state", "CLEAR")
        narrative = sc.get("narrative", "")
        per_radar[src_id] = {
            "state": state,
            "narrative": narrative,
            "severe_present": sc.get("severe_present", False),
            "eta_min": sc.get("eta_min"),
        }
        summary_lines.append(f"{label}: [{state}] {narrative}")

        if sc.get("severe_present"):
            composite_severe = True
        eta_min = sc.get("eta_min")
        if eta_min is not None and (closest_eta is None or eta_min < closest_eta):
            closest_eta = eta_min
            
        if composite_state is None or (SCENARIO_PRIORITY.get(state, 99) < SCENARIO_PRIORITY.get(composite_state, 99)):
            composite_state = state
            composite_narrative = narrative
            composite_src = src_id

    out["summary"] = {
        "scenario_state": composite_state,
        "scenario_narrative": composite_narrative,
        "scenario_source": composite_src,
        "severe_present": composite_severe,
        "scenario_eta_minutes": closest_eta,
        "per_radar": per_radar,
        "rain_approaching": composite_state in ("IMMINENT", "LIKELY", "POSSIBLE", "SEVERE"),
        "rain_at_location": composite_state == "RAINING",
        "rain_in_vicinity": composite_state in ("IMMINENT", "LIKELY", "POSSIBLE", "SEVERE", "LIKELY_NO_RAIN"),
        "closest_eta_minutes": closest_eta,
        "lines": summary_lines,
    }
    return out