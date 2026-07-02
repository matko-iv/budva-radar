"""Compute the pysteps ANVIL nowcast for Budva and write docs/nowcast_data.js
(+ output/nowcast.json) for docs/nowcast.html.

    python run_nowcast.py            # from cached OPERA frames (loop.py fetches them)
    python run_nowcast.py --live     # fetch the latest OPERA frames now
    python run_nowcast.py --demo     # synthetic cell (no files) for a page preview
    python run_nowcast.py --frames a.png b.png c.png d.png --source opera
                                     # your own case: 2-4 composite frames,
                                     # oldest first, from a known source
                                     # (opera|dhmz) so the colour->dBZ legend
                                     # and geolocation line up

Needs pysteps + opencv (see requirements.txt). On failure it still writes a
{ok:false,error} payload so the page can say so instead of breaking.
"""

import datetime
import json
import sys
import tempfile
from pathlib import Path

import config

BASE = Path(__file__).resolve().parent
DOCS = BASE / "docs"
OUT_JS = DOCS / "nowcast_data.js"
OUT_JSON = BASE / "output" / "nowcast.json"

N_FRAMES = 4            # ANVIL needs ar_order+2 = 4
N_LEAD = 12             # 12 x 5 min = 60 min
HORIZON_CAP_MIN = 45    # extrapolation skill ceiling
DISP_SCALE = 3          # upscale (nearest) the small crop for a legible map
FRAMES_DIR = "nowcast_frames"   # under docs/


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0, tzinfo=None).isoformat() + "Z"


