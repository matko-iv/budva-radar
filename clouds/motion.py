"""Whole-field cloud advection vector between two normalized cloud frames.

Clouds are diffuse fields, not discrete cells, so we estimate ONE motion vector
for the whole scene by cross-correlating the cloud-fraction field between two
frames (mirrors radar/motion.py, but on a regular lat/lon grid so the shift maps
straight to degrees — no affine calibration needed).
"""

import numpy as np
from scipy.signal import correlate2d

import config
from radar import calibration


def _field(frame):
    """Cloud-fraction grid with NaN -> 0 (so missing data can't drive corr)."""
    a = frame.layers.get("frac")
    if a is None:
        a = frame.layers["mask"]
    return np.nan_to_num(np.asarray(a, dtype="float64"), nan=0.0)


def compute_motion(prev_frame, curr_frame, lat_c, lon_c, dt_min,
                   max_shift_cells=25):
    """Motion of the cloud field from prev -> curr.

    Returns dict {dlat_per_min, dlon_per_min, speed_kmh, direction_deg,
    direction_cardinal, confidence, dt_min} (direction = compass bearing the
    clouds move TOWARD), or None if there is too little signal.
    """
    g_prev, g_curr = _field(prev_frame), _field(curr_frame)
    if g_prev.shape != g_curr.shape:
        return None
    if g_prev.sum() < 1.0 or g_curr.sum() < 1.0 or not dt_min or dt_min <= 0:
        return None

    # De-mean so a uniform overcast doesn't bias the correlation peak to (0,0).
    a = g_prev - g_prev.mean()
    b = g_curr - g_curr.mean()
    corr = correlate2d(b, a, mode="full", boundary="fill", fillvalue=0)
    cy, cx = corr.shape[0] // 2, corr.shape[1] // 2
    ms = min(max_shift_cells, cy, cx)
    sub = corr[cy - ms:cy + ms + 1, cx - ms:cx + ms + 1]
    di_idx, dj_idx = np.unravel_index(int(np.argmax(sub)), sub.shape)
    di = di_idx - ms   # row shift  (prev feature appears at curr row + di)
    dj = dj_idx - ms   # col shift

    norm = np.sqrt((a ** 2).sum() * (b ** 2).sum())
    confidence = float(sub.max() / norm) if norm > 0 else 0.0

    # Array index -> geographic step (regular grid; spacing carries its sign).
    dlat_cell = float(prev_frame.lats[1] - prev_frame.lats[0])
    dlon_cell = float(prev_frame.lons[1] - prev_frame.lons[0])
    dlat_per_frame = di * dlat_cell
    dlon_per_frame = dj * dlon_cell

    dlat_per_min = dlat_per_frame / dt_min
    dlon_per_min = dlon_per_frame / dt_min

    if di == 0 and dj == 0:
        direction_deg = None
        speed_kmh = 0.0
    else:
        direction_deg = float(calibration.bearing_deg(
            lat_c, lon_c, lat_c + dlat_per_frame, lon_c + dlon_per_frame))
        km_per_frame = float(calibration.haversine_km(
            lat_c, lon_c, lat_c + dlat_per_frame, lon_c + dlon_per_frame))
        speed_kmh = km_per_frame * (60.0 / dt_min)

    # One global cross-correlation vector can lock onto a spurious far peak.
    # An unphysical implied speed gets confidence 0 so every downstream gate
    # ignores it instead of reporting "ka SW @ 408 km/h".
    max_speed = float((config.CLOUDS or {}).get("motion_max_speed_kmh", 250.0))
    if speed_kmh > max_speed:
        confidence = 0.0

    return {
        "dlat_per_min": dlat_per_min,
        "dlon_per_min": dlon_per_min,
        "speed_kmh": round(speed_kmh, 1),
        "direction_deg": round(direction_deg, 1) if direction_deg is not None else None,
        "direction_cardinal": (calibration.bearing_to_cardinal(direction_deg)
                               if direction_deg is not None else None),
        "confidence": round(confidence, 3),
        "dt_min": round(float(dt_min), 1),
    }
