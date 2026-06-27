"""One-shot cloud pipeline: fetch latest EUMETSAT cloud frame -> interpret at
Budva -> write output/cloud_status.json, docs/cloud_data.js (inline for the
browser), docs/cloud_status.json (public), docs/latest_cloud.png.

    python run_clouds.py            # live (needs EUMETSAT creds + pinned ids)
    python run_clouds.py --demo     # synthetic frame, no credentials needed

Mirrors run.py for the radar pipeline; fully independent of it.
"""

import datetime
import json
import sys
from pathlib import Path

import numpy as np

import config
from clouds import (fetch, highsight, interpret, motion as cmotion, render,
                    verdict, visible)
from clouds.grid import CloudField, downsample_for_browser

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"
OUTPUT_JSON = BASE_DIR / "output" / "cloud_status.json"
OUTPUT_JS = DOCS_DIR / "cloud_data.js"
DOCS_STATUS_JSON = DOCS_DIR / "cloud_status.json"
LATEST_PNG = DOCS_DIR / "latest_cloud.png"
DOCS_HISTORY = DOCS_DIR / "cloud_history"
PREVIEW_SCALE = 4


def _dt_min(prev, curr):
    try:
        t0 = datetime.datetime.fromisoformat(prev.sensing_time)
        t1 = datetime.datetime.fromisoformat(curr.sensing_time)
        d = (t1 - t0).total_seconds() / 60.0
        return d if 0.5 <= d <= 180 else 10.0
    except Exception:
        return 10.0


def _render_map(field, cfg, loc, gc_rgb=None):
    """Write the preview PNG and return its [W, H]. Prefer EUMETSAT GeoColour
    (shows real cumulus, matches EUMETView); fall back to the L2-derived overlay
    if GeoColour is disabled or unavailable. Reuses gc_rgb if already fetched."""
    if cfg.get("use_geocolour_map", True) and gc_rgb is not None:
        try:
            W, H = visible.render_map_png(cfg, loc, LATEST_PNG, source_image=gc_rgb)
            return [W, H]
        except Exception as e:
            print(f"  GeoColour map render failed ({e}); using L2 render",
                  file=sys.stderr)
    render.to_png(field, LATEST_PNG, scale=PREVIEW_SCALE)
    H, W = field.shape
    return [W * PREVIEW_SCALE, H * PREVIEW_SCALE]


def publish_history(cfg, loc):
    """Render the cached HighSight frames from the past N hours to
    docs/cloud_history/<ts>.png (with the Budva marker, like latest_cloud.png) and
    return a manifest [{t, image}] ascending in time — the page's 2 h history loop.
    Already-rendered frames are reused; anything outside the window is pruned."""
    from PIL import Image
    hours = float(cfg.get("highsight_history_hours", 2.0))
    DOCS_HISTORY.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    manifest, keep = [], set()
    for png in sorted(highsight._frame_dir().glob("*.png")):
        try:                                    # cache name: YYYYmmdd_HHMMSS_<sha>.png
            dt = datetime.datetime.strptime("_".join(png.stem.split("_")[:2]),
                                            "%Y%m%d_%H%M%S")
        except Exception:
            continue
        if dt < cutoff:
            continue
        name = dt.strftime("%Y%m%dT%H%M%SZ") + ".png"
        out = DOCS_HISTORY / name
        if not out.exists():
            try:
                visible.render_map_png(cfg, loc, out, source_image=Image.open(png),
                                       attribution="HighSight ©")
            except Exception as e:
                print(f"  history render failed ({png.name}): {e}", file=sys.stderr)
                continue
        keep.add(name)
        manifest.append({"t": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "image": f"cloud_history/{name}"})
    for old in DOCS_HISTORY.glob("*.png"):       # prune frames outside the window
        if old.name not in keep:
            try:
                old.unlink()
            except Exception:
                pass
    manifest.sort(key=lambda m: m["t"])
    return manifest


