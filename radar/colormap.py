"""RGB pixel -> reflectivity (dBZ) -> rain rate (mm/h).

Each radar image has its own color scale (legend). We map known colors to
known dBZ values, then classify each pixel via nearest-neighbor in RGB space.

dBZ -> mm/h conversion uses the Marshall-Palmer Z-R relation:
    Z = 200 * R^1.6   =>   R = (Z / 200)^(1 / 1.6)
where Z is linear reflectivity (mm^6 / m^3) and R is rain rate (mm/h).
dBZ = 10 * log10(Z).
"""

import numpy as np
from PIL import Image


# ----------------------------------------------------------------------------
# Standard radar color scales (RGB, dBZ)
#
# DHMZ uses a standard "rainbow" colorbar. OPERA Odyssey uses something
# very similar. Values come from visual inspection of the legend in each
# image's corner. They don't need to be exact — nearest-neighbor tolerates
# ~20-30 RGB Euclidean distance.
# ----------------------------------------------------------------------------

# DHMZ Uljenje legend (RGB, dBZ equivalent)
# From legend in the image corner (visual inspection):
# dark blue -> light blue -> cyan -> green -> yellow -> orange -> red -> magenta
DHMZ_LEGEND = [
    # (R, G, B, dBZ)
    ( 96,  96, 160,  10),  # dark blue - trace
    (128, 128, 192,  15),
    ( 64,  64, 255,  20),  # blue - very light
    ( 64, 192, 255,  25),  # light blue
    ( 64, 255, 192,  30),  # turquoise
    ( 64, 255,  64,  35),  # green
    (192, 255,  64,  40),  # yellow-green
    (255, 192,  64,  45),  # orange-yellow
    (255, 128,  64,  50),  # orange
    (255,  64,  64,  55),  # red
    (255,  64, 192,  60),  # magenta - extreme
]

# OPERA Odyssey legend (RGB, dBZ)
# Very similar to DHMZ, small variation in shades
OPERA_LEGEND = [
    ( 80,  80, 144,  10),
    (112, 112, 192,  15),
    ( 64,  64, 224,  20),
    ( 64, 176, 240,  25),
    ( 64, 240, 176,  30),
    ( 64, 224,  64,  35),
    (192, 240,  64,  40),
    (240, 192,  64,  45),
    (240, 128,  64,  50),
    (240,  64,  64,  55),
    (240,  64, 176,  60),
]

# Basemap tones (sea, land, terrain, text) should be treated as "no precip".
# Pixels close to any of these are mapped to NaN dBZ.
BACKGROUND_RGB_TONES = [
    # Sea / water
    ( 65,  91, 138),    # blue sea
    (110, 140, 170),
    # Forests / vegetation greens
    (130, 160, 130),
    (180, 200, 180),
    # Pale neutrals (basemap land)
    (192, 192, 192),
    (220, 215, 200),
    (200, 200, 195),
    # Terrain / relief shading (DHMZ uses tan/brown shaded relief for hills)
    (212, 155,  95),    # one of the false-positive tones we hit
    (190, 140,  85),
    (170, 130,  90),
    (155, 120,  85),
    (140, 105,  70),
    # White / black (text, borders, lines)
    (255, 255, 255),
    (  0,   0,   0),
    (240, 240, 240),
]

LEGENDS = {
    "dhmz": np.array(DHMZ_LEGEND, dtype=np.int32),
    "opera": np.array(OPERA_LEGEND, dtype=np.int32),
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


def dbz_to_mmh(dbz: np.ndarray) -> np.ndarray:
    """Marshall-Palmer Z-R: R = (Z / 200)^(1 / 1.6).
    Where dBZ is NaN, output is 0 (no rain)."""
    dbz = np.asarray(dbz, dtype=float)
    mmh = np.where(
        np.isnan(dbz), 0.0,
        np.power(np.power(10.0, dbz / 10.0) / 200.0, 1.0 / 1.6)
    )
    return mmh


def classify_intensity(dbz_value) -> str:
    """Human-readable classification for a single dBZ value."""
    if dbz_value is None or (isinstance(dbz_value, float) and np.isnan(dbz_value)):
        return "no precipitation"
    if dbz_value < 20:
        return "trace"
    if dbz_value < 30:
        return "light rain"
    if dbz_value < 40:
        return "moderate rain"
    if dbz_value < 50:
        return "heavy rain"
    return "extreme (likely hail)"


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
