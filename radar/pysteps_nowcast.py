"""Precipitation nowcast for Budva built on pysteps.

Composite frames (or ORD dBZ grids) become a rain-rate stack via the repo's
decode + geolocation, then dense Lucas-Kanade optical flow feeds one of two
growth/decay-aware models chosen per scene (method="auto"): ANVIL (Pulkkinen
et al. 2020) for widespread/stratiform rain, LINDA (Pulkkinen et al. 2021) for
convective scenes, where its per-cell localized ARI model tracks Budva's
dangerous case — sudden coastal downpours — better than field-scale methods.

pysteps + opencv are imported lazily; LINDA additionally needs scikit-image
and falls back to ANVIL on error. Fewer than ar_order+2 frames drops both to
plain semi-Lagrangian extrapolation.
"""

import warnings

import numpy as np

import config
from radar import calibration, colormap

DEFAULT_AR_ORDER = 2            # ANVIL needs ar_order+2 input frames
DEFAULT_TIMESTEP_MIN = 5.0
RAIN_ONSET_MMH = 0.2            # disc rate above which rain "starts" at the point
DB_THRESHOLD_MMH = 0.1          # below this = dry, for the dB motion transform
CROP_HALF_KM = 300.0            # nowcast domain half-width around Budva


def km_per_pixel(cal, lat, lon):
    """Local km/pixel of the composite near (lat,lon) + Budva's full-image pixel."""
    px, py = cal.latlon_to_pixel(lat, lon)
    la0, lo0 = cal.pixel_to_latlon(px, py)
    la_x, lo_x = cal.pixel_to_latlon(px + 1, py)
    la_y, lo_y = cal.pixel_to_latlon(px, py + 1)
    kx = calibration.haversine_km(la0, lo0, la_x, lo_x)
    ky = calibration.haversine_km(la0, lo0, la_y, lo_y)
    return float((kx + ky) / 2.0), (float(px), float(py))


def decode_dbz(path, source):
    """OPERA/DHMZ composite image -> (H,W) dBZ grid (NaN where no echo)."""
    rgb = colormap.load_image_as_rgb(path)
    h, w = rgb.shape[:2]
    return colormap.pixels_to_dbz(rgb.reshape(-1, 3), source).reshape(h, w)


def _crop(grid, cx, cy, half_px):
    """Crop a window of half-width half_px around (cx,cy), clamped to the grid.
    Returns (crop, cx_in_crop, cy_in_crop)."""
    h, w = grid.shape
    x0 = max(0, int(round(cx - half_px))); x1 = min(w, int(round(cx + half_px)) + 1)
    y0 = max(0, int(round(cy - half_px))); y1 = min(h, int(round(cy + half_px)) + 1)
    return grid[y0:y1, x0:x1], cx - x0, cy - y0


def build_rainrate_stack(paths, source, lat, lon, half_km=CROP_HALF_KM,
                         scenario=None):
    """Decode composite frames (oldest->newest) into a cropped rain-rate stack
    (mm/h) around (lat,lon). When scenario is None it is inferred from the LOCAL
    (cropped) scene -- so the Z-R relation and the ANVIL/LINDA choice reflect
    Budva's domain, not the whole European composite. Returns (R_stack, info);
    info carries the resolved "scenario"."""
    cal = calibration.get_calibration(source)
    kmpp, (px, py) = km_per_pixel(cal, lat, lon)
    half_px = half_km / kmpp
    dbz_crops, cxy = [], None
    for p in paths:                                # decode dBZ + crop (Z-R independent)
        crop, cx, cy = _crop(decode_dbz(p, source), px, py, half_px)
        dbz_crops.append(crop)
        cxy = (cx, cy)
    if scenario is None:                           # Z-R / method from the local scene
        last = dbz_crops[-1]
        mx = float(np.nanmax(last)) if np.isfinite(last).any() else None
        scenario = colormap.pick_zr_scenario(mx)
    frames = [np.nan_to_num(colormap.dbz_to_mmh(d, scenario), nan=0.0) for d in dbz_crops]
    R_stack = np.stack(frames, axis=0).astype("float64")
    info = {"km_per_px": kmpp, "budva_crop_xy": cxy, "budva_full_xy": (px, py),
            "shape": R_stack.shape[1:], "scenario": scenario}
    return R_stack, info


