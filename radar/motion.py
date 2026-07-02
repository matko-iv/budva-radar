"""Frame-to-frame precipitation motion via cross-correlation.

A global vector comes from correlating a window around Budva between two
frames; trec_field() adds per-tile local vectors for scenes with
differential motion. Coarser than optical flow, sufficient for synoptic
rain-band movement.
"""

import datetime
import numpy as np
from scipy.signal import correlate2d

import config
from radar import calibration, colormap


def _frame_timestamp(path) -> datetime.datetime:
    """Parse the timestamp from the filename (YYYYMMDD_HHMMSS_...)."""
    stem = path.stem if hasattr(path, "stem") else str(path).rsplit("/", 1)[-1].split(".")[0]
    ts_str = stem[:15]  # YYYYMMDD_HHMMSS
    return datetime.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")


def _to_intensity_grid(rgb_array, source) -> np.ndarray:
    """RGB image -> 2D intensity grid (mm/h). Pixels outside the source's
    valid_area are forced to zero so timestamp/legend noise can't drive the
    cross-correlation."""
    H, W = rgb_array.shape[:2]
    flat = rgb_array.reshape(-1, 3)
    dbz = colormap.pixels_to_dbz(flat, source)
    mmh = colormap.dbz_to_mmh(dbz).reshape(H, W)
    va = config.SOURCES.get(source, {}).get("valid_area")
    if va is not None:
        vx0, vy0, vx1, vy1 = va
        mask = np.zeros_like(mmh, dtype=bool)
        mask[vy0:vy1, vx0:vx1] = True
        mmh = np.where(mask, mmh, 0.0)
    return mmh


def _crop_around_pixel(grid, px, py, half_size):
    """Return a square crop grid[py-h..py+h, px-h..px+h], zero-padded if needed."""
    H, W = grid.shape
    x0, y0 = int(round(px - half_size)), int(round(py - half_size))
    x1, y1 = x0 + 2 * half_size, y0 + 2 * half_size
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(W, x1), min(H, y1)
    crop = np.zeros((2 * half_size, 2 * half_size))
    crop[sy0 - y0: sy1 - y0, sx0 - x0: sx1 - x0] = grid[sy0:sy1, sx0:sx1]
    return crop