def _live_frames(n):
    """Download the last n OPERA composite frames to a temp dir; return paths."""
    import requests
    src = config.SOURCES["opera"]
    r = requests.get(src["list_url"], timeout=30, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    imgs = r.json().get("images", [])[-n:]
    d = Path(tempfile.mkdtemp())
    paths = []
    for it in imgs:
        rr = requests.get(it["url"], timeout=60, headers={"User-Agent": config.USER_AGENT})
        rr.raise_for_status()
        ts = datetime.datetime.utcfromtimestamp(it["epoch"] / 1000).strftime("%Y%m%d_%H%M%S")
        p = d / f"{ts}.gif"
        p.write_bytes(rr.content)
        paths.append(str(p))
    return paths


def _cached_frames(n):
    from radar import fetch
    return [str(p) for p in fetch.list_cached_frames("opera")[-n:]]


def _render_map(R_stack, fc, info, source, timestep_min):
    """Render the 'now' field + each forecast field to docs/nowcast_frames/*.png
    (stepped radar palette, transparent where dry) and return the map metadata the
    page needs (display size, Budva marker pixel, corner lat/lons, frame list)."""
    import numpy as np
    from PIL import Image
    from radar import pysteps_nowcast as pn, calibration
    fdir = DOCS / FRAMES_DIR
    fdir.mkdir(parents=True, exist_ok=True)
    for old in fdir.glob("*.png"):
        try:
            old.unlink()
        except Exception:
            pass
    h, w = info["shape"]
    cx, cy = info["budva_crop_xy"]
    fields = [(0.0, R_stack[-1], True)]
    fields += [((k + 1) * timestep_min, fc[k], False) for k in range(fc.shape[0])]
    frames = []
    for i, (lead, fld, is_now) in enumerate(fields):
        img = Image.fromarray(pn.rain_rgba(fld), "RGBA")
        if DISP_SCALE != 1:
            img = img.resize((w * DISP_SCALE, h * DISP_SCALE), Image.NEAREST)
        name = f"{FRAMES_DIR}/f{i:02d}.png"
        img.save(DOCS / name)
        frames.append({"lead_min": round(lead, 1), "image": name, "is_now": is_now})

    cal = calibration.get_calibration(source)
    fx, fy = info["budva_full_xy"]
    x0, y0 = fx - cx, fy - cy                       # crop origin in full-image px
    nw = cal.pixel_to_latlon(x0, y0)
    se = cal.pixel_to_latlon(x0 + w, y0 + h)
    b_la, b_lo = cal.pixel_to_latlon(fx, fy)        # Budva lat/lon for the Leaflet marker
    return {
        "image_size": [w * DISP_SCALE, h * DISP_SCALE],
        "budva_xy": [round(cx * DISP_SCALE, 1), round(cy * DISP_SCALE, 1)],
        "budva_latlon": [round(b_la, 4), round(b_lo, 4)],
        "km_per_px": info["km_per_px"],
        "extent_km": round(max(h, w) * info["km_per_px"]),
        "corner_nw": [round(nw[0], 2), round(nw[1], 2)],
        "corner_se": [round(se[0], 2), round(se[1], 2)],
        "frames": frames,
    }


def build(live=False, paths=None, source="opera"):
    """Build the nowcast product from cached/live OPERA frames, or from an
    explicit oldest-first list of composite-frame paths (--frames)."""
    if paths is None:
        paths = _live_frames(N_FRAMES) if live else _cached_frames(N_FRAMES)
        if len(paths) < 2 and not live:
            paths = _live_frames(N_FRAMES)        # fall back to live when cache is empty
    if len(paths) < 2:
        raise RuntimeError("need >= 2 composite frames (cache empty / fetch failed / "
                           "too few --frames given)")
    from radar import pysteps_nowcast as pn
    loc = config.LOCATION
    # scenario (Z-R + ANVIL/LINDA choice) is inferred from Budva's LOCAL crop
    R_stack, info = pn.build_rainrate_stack(paths, source, loc["lat"], loc["lon"])
    scenario = info["scenario"]
    velocity = pn.motion_field(R_stack)
    fc, velocity, method = pn.nowcast_fields(
        R_stack, N_LEAD, velocity=velocity, method="auto",
        scenario=scenario, kmperpixel=info["km_per_px"])
    prod = pn.nowcast_product(R_stack, info, source, n_leadtimes=N_LEAD,
                              scenario=scenario, fc=fc, velocity=velocity, method=method)
    prod["map"] = _render_map(R_stack, fc, info, source, prod["timestep_min"])
    prod.update(ok=True, generated=_utc_now(), location=loc.get("name", "Budva"),
                horizon_cap_min=HORIZON_CAP_MIN, source_label=source)
    return prod


def _demo_product():
    """Synthetic intensifying cell approaching from the SW, run through the
    real pipeline so the page can be previewed without waiting for rain."""
    import numpy as np
    from radar import pysteps_nowcast as pn, calibration
    h = w = 160
    cx = cy = 80                                   # Budva at the crop centre

    def blob(bx, by, amp, sig=11):
        yy, xx = np.mgrid[:h, :w]
        return amp * np.exp(-(((xx - bx) ** 2 + (yy - by) ** 2) / (2 * sig ** 2)))

    amps = [3, 6, 10, 15]                          # intensifying, approaching from SW
    stack = np.stack([blob(68 + 2 * k, 92 - 2 * k, amps[k]) for k in range(4)], 0).astype(float)
    cal = calibration.get_calibration("opera")
    loc = config.LOCATION
    _, (px, py) = pn.km_per_pixel(cal, loc["lat"], loc["lon"])
    info = {"km_per_px": 1.5, "budva_crop_xy": (cx, cy),     # finer synthetic grid so
            "budva_full_xy": (px, py), "shape": (h, w)}       # the motion reads ~50 km/h
    velocity = pn.motion_field(stack)
    fc, velocity, method = pn.nowcast_fields(            # convective -> auto picks LINDA
        stack, N_LEAD, velocity=velocity, method="auto",
        scenario="convective", kmperpixel=info["km_per_px"])
    prod = pn.nowcast_product(stack, info, "opera", n_leadtimes=N_LEAD,
                              scenario="convective", fc=fc, velocity=velocity, method=method)
    prod["map"] = _render_map(stack, fc, info, "opera", prod["timestep_min"])
    prod.update(ok=True, demo=True, generated=_utc_now(),
                location="Budva (DEMO — sintetička ćelija)", horizon_cap_min=HORIZON_CAP_MIN)
    return prod


def write(prod):
    DOCS.mkdir(exist_ok=True)
    OUT_JSON.parent.mkdir(exist_ok=True)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by run_nowcast.py; do not edit by hand.\n")
        f.write("window.NOWCAST_DATA = ")
        json.dump(prod, f, ensure_ascii=False, indent=2, default=str)
        f.write(";\n")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(prod, f, ensure_ascii=False, indent=2, default=str)


def _parse_argv(argv):
    """Light flag parser: --live, --demo, --source <name>, --frames <p1 p2 ...>
    (consumes paths until the next --flag)."""
    opts = {"live": False, "demo": False, "frames": None, "source": "opera"}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--live":
            opts["live"] = True
        elif a == "--demo":
            opts["demo"] = True
        elif a == "--source" and i + 1 < len(argv):
            i += 1
            opts["source"] = argv[i]
        elif a == "--frames":
            frames = []
            while i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                i += 1
                frames.append(argv[i])
            opts["frames"] = frames
        i += 1
    return opts


def main(argv):
    opts = _parse_argv(argv)
    try:
        if opts["demo"]:
            prod = _demo_product()
        else:
            prod = build(live=opts["live"], paths=opts["frames"], source=opts["source"])
    except Exception as e:
        write({"ok": False, "error": str(e), "generated": _utc_now()})
        print("ERROR:", e)
        return 1
    write(prod)
    print(f"[{prod['method']}] motion {prod['motion_cardinal']} @ {prod['motion_kmh']} km/h | "
          f"ETA {prod['eta_onset_min']} min | peak {prod['peak_mmh']} mm/h | trend {prod['trend']}")
    print("Saved:", OUT_JS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