def build_rainrate_stack_from_grids(dbz_grids, cal, km_per_px, lat, lon,
                                    half_km=CROP_HALF_KM, scenario=None):
    """Like build_rainrate_stack but from pre-decoded cartesian dBZ grids
    (radar/ord.py load_grid), so measured dBZ replaces colour-classified
    pixels. `cal` is the grid's latlon<->pixel object; info["cal"] is carried
    through for nowcast_product."""
    px, py = cal.latlon_to_pixel(lat, lon)
    half_px = half_km / km_per_px
    crops, cxy = [], None
    for g in dbz_grids:
        crop, cx, cy = _crop(np.asarray(g, dtype="float64"), px, py, half_px)
        crops.append(crop)
        cxy = (cx, cy)
    if scenario is None:
        last = crops[-1]
        mx = float(np.nanmax(last)) if np.isfinite(last).any() else None
        scenario = colormap.pick_zr_scenario(mx)
    frames = [np.nan_to_num(colormap.dbz_to_mmh(c, scenario), nan=0.0) for c in crops]
    R_stack = np.stack(frames, axis=0).astype("float64")
    info = {"km_per_px": float(km_per_px), "budva_crop_xy": cxy,
            "budva_full_xy": (float(px), float(py)), "shape": R_stack.shape[1:],
            "scenario": scenario, "cal": cal}
    return R_stack, info


def motion_field(R_stack):
    """Dense Lucas-Kanade motion (2,h,w) from the rain-rate stack, estimated on
    the dB-transformed field so the optical flow tracks precip features."""
    from pysteps import motion
    from pysteps.utils import transformation
    R_db, _ = transformation.dB_transform(R_stack, threshold=DB_THRESHOLD_MMH,
                                          zerovalue=-15.0)
    R_db = np.where(np.isfinite(R_db), R_db, -15.0)
    return motion.get_method("LK")(R_db)


def _resolve_method(requested, scenario, n_frames, ar_order):
    """Pick the concrete nowcast method. Too few frames always -> extrapolation
    (the AR models need ar_order+2). method="auto" picks LINDA for convective
    scenes (localized cells) and ANVIL otherwise."""
    if n_frames < ar_order + 2:
        return "extrapolation"
    if requested in (None, "auto"):
        return "linda" if scenario == "convective" else "anvil"
    return requested


# LINDA's default localization window (0.2*min(shape) ~ 51 px on the 256-px
# tile) convolves real cells into round blobs from the first forecast frame;
# more features + a convective-cell-scale window + the anisotropic kernel
# preserve the precip shape.
LINDA_MAX_FEATURES = 40
LINDA_LOCAL_KM = 10.0
# Deterministic LINDA-D converges to the smooth conditional mean ("rounded
# blobs"), so run a stochastic LINDA-P ensemble and collapse it with the
# probability-matched ensemble mean: location skill from the mean, realistic
# intensity cores from the matching. More members = smoother but slower
# (~2 min for 20 members x 16 leads on the 256 tile).
LINDA_ENS_MEMBERS = 20
LINDA_SEED = 42


def _linda_forecast(precip, velocity, n_leadtimes, ari_order, kmperpixel, timestep_min):
    """Stochastic LINDA-P ensemble collapsed to one field by the
    probability-matched ensemble mean (Ebert 2001). precip is exactly
    ari_order+2 frames, oldest->newest. Returns (n_leadtimes, m, n); the
    caller falls back to ANVIL on any error."""
    import numpy as np
    from pysteps import nowcasts
    from pysteps.postprocessing import probmatching
    local_px = max(6.0, LINDA_LOCAL_KM / kmperpixel) if kmperpixel else None
    ens = nowcasts.get_method("linda")(
        precip, velocity, n_leadtimes,
        feature_method="blob", max_num_features=LINDA_MAX_FEATURES,
        kernel_type="anisotropic", localization_window_radius=local_px,
        ari_order=ari_order, add_perturbations=True,
        n_ens_members=LINDA_ENS_MEMBERS, vel_pert_method="bps",
        kmperpixel=kmperpixel, timestep=timestep_min, seed=LINDA_SEED,
        num_workers=1, measure_time=False)
    # LINDA marks dry pixels NaN; they are zeros, not missing, and must be
    # filled before averaging. nanmean averaged only over members where a
    # pixel was wet, so a pixel raining in 1 of 20 members at 25 mm/h read
    # ~25 mm/h instead of ~1.3 -- rain appeared to spread into speckle across
    # the map. Zero-fill first, then plain mean.
    ens = np.nan_to_num(np.asarray(ens, dtype="float64"), nan=0.0, copy=False)
    ens_mean = ens.mean(axis=0)
    obs = np.nan_to_num(precip[-1], nan=0.0)
    out = np.empty_like(ens_mean)
    for k in range(ens_mean.shape[0]):
        fld = ens_mean[k]
        try:
            out[k] = probmatching.nonparam_match_empirical_cdf(fld, obs)
        except Exception:
            out[k] = fld
    return out


