"""Object-based cell extraction and tracking.

scipy.ndimage.label groups contiguous reflectivity pixels into storm objects;
matching them between frames yields per-cell velocity and growth trends.
"""

import math
import uuid
import numpy as np
from scipy import ndimage

import config
from radar import colormap, calibration

def extract_cells(rgb_array, source_id, lat_c, lon_c):
    """Contiguous storm cells from the colour-classified radar image,
    formatted for nowcast.py."""
    H, W = rgb_array.shape[:2]
    flat = rgb_array.reshape(-1, 3)
    dbz_flat = colormap.pixels_to_dbz(flat, source_id)
    dbz = dbz_flat.reshape(H, W)

    cal = calibration.get_calibration(source_id)
    bx, by = cal.latlon_to_pixel(lat_c, lon_c)

    px_n, py_n = cal.latlon_to_pixel(lat_c + 1.0, lon_c)
    km_per_deg = 111.32
    px_per_deg = math.hypot(px_n - bx, py_n - by)
    km_per_px = km_per_deg / max(px_per_deg, 1e-6)

    mask = (~np.isnan(dbz)) & (dbz >= config.RAIN_DBZ_THRESHOLD)

    # Without the valid_area clip the legend bar and frame text become cells.
    src_cfg = config.SOURCES.get(source_id, {})
    va = src_cfg.get("valid_area")
    if va:
        vx0, vy0, vx1, vy1 = va
        bounded = np.zeros_like(mask)
        bounded[vy0:vy1, vx0:vx1] = mask[vy0:vy1, vx0:vx1]
        mask = bounded

    # Pixels outside the coverage disc are basemap, never echo.
    site = src_cfg.get("radar_site")
    if site:
        s_lat, s_lon, range_km = site
        sx, sy = cal.latlon_to_pixel(s_lat, s_lon)
        r_px = range_km / km_per_px
        yy, xx = np.ogrid[:H, :W]
        mask &= ((xx - sx) ** 2 + (yy - sy) ** 2) <= r_px ** 2

    return cells_from_dbz(dbz, mask, cal.pixel_to_latlon, bx, by, km_per_px)


def cells_from_dbz(dbz, mask, pixel_to_latlon, bx, by, km_per_px):
    """Core cell extraction from a dBZ field + rain mask. Shared by the
    colour-classified image path (extract_cells) and the raw ODIM grid path
    (radar/ord.py), so both produce identical cell dicts."""
    labeled_array, num_features = ndimage.label(mask)

    cells = []
    for i in range(1, num_features + 1):
        cell_mask = (labeled_array == i)
        area_px = cell_mask.sum()
        area_km2 = float(area_px * (km_per_px ** 2))

        if area_km2 < 2.0:  # noise specks
            continue

        cell_dbz = np.where(cell_mask, dbz, np.nan)
        max_dbz = float(np.nanmax(cell_dbz))

        cy, cx = ndimage.center_of_mass(cell_mask)
        cx, cy = float(cx), float(cy)
        lat, lon = pixel_to_latlon(cx, cy)

        dx_km = (cx - bx) * km_per_px
        dy_km = (cy - by) * km_per_px
        dist_centroid_km = math.hypot(dx_km, dy_km)
        equiv_diam_km = 2.0 * math.sqrt(area_km2 / math.pi)
        edge_km = max(0.0, dist_centroid_km - (equiv_diam_km / 2.0))

        bearing_deg = float((math.degrees(math.atan2(dx_km, -dy_km)) + 360) % 360)

        # SCIT-style core counting
        core_mask = cell_mask & (dbz >= config.CELL_CORE_DBZ)
        _, n_cores = ndimage.label(core_mask)

        cell_type = "convective" if max_dbz >= config.CELL_CORE_DBZ else "stratiform"

        # Major axis + eccentricity from the covariance-matrix eigenvalues.
        y_indices, x_indices = np.where(cell_mask)
        if len(x_indices) > 0:
            mu20 = np.var(x_indices)
            mu02 = np.var(y_indices)
            mu11 = np.mean((x_indices - cx) * (y_indices - cy))

            diff = (mu20 - mu02) / 2.0
            term = math.sqrt(diff**2 + mu11**2)
            avg = (mu20 + mu02) / 2.0

            lambda1 = max(avg + term, 1e-6)
            lambda2 = max(avg - term, 0.0)

            major_px = 4.0 * math.sqrt(lambda1)
            major_km = major_px * km_per_px
            eccentricity = math.sqrt(1.0 - lambda2 / lambda1)
        else:
            major_km = 0.0
            eccentricity = 0.0

        cells.append({
            "lat": float(lat),
            "lon": float(lon),
            "cx": cx,
            "cy": cy,
            "area_km2": area_km2,
            "max_dbz": max_dbz,
            "equiv_diam_km": equiv_diam_km,
            "major_km": major_km,
            "eccentricity": eccentricity,
            "cell_type": cell_type,
            "edge_km": edge_km,
            "n_cores": n_cores,
            "bearing_deg": bearing_deg,
            "bearing_cardinal": calibration.bearing_to_cardinal(bearing_deg),
            "contains_location": edge_km <= 0
        })
        
    return cells

