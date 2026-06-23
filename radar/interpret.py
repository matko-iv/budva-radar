"""Top-level interpretation: combines sampling + motion + tracking -> human-readable status."""

import datetime
import json
from pathlib import Path
from PIL import Image
import numpy as np

import config
from radar import fetch, calibration, colormap, sampling, motion
import nowcast
import tracking

# Track histories between runs. Persisted to disk so per-cell velocities work
# in ONE-SHOT runs too (GH Actions / cron run a fresh process each time; a
# memory-only cache meant every cell looked brand new and fell back to the
# scene-motion prior).
_TRACK_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "track_cache.json"
_TRACK_CACHE = None


def _track_cache():
    global _TRACK_CACHE
    if _TRACK_CACHE is None:
        try:
            with open(_TRACK_CACHE_PATH, encoding="utf-8") as f:
                _TRACK_CACHE = json.load(f)
        except Exception:
            _TRACK_CACHE = {}
    return _TRACK_CACHE


def _track_cache_put(source_id, frame_key, frame_time, summaries):
    cache = _track_cache()
    cache[source_id] = {"frame_key": frame_key, "frame_time": frame_time,
                        "summaries": summaries}
    try:
        _TRACK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRACK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, default=float)
    except Exception as e:
        print(f"  [track-cache] save failed: {e}")


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except Exception:
        return None

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
    
    # 1. Global motion vector (from motion.py) + the block/TREC dense motion
    # field (PDF Part B1): the single global vector assumes rigid-block motion;
    # the field captures differential motion/growth for advection.
    motion_info = None
    motion_field = None
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
            try:
                motion_field = motion.motion_field(prev_rgb, latest_rgb, source_id)
            except Exception as fe:
                print(f"  [{source_id}] motion field failed: {fe}")
        except Exception as e:
            motion_info = {"error": str(e)}

    # STAGE 4: raw ODIM volume (MeteoGate ORD) is the primary DATA source for
    # the dhmz/Uljenje interpretation — measured dBZ + RHOHV clutter filter
    # instead of colour classification. The PNG stays the display layer and
    # the automatic fallback whenever ORD fetch/decode fails.
    ord_grid = None
    if source_id == "dhmz" and getattr(config, "ORD_ENABLED", False):
        try:
            from radar import ord as ord_mod
            vol_path = ord_mod.fetch_latest()
            if vol_path is not None:
                ord_grid = ord_mod.load_grid(vol_path)
        except Exception as e:
            print(f"  [{source_id}] ORD unavailable, PNG fallback: {e}")

    # 2. STAGE 2: Object Extraction & ring sampling (ORD raw-dBZ or PNG colours)
    ord_volume = None
    if ord_grid is not None:
        data_source = "ord_pvol"
        current_cells = ord_mod.cells_from_grid(ord_grid, location["lat"], location["lon"])
        # Per-cell full-volume products (PDF C2/B2) attached BEFORE tracking, so
        # update_summaries can compute the VIL trend across frames. One volume
        # read, reused by STAGE 4b for the Budva column.
        try:
            from radar import volume as volume_mod
            ord_volume = volume_mod.read_volume(vol_path)
            for c in current_cells:
                prof = volume_mod.column_profile_at(ord_volume, c["lat"], c["lon"])
                prod = volume_mod.column_products(prof["heights_m"], prof["dbz"])
                c["vil_kg_m2"] = prod["vil_kg_m2"]
                c["echo_top_m"] = prod["echo_top_m"]
        except Exception as e:
            print(f"  [{source_id}] per-cell volume products failed: {e}")
        try:
            rings = ord_mod.sample_rings(ord_grid, location["lat"], location["lon"], radii_km)
        except Exception as e:
            rings = []
            print(f"  [{source_id}] ORD ring sampling failed: {e}")
        frame_ts = ord_grid["frame_timestamp_local"]
        frame_key = f"ord:{frame_ts}"
    else:
        data_source = "png"
        current_cells = tracking.extract_cells(latest_rgb, source_id, location["lat"], location["lon"])
        # Concentric-ring sampling for the transparent per-source detail table.
        # (The cell/nowcast path above is the brain; these rings are the raw view
        # the UI table shows.)
        try:
            rings = sampling.sample_concentric(latest_rgb, source_id, location["lat"], location["lon"], radii_km)
        except Exception as e:
            rings = []
            print(f"  [{source_id}] ring sampling failed: {e}")
        frame_ts = motion._frame_timestamp(latest_path).isoformat() if "_" in latest_path.stem else None
        frame_key = f"png:{latest_path.name}"
    # Annotate the beam-overshoot range-confidence taper on either path.
    for r in rings:
        r["confidence"] = _range_confidence(r.get("radius_km"))

    # Track continuity (disk-persisted): reuse summaries when the frame hasn't
    # changed (re-tracking an identical frame would zero all velocities), else
    # match against the previous frame with the TRUE time step between them.
    prev_entry = _track_cache().get(source_id) or {}
    if prev_entry.get("frame_key") == frame_key and prev_entry.get("summaries"):
        cell_summaries = prev_entry["summaries"]
    else:
        dt_min = None
        t_now, t_prev = _parse_iso(frame_ts), _parse_iso(prev_entry.get("frame_time"))
        if t_now and t_prev:
            dt_min = (t_now - t_prev).total_seconds() / 60.0
            if not (0.5 <= dt_min <= 180):
                dt_min = None  # gap too odd — use the nominal interval
        cell_summaries = tracking.update_summaries(
            current_cells, prev_entry.get("summaries") or [], motion_info, dt_min=dt_min)
        _track_cache_put(source_id, frame_key, frame_ts, cell_summaries)

    # 3. STAGE 2: Probabilistic Nowcast & Storm Mode Morphology
    nowcast_results = nowcast.arrival_nowcast(cell_summaries, location["lat"], location["lon"])
    storm_mode = nowcast.classify_storm_mode(current_cells, cell_summaries, motion_info)

    # STAGE 4: the "needs Doppler velocity to confirm" flags can now actually
    # be checked — the ORD volume carries VRADH. Run the gate-to-gate azimuthal
    # shear (mesocyclone proxy) at the strongest cell and report the measurement
    # alongside the suspicion. Confirmation aid only; the verdict is untouched.
    if (ord_grid is not None and current_cells
            and any("Doppler" in fl for fl in storm_mode.get("flags", []))):
        try:
            strong = max(current_cells, key=lambda c: c["max_dbz"])
            rc = ord_mod.rotation_check(vol_path, strong["lat"], strong["lon"])
            if rc is not None:
                storm_mode["doppler"] = rc
                ni_txt = (f"{rc['elangle']:.1f}°, Nyquist ±{rc['nyquist_ms']:.0f} m/s"
                          if rc.get("nyquist_ms") is not None else f"{rc.get('elangle', 0):.1f}°")
                if rc.get("max_shear_ms") is None:
                    msg = f"VRADH (Doppler, {ni_txt}) at the strongest cell: no valid velocity gates"
                elif rc["couplet"]:
                    msg = (f"VRADH (Doppler, {ni_txt}) at the strongest cell: velocity couplet, "
                           f"gate-to-gate shear {rc['couplet_shear_ms']:.0f} m/s — rotation SUPPORTED"
                           + (" (aliasing possible)" if rc.get("aliasing_possible") else ""))
                elif rc.get("limited_nyquist"):
                    # a >=20 m/s couplet physically FOLDS at this Nyquist — saying
                    # "not confirmed" would be false certainty
                    msg = (f"VRADH (Doppler, {ni_txt}) at the strongest cell: max gate-to-gate "
                           f"shear {rc['max_shear_ms']:.0f} m/s, but mesocyclone-scale rotation "
                           f"folds at this Nyquist — INCONCLUSIVE without dealiasing")
                else:
                    msg = (f"VRADH (Doppler, {ni_txt}) at the strongest cell: max gate-to-gate "
                           f"shear {rc['max_shear_ms']:.0f} m/s, no couplet — rotation NOT confirmed")
                storm_mode["flags"].append(msg)
        except Exception as e:
            print(f"  [{source_id}] VRADH rotation check failed: {e}")

    # STAGE 4b: full-volume column products over the location (PDF Part C2/C1).
    # VIL / 18-dBZ echo-top / VIL-density from ALL sweeps, the ZDR-column updraft
    # proxy, and the surface-rain confidence cue — the lowest beam is ~2.5 km up
    # over Budva at 130 km, so an aloft echo is NOT guaranteed to reach ground.
    # Purely additive (shipped under source.volume); the verdict is untouched.
    volume_products = None
    if ord_grid is not None and ord_volume is not None:
        try:
            from radar import volume as volume_mod
            vol = ord_volume
            prof = volume_mod.column_profile_at(vol, location["lat"], location["lon"])
            prod = volume_mod.column_products(prof["heights_m"], prof["dbz"])
            fl_m = float(getattr(config, "FREEZING_LEVEL_M", 3500.0))
            zdr_col = volume_mod.zdr_column(prof["heights_m"], prof["zdr"],
                                            prof["dbz"], fl_m)
            low_beam = prof["heights_m"][0] if prof["heights_m"] else None
            srconf = volume_mod.surface_rain_confidence(low_beam)
            volume_products = {
                **prod,
                "n_levels": len(prof["heights_m"]),
                "lowest_beam_m": low_beam,
                "ground_range_km": prof["ground_range_km"],
                "zdr_column": zdr_col,
                "surface_rain_confidence": srconf["confidence"],
                "surface_rain_confidence_reason": srconf["reason"],
                "freezing_level_m": fl_m,
            }
        except Exception as e:
            print(f"  [{source_id}] volume products failed: {e}")

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

    # Coastal-arrival score for the dominant cell (PDF Part C3): combine the
    # base arrival probability with the growth/decay trend and the Dinaric-ridge
    # dissipation filter (cells that must descend the seaward slope are
    # down-weighted). Additive; shipped under nowcast_details.coastal_arrival.
    if dom:
        from radar import coastal as coastal_mod
        dom_summ = next((s for s in cell_summaries
                         if s.get("id") == dom.get("track_id")), None)
        descends = coastal_mod.descends_seaward(
            dom.get("bearing_deg"),
            dom_summ.get("direction_deg") if dom_summ else None)
        ca = coastal_mod.coastal_arrival_score(
            base_prob=nowcast_results.get("p_rain", 0.0),
            vil_trend_per_min=dom_summ.get("vil_trend_per_min") if dom_summ else None,
            dbz_trend_per_min=dom_summ.get("dbz_trend_per_min") if dom_summ else None,
            descends_ridge=descends)
        ca["descends_seaward"] = descends
        nowcast_results["coastal_arrival"] = ca

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
                "vil_kg_m2": c.get("vil_kg_m2"),
                "vil_trend_per_min": s.get("vil_trend_per_min"),
            })

    return {
        "source": source_id,
        "ok": True,
        "frame_path": frame_path_rel.replace("\\", "/"),
        "frame_timestamp": frame_ts,
        "data_source": data_source,
        "motion": motion_info,
        "motion_field": motion_field,
        "approaching": approaching_legacy,
        "storm_mode": storm_mode,
        "scenario": scenario,
        "zr_scenario": "convective" if storm_mode["n_convective"] > 0 else "stratiform",
        "rings": rings,
        "cells": cells_catalog,
        "volume": volume_products,
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
        # SEVERE removed — it false-triggered on distant high-dBZ cells.
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

    # THE canonical Budva verdict — computed ONCE here, rendered verbatim by
    # every page (budva-radar index + radar-map, and the forecast page). Also
    # a per-source verdict so the per-radar summary lines come from the same
    # interpreter instead of being re-derived in browser JS.
    try:
        from radar import verdict as verdict_mod
        loc_name = config.LOCATION.get("name") or "Budva"
        for src_id, info in out["sources"].items():
            if info.get("ok"):
                f = verdict_mod.facts_from_source(info, loc_name)
                r = verdict_mod.interpret(f)
                info["verdict"] = {"state": r["state"], "headline": r["headline"],
                                   "narrative": r["narrative"]}
        out["summary"]["budva_verdict"] = verdict_mod.budva_verdict(out)
    except Exception as e:
        print(f"  [verdict] failed: {e}")
        out["summary"]["budva_verdict"] = None
    return out