"""Sample radar pixels in concentric circles around a location.

For each radius R in km:
  1) Generate N points spaced evenly along a circle (every 15° = 24 points).
  2) Convert each point to (lat, lon) then to (px, py) via calibration.
  3) Read RGB from the image at that position.
  4) Classify as dBZ via colormap.

Returns per-ring statistics: max dBZ, mean dBZ, fraction of wet pixels,
plus the sector with the strongest signal.
"""

import math
import numpy as np

import config
from radar import calibration, colormap


def points_on_circle(lat_c, lon_c, radius_km, n_points=24):
    """Generate n_points lat/lon points evenly spaced on a circle of radius
    radius_km around (lat_c, lon_c). 0 deg = North, 90 = East."""
    EARTH_R = 6371.0
    dr = radius_km / EARTH_R  # angular distance in radians
    lat_r, lon_r = math.radians(lat_c), math.radians(lon_c)
    out = []
    for k in range(n_points):
        bearing = 2 * math.pi * k / n_points  # 0..2pi
        lat2 = math.asin(math.sin(lat_r) * math.cos(dr) +
                         math.cos(lat_r) * math.sin(dr) * math.cos(bearing))
        lon2 = lon_r + math.atan2(
            math.sin(bearing) * math.sin(dr) * math.cos(lat_r),
            math.cos(dr) - math.sin(lat_r) * math.sin(lat2),
        )
        out.append((math.degrees(lat2), math.degrees(lon2), math.degrees(bearing)))
    return out  # list of (lat, lon, bearing_deg)


def sample_image_at_pixels(rgb_array, pixels_xy):
    """Read RGB values from the image at the given pixel coordinates.
    pixels_xy: list of (x, y); rgb_array: (H, W, 3) numpy.
    Returns (N, 3) array; for out-of-bounds pixels returns (0, 0, 0)."""
    H, W = rgb_array.shape[:2]
    out = np.zeros((len(pixels_xy), 3), dtype=np.int32)
    for i, (x, y) in enumerate(pixels_xy):
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < W and 0 <= yi < H:
            out[i] = rgb_array[yi, xi]
    return out


def sample_concentric(rgb_array, source, lat_c, lon_c, radii_km, n_per_ring=24):
    """Sample the radar image around (lat_c, lon_c) on every radius in radii_km.

    Returns a list of dicts (one per ring) with:
      radius_km, n_samples, max_dbz, mean_dbz, frac_wet,
      strongest_bearing (deg), strongest_dbz, samples (list of details).
    """
    cal = calibration.get_calibration(source)
    H, W = rgb_array.shape[:2]
    # Apply per-source valid_area mask (excludes legend bar, text labels)
    valid_area = config.SOURCES[source].get("valid_area", (0, 0, W, H))
    vx0, vy0, vx1, vy1 = valid_area
    results = []
    for r_km in radii_km:
        pts = points_on_circle(lat_c, lon_c, r_km, n_per_ring)
        in_bounds_idx = []
        pixels_xy = []
        for k, (lat, lon, brg) in enumerate(pts):
            px, py = cal.latlon_to_pixel(lat, lon)
            if vx0 <= px < vx1 and vy0 <= py < vy1:
                in_bounds_idx.append(k)
                pixels_xy.append((px, py))

        if not pixels_xy:
            results.append({"radius_km": r_km, "n_samples": 0, "out_of_image": True})
            continue

        rgb = sample_image_at_pixels(rgb_array, pixels_xy)
        dbz = colormap.pixels_to_dbz(rgb, source)
        valid = ~np.isnan(dbz)
        n_valid = int(valid.sum())
        n_wet = int((valid & (dbz >= 20)).sum())
        max_dbz = float(np.nanmax(dbz)) if n_valid else float("nan")
        mean_dbz = float(np.nanmean(dbz)) if n_valid else float("nan")
        frac_wet = n_wet / n_per_ring

        # Find direction of strongest signal
        if n_valid > 0:
            best_idx_local = int(np.nanargmax(dbz))
            best_bearing = pts[in_bounds_idx[best_idx_local]][2]
            strongest_dbz = float(dbz[best_idx_local])
        else:
            best_bearing = None
            strongest_dbz = float("nan")

        # Per-sample detail (compact)
        samples_detail = []
        for k_local, k_orig in enumerate(in_bounds_idx):
            lat, lon, brg = pts[k_orig]
            samples_detail.append({
                "bearing": round(brg, 1),
                "lat": round(lat, 4), "lon": round(lon, 4),
                "rgb": tuple(int(v) for v in rgb[k_local]),
                "dbz": float(dbz[k_local]) if not np.isnan(dbz[k_local]) else None,
            })
        results.append({
            "radius_km": r_km,
            "n_samples": n_per_ring,
            "n_in_image": len(in_bounds_idx),
            "n_valid_color": n_valid,
            "n_wet": n_wet,
            "frac_wet": round(frac_wet, 3),
            "max_dbz": None if np.isnan(max_dbz) else round(max_dbz, 1),
            "mean_dbz": None if np.isnan(mean_dbz) else round(mean_dbz, 1),
            "strongest_bearing": round(best_bearing, 1) if best_bearing is not None else None,
            "strongest_bearing_cardinal": (
                calibration.bearing_to_cardinal(best_bearing)
                if best_bearing is not None else None
            ),
            "strongest_dbz": None if np.isnan(strongest_dbz) else round(strongest_dbz, 1),
            "samples": samples_detail,
        })
    return results
