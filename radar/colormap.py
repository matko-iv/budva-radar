"""RGB pixel -> reflectivity (dBZ) -> rain rate (mm/h).

Each radar image has its own color scale (legend). We map known colors to
known dBZ values, then classify each pixel via nearest-neighbor in RGB space.

dBZ -> mm/h conversion is adaptive: Marshall-Palmer (Z=200 R^1.6) for
stratiform rain, WSR-88D convective (Z=300 R^1.4, Fulton 1998) for
convection. Marshall-Palmer underestimates convective rain by 30-50%.
Above dBZ 50 both relations break down (hail / Mie scattering) so rates
are capped via config.RAIN_RATE_CAP_MMH.
"""

import numpy as np
from PIL import Image

import config


# ----------------------------------------------------------------------------
# Standard radar color scales (RGB, dBZ)
#
# DHMZ uses a standard "rainbow" colorbar. OPERA Odyssey uses something
# very similar. Values come from visual inspection of the legend in each
# image's corner. They don't need to be exact — nearest-neighbor tolerates
# ~20-30 RGB Euclidean distance.
# ----------------------------------------------------------------------------

# DHMZ Uljenje legend (RGB, dBZ equivalent)
# Sampled directly from the legend bar at the bottom of the actual image
# (y=722). Labels on the bar mark RANGES (not point values):
#   2-10, 10-15, 15-20, 20-25, ..., 65-70 dBZ.
# We assign each color the MIDPOINT of its range — better for max_dbz reports
# and for the Marshall-Palmer Z->R conversion than using the lower bound.
#
# The (165, 165, 165) gray "2-10 dBZ" band is excluded: it is essentially
# radar noise and overlaps with basemap text/city tones, so it lives in
# BACKGROUND_RGB_TONES instead.
DHMZ_LEGEND = [
    # (R, G, B, dBZ at range midpoint)
    (  0, 150, 219,  12.5),  # 10-15 light blue
    (  0,  85, 190,  17.5),  # 15-20 medium blue
    (  0,  78, 128,  22.5),  # 20-25 dark blue
    (  0, 150,  10,  27.5),  # 25-30 dark green
    (  0, 192,  39,  32.5),  # 30-35 green
    (  0, 232,  10,  37.5),  # 35-40 bright green
    (255, 255,   0,  42.5),  # 40-45 yellow
    (255, 187,   0,  47.5),  # 45-50 amber
    (255, 131,   0,  52.5),  # 50-55 orange
    (255,   0,   0,  57.5),  # 55-60 red
    (161,   0,   0,  62.5),  # 60-65 dark red
    (115,   0, 112,  67.5),  # 65-70 purple - extreme
]

# OPERA Odyssey legend (RGB, dBZ)
# Sampled directly from the vertical legend bar in the top-right of the
# actual OPERA composite image (x=880, scan y=20..330).
# Labels are at POINT VALUES (irregular spacing: 50, 45, 40, 34, 30, 24, 18,
# 12, 8, 0, -6), so each color = exact dBZ, not a range midpoint.
OPERA_LEGEND = [
    (190, 255, 255,  50),  # very pale cyan - max
    (250, 120, 255,  45),  # pink/magenta
    (255,  80,  60,  40),  # red
    (255, 150,  50,  34),  # orange
    (255, 205,  20,  30),  # amber
    (240, 240,  20,  24),  # yellow
    (140, 230,  20,  18),  # yellow-green
    (  5, 205, 170,  12),  # teal
    ( 10, 185, 175,   8),  # cyan
    ( 10, 155, 180,   0),  # light blue
    ( 10, 130, 200,  -6),  # blue - below detection
]

