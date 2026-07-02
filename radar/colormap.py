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


# Sampled off the legend bar at the bottom of the image (y=722). Bar labels
# mark ranges (10-15, 15-20, ...); each color gets the range midpoint. The
# gray 2-10 dBZ band overlaps basemap text tones and lives in
# BACKGROUND_RGB_TONES instead.
DHMZ_LEGEND = [
    (  0, 150, 219,  12.5),
    (  0,  85, 190,  17.5),
    (  0,  78, 128,  22.5),
    (  0, 150,  10,  27.5),
    (  0, 192,  39,  32.5),
    (  0, 232,  10,  37.5),
    (255, 255,   0,  42.5),
    (255, 187,   0,  47.5),
    (255, 131,   0,  52.5),
    (255,   0,   0,  57.5),
    (161,   0,   0,  62.5),
    (115,   0, 112,  67.5),
]

# Sampled off the vertical legend bar top-right of the composite (x=880).
# Labels are point values with irregular spacing, so no midpointing.
OPERA_LEGEND = [
    (190, 255, 255,  50),
    (250, 120, 255,  45),
    (255,  80,  60,  40),
    (255, 150,  50,  34),
    (255, 205,  20,  30),
    (240, 240,  20,  24),
    (140, 230,  20,  18),
    (  5, 205, 170,  12),
    ( 10, 185, 175,   8),
    ( 10, 155, 180,   0),
    ( 10, 130, 200,  -6),
]

# Basemap tones (sea, land, relief, text); pixels near these map to NaN.
BACKGROUND_RGB_TONES = [
    (  0,  60, 112),
    (  0,  75, 140),
    ( 65,  91, 138),
    (110, 140, 170),
    ( 64, 104,  40),
    ( 80, 130,  51),
    (130, 160, 130),
    (180, 200, 180),
    (192, 192, 192),
    (220, 215, 200),
    (200, 200, 195),
    (189, 189, 255),    # DHMZ lavender "no echo" overlay
    (230, 222, 189),
    (239, 214, 189),
    (247, 230, 189),
    (212, 155,  95),
    (190, 140,  85),
    (170, 130,  90),
    (155, 120,  85),
    (140, 105,  70),
    (255, 255, 255),
    (  0,   0,   0),
    (240, 240, 240),
    (165, 165, 165),    # DHMZ 2-10 dBZ gray, doubles as basemap text tone
]

LEGENDS = {
    # float dtype so the fractional DHMZ midpoints survive
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
    """[N, M] Euclidean RGB distances; only the palette's first 3 columns count."""
    pix = pixels[:, None, :].astype(np.int32)
    pal = palette[None, :, :3].astype(np.int32)
    return np.sqrt(((pix - pal) ** 2).sum(axis=2))


def pixels_to_dbz(pixels_rgb: np.ndarray, source: str) -> np.ndarray:
    """(N, 3) RGB -> (N,) dBZ; NaN where nothing in the legend matches."""
    palette = LEGENDS[source]
    pixels = np.asarray(pixels_rgb, dtype=np.int32)
    if pixels.ndim == 1:
        pixels = pixels[None, :]

    d_leg = _rgb_distance(pixels, palette)
    d_bg = _rgb_distance(pixels, BACKGROUND_ARRAY)

    min_d_leg = d_leg.min(axis=1)
    min_d_bg = d_bg.min(axis=1)
    best_leg_idx = d_leg.argmin(axis=1)

    dbz_out = np.full(len(pixels), np.nan)
    legend_dbz = palette[:, 3].astype(float)
    legend_wins = (min_d_leg < NO_MATCH_THRESHOLD) & (min_d_leg < min_d_bg)
    dbz_out[legend_wins] = legend_dbz[best_leg_idx[legend_wins]]
    return dbz_out


def dbz_to_mmh(dbz, scenario: str = "stratiform"):
    """dBZ -> mm/h. "stratiform" = Marshall-Palmer Z=200 R^1.6; "convective" =
    WSR-88D Z=300 R^1.4 (Fulton 1998), ~2-3x higher for the same dBZ. Rates at
    dBZ >= SEVERE_DBZ are capped: Z-R breaks down once hail enters the volume.
    NaN dBZ -> 0."""
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
    cap = np.where((~np.isnan(dbz)) & (dbz >= config.SEVERE_DBZ),
                   np.minimum(mmh, config.RAIN_RATE_CAP_MMH), mmh)
    return float(cap) if scalar_in else cap


def pick_zr_scenario(max_dbz_in_scene, cell_max_diameter_km=None) -> str:
    """Convective when a compact (<30 km) core reaches 45 dBZ, else stratiform."""
    if max_dbz_in_scene is None or np.isnan(max_dbz_in_scene):
        return "stratiform"
    if max_dbz_in_scene < 45.0:
        return "stratiform"
    if cell_max_diameter_km is not None and cell_max_diameter_km > 30.0:
        return "stratiform"
    return "convective"


def classify_intensity(dbz_value) -> str:
    """Label a dBZ value on the NOAA / DHMZ operational scale."""
    if dbz_value is None or (isinstance(dbz_value, float) and np.isnan(dbz_value)):
        return "no precipitation"
    if dbz_value < config.NOISE_DBZ:
        return "no precipitation"
    if dbz_value < 15.0:
        return "noise / clear-air"
    if dbz_value < config.RAIN_DBZ_THRESHOLD:
        return "trace (sub-rain)"
    if dbz_value < 25.0:
        return "light rain"
    if dbz_value < config.MODERATE_DBZ:
        return "light to moderate rain"
    if dbz_value < config.HEAVY_DBZ_THRESHOLD:
        return "moderate rain"
    if dbz_value < 45.0:
        return "heavy rain"
    if dbz_value < config.SEVERE_DBZ:
        return "very heavy rain"
    if dbz_value < config.EXTREME_DBZ:
        return "severe (likely hail)"
    return "extreme (hail core)"


def load_image_as_rgb(path) -> np.ndarray:
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
