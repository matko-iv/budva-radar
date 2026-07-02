"""Sample radar pixels in concentric annuli around a location.

Each ring reports max/mean dBZ, wet-pixel counts, and the strongest sector,
computed from a dense scan of every pixel in the annulus. A sparse 24-point
circle is also recorded for the HTML preview.
"""

import math
import numpy as np
from scipy.ndimage import convolve

import config
from radar import calibration, colormap


# Close-range floor for "this annulus has rain"; stray legend matches on
# basemap textures produce 1-2 wet pixels, real cells produce tens to
# thousands. min_wet_for_distance() scales this with range.
MIN_WET_PIXELS_PER_ANNULUS = 5

# A wet pixel survives only with this many wet 3x3 neighbours; standard
# operational suppression for salt-and-pepper artefacts.
SPECKLE_MIN_NEIGHBOURS = 4


def _apply_speckle_filter(wet_mask: np.ndarray) -> np.ndarray:
    if wet_mask.size == 0:
        return wet_mask
    kernel = np.ones((3, 3), dtype=np.int8)
    kernel[1, 1] = 0
    neighbours = convolve(wet_mask.astype(np.int8), kernel,
                          mode="constant", cval=0)
    return wet_mask & (neighbours >= SPECKLE_MIN_NEIGHBOURS)


def min_wet_for_distance(distance_km: float, km_per_pixel: float = 2.0) -> int:
    """Wet-pixel threshold per annulus, scaled so the equivalent km^2 cluster
    size stays constant across pixel resolutions (calibrated at 2 km/px):
    ~12 km^2 inside 25 km up to ~200 km^2 beyond 150 km."""
    if distance_km is None or distance_km <= 25:
        base_at_2km = 3
    elif distance_km <= 50:
        base_at_2km = 5
    elif distance_km <= 100:
        base_at_2km = 10
    elif distance_km <= 150:
        base_at_2km = 25
    else:
        base_at_2km = 50
    area_scale = (2.0 / max(km_per_pixel, 0.1)) ** 2
    return max(MIN_WET_PIXELS_PER_ANNULUS, int(round(base_at_2km * area_scale)))


def points_on_circle(lat_c, lon_c, radius_km, n_points=24):
    """n_points (lat, lon, bearing_deg) evenly spaced on the circle; 0 = N."""
    EARTH_R = 6371.0
    dr = radius_km / EARTH_R
    lat_r, lon_r = math.radians(lat_c), math.radians(lon_c)
    out = []
    for k in range(n_points):
        bearing = 2 * math.pi * k / n_points
        lat2 = math.asin(math.sin(lat_r) * math.cos(dr) +
                         math.cos(lat_r) * math.sin(dr) * math.cos(bearing))
        lon2 = lon_r + math.atan2(
            math.sin(bearing) * math.sin(dr) * math.cos(lat_r),
            math.cos(dr) - math.sin(lat_r) * math.sin(lat2),
        )
        out.append((math.degrees(lat2), math.degrees(lon2), math.degrees(bearing)))
    return out


def sample_image_at_pixels(rgb_array, pixels_xy):
    """RGB at each (x, y); out-of-bounds positions read as (0, 0, 0)."""
    H, W = rgb_array.shape[:2]
    out = np.zeros((len(pixels_xy), 3), dtype=np.int32)
    for i, (x, y) in enumerate(pixels_xy):
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < W and 0 <= yi < H:
            out[i] = rgb_array[yi, xi]
    return out


