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
from scipy.ndimage import convolve

import config
from radar import calibration, colormap


# Minimum wet-pixel count per annulus to call it "rain". One or two stray
# legend matches happen on basemap textures, so we require a small cluster
# before reporting precipitation. Tuned conservatively - real cells produce
# tens to thousands of wet pixels in a single annulus.
#
# The distance-aware version is min_wet_for_distance() — this constant is
# the close-range floor that the per-source threshold scales up from.
MIN_WET_PIXELS_PER_ANNULUS = 5

# Speckle filter: a pixel keeps "wet" status only when at least this many
# of its 8 neighbours are also wet. Operational radar processing routinely
# applies this 3x3 rule to suppress salt-and-pepper artefacts (RF interference,
# isolated bright-band specks, single-pixel sun spikes).
SPECKLE_MIN_NEIGHBOURS = 4


def _apply_speckle_filter(wet_mask: np.ndarray) -> np.ndarray:
    """Drop wet pixels that have fewer than SPECKLE_MIN_NEIGHBOURS wet
    neighbours in their 3x3 window. Removes isolated 1-2 pixel echoes."""
    if wet_mask.size == 0:
        return wet_mask
    kernel = np.ones((3, 3), dtype=np.int8)
    kernel[1, 1] = 0  # exclude self
    neighbours = convolve(wet_mask.astype(np.int8), kernel,
                          mode="constant", cval=0)
    return wet_mask & (neighbours >= SPECKLE_MIN_NEIGHBOURS)