def update_summaries(current_cells, previous_summaries, scene_motion, dt_min=None):
    """Match current cells to previous frames for speed and trends; summaries
    are formatted for nowcast.py. dt_min falls back to the nominal fetch
    interval when the caller can't compute the true gap."""
    summaries = []
    dt_min = float(dt_min) if dt_min else float(config.FETCH_INTERVAL_MIN)

    # Global motion vector as the prior for brand-new cells.
    gx_km_min, gy_km_min = 0.0, 0.0
    if scene_motion and scene_motion.get("speed_kmh") and scene_motion.get("direction_deg") is not None:
        g_speed_km_min = scene_motion["speed_kmh"] / 60.0
        g_dir_rad = math.radians(scene_motion["direction_deg"])
        gx_km_min = g_speed_km_min * math.sin(g_dir_rad)
        gy_km_min = g_speed_km_min * math.cos(g_dir_rad)

    unmatched_prev = list(previous_summaries)

    for cell in current_cells:
        best_match = None
        best_dist = 15.0  # max search radius (km), TITAN overlap proxy

        for prev in unmatched_prev:
            pc = prev["latest"]
            lat_dist = (cell["lat"] - pc["lat"]) * 111.32
            lon_dist = (cell["lon"] - pc["lon"]) * 111.32 * math.cos(math.radians(cell["lat"]))
            dist_km = math.hypot(lat_dist, lon_dist)

            if dist_km < best_dist:
                best_dist = dist_km
                best_match = prev

        if best_match:
            unmatched_prev.remove(best_match)
            pc = best_match["latest"]

            lat_dist = (cell["lat"] - pc["lat"]) * 111.32
            lon_dist = (cell["lon"] - pc["lon"]) * 111.32 * math.cos(math.radians(cell["lat"]))

            vx_km_min = lon_dist / dt_min
            vy_km_min = lat_dist / dt_min
            speed_kmh = math.hypot(vx_km_min, vy_km_min) * 60.0
            direction_deg = (math.degrees(math.atan2(vx_km_min, vy_km_min)) + 360) % 360

            dbz_diff = cell["max_dbz"] - pc["max_dbz"]
            dbz_trend_per_min = dbz_diff / dt_min

            if dbz_trend_per_min > 0.5: trend = "growing"
            elif dbz_trend_per_min < -0.5: trend = "decaying"
            else: trend = "steady"

            # VIL trend when both frames carry a full-volume column (ORD
            # path); None on the PNG path, where survival uses the dBZ trend.
            vil_now, vil_prev = cell.get("vil_kg_m2"), pc.get("vil_kg_m2")
            vil_trend_per_min = ((vil_now - vil_prev) / dt_min
                                 if vil_now is not None and vil_prev is not None else None)

            summaries.append({
                "id": best_match["id"],
                "latest": cell,
                "n_frames": best_match.get("n_frames", 1) + 1,
                "vx_km_min": vx_km_min,
                "vy_km_min": vy_km_min,
                "speed_kmh": round(speed_kmh, 1),
                "direction_deg": round(direction_deg, 1),
                "direction_cardinal": calibration.bearing_to_cardinal(direction_deg),
                "dbz_trend_per_min": round(dbz_trend_per_min, 2),
                "vil_trend_per_min": (round(vil_trend_per_min, 3)
                                      if vil_trend_per_min is not None else None),
                "trend": trend,
                "path_min_edge_km": min(best_match.get("path_min_edge_km", 1e9), cell["edge_km"])
            })
        else:
            summaries.append({
                "id": str(uuid.uuid4())[:8],
                "latest": cell,
                "n_frames": 1,
                "vx_km_min": gx_km_min,
                "vy_km_min": gy_km_min,
                "speed_kmh": scene_motion.get("speed_kmh", 0.0) if scene_motion else 0.0,
                "direction_deg": scene_motion.get("direction_deg") if scene_motion else None,
                "direction_cardinal": scene_motion.get("direction_cardinal") if scene_motion else None,
                "dbz_trend_per_min": 0.0,
                "vil_trend_per_min": None,   # need >=2 frames for a trend
                "trend": "steady",
                "path_min_edge_km": cell["edge_km"]
            })

    return summaries