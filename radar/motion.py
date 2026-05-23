"""Detect cloud motion between two frames via cross-correlation.

Approach: take a region of interest (ROI) around the location from 2 frames
and find the shift (dx, dy) that maximizes the cross-correlation. This gives
the average motion vector in pixels/frame, then converts it to km/h using
the calibration.

This is simpler than full optical flow (Lucas-Kanade) but is sufficient for
synoptic-scale rain band motion.
"""

import datetime
import numpy as np
from scipy.signal import correlate2d

from radar import calibration, colormap


def _frame_timestamp(path) -> datetime.datetime:
    """Parse the timestamp from the filename (YYYYMMDD_HHMMSS_...)."""
    stem = path.stem if hasattr(path, "stem") else str(path).rsplit("/", 1)[-1].split(".")[0]
    ts_str = stem[:15]  # YYYYMMDD_HHMMSS
    return datetime.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")


def _to_intensity_grid(rgb_array, source) -> np.ndarray:
    """Convert an RGB image to a 2D intensity grid (mm/h).
    This gives us an 'image' that motion detection can cross-correlate."""
    H, W = rgb_array.shape[:2]
    flat = rgb_array.reshape(-1, 3)
    dbz = colormap.pixels_to_dbz(flat, source)
    mmh = colormap.dbz_to_mmh(dbz)
    return mmh.reshape(H, W)


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
    """Compute the average motion (vx, vy) in pixels/frame.

    rgb_prev/curr: (H, W, 3) numpy arrays (older and newer frame)
    source: 'dhmz' or 'opera'
    lat_c, lon_c: center of the area of interest (e.g., Budva)
    window_km: how large the ROI is around the center
    max_shift_px: maximum expected shift in pixels (limits search space)

    Returns: dict {
      dx_px, dy_px,        # shift in pixels from frame_prev -> frame_curr
      confidence,          # max correlation (0..1)
      direction_deg,       # 0=N, direction the precipitation is moving toward
      direction_cardinal,
      speed_px_per_frame
    }
    Or None if no reliable detection.
    """
    cal = calibration.get_calibration(source)
    cx, cy = cal.latlon_to_pixel(lat_c, lon_c)

    # Estimate pixel scale: distance for 1° latitude at lat_c
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

    # Need some signal in both crops to get a meaningful motion estimate
    if crop_prev.sum() < 0.5 or crop_curr.sum() < 0.5:
        return None

    # Cross-correlation. We want corr[shift_y, shift_x] = sum_{r,c} curr[r,c] * prev[r-dy, c-dx]
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
        # Convention: direction the precipitation is moving toward.
        # In image coordinates, dx>0 = east, dy>0 = south (y axis points down).
        # Convert to compass: 0 = N, 90 = E.
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