def sample_concentric(rgb_array, source, lat_c, lon_c, radii_km, n_per_ring=24):
    """Per-ring stats around (lat_c, lon_c); ring i covers the annulus between
    radii i-1 and i. Aggregates come from the dense annular scan; the sparse
    24-point circle is kept under `samples` for the preview only."""
    cal = calibration.get_calibration(source)
    H, W = rgb_array.shape[:2]
    valid_area = config.SOURCES[source].get("valid_area", (0, 0, W, H))
    vx0, vy0, vx1, vy1 = valid_area

    bx, by = cal.latlon_to_pixel(lat_c, lon_c)
    px_n, py_n = cal.latlon_to_pixel(lat_c + 1.0, lon_c)
    pixels_per_deg_lat = math.hypot(px_n - bx, py_n - by)
    KM_PER_DEG_LAT = 111.32
    px_per_km = pixels_per_deg_lat / KM_PER_DEG_LAT
    km_per_pixel = 1.0 / max(px_per_km, 1e-6)
    ys = np.arange(vy0, vy1)
    xs = np.arange(vx0, vx1)
    XX, YY = np.meshgrid(xs, ys)
    DX = XX - bx
    DY = YY - by
    DIST_KM = np.sqrt(DX * DX + DY * DY) / max(px_per_km, 1e-6)
    # image y grows downward, so up = -dy
    BRG = (np.degrees(np.arctan2(DX, -DY)) + 360) % 360

    area_rgb = rgb_array[vy0:vy1, vx0:vx1].reshape(-1, 3)
    area_dbz_flat = colormap.pixels_to_dbz(area_rgb, source)
    area_dbz = area_dbz_flat.reshape(vy1 - vy0, vx1 - vx0)
    # Speckle-filter the whole area at once so the 3x3 rule sees across
    # annulus boundaries; a cluster straddling 49/51 km is still a cluster.
    area_valid = ~np.isnan(area_dbz)
    area_wet_raw = area_valid & (area_dbz >= config.RAIN_DBZ_THRESHOLD)
    area_wet = _apply_speckle_filter(area_wet_raw)

    results = []
    prev_r = 0.0
    for r_km in radii_km:
        annulus_mask = (DIST_KM > prev_r) & (DIST_KM <= r_km)
        n_pixels_in_annulus = int(annulus_mask.sum())
        if n_pixels_in_annulus == 0:
            prev_r = r_km
            results.append({"radius_km": r_km, "n_samples": 0, "out_of_image": True})
            continue
        ann_dbz = area_dbz[annulus_mask]
        ann_brg = BRG[annulus_mask]
        ann_dist = DIST_KM[annulus_mask]
        ann_wet_filtered = area_wet[annulus_mask]
        valid_ann = ~np.isnan(ann_dbz)
        n_valid_ann = int(valid_ann.sum())
        # Tiered counts: 5-20 dBZ traces (drizzle / bright band / clutter)
        # aren't rain but also aren't "nothing on the radar".
        echo_mask  = valid_ann & (ann_dbz >= config.NOISE_DBZ)
        trace_mask = echo_mask & (ann_dbz <  config.RAIN_DBZ_THRESHOLD)
        wet_raw_mask = valid_ann & (ann_dbz >= config.RAIN_DBZ_THRESHOLD)
        n_echo_ann    = int(echo_mask.sum())
        n_trace_ann   = int(trace_mask.sum())
        n_wet_raw_ann = int(wet_raw_mask.sum())
        wet_mask = ann_wet_filtered
        n_wet_ann = int(wet_mask.sum())
        if n_valid_ann > 0:
            max_dbz = float(np.nanmax(ann_dbz))
            mean_dbz = float(np.nanmean(ann_dbz))
            best_idx = int(np.nanargmax(ann_dbz))
            strongest_bearing = float(ann_brg[best_idx])
            strongest_dbz = float(ann_dbz[best_idx])
        else:
            max_dbz = float("nan")
            mean_dbz = float("nan")
            strongest_bearing = None
            strongest_dbz = float("nan")
        # Closest wet pixel lets downstream say "rain is at the location"
        # instead of rounding to the coarse ring radius.
        if n_wet_ann > 0:
            wet_dists = ann_dist[wet_mask]
            wet_brgs = ann_brg[wet_mask]
            wet_dbzs = ann_dbz[wet_mask]
            closest_local = int(np.argmin(wet_dists))
            closest_wet_km = float(wet_dists[closest_local])
            closest_wet_bearing = float(wet_brgs[closest_local])
            closest_wet_dbz = float(wet_dbzs[closest_local])
        else:
            closest_wet_km = None
            closest_wet_bearing = None
            closest_wet_dbz = None
        frac_wet = n_wet_ann / max(n_pixels_in_annulus, 1)

        pts = points_on_circle(lat_c, lon_c, r_km, n_per_ring)
        in_bounds_idx = []
        pixels_xy = []
        for k, (lat, lon, brg) in enumerate(pts):
            px, py = cal.latlon_to_pixel(lat, lon)
            if vx0 <= px < vx1 and vy0 <= py < vy1:
                in_bounds_idx.append(k)
                pixels_xy.append((px, py))
        rgb = sample_image_at_pixels(rgb_array, pixels_xy) if pixels_xy else np.zeros((0, 3))
        dbz = colormap.pixels_to_dbz(rgb, source) if len(rgb) else np.array([])
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
            "km_per_pixel": round(km_per_pixel, 3),
            "min_wet_threshold": min_wet_for_distance(r_km, km_per_pixel),
            "n_pixels_in_annulus": n_pixels_in_annulus,
            "n_valid_color": n_valid_ann,
            "n_wet": n_wet_ann,            # speckle-filtered; drives rain decisions
            "n_wet_raw": n_wet_raw_ann,
            "n_echo": n_echo_ann,
            "n_trace": n_trace_ann,
            "frac_wet": round(frac_wet, 5),
            "max_dbz": None if np.isnan(max_dbz) else round(max_dbz, 1),
            "mean_dbz": None if np.isnan(mean_dbz) else round(mean_dbz, 1),
            "strongest_bearing": round(strongest_bearing, 1) if strongest_bearing is not None else None,
            "strongest_bearing_cardinal": (
                calibration.bearing_to_cardinal(strongest_bearing)
                if strongest_bearing is not None else None
            ),
            "strongest_dbz": None if np.isnan(strongest_dbz) else round(strongest_dbz, 1),
            "closest_wet_km": round(closest_wet_km, 2) if closest_wet_km is not None else None,
            "closest_wet_bearing": round(closest_wet_bearing, 1) if closest_wet_bearing is not None else None,
            "closest_wet_bearing_cardinal": (
                calibration.bearing_to_cardinal(closest_wet_bearing)
                if closest_wet_bearing is not None else None
            ),
            "closest_wet_dbz": round(closest_wet_dbz, 1) if closest_wet_dbz is not None else None,
            "n_samples": n_per_ring,
            "n_in_image": len(in_bounds_idx),
            "samples": samples_detail,
        })
        prev_r = r_km
    return results
