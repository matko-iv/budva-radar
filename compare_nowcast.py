"""Side-by-side nowcast comparison for Budva: run several models on the SAME
input frames and write docs/compare_data.js (+ output/compare.json) for
docs/nowcast-compare.html.

Models (each on an identical Budva-centred 256 x 256 / 1 km tile so the maps
overlay exactly):
  * extrapolation  - Lagrangian persistence (pysteps) -- robustness baseline
  * linda          - LINDA-D (pysteps) -- the headline deterministic nowcast the
                     Skala PDF recommends for localized convective cells
  * dgmr           - DeepMind DGMR (pysteps-dgmr-nowcasts) -- a pretrained deep
                     generative nowcaster, shown for accuracy comparison. Gated:
                     if the plugin/weights are not installed it shows an enable
                     note instead of a column (a labelled stand-in in --demo).

Input:
    python compare_nowcast.py --h5 a.h5 b.h5 c.h5 d.h5   # ORD ODIM frames (oldest first)
    python compare_nowcast.py --ord-latest               # fetch the latest ORD volumes
    python compare_nowcast.py --demo                     # synthetic cell (page preview)

On a successful run the outputs are published to Cloudflare R2 (instant serving; see
radar/r2_publish.py + config.R2). It then pushes to git ONLY if R2 isn't configured
(so the data still reaches GitHub Pages) or if --push is given (refresh the slower
Pages fallback + archive). --no-push always skips — so a routine R2 run no longer
triggers the slow Pages rebuild.

The honest accuracy comparison is the verification harness (verify_nowcast.py,
FSS/CSI vs lead time on your archive); this page is the qualitative side-by-side.
"""

import datetime
import json
import sys
from pathlib import Path

import config

BASE = Path(__file__).resolve().parent
DOCS = BASE / "docs"
OUT_JS = DOCS / "compare_data.js"
OUT_JSON = BASE / "output" / "compare.json"
# Compact Budva rain-FORECAST status for downstream consumers (the matko
# forecast page): DGMR verdict + per-lead rate + integrated hourly mm.
OUT_STATUS = DOCS / "nowcast_status.json"
FRAMES_ROOT = "compare_frames"          # docs/compare_frames/<model>/fNN.png

N_FRAMES = 4
N_LEAD = 16                             # 16 x 5 min = 80 min (DGMR native max = 90)
HORIZON_CAP_MIN = 45
RAIN_MMH = 0.2                          # disc rate above which we call it rain
TILE = 256                              # DGMR's fixed size; all models share it
DISC_KM = 8.0


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0, tzinfo=None).isoformat() + "Z"


# --------------------------------------------------------------------------
# input -> a Budva-centred 256x256 / 1 km rain-rate stack (+ cal, timestep)
# --------------------------------------------------------------------------
def _force_tile(R_stack, info, cal, size=TILE):
    """Center-crop/pad every frame to size x size with Budva at the centre, so all
    models (incl. DGMR) share one geometry. Returns (stack, info2). info2["cal"] is
    a fresh Budva-centred GridCal for the TILE -- the crop changes the pixel origin,
    so the radar-centred input cal must NOT be reused for the tile's geo-corners."""
    import numpy as np
    from radar import dgmr_adapter as dg, ord as ordmod
    cx, cy = info["budva_crop_xy"]
    tiles = np.stack([dg.center_tile(R_stack[k], cx, cy, size)[0]
                      for k in range(R_stack.shape[0])], axis=0)
    c = size // 2
    kmpp = info.get("km_per_px", 1.0)
    loc = config.LOCATION
    tile_cal = ordmod.GridCal(loc["lat"], loc["lon"], c, kmpp)   # Budva at (c, c)
    info2 = dict(info)
    info2.update(shape=(size, size), budva_crop_xy=(c, c), budva_full_xy=(c, c),
                 km_per_px=kmpp, cal=tile_cal)
    return tiles, info2