def _highsight_parts(loc, cfg):
    """Build the HighSight (picture) field + motion + facts + map, mirroring SKALA
    RAIN: fetch the visible tiles, advect successive frames for the nowcast, read
    the picture at every point. Returns (field, mot, facts, verdict_source,
    img_size, sensing_time) or None if HighSight is unavailable (-> L2 fallback)."""
    try:
        hs_field, hs_rgb, hs_time = highsight.fetch_field(cfg)
    except Exception as e:
        print(f"  HighSight unavailable ({e}); falling back to L2", file=sys.stderr)
        return None
    highsight.save_frame(hs_field, hs_time, rgb_image=hs_rgb)
    prev, curr = highsight.latest_two_fields()
    field = curr or hs_field
    mot = None
    if prev is not None:
        mot = cmotion.compute_motion(prev, field, loc["lat"], loc["lon"],
                                     _dt_min(prev, field))
    # Picture-only field (no OCA COT): cloud_facts runs the SAME advection nowcast
    # and per-point disc reads, with the brightness sun-axis fallback.
    facts = interpret.cloud_facts(field, mot, loc["lat"], loc["lon"], loc["name"], cfg)
    try:
        W, H = visible.render_map_png(cfg, loc, LATEST_PNG, source_image=hs_rgb,
                                      attribution="HighSight ©")
        img_size = [W, H]
    except Exception as e:
        print(f"  HighSight map render failed ({e})", file=sys.stderr)
        render.to_png(field, LATEST_PNG, scale=PREVIEW_SCALE)
        h, w = field.shape
        img_size = [w * PREVIEW_SCALE, h * PREVIEW_SCALE]
    history = publish_history(cfg, loc)
    return field, mot, facts, "HighSight", img_size, hs_time, history


def build_status(field, prev_field, data_source):
    loc = config.LOCATION
    cfg = config.CLOUDS

    # HighSight (visible picture) is the active interim source; the L2/GeoColour
    # path below stays in place behind its flags for the ongoing L2 fix.
    use_hs = cfg.get("use_highsight", False)
    parts = _highsight_parts(loc, cfg) if use_hs else None
    if use_hs and parts is None:
        # The user selected HighSight — do NOT silently serve an EUMETSAT verdict.
        raise RuntimeError(
            "HighSight is the selected SKALA CLOUD source but it is UNAVAILABLE "
            "(missing HIGHSIGHT_KEY or fetch failed). Refusing to fall back to the "
            "EUMETSAT L2 verdict. Set the key (env HIGHSIGHT_KEY or highsight_key.txt) "
            "and re-run, or set use_highsight=False to use L2 on purpose.")
    if parts is not None:
        field, mot, facts, verdict_source, img_size, sensing_time, history = parts
        source_name, gc_time = "HighSight satellite", None
        return _assemble_status(field, mot, facts, verdict_source, img_size,
                                sensing_time, gc_time, source_name, data_source, cfg, loc,
                                history=history)

    mot = None
    if prev_field is not None:
        mot = cmotion.compute_motion(prev_field, field, loc["lat"], loc["lon"],
                                     _dt_min(prev_field, field))

    # GeoColour (what EUMETView shows) drives BOTH the Budva verdict and the map.
    # Fetch the LATEST frame ONCE; the L2 field is the fallback when GeoColour
    # can't be fetched.
    gc_rgb = None
    gc_time = None
    if cfg.get("use_geocolour_map", True) or cfg.get("use_geocolour_verdict", False):
        try:
            gc_rgb, gc_time = visible.fetch_geocolour(cfg)
        except Exception as e:
            print(f"  GeoColour unavailable ({e}); using L2 field", file=sys.stderr)

    # Default verdict source is the L2 retrieval (CLM presence + OCA COT + solar
    # zenith). GeoColour is a rendered picture, not a measurement, so it only
    # drives the verdict when explicitly opted in AND the sun is high enough that
    # brightness is a usable cloud proxy (no glint/twilight/night); otherwise it
    # falls back to L2 (PDF Section 5).
    use_gc = (gc_rgb is not None and cfg.get("use_geocolour_verdict", False)
              and visible.geocolour_verdict_ok(cfg, loc, gc_time))
    if use_gc:
        sky = visible.budva_sky_from_geocolour(gc_rgb, cfg, loc)
        facts = visible.geocolour_facts(sky, loc, cfg, motion=mot)
        verdict_source = "GeoColour"
    else:
        # L2 verdict, with the GeoColour picture as a DAYTIME cross-check that vetoes
        # the OCA optical-thickness over-read (a phantom thick high-ice shield where
        # the picture is clear). Downward-only + day-gated, so glint/night can never
        # ADD cloud; at night / low sun gc_sky stays None and pure L2 is used.
        gc_sky = None
        if (gc_rgb is not None and cfg.get("use_geocolour_crosscheck", True)
                and visible.geocolour_verdict_ok(cfg, loc, gc_time)):
            try:
                gc_sky = visible.budva_sky_from_geocolour(gc_rgb, cfg, loc)
            except Exception as e:
                print(f"  GeoColour cross-check skipped ({e})", file=sys.stderr)
        facts = interpret.cloud_facts(field, mot, loc["lat"], loc["lon"],
                                      loc["name"], cfg, gc_sky=gc_sky)
        verdict_source = ("L2-COT+GeoColour"
                          if facts.get("geocolourCapped") else "L2-COT")

    # Preview PNG (north-up) + its pixel size, so the page can place the marker.
    # Reuses the already-fetched GeoColour image so the marker sits exactly on
    # the pixels the verdict measured.
    img_size = _render_map(field, cfg, loc, gc_rgb=gc_rgb)
    sensing_time = gc_time or field.sensing_time
    return _assemble_status(field, mot, facts, verdict_source, img_size,
                            sensing_time, gc_time, "EUMETSAT cloud products",
                            data_source, cfg, loc)