def compute_motion_vector(rgb_prev, rgb_curr, source, lat_c, lon_c,
                          window_km=200, max_shift_px=40):
    """Global motion of the window_km ROI around (lat_c, lon_c) between two
    frames. Returns {dx_px, dy_px, speed_px_per_frame, confidence,
    direction_deg (toward, 0=N), direction_cardinal, px_per_km}; None when
    either crop lacks signal."""
    cal = calibration.get_calibration(source)
    cx, cy = cal.latlon_to_pixel(lat_c, lon_c)

    px_n, py_n = cal.latlon_to_pixel(lat_c + 1.0, lon_c)
    pixels_per_deg_lat = np.hypot(px_n - cx, py_n - cy)
    km_per_deg_lat = 111.32
    px_per_km = pixels_per_deg_lat / km_per_deg_lat

    half_window_px = int(window_km * px_per_km / 2)
    half_window_px = max(60, min(half_window_px, min(rgb_prev.shape[:2]) // 3))

    g_prev = _to_intensity_grid(rgb_prev, source)
    g_curr = _to_intensity_grid(rgb_curr, source)
    crop_prev = _crop_around_pixel(g_prev, cx, cy, half_window_px)
    crop_curr = _crop_around_pixel(g_curr, cx, cy, half_window_px)

    if crop_prev.sum() < 0.5 or crop_curr.sum() < 0.5:
        return None

    corr = correlate2d(crop_curr, crop_prev, mode="full", boundary="fill", fillvalue=0)
    cy_c, cx_c = corr.shape[0] // 2, corr.shape[1] // 2
    y0, y1 = cy_c - max_shift_px, cy_c + max_shift_px + 1
    x0, x1 = cx_c - max_shift_px, cx_c + max_shift_px + 1
    sub = corr[y0:y1, x0:x1]
    flat_idx = int(np.argmax(sub))
    dy_idx, dx_idx = np.unravel_index(flat_idx, sub.shape)
    dy = dy_idx - max_shift_px
    dx = dx_idx - max_shift_px

    norm = np.sqrt((crop_prev ** 2).sum() * (crop_curr ** 2).sum())
    confidence = float(sub.max() / norm) if norm > 0 else 0.0

    if dx == 0 and dy == 0:
        direction_deg = None
    else:
        # direction moved toward; image dx>0 = east, dy>0 = south
        direction_deg = (np.degrees(np.arctan2(dx, -dy)) + 360) % 360

    speed_px = float(np.hypot(dx, dy))
    return {
        "dx_px": int(dx),
        "dy_px": int(dy),
        "speed_px_per_frame": round(speed_px, 2),
        "confidence": round(confidence, 3),
        "direction_deg": round(direction_deg, 1) if direction_deg is not None else None,
        "direction_cardinal": (calibration.bearing_to_cardinal(direction_deg)
                                if direction_deg is not None else None),
        "px_per_km": round(px_per_km, 3),
    }


def _best_shift(prev, curr, max_shift_px):
    """Best (dx, dy) shift prev->curr with peak-correlation confidence;
    None when either patch has no signal."""
    if prev.sum() < 1e-9 or curr.sum() < 1e-9:
        return None
    corr = correlate2d(curr, prev, mode="full", boundary="fill", fillvalue=0)
    cy_c, cx_c = corr.shape[0] // 2, corr.shape[1] // 2
    sub = corr[cy_c - max_shift_px: cy_c + max_shift_px + 1,
               cx_c - max_shift_px: cx_c + max_shift_px + 1]
    dy_idx, dx_idx = np.unravel_index(int(np.argmax(sub)), sub.shape)
    dx = int(dx_idx - max_shift_px)
    dy = int(dy_idx - max_shift_px)
    norm = np.sqrt((prev ** 2).sum() * (curr ** 2).sum())
    conf = float(sub.max() / norm) if norm > 0 else 0.0
    return dx, dy, conf


def trec_field(prev, curr, block_px=64, max_shift_px=20, min_signal=0.5,
               min_conf=0.2):
    """TREC motion field: {row, col, dx, dy, confidence} per tile with enough
    signal and a confident match. Handles differential motion that a single
    global vector cannot."""
    H, W = prev.shape
    half = block_px // 2
    vectors = []
    for r0 in range(0, H - block_px + 1, block_px):
        for c0 in range(0, W - block_px + 1, block_px):
            bp = prev[r0:r0 + block_px, c0:c0 + block_px]
            bc = curr[r0:r0 + block_px, c0:c0 + block_px]
            if bp.sum() < min_signal or bc.sum() < min_signal:
                continue
            res = _best_shift(bp, bc, max_shift_px)
            if res is None:
                continue
            dx, dy, conf = res
            if conf < min_conf:
                continue
            vectors.append({"row": r0 + half, "col": c0 + half,
                            "dx": dx, "dy": dy, "confidence": round(conf, 3)})
    return vectors


def field_median(vectors):
    """Component-wise median of the block vectors; None for an empty field."""
    if not vectors:
        return None
    dxs = [v["dx"] for v in vectors]
    dys = [v["dy"] for v in vectors]
    return {"dx": float(np.median(dxs)), "dy": float(np.median(dys)),
            "n": len(vectors)}


def motion_field(rgb_prev, rgb_curr, source, block_km=80, max_shift_px=20):
    """Geo-located TREC field: each block vector annotated with lat/lon and
    compass direction/speed. Returns {vectors, median, px_per_km, block_px}
    or None."""
    cal = calibration.get_calibration(source)
    H, W = rgb_prev.shape[:2]
    clat, clon = cal.pixel_to_latlon(W / 2.0, H / 2.0)
    bx, by = cal.latlon_to_pixel(clat, clon)
    px_n, py_n = cal.latlon_to_pixel(clat + 1.0, clon)
    px_per_km = float(np.hypot(px_n - bx, py_n - by) / 111.32)
    block_px = max(24, int(block_km * px_per_km))

    g_prev = _to_intensity_grid(rgb_prev, source)
    g_curr = _to_intensity_grid(rgb_curr, source)
    vecs = trec_field(g_prev, g_curr, block_px=block_px, max_shift_px=max_shift_px)
    out = []
    for v in vecs:
        lat, lon = cal.pixel_to_latlon(v["col"], v["row"])
        dx, dy = v["dx"], v["dy"]
        direction = (None if dx == 0 and dy == 0
                     else float((np.degrees(np.arctan2(dx, -dy)) + 360) % 360))
        out.append({
            "lat": round(lat, 4), "lon": round(lon, 4),
            "dx_px": dx, "dy_px": dy, "confidence": v["confidence"],
            "speed_px_per_frame": round(float(np.hypot(dx, dy)), 2),
            "direction_deg": None if direction is None else round(direction, 1),
            "direction_cardinal": (calibration.bearing_to_cardinal(direction)
                                   if direction is not None else None),
        })
    if not out:
        return None
    return {"vectors": out, "median": field_median(vecs),
            "px_per_km": round(px_per_km, 3), "block_px": block_px}


def estimate_kmh_from_motion(motion_dict, frame_dt_minutes):
    """Convert px/frame to km/h using calibration and frame interval (minutes)."""
    if motion_dict is None:
        return None
    px_per_km = motion_dict["px_per_km"]
    speed_px = motion_dict["speed_px_per_frame"]
    if px_per_km <= 0 or frame_dt_minutes <= 0:
        return None
    km_per_frame = speed_px / px_per_km
    km_per_hour = km_per_frame * (60.0 / frame_dt_minutes)
    return round(km_per_hour, 1)