def _from_h5(paths):
    """ORD ODIM HDF5 frames -> (R_stack, info, cal, timestep_min)."""
    import numpy as np
    from radar import ord as ordmod, pysteps_nowcast as pn
    grids = [ordmod.load_grid(p) for p in paths]
    cal = grids[-1]["cal"]
    loc = config.LOCATION
    R_stack, info = pn.build_rainrate_stack_from_grids(
        [g["dbz"] for g in grids], cal, grids[-1]["km_per_px"],
        loc["lat"], loc["lon"], half_km=140.0)
    R_stack, info = _force_tile(R_stack, info, cal)
    # timestep from the nominal times (fall back to 5 min)
    ts = [g.get("nominal_utc") for g in grids if g.get("nominal_utc")]
    dt = 5.0
    if len(ts) >= 2:
        deltas = [(ts[i + 1] - ts[i]).total_seconds() / 60.0 for i in range(len(ts) - 1)]
        deltas = [d for d in deltas if d > 0]
        if deltas:
            dt = float(np.median(deltas))
    if not (2.5 <= dt <= 7.5):               # ORD scans are 5 min apart; an out-of-range
        print(f"  WARN: input frames ~{dt:.0f} min apart (not a consecutive 5-min run); "
              f"clamping timestep to 5 min (else the horizon is wrong, e.g. 16x35=560 min)",
              file=sys.stderr)
        dt = 5.0
    # absolute time of the NEWEST observed frame ("sada"); forecast frame N is base
    # + lead_min, so the page can label each frame with a real clock time.
    base = ts[-1] if ts else None
    info["base_epoch_ms"] = int(base.timestamp() * 1000) if base else None
    return R_stack, info, info["cal"], dt        # the Budva-centred TILE cal


def _demo_input():
    """Synthetic intensifying convective cell on a 256x256 / 1 km Budva tile."""
    import numpy as np
    from radar import ord as ordmod
    loc = config.LOCATION
    cal = ordmod.GridCal(loc["lat"], loc["lon"], TILE // 2, 1.0)   # Budva at centre
    h = w = TILE
    c = TILE // 2

    def blob(bx, by, amp, sig=16):
        yy, xx = np.mgrid[:h, :w]
        return amp * np.exp(-(((xx - bx) ** 2 + (yy - by) ** 2) / (2 * sig ** 2)))

    amps = [4, 8, 13, 20]                              # intensifying, approaching from SW
    stack = np.stack([blob(c - 34 + 7 * k, c + 30 - 6 * k, amps[k]) for k in range(4)],
                     0).astype(float)
    info = {"km_per_px": 1.0, "budva_crop_xy": (c, c), "budva_full_xy": (c, c),
            "shape": (h, w), "scenario": "convective", "cal": cal,
            "base_epoch_ms": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}
    return stack, info, cal, 5.0


# --------------------------------------------------------------------------
# per-model rendering
# --------------------------------------------------------------------------
def _render_frames(R_now, fc, model_key, timestep_min):
    """Write now + forecast fields to docs/compare_frames/<model_key>/fNN.png and
    return the frame list (stepped radar palette, transparent where dry)."""
    from PIL import Image
    from radar import pysteps_nowcast as pn
    fdir = DOCS / FRAMES_ROOT / model_key
    fdir.mkdir(parents=True, exist_ok=True)
    for old in fdir.glob("*.png"):
        try:
            old.unlink()
        except Exception:
            pass
    fields = [(0.0, R_now, True)] + [((k + 1) * timestep_min, fc[k], False)
                                     for k in range(fc.shape[0])]
    frames = []
    for i, (lead, fld, is_now) in enumerate(fields):
        Image.fromarray(pn.rain_rgba(fld), "RGBA").save(
            DOCS / FRAMES_ROOT / model_key / f"f{i:02d}.png")
        frames.append({"lead_min": round(lead, 1),
                       "image": f"{FRAMES_ROOT}/{model_key}/f{i:02d}.png",
                       "is_now": is_now})
    return frames


def _merc(lat, lon):
    """lon/lat -> Web Mercator (EPSG:3857) metres."""
    import math
    R = 20037508.342789244
    x = lon * R / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0) * R / 180.0
    return x, y