def _assemble_status(field, mot, facts, verdict_source, img_size, sensing_time,
                     gc_time, source_name, data_source, cfg, loc, history=None):
    """Pack the common status dict (shared by the HighSight and L2 paths)."""
    status = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": loc,
        "source": {"name": source_name, "ok": True,
                   "sensing_time": sensing_time,
                   "data_source": data_source, "verdict_source": verdict_source,
                   "geocolour_time": gc_time},
        "motion": None if mot is None else {
            "direction_deg": mot["direction_deg"],
            "direction_cardinal": mot["direction_cardinal"],
            "speed_kmh": mot["speed_kmh"], "confidence": mot["confidence"],
        },
        "facts": facts,
        "field": {
            **downsample_for_browser(field),
            "bbox": cfg["bbox"], "image": LATEST_PNG.name, "image_size": img_size,
            "history": history,        # past-2h frames for the page loop (None on L2)
            # full motion (incl. deg/min) for the browser-side nowcast replay
            "motion": mot,
        },
        "params": {
            "frac_clear_max": cfg["frac_clear_max"],
            "frac_overcast_min": cfg["frac_overcast_min"],
            "height_low_max_m": cfg["height_low_max_m"],
            "height_mid_max_m": cfg["height_mid_max_m"],
            "cot_thin_max": cfg["cot_thin_max"],
            "semi_sky_weight": cfg["semi_sky_weight"],
            "nowcast_lead_step_min": cfg["nowcast_lead_step_min"],
            "nowcast_lead_max_min": cfg["nowcast_lead_max_min"],
            "nowcast_dir_spread_deg": cfg["nowcast_dir_spread_deg"],
            "nowcast_dir_growth_deg_per_min": cfg["nowcast_dir_growth_deg_per_min"],
            "sample_radius_now_km": config.SAMPLE_RADII_KM[0],
            "point_read_radius_km": cfg.get("point_read_radius_km", config.SAMPLE_RADII_KM[0]),
            "sample_radii_km": config.SAMPLE_RADII_KM,
        },
        "nwp_forecast_url": config.NWP_FORECAST_URL,
        "summary": {},
    }
    status["summary"]["cloud_verdict"] = verdict.cloud_verdict(status)
    return status