# Basemap tones (sea, land, terrain, text) should be treated as "no precip".
# Pixels close to any of these are mapped to NaN dBZ.
BACKGROUND_RGB_TONES = [
    # Sea / water (OPERA dark sea)
    (  0,  60, 112),    # OPERA dark sea
    (  0,  75, 140),    # OPERA medium sea
    ( 65,  91, 138),    # blue sea
    (110, 140, 170),
    # Forests / vegetation greens (OPERA land)
    ( 64, 104,  40),    # OPERA dark green land
    ( 80, 130,  51),    # OPERA medium green land
    (130, 160, 130),
    (180, 200, 180),
    # Pale neutrals (basemap land)
    (192, 192, 192),
    (220, 215, 200),
    (200, 200, 195),
    # DHMZ basemap shaded relief (terrain visible through transparent radar layer)
    (189, 189, 255),    # lavender "no echo" overlay over coverage area
    (230, 222, 189),    # tan terrain
    (239, 214, 189),
    (247, 230, 189),
    # Terrain / relief shading (DHMZ uses tan/brown shaded relief for hills)
    (212, 155,  95),
    (190, 140,  85),
    (170, 130,  90),
    (155, 120,  85),
    (140, 105,  70),
    # White / black (text, borders, lines, city dots)
    (255, 255, 255),
    (  0,   0,   0),
    (240, 240, 240),
    (165, 165, 165),    # DHMZ "2 dBZ" gray = also basemap text/city tone
]

LEGENDS = {
    # float dtype so fractional dBZ values (DHMZ midpoints: 12.5, 17.5, ...) survive.
    # RGB columns are still integer-valued, just stored as float.
    "dhmz": np.array(DHMZ_LEGEND, dtype=np.float64),
    "opera": np.array(OPERA_LEGEND, dtype=np.float64),
}
BACKGROUND_ARRAY = np.array(BACKGROUND_RGB_TONES, dtype=np.int32)


# Maximum Euclidean RGB distance for a color to count as a legend match.
# Strict (35) prevents false-matching tan/brown land tones to orange/red.
NO_MATCH_THRESHOLD = 35
# Distance below which a pixel is matched as background rather than legend.
BG_MATCH_THRESHOLD = 50


def _rgb_distance(pixels: np.ndarray, palette: np.ndarray) -> np.ndarray:
    """Return an [N, M] matrix of Euclidean RGB distances from each pixel
    to each palette color.
    pixels: (N, 3); palette: (M, 3 or 4) — if 4, only first 3 are used."""
    pix = pixels[:, None, :].astype(np.int32)     # (N, 1, 3)
    pal = palette[None, :, :3].astype(np.int32)   # (1, M, 3)
    return np.sqrt(((pix - pal) ** 2).sum(axis=2))  # (N, M)


def pixels_to_dbz(pixels_rgb: np.ndarray, source: str) -> np.ndarray:
    """Convert RGB pixels to dBZ. NaN where no legend match (background / unknown).

    pixels_rgb: (N, 3) numpy int32/uint8 array.
    source: 'dhmz' or 'opera'.
    Returns: (N,) float array of dBZ values (NaN if not rain).
    """
    palette = LEGENDS[source]
    pixels = np.asarray(pixels_rgb, dtype=np.int32)
    if pixels.ndim == 1:
        pixels = pixels[None, :]

    d_leg = _rgb_distance(pixels, palette)        # (N, M_legend)
    d_bg = _rgb_distance(pixels, BACKGROUND_ARRAY)  # (N, M_bg)

    min_d_leg = d_leg.min(axis=1)
    min_d_bg = d_bg.min(axis=1)
    best_leg_idx = d_leg.argmin(axis=1)

    # Decision logic:
    #   1) If background wins (smaller distance, below threshold)  -> NaN.
    #   2) Otherwise, if legend distance is below threshold        -> assign dBZ.
    #   3) Otherwise                                              -> NaN.
    dbz_out = np.full(len(pixels), np.nan)
    legend_dbz = palette[:, 3].astype(float)
    legend_wins = (min_d_leg < NO_MATCH_THRESHOLD) & (min_d_leg < min_d_bg)
    dbz_out[legend_wins] = legend_dbz[best_leg_idx[legend_wins]]
    return dbz_out


