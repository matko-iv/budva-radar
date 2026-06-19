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
from clouds import fetch, interpret, motion as cmotion, render, verdict
from clouds.grid import CloudField, downsample_for_browser

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"
OUTPUT_JSON = BASE_DIR / "output" / "cloud_status.json"
OUTPUT_JS = DOCS_DIR / "cloud_data.js"
DOCS_STATUS_JSON = DOCS_DIR / "cloud_status.json"
LATEST_PNG = DOCS_DIR / "latest_cloud.png"
PREVIEW_SCALE = 4


def _dt_min(prev, curr):
    try:
        t0 = datetime.datetime.fromisoformat(prev.sensing_time)
        t1 = datetime.datetime.fromisoformat(curr.sensing_time)
        d = (t1 - t0).total_seconds() / 60.0
        return d if 0.5 <= d <= 180 else 10.0
    except Exception:
        return 10.0


def build_status(field, prev_field, data_source):
    loc = config.LOCATION
    cfg = config.CLOUDS

    mot = None
    if prev_field is not None:
        mot = cmotion.compute_motion(prev_field, field, loc["lat"], loc["lon"],
                                     _dt_min(prev_field, field))

    facts = interpret.cloud_facts(field, mot, loc["lat"], loc["lon"], loc["name"], cfg)

    # Preview PNG (north-up) + its pixel size, so the page can place the marker.
    render.to_png(field, LATEST_PNG, scale=PREVIEW_SCALE)
    H, W = field.shape
    img_size = [W * PREVIEW_SCALE, H * PREVIEW_SCALE]

    status = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "location": loc,
        "source": {"name": "EUMETSAT cloud products", "ok": True,
                   "sensing_time": field.sensing_time, "data_source": data_source},
        "motion": None if mot is None else {
            "direction_deg": mot["direction_deg"],
            "direction_cardinal": mot["direction_cardinal"],
            "speed_kmh": mot["speed_kmh"], "confidence": mot["confidence"],
        },
        "facts": facts,
        "field": {
            **downsample_for_browser(field),
            "bbox": cfg["bbox"], "image": LATEST_PNG.name, "image_size": img_size,
            # full motion (incl. deg/min) for the browser-side nowcast replay
            "motion": mot,
        },
        "params": {
            "frac_clear_max": cfg["frac_clear_max"],
            "frac_overcast_min": cfg["frac_overcast_min"],
            "height_low_max_m": cfg["height_low_max_m"],
            "height_mid_max_m": cfg["height_mid_max_m"],
            "cot_thin_max": cfg["cot_thin_max"],
            "nowcast_lead_step_min": cfg["nowcast_lead_step_min"],
            "nowcast_lead_max_min": cfg["nowcast_lead_max_min"],
            "nowcast_dir_spread_deg": cfg["nowcast_dir_spread_deg"],
            "nowcast_dir_growth_deg_per_min": cfg["nowcast_dir_growth_deg_per_min"],
            "sample_radius_now_km": config.SAMPLE_RADII_KM[0],
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
        "mask": mask, "frac": mask,
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
    try:
        meta = fetch.fetch_latest()
        print(f"  fetch: {meta}")
    except Exception as e:
        print(f"  WARN: live fetch failed: {e}")
    prev, curr = fetch.latest_two_fields()
    if curr is None:
        print("  No cloud frames available. Add EUMETSAT creds + run "
              "`python -m clouds.discover`, or try `python run_clouds.py --demo`.")
        return None
    return build_status(curr, prev, data_source="EUMETSAT")


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


def main(argv):
    print("=" * 60)
    print("  budva-radar — clouds: fetch + interpret")
    print("=" * 60)
    demo = "--demo" in argv
    status = run_demo() if demo else run_live()
    if status is None:
        return 1
    _write(status)
    v = (status.get("summary") or {}).get("cloud_verdict") or {}
    print(f"\n  [{v.get('state')}] {v.get('narrative')}")
    print(f"  SR: {v.get('line_sr')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