def _demo_field(sensing_time, lon_edge):
    """Synthetic frame: a mid-level altostratus deck west of lon_edge advecting
    east toward Budva (42.28, 18.84). Lets the whole pipeline + page run offline."""
    lats, lons = fetch.target_grid(config.CLOUDS)
    lon2d = np.broadcast_to(lons, (len(lats), len(lons)))
    cloudy = lon2d < lon_edge
    mask = cloudy.astype(float)
    nan = np.full(mask.shape, np.nan)
    return CloudField(lats, lons, {
        "mask": mask, "frac": mask, "opaque": mask,   # demo deck = opaque cloud
        "ctt": np.where(cloudy, 260.0, nan),    # ~ -13 C
        "cth": np.where(cloudy, 4200.0, nan),   # mid-level
        "cot": np.where(cloudy, 8.0, nan),      # thick
        "phase": np.where(cloudy, 1.0, nan),    # water
    }, meta={"sensing_time": sensing_time, "source": "demo"})


def run_demo():
    now = datetime.datetime.now().replace(microsecond=0)
    prev = _demo_field((now - datetime.timedelta(minutes=10)).isoformat(), lon_edge=18.45)
    curr = _demo_field(now.isoformat(), lon_edge=18.70)  # deck moved east ~0.25 deg
    return build_status(curr, prev, data_source="demo")


def run_live():
    cfg = config.CLOUDS
    highsight_on = cfg.get("use_highsight", False)
    # In HighSight mode the visible picture is the source and needs NO EUMETSAT
    # credentials, so skip the L2 live fetch entirely (the L2 fix runs separately).
    # Cached L2 frames, if any, are still read for the fallback.
    if not highsight_on:
        try:
            meta = fetch.fetch_latest()
            print(f"  fetch: {meta}")
        except Exception as e:
            print(f"  WARN: live fetch failed: {e}")
    prev, curr = fetch.latest_two_fields()
    if curr is None and not highsight_on:
        print("  No cloud frames available. Add EUMETSAT creds + run "
              "`python -m clouds.discover`, or try `python run_clouds.py --demo`.")
        return None
    data_source = "HighSight" if highsight_on else "EUMETSAT"
    return build_status(curr, prev, data_source=data_source)


def _write(status):
    OUTPUT_JSON.parent.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False, default=str)
    with open(OUTPUT_JS, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by run_clouds.py; do not edit by hand.\n")
        f.write("window.CLOUD_DATA = ")
        json.dump(status, f, indent=2, ensure_ascii=False, default=str)
        f.write(";\n")
    with open(DOCS_STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False, default=str)
    print(f"  Saved: {OUTPUT_JSON}\n  Saved: {OUTPUT_JS}\n  Saved: {DOCS_STATUS_JSON}")
    print(f"  Saved: {LATEST_PNG}")
    # Mirror to Cloudflare R2 for instant serving (no-op if R2 isn't configured).
    from radar import r2_publish
    r2_publish.publish(["cloud_data.js", "cloud_status.json", LATEST_PNG.name])
    r2_publish.publish_glob(["cloud_history/*.png"])


def main(argv):
    print("=" * 60)
    print("  budva-radar — clouds: fetch + interpret")
    print("=" * 60)
    demo = "--demo" in argv
    try:
        status = run_demo() if demo else run_live()
    except Exception as e:
        print(f"\n  ERROR: {e}")
        return 1
    if status is None:
        return 1
    _write(status)
    v = (status.get("summary") or {}).get("cloud_verdict") or {}
    print(f"\n  [{v.get('state')}] {v.get('narrative')}")
    print(f"  SR: {v.get('line_sr')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