def nowcast_fields(R_stack, n_leadtimes, ar_order=DEFAULT_AR_ORDER, velocity=None,
                   method="auto", scenario=None, kmperpixel=None,
                   timestep_min=DEFAULT_TIMESTEP_MIN):
    """Growth/decay-aware nowcast on the rain-rate stack. `method` is "auto"
    (LINDA for convective scenes, ANVIL otherwise), or an explicit
    "linda"/"anvil"/"extrapolation". LINDA falls back to ANVIL on error; both
    fall back to semi-Lagrangian extrapolation with too few frames. Returns
    (forecast [n_leadtimes,h,w] mm/h, velocity, resolved_method)."""
    from pysteps import nowcasts
    if velocity is None:
        velocity = motion_field(R_stack)
    need = ar_order + 2
    chosen = _resolve_method(method, scenario, R_stack.shape[0], ar_order)
    fc = None
    if chosen == "linda":
        try:
            fc = _linda_forecast(R_stack[-need:], velocity, n_leadtimes,
                                 ar_order, kmperpixel, timestep_min)
        except Exception as e:                       # pragma: no cover - robustness
            warnings.warn(f"LINDA failed ({type(e).__name__}: {e}); using ANVIL")
            chosen = "anvil"
    if chosen == "anvil":
        fc = nowcasts.get_method("anvil")(
            R_stack[-need:], velocity, n_leadtimes, rainrate=None, ar_order=ar_order)
    elif chosen == "extrapolation":
        fc = nowcasts.get_method("extrapolation")(R_stack[-1], velocity, n_leadtimes)
    fc = np.clip(np.nan_to_num(np.asarray(fc, dtype="float64"), nan=0.0), 0.0, None)
    return fc, velocity, chosen


def _disc_mask(h, w, cx, cy, radius_px):
    yy, xx = np.ogrid[:h, :w]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= radius_px ** 2


# Stepped radar palette for the nowcast map; transparent below the rain
# threshold so frames can overlay a basemap.
_RAIN_STOPS = [  # (mm/h floor, (r, g, b))
    (0.2, (150, 200, 255)), (1.0, (70, 130, 235)), (2.5, (40, 190, 110)),
    (5.0, (235, 225, 55)), (10.0, (245, 150, 40)), (20.0, (230, 65, 40)),
    (50.0, (175, 40, 180)),
]


def rain_rgba(field, alpha=210):
    """(h,w) rain rate mm/h -> (h,w,4) uint8 RGBA, transparent where < first stop."""
    f = np.asarray(field, dtype="float64")
    out = np.zeros(f.shape + (4,), dtype="uint8")
    for lo, (r, g, b) in _RAIN_STOPS:
        m = f >= lo
        out[m] = (r, g, b, alpha)
    return out

def budva_nowcast(paths, source, lat, lon, *, n_leadtimes=24,
                  timestep_min=DEFAULT_TIMESTEP_MIN, disc_km=8.0,
                  half_km=CROP_HALF_KM, ar_order=DEFAULT_AR_ORDER, scenario=None,
                  nowcast_method="auto"):
    """End-to-end nowcast at (lat,lon) from a sequence of composite frames
    (oldest->newest). Returns a product dict with the per-lead-time series, ETA,
    peak, growth/decay trend and storm motion. nowcast_method="auto" adapts
    ANVIL/LINDA to the scene."""
    if not paths or len(paths) < 2:
        raise ValueError("need at least 2 composite frames for a nowcast")
    R_stack, info = build_rainrate_stack(paths, source, lat, lon, half_km, scenario)
    return nowcast_product(R_stack, info, source, n_leadtimes=n_leadtimes,
                           timestep_min=timestep_min, disc_km=disc_km,
                           ar_order=ar_order, scenario=info["scenario"],
                           nowcast_method=nowcast_method)