def min_wet_for_distance(distance_km: float, km_per_pixel: float = 2.0) -> int:
    """Distance-aware threshold for "this annulus has rain".

    Calibrated against 2 km pixels (OPERA ODYSSEY); for finer-resolution
    sources the count is scaled up by the pixel area ratio so we keep the
    same equivalent square-kilometre cluster size.
      <= 25 km:  ~12 km^2 cluster
      <= 50 km:  ~20 km^2 cluster
      <= 100 km: ~40 km^2 cluster
      <= 150 km: ~100 km^2 cluster
      > 150 km:  ~200 km^2 cluster (effectively only large systems count)
    """
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

    For each radius R_i, we examine the ANNULUS between R_{i-1} and R_i
    (so the 50 km ring covers pixels 25..50 km from the center, etc.).
    This is much more sensitive than sampling 24 points on a single circle —
    a sparse circle sample easily misses small/scattered echoes.

    The 24-point circle samples are still recorded under `samples` for the
    HTML preview / debugging, but `frac_wet`, `max_dbz`, etc. now come from
    the dense annular scan.

    Returns a list of dicts (one per ring) with:
      radius_km, n_samples, max_dbz, mean_dbz, frac_wet,
      strongest_bearing (deg), strongest_dbz, samples (list of details).
    """
    cal = calibration.get_calibration(source)
    H, W = rgb_array.shape[:2]
    # Apply per-source valid_area mask (excludes legend bar, text labels)
    valid_area = config.SOURCES[source].get("valid_area", (0, 0, W, H))
    vx0, vy0, vx1, vy1 = valid_area

    # ----- One-time setup: compute per-pixel distance and bearing from center -----
    # We compute this lazily and only once; for typical images (~750x950)
    # this is well under 100ms.
    bx, by = cal.latlon_to_pixel(lat_c, lon_c)
    # Local pixel-per-km scale (from 1-degree-lat probe)
    px_n, py_n = cal.latlon_to_pixel(lat_c + 1.0, lon_c)
    pixels_per_deg_lat = math.hypot(px_n - bx, py_n - by)
    KM_PER_DEG_LAT = 111.32
    px_per_km = pixels_per_deg_lat / KM_PER_DEG_LAT
    km_per_pixel = 1.0 / max(px_per_km, 1e-6)
    # Grid of pixel coordinates within the valid area
    ys = np.arange(vy0, vy1)
    xs = np.arange(vx0, vx1)
    XX, YY = np.meshgrid(xs, ys)
    DX = XX - bx
    DY = YY - by
    # Distance from center, in km
    DIST_KM = np.sqrt(DX * DX + DY * DY) / max(px_per_km, 1e-6)
    # Bearing from center: 0 = N, 90 = E. Note image y grows DOWNWARD, so up = -dy.
    BRG = (np.degrees(np.arctan2(DX, -DY)) + 360) % 360

    # dBZ map for every valid-area pixel (NaN for non-echo)
    area_rgb = rgb_array[vy0:vy1, vx0:vx1].reshape(-1, 3)
    area_dbz_flat = colormap.pixels_to_dbz(area_rgb, source)
    area_dbz = area_dbz_flat.reshape(vy1 - vy0, vx1 - vx0)
    # 2D wet mask over the whole valid area, then speckle-filter it once.
    # Doing this on the full area (not per annulus) lets the 3x3 neighbour
    # rule see across annulus boundaries — a cluster straddling 49/51 km is
    # still a cluster.
    area_valid = ~np.isnan(area_dbz)
    area_wet_raw = area_valid & (area_dbz >= config.RAIN_DBZ_THRESHOLD)
    area_wet = _apply_speckle_filter(area_wet_raw)

    results = []
    prev_r = 0.0
    for r_km in radii_km:
        # ----- Dense annular scan: all pixels with prev_r < dist <= r_km -----
        annulus_mask = (DIST_KM > prev_r) & (DIST_KM <= r_km)
        n_pixels_in_annulus = int(annulus_mask.sum())
        if n_pixels_in_annulus == 0:
            # Annulus completely outside image
            prev_r = r_km
            results.append({"radius_km": r_km, "n_samples": 0, "out_of_image": True})
            continue
        ann_dbz = area_dbz[annulus_mask]
        ann_brg = BRG[annulus_mask]
        ann_dist = DIST_KM[annulus_mask]
        ann_wet_filtered = area_wet[annulus_mask]
        valid_ann = ~np.isnan(ann_dbz)
        n_valid_ann = int(valid_ann.sum())
        wet_raw_mask = valid_ann & (ann_dbz >= config.RAIN_DBZ_THRESHOLD)
        n_wet_raw_ann = int(wet_raw_mask.sum())
        # Speckle-filtered count is the one the rest of the pipeline uses.
        wet_mask = ann_wet_filtered
        n_wet_ann = int(wet_mask.sum())
        if n_valid_ann > 0:
            max_dbz = float(np.nanmax(ann_dbz))
            mean_dbz = float(np.nanmean(ann_dbz))
            # Bearing of the strongest pixel in the annulus
            best_idx = int(np.nanargmax(ann_dbz))
            strongest_bearing = float(ann_brg[best_idx])
            strongest_dbz = float(ann_dbz[best_idx])
        else:
            max_dbz = float("nan")
            mean_dbz = float("nan")
            strongest_bearing = None
            strongest_dbz = float("nan")
        # Exact distance + bearing of the CLOSEST wet pixel inside this annulus.
        # Lets downstream consumers report "rain is at the location" when the
        # nearest echo is sub-kilometre, instead of the coarse ring radius.
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
        # frac_wet = fraction of the annulus area that has rain
        # (relative to all in-image pixels in the annulus, not just valid ones)
        frac_wet = n_wet_ann / max(n_pixels_in_annulus, 1)

        # ----- Also keep the 24-point circle for HTML preview / debugging -----
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
        # Per-sample detail (compact) — kept only for the HTML preview / debugging.
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
            # Annulus-based aggregates (these are the values the table uses)
            "n_pixels_in_annulus": n_pixels_in_annulus,
            "n_valid_color": n_valid_ann,
            "n_wet": n_wet_ann,            # speckle-filtered count (used downstream)
            "n_wet_raw": n_wet_raw_ann,    # pre-speckle count, kept for debugging
            "frac_wet": round(frac_wet, 5),
            "max_dbz": None if np.isnan(max_dbz) else round(max_dbz, 1),
            "mean_dbz": None if np.isnan(mean_dbz) else round(mean_dbz, 1),
            "strongest_bearing": round(strongest_bearing, 1) if strongest_bearing is not None else None,
            "strongest_bearing_cardinal": (
                calibration.bearing_to_cardinal(strongest_bearing)
                if strongest_bearing is not None else None
            ),
            "strongest_dbz": None if np.isnan(strongest_dbz) else round(strongest_dbz, 1),
            # Closest wet-pixel exact distance + bearing within this annulus.
            "closest_wet_km": round(closest_wet_km, 2) if closest_wet_km is not None else None,
            "closest_wet_bearing": round(closest_wet_bearing, 1) if closest_wet_bearing is not None else None,
            "closest_wet_bearing_cardinal": (
                calibration.bearing_to_cardinal(closest_wet_bearing)
                if closest_wet_bearing is not None else None
            ),
            "closest_wet_dbz": round(closest_wet_dbz, 1) if closest_wet_dbz is not None else None,
            # 24-point circle (kept for the preview / for backwards compatibility)
            "n_samples": n_per_ring,
            "n_in_image": len(in_bounds_idx),
            "samples": samples_detail,
        })
        prev_r = r_km
    return results