def dbz_to_mmh(dbz, scenario: str = "stratiform"):
    """dBZ -> rain rate (mm/h) under one of two Z-R relations.

    scenario:
      "stratiform" (default): Marshall-Palmer Z=200 R^1.6 — wide-area frontal
                              rain, melting layer, drizzle.
      "convective":           WSR-88D Z=300 R^1.4 (Fulton 1998) — pulse
                              storms, multicells, squall lines. ~2-3x higher
                              rates than M-P for the same dBZ.

    Rates are clipped to config.RAIN_RATE_CAP_MMH for dBZ >= SEVERE_DBZ since
    the Z-R assumption (Rayleigh scattering off rain drops) breaks down once
    hail enters the radar volume.

    NaN dBZ -> 0 (no rain).
    """
    dbz = np.asarray(dbz, dtype=float)
    scalar_in = dbz.ndim == 0
    if scenario == "convective":
        a, b = 300.0, 1.4
    else:
        a, b = 200.0, 1.6
    with np.errstate(invalid="ignore"):
        mmh = np.where(
            np.isnan(dbz), 0.0,
            np.power(np.power(10.0, dbz / 10.0) / a, 1.0 / b),
        )
    # Z-R cap: hail / extreme reflectivity inflates the inversion wildly.
    cap = np.where((~np.isnan(dbz)) & (dbz >= config.SEVERE_DBZ),
                   np.minimum(mmh, config.RAIN_RATE_CAP_MMH), mmh)
    return float(cap) if scalar_in else cap


def pick_zr_scenario(max_dbz_in_scene, cell_max_diameter_km=None) -> str:
    """Pick which Z-R relation to use based on scene characteristics.

    Convective if there is at least one cell core >= 45 dBZ AND (if known)
    the cell is compact (<30 km across). Otherwise stratiform.
    Matches the PDF recommendation: convective for narrow strong cores,
    stratiform for broad moderate fields.
    """
    if max_dbz_in_scene is None or np.isnan(max_dbz_in_scene):
        return "stratiform"
    if max_dbz_in_scene < 45.0:
        return "stratiform"
    if cell_max_diameter_km is not None and cell_max_diameter_km > 30.0:
        return "stratiform"
    return "convective"


def classify_intensity(dbz_value) -> str:
    """Human-readable classification for a single dBZ value.

    Tracks the NOAA / DHMZ operational scale: below 20 dBZ is sub-rain
    (clutter / drizzle / virga / bright-band), 20 is where rain begins,
    50+ is severe and likely contains hail.
    """
    if dbz_value is None or (isinstance(dbz_value, float) and np.isnan(dbz_value)):
        return "no precipitation"
    if dbz_value < config.NOISE_DBZ:           # < 5
        return "no precipitation"
    if dbz_value < 15.0:
        return "noise / clear-air"             # 5-15: insects, virga, clutter
    if dbz_value < config.RAIN_DBZ_THRESHOLD:  # 15-20
        return "trace (sub-rain)"
    if dbz_value < 25.0:
        return "light rain"                    # 20-25
    if dbz_value < config.MODERATE_DBZ:        # 25-30
        return "light to moderate rain"
    if dbz_value < config.HEAVY_DBZ_THRESHOLD: # 30-40
        return "moderate rain"
    if dbz_value < 45.0:                       # 40-45
        return "heavy rain"
    if dbz_value < config.SEVERE_DBZ:          # 45-50
        return "very heavy rain"
    if dbz_value < config.EXTREME_DBZ:         # 50-55
        return "severe (likely hail)"
    return "extreme (hail core)"               # 55+


def load_image_as_rgb(path) -> np.ndarray:
    """Open a PIL image and return an (H, W, 3) numpy RGB array."""
    img = Image.open(path).convert("RGB")
    return np.array(img)


if __name__ == "__main__":
    sample = np.array([
        [255, 255, 255],   # white
        [64, 255, 64],     # green - light rain
        [255, 64, 64],     # red - heavy
        [65, 91, 138],     # sea
        [128, 128, 192],   # light blue
    ])
    for src in ["dhmz", "opera"]:
        print(f"\n--- {src} ---")
        dbz = pixels_to_dbz(sample, src)
        mmh = dbz_to_mmh(dbz)
        for p, d, m in zip(sample, dbz, mmh):
            print(f"  RGB={tuple(int(v) for v in p)}  dBZ={d}  mm/h={m:.3f}  -> {classify_intensity(d)}")