def nowcast_product(R_stack, info, source, *, n_leadtimes=24,
                    timestep_min=DEFAULT_TIMESTEP_MIN, disc_km=8.0,
                    ar_order=DEFAULT_AR_ORDER, scenario="stratiform",
                    nowcast_method="auto", fc=None, velocity=None, method=None,
                    cal=None):
    """Run the nowcast on a prebuilt rain-rate stack + info and assemble the Budva
    product dict. Split out from build_rainrate_stack so it is unit-testable with a
    synthetic stack (no image files). nowcast_method="auto" adapts ANVIL/LINDA to
    the scene. If fc/velocity/method are supplied (already computed) they are
    reused, so the nowcast is not run twice when the caller also renders the
    forecast fields into map frames. `cal` overrides get_calibration(source) so the
    ORD H5 grid's GridCal (or any latlon<->pixel object) can be used directly."""
    cx, cy = info["budva_crop_xy"]; kmpp = info["km_per_px"]
    if fc is None:
        velocity = motion_field(R_stack) if velocity is None else velocity
        fc, velocity, method = nowcast_fields(
            R_stack, n_leadtimes, ar_order, velocity, method=nowcast_method,
            scenario=scenario, kmperpixel=kmpp, timestep_min=timestep_min)

    h, w = fc.shape[1:]
    disc = _disc_mask(h, w, cx, cy, max(1.0, disc_km / kmpp))
    ix, iy = int(round(cx)), int(round(cy))
    in_grid = (0 <= iy < h and 0 <= ix < w)
    series = []
    for k in range(n_leadtimes):
        f = fc[k]
        pt = float(f[iy, ix]) if in_grid else 0.0
        dm = float(f[disc].max()) if disc.any() else pt
        series.append({"lead_min": round((k + 1) * timestep_min, 1),
                       "point_mmh": round(pt, 2), "disc_max_mmh": round(dm, 2)})

    onset = next((s["lead_min"] for s in series if s["disc_max_mmh"] >= RAIN_ONSET_MMH), None)
    peak = max(series, key=lambda s: s["disc_max_mmh"]) if series else None
    now_disc = float(R_stack[-1][disc].max()) if disc.any() else 0.0
    now_point = float(R_stack[-1][iy, ix]) if in_grid else 0.0   # observed AT Budva
    end_disc = series[-1]["disc_max_mmh"] if series else 0.0
    trend = ("intensifying" if end_disc > now_disc * 1.15 + 0.05
             else "decaying" if end_disc < now_disc * 0.85
             else "steady")

    # storm motion from the precip area, through the real geolocation (no
    # image-orientation assumptions): advect Budva's pixel by the mean vector.
    wet = R_stack[-1] > DB_THRESHOLD_MMH
    u = float(np.mean(velocity[0][wet])) if wet.any() else 0.0
    v = float(np.mean(velocity[1][wet])) if wet.any() else 0.0
    spd_px = float(np.hypot(u, v))
    speed_kmh = spd_px * kmpp * (60.0 / timestep_min)         # px/frame -> km/h
    cal = cal if cal is not None else calibration.get_calibration(source)
    px, py = info["budva_full_xy"]
    la0, lo0 = cal.pixel_to_latlon(px, py)
    la1, lo1 = cal.pixel_to_latlon(px + u, py + v)            # cal gives DIRECTION
    bearing = calibration.bearing_deg(la0, lo0, la1, lo1) if spd_px > 1e-6 else None

    return {
        "method": method, "source": source, "scenario": scenario,
        "timestep_min": timestep_min, "n_frames": int(R_stack.shape[0]),
        "domain_px": [h, w], "km_per_px": round(kmpp, 3),
        "now_disc_mmh": round(now_disc, 2),
        "now_point_mmh": round(now_point, 2),
        "series": series,
        "eta_onset_min": onset,
        "peak_mmh": round(peak["disc_max_mmh"], 2) if peak else 0.0,
        "peak_lead_min": peak["lead_min"] if peak else None,
        "trend": trend,
        "motion_kmh": round(speed_kmh, 1),
        "motion_dir_deg": None if bearing is None else round(bearing, 0),
        "motion_cardinal": None if bearing is None else calibration.bearing_to_cardinal(bearing),
    }


def run_from_cache(source="opera", *, n_frames=4, **kwargs):
    """Nowcast from the last `n_frames` cached composite frames of `source`."""
    from radar import fetch
    loc = config.LOCATION
    paths = fetch.list_cached_frames(source)[-n_frames:]
    if len(paths) < 2:
        raise RuntimeError(f"only {len(paths)} cached {source} frames; need >= 2")
    return budva_nowcast(paths, source, loc["lat"], loc["lon"], **kwargs)


if __name__ == "__main__":
    import json
    print(json.dumps(run_from_cache(), indent=2, default=str))