def _bake_basemap(nw, se, out_png, max_px=1024):
    """Fetch a NO-LABEL shaded-relief basemap for the tile bbox (Esri World Shaded
    Relief, exported in Web Mercator so it aligns with Leaflet) -> a local PNG used
    by the LEGACY compare page (the main nowcast-compare page uses live tiles). The
    Esri export silently returns a blank dark-grey image at large sizes, so keep
    max_px conservative AND reject a near-uniform response. Returns the docs-relative
    path, or None on any failure (the page then falls back to live relief tiles)."""
    try:
        import requests
        x0, y1 = _merc(nw[0], nw[1])         # NW: north lat, west lon
        x1, y0 = _merc(se[0], se[1])         # SE: south lat, east lon
        xmin, xmax = sorted((x0, x1)); ymin, ymax = sorted((y0, y1))
        ar = (ymax - ymin) / (xmax - xmin) if xmax > xmin else 1.0
        w = max_px; h = max(1, int(round(max_px * ar)))
        url = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief"
               "/MapServer/export")
        params = {"bbox": f"{xmin},{ymin},{xmax},{ymax}", "bboxSR": "3857",
                  "imageSR": "3857", "size": f"{w},{h}", "format": "png",
                  "transparent": "false", "f": "image"}
        r = requests.get(url, params=params, timeout=30,
                         headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        if r.content[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        # The Esri export silently returns a uniform dark-grey image at some sizes;
        # never serve that blank canvas (the page falls back to live relief tiles).
        try:
            import io
            import numpy as np
            from PIL import Image
            arr = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
            if float(arr.std()) < 10.0:
                return None
        except Exception:
            pass
        (DOCS / FRAMES_ROOT).mkdir(parents=True, exist_ok=True)
        (DOCS / out_png).write_bytes(r.content)
        return out_png
    except Exception:
        return None


def _map_meta(info, cal):
    """Shared geo metadata for the Leaflet overlay (all models use this tile)."""
    h, w = info["shape"]
    cx, cy = info["budva_crop_xy"]
    nw = cal.pixel_to_latlon(0, 0)
    se = cal.pixel_to_latlon(w, h)
    b_la, b_lo = cal.pixel_to_latlon(cx, cy)
    basemap = _bake_basemap(nw, se, f"{FRAMES_ROOT}/basemap.png")
    return {
        "image_size": [w, h],
        "budva_xy": [cx, cy],
        "budva_latlon": [round(b_la, 4), round(b_lo, 4)],
        "km_per_px": round(info["km_per_px"], 3),
        "extent_km": round(max(h, w) * info["km_per_px"]),
        "corner_nw": [round(nw[0], 4), round(nw[1], 4)],
        "corner_se": [round(se[0], 4), round(se[1], 4)],
        "basemap": basemap,
    }


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def build(mode, paths=None):
    import numpy as np
    from radar import pysteps_nowcast as pn, dgmr_adapter as dg
    if mode == "demo":
        R_stack, info, cal, dt = _demo_input()
    else:
        R_stack, info, cal, dt = _from_h5(paths)
    velocity = pn.motion_field(R_stack)
    scenario = info["scenario"]

    models = []

    # 1) extrapolation (baseline) and 2) LINDA-D (headline) -- both pysteps
    for key, req, label, role in (
            ("extrapolation", "extrapolation", "Ekstrapolacija (Lagrange persistencija)", "baseline"),
            ("linda", "linda", "LINDA-D (pysteps)", "headline")):
        fc, _, m = pn.nowcast_fields(R_stack, N_LEAD, velocity=velocity, method=req,
                                     scenario=scenario, kmperpixel=info["km_per_px"],
                                     timestep_min=dt)
        # nowcast_product needs the velocity for the motion vector
        prod = pn.nowcast_product(R_stack, info, "ord", n_leadtimes=N_LEAD,
                                  timestep_min=dt, disc_km=DISC_KM, scenario=scenario,
                                  fc=fc, velocity=velocity, method=m, cal=cal)
        prod.update(key=key, label=label, role=role,
                    frames=_render_frames(R_stack[-1], fc, key, dt))
        models.append(prod)

    # 3) DGMR (DeepMind) -- gated on the plugin/weights; labelled stand-in in demo
    dgmr_fc, dmeta = dg.forecast(R_stack, info, N_LEAD, timestep_min=dt)
    dgmr_entry = {"key": "dgmr", "label": "DGMR (DeepMind)", "role": "compare"}
    if dgmr_fc is not None:
        prod = pn.nowcast_product(R_stack, info, "ord", n_leadtimes=dgmr_fc.shape[0],
                                  timestep_min=dt, disc_km=DISC_KM, scenario=scenario,
                                  fc=dgmr_fc, velocity=velocity, method="dgmr", cal=cal)
        prod.update(dgmr_entry, available=True,
                    frames=_render_frames(R_stack[-1], dgmr_fc, "dgmr", dt))
        models.append(prod)
    elif mode == "demo":
        # labelled stand-in so the 3-way layout is previewable without the weights
        mock = _mock_dgmr_fc(R_stack, velocity, N_LEAD)
        prod = pn.nowcast_product(R_stack, info, "ord", n_leadtimes=N_LEAD,
                                  timestep_min=dt, disc_km=DISC_KM, scenario=scenario,
                                  fc=mock, velocity=velocity, method="dgmr", cal=cal)
        prod.update(dgmr_entry, available=False, stand_in=True,
                    reason=dmeta.get("reason"),
                    frames=_render_frames(R_stack[-1], mock, "dgmr", dt))
        models.append(prod)
    else:
        dgmr_entry.update(available=False, reason=dmeta.get("reason"), series=None,
                          frames=None)
        models.append(dgmr_entry)

    return {
        "ok": True, "mode": mode, "demo": mode == "demo",
        "generated": _utc_now(), "location": config.LOCATION.get("name", "Budva"),
        "base_epoch_ms": info.get("base_epoch_ms"),
        "timestep_min": dt, "n_frames": int(R_stack.shape[0]),
        "horizon_cap_min": HORIZON_CAP_MIN, "disc_km": DISC_KM,
        "scenario": scenario, "map_meta": _map_meta(info, cal),
        "models": models,
        "dgmr_enabled": any(m.get("key") == "dgmr" and m.get("available") for m in models),
    }


def _mock_dgmr_fc(R_stack, velocity, n_leadtimes):
    """Cheap advection+growth stand-in for DGMR in --demo only (clearly badged in
    the page). NOT the real model -- install the plugin for that."""
    import numpy as np
    from pysteps import nowcasts
    fc = np.asarray(nowcasts.get_method("extrapolation")(R_stack[-1], velocity, n_leadtimes))
    fc = np.nan_to_num(fc, nan=0.0)
    ramp = np.linspace(1.05, 1.6, n_leadtimes)[:, None, None]    # mild growth signature
    return np.clip(fc * ramp, 0.0, None)


def _iso_from_ms(ms):
    if ms is None:
        return None
    return datetime.datetime.utcfromtimestamp(ms / 1000.0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _intensity_sr(mmh):
    if mmh < 0.5:
        return "bez kiše"
    if mmh < 2.5:
        return "slaba kiša"
    if mmh < 7.6:
        return "umjerena kiša"
    return "jaka kiša"


def _hourly_mm(base_ms, series, dt):
    """Integrate the POINT rate into per-clock-hour mm over the forecast window, so
    the matko page can override its NWP precipitation for the first ~80 min. Each
    step (mm/h) covers dt minutes, credited to the hour of its midpoint. Returns
    [{hour: 'YYYY-MM-DDTHH:00:00Z', mm, covered_min}] ascending in time (UTC)."""
    base = datetime.datetime.utcfromtimestamp(base_ms / 1000.0)
    buckets = {}
    for s in series:
        mid = base + datetime.timedelta(minutes=s["lead_min"] - dt / 2.0)
        hour = mid.replace(minute=0, second=0, microsecond=0)
        slot = buckets.setdefault(hour, [0.0, 0.0])
        slot[0] += s["point_mmh"] * dt / 60.0
        slot[1] += dt
    return [{"hour": h.strftime("%Y-%m-%dT%H:00:00Z"),
             "mm": round(mm, 2), "covered_min": round(cov)}
            for h, (mm, cov) in sorted(buckets.items())]


def nowcast_status(prod):
    """Compact Budva rain-FORECAST status from the DGMR model in `prod` — the
    contract the matko forecast page consumes (verdict + per-lead rate + integrated
    hourly mm). Returns None if DGMR isn't present/available in the product."""
    dgmr = next((m for m in prod.get("models", [])
                 if m.get("key") == "dgmr" and m.get("series")), None)
    if dgmr is None:
        return None
    series = [{"lead_min": s["lead_min"], "point_mmh": s["point_mmh"],
               "disc_max_mmh": s["disc_max_mmh"]} for s in dgmr["series"]]
    dt = prod.get("timestep_min", 5.0)
    base_ms = prod.get("base_epoch_ms")
    # The verdict is for the BUDVA POINT ONLY (exact at Budva). The disc-max is still
    # shipped in `now`/`series` so the PAGE can show cloud/thunderstorm for cells in
    # the region around Budva, but the VERDICT amount is strictly the point.
    now_point = round(float(dgmr.get("now_point_mmh", 0.0)), 2)
    now_disc = round(float(dgmr.get("now_disc_mmh", 0.0)), 2)
    raining_now = now_point >= RAIN_MMH
    onset = next((s["lead_min"] for s in series if s["point_mmh"] >= RAIN_MMH), None)
    peak = max(series, key=lambda s: s["point_mmh"]) if series else None
    peak_mmh = round(peak["point_mmh"], 1) if peak else 0.0
    total_mm = round(sum(s["point_mmh"] * dt / 60.0 for s in series), 2)
    horizon = series[-1]["lead_min"] if series else 0
    intensity = _intensity_sr(peak_mmh)

    if raining_now:
        state = "RAIN_NOW"
        line = (f"kiša nad Budvom — {intensity}, do ~{peak_mmh} mm/h "
                f"u narednih {horizon:.0f} min (tačka Budva; SKALA NOWCAST)")
    elif onset is not None:
        state = "RAIN_SOON"
        line = (f"kiša kreće ~{onset:.0f} min nad Budvom — {intensity}, do ~{peak_mmh} mm/h "
                f"(tačka Budva; SKALA NOWCAST)")
    else:
        state = "NO_RAIN"
        line = (f"nema kiše nad Budvom (tačka) u narednih {horizon:.0f} min "
                f"(SKALA NOWCAST)")

    return {
        "ok": True,
        "generated": prod.get("generated"),
        "base_time": _iso_from_ms(base_ms),
        "base_epoch_ms": base_ms,
        "location": prod.get("location", "Budva"),
        "source": "SKALA NOWCAST — DGMR (DeepMind) na ORD 1 km",
        "horizon_min": horizon,
        "timestep_min": dt,
        "now": {"point_mmh": now_point, "disc_max_mmh": now_disc, "raining": raining_now},
        "verdict": {
            "state": state, "eta_min": onset, "peak_mmh": peak_mmh,
            "peak_lead_min": peak["lead_min"] if peak else None,
            "total_mm": total_mm, "intensity_sr": intensity, "line_sr": line,
        },
        "series": series,
        "hourly_mm": _hourly_mm(base_ms, series, dt) if base_ms else [],
    }


def write(prod):
    DOCS.mkdir(exist_ok=True)
    OUT_JSON.parent.mkdir(exist_ok=True)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by compare_nowcast.py; do not edit by hand.\n")
        f.write("window.COMPARE_DATA = ")
        json.dump(prod, f, ensure_ascii=False, indent=2, default=str)
        f.write(";\n")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(prod, f, ensure_ascii=False, indent=2, default=str)
    # docs/compare.json: pure-JSON twin of compare_data.js so the page can FETCH it
    # (from R2 / Pages, cache-busted) instead of loading the JS synchronously.
    with open(DOCS / "compare.json", "w", encoding="utf-8") as f:
        json.dump(prod, f, ensure_ascii=False, indent=2, default=str)
    status = nowcast_status(prod) if prod.get("ok") else None
    if status is not None:
        with open(OUT_STATUS, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2, default=str)
        print(f"  Saved: {OUT_STATUS}  [{status['verdict']['state']}]")
    # Mirror to Cloudflare R2 for instant serving (no-op if R2 isn't configured).
    from radar import r2_publish
    r2_publish.publish(["compare_data.js", "compare.json", "nowcast_status.json"])
    r2_publish.publish_glob([f"{FRAMES_ROOT}/**/*.png", f"{FRAMES_ROOT}/*.png"])


def _expand_h5(raw):
    """Turn CLI path args into a sorted list of H5 files. Each arg may be a file,
    a directory (globs *.h5/*.hdf inside), or a glob pattern -- so it works the
    same on Windows (no shell globbing) as on Linux."""
    import glob
    import os
    out = []
    for a in raw:
        if a.startswith("--"):
            continue
        if os.path.isdir(a):
            out += glob.glob(os.path.join(a, "*.h5")) + glob.glob(os.path.join(a, "*.hdf"))
        elif any(c in a for c in "*?["):
            out += glob.glob(a)
        elif a.lower().endswith((".h5", ".hdf")):
            out.append(a)
    return sorted(out)


def _pick_consecutive(paths, n=N_FRAMES, step_min=5.0, tol_min=2.5):
    """Pick the MOST RECENT run of `n` ORD frames spaced ~step_min apart (gap-free),
    oldest->newest. ORD scans are 5 min apart and DGMR is fixed at 5-min cadence, so a
    non-consecutive set gives a garbage nowcast AND an absurd horizon (16x35=560 min).
    Falls back to the last n by time if no clean run exists (caller clamps the step)."""
    from radar import ord as ordmod
    timed = sorted((t, p) for t, p in
                   ((ordmod.nominal_time_utc(p), p) for p in paths) if t is not None)
    if len(timed) < n:
        return [p for _, p in timed] or list(paths)[-n:]
    best = None
    for i in range(len(timed) - n + 1):
        win = timed[i:i + n]
        if all(abs((win[j + 1][0] - win[j][0]).total_seconds() / 60.0 - step_min) <= tol_min
               for j in range(n - 1)):
            best = win                                   # keep the LATEST qualifying window
    chosen = best if best is not None else timed[-n:]
    return [p for _, p in chosen]


def _check_dgmr():
    """Diagnose why DGMR is/ isn't active (run: python compare_nowcast.py --check-dgmr)."""
    import importlib.util
    import sys
    from radar import dgmr_adapter as dg
    print("Python running this script:", sys.executable)
    print("  (pip install must run in THIS same Python, else the plugin lands elsewhere)")
    for mod in ("tensorflow", "dgmr_module_plugin", "dgmr_module_plugin.dgmr", "pysteps_dgmr"):
        try:
            found = importlib.util.find_spec(mod) is not None
        except Exception:
            found = False
        print(f"  import {mod:28} -> {'OK' if found else 'MISSING'}")
    reason = dg.unavailable_reason()
    print("DGMR active:", reason is None, "" if reason is None else f"| reason: {reason}")
    if reason:
        print("Fix: install in THIS Python, and make sure 'git' is on PATH:")
        print(f"  {sys.executable} -m pip install tensorflow wradlib xarray pyproj")
        print(f"  {sys.executable} -m pip install git+https://github.com/pySTEPS/"
              "pysteps-dgmr-nowcasts.git")
    return 0


def main(argv):
    if "--check-dgmr" in argv:
        return _check_dgmr()
    mode, paths = "demo", None
    if "--h5" in argv:
        i = argv.index("--h5")
        found = _expand_h5(argv[i + 1:])
        paths = _pick_consecutive(found)     # most recent gap-free 5-min run of N_FRAMES
        mode = "ord-h5"
        print(f"using {len(paths)} of {len(found)} H5 file(s): "
              f"{', '.join(__import__('os').path.basename(p) for p in paths)}")
    elif "--ord-latest" in argv:
        from radar import ord as ordmod
        # fetch a few recent volumes
        latest = ordmod.fetch_latest()
        paths = _pick_consecutive([str(p) for p in ordmod.ORD_FRAMES_DIR.glob("*.h5")])
        mode = "ord-h5"
    elif "--demo" in argv:
        mode = "demo"
    try:
        prod = build(mode, paths)
    except Exception as e:
        write({"ok": False, "error": str(e), "generated": _utc_now()})
        import traceback
        traceback.print_exc()
        return 1
    write(prod)
    line = " | ".join(f"{m['key']}:" + (
        f"peak {m.get('peak_mmh')}@+{m.get('peak_lead_min')}" if m.get("series")
        else f"n/a ({m.get('reason','')[:30]})") for m in prod["models"])
    print(f"[{prod['mode']}] {line}")
    # --- geo report: the map MUST be centred on Budva (config.LOCATION) ---
    mm = prod.get("map_meta", {})
    bl = mm.get("budva_latlon")
    print(f"GEO: config.LOCATION = [{config.LOCATION['lat']}, {config.LOCATION['lon']}] (Budva)")
    print(f"GEO: map centre      = {bl}  | covers NW {mm.get('corner_nw')} -> SE {mm.get('corner_se')}")
    if mode == "ord-h5" and paths:
        try:
            import h5py
            with h5py.File(paths[-1], "r") as f:
                w = f["where"].attrs
                print(f"GEO: radar site (from file) = [{float(w['lat']):.3f}, {float(w['lon']):.3f}]")
        except Exception:
            pass
    if bl and (abs(bl[0] - config.LOCATION["lat"]) > 0.01 or abs(bl[1] - config.LOCATION["lon"]) > 0.01):
        print("GEO: ** map centre is NOT Budva -> you are running an OLD compare_nowcast.py **")
    else:
        print("GEO: map centre is Budva. If the BROWSER still shows the old spot, hard-refresh "
              "(Ctrl+F5) — it cached compare_data.js.")
    print("DGMR enabled:", prod["dgmr_enabled"], "| Saved:", OUT_JS)
    # Serving is via Cloudflare R2 now (instant; done in write()). Push to git ONLY
    # when R2 isn't configured (so the data still reaches GitHub Pages) or when --push
    # is given (refresh the slower Pages fallback + archive the run). --no-push always
    # skips. The point: a routine R2 run no longer triggers the slow Pages rebuild.
    from radar import r2_publish
    if "--no-push" not in argv and ("--push" in argv or not r2_publish.available()):
        try:
            from loop import push_docs        # reuse the loop's commit+pull-rebase+push
            push_docs()
        except Exception as e:
            print(f"  git push skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
