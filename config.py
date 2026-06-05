"""Configuration for budva-radar.

Change LOCATION for a different city. Other settings rarely need touching
unless a radar source changes format or calibration needs an update.
"""

# ============================================================================
# Location (default: Budva, Montenegro)
# ============================================================================
LOCATION = {
    "name": "Budva",
    "lat": 42.2864,
    "lon": 18.8400,
}

# Concentric ring radii (km) around the location for radar sampling
SAMPLE_RADII_KM = [10, 25, 50, 100, 150]

# ============================================================================
# Radar sources
# ============================================================================
SOURCES = {
    "dhmz": {
        "name": "DHMZ MRC Uljenje",
        "url": "https://vrijeme.hr/uljenje-stat.png",
        "format": "png_static",
        "expected_size": (720, 751),
        "coverage": "Adriatic Sea region (Croatia + Montenegro + parts of Italy/BiH)",
        # Tight bounding box of the actual radar map. The corners I measured are
        # (3, 84), (657, 96), (657, 716), (2, 716) - the top edge is slanted by
        # ~12 px so the box isn't exactly axis-aligned. I take the strict
        # inner rectangle so nothing outside the slanted top sneaks in.
        # Cuts the noisy top strip and the dBZ scale column on the right.
        # Format: (x_min, y_min, x_max, y_max).
        "valid_area": (3, 96, 657, 716),
        "calibration": None,
    },
    "opera": {
        "name": "OPERA Odyssey Composite (FMI)",
        "list_url": "https://cdn.fmi.fi/demos/eumetnet-web-site-radar-animator/list-images/",
        "format": "json_listing",  # JSON returns list of GIF URLs
        "expected_size": (950, 1100),
        "coverage": "All of Europe",
        # OPERA legend is in upper-right corner; date/timestamp upper-right.
        # The main map area excludes those.
        "valid_area": (10, 50, 900, 1080),
        "calibration": None,
    },
}

# ============================================================================
# Fetch settings
# ============================================================================
FETCH_INTERVAL_MIN = 7            # Fetch every N minutes (OPERA updates every 5 min)
KEEP_FRAMES = 16                  # Keep last N frames (16 = ~80 min of history)
USER_AGENT = "budva-radar/0.1 (local precipitation analysis; non-commercial)"

# ============================================================================
# Interpretation
# ============================================================================
# dBZ thresholds. Calibrated against NOAA JetStream ("light rain begins at
# 20 dBZ") and DHMZ operational scale. Below 5 dBZ is essentially clear-air
# noise (insects, ground/sea clutter); 5-20 is sub-rain (drizzle, virga,
# bright band); 20+ is what NOAA calls light rain.
NOISE_DBZ = 5.0             # below this: clear-air noise / insects / OPERA noise floor
RAIN_DBZ_THRESHOLD = 20.0   # NOAA JetStream: "20 dBZ point at which light rain begins"
MODERATE_DBZ = 30.0
HEAVY_DBZ_THRESHOLD = 40.0  # ~ 12 mm/h Marshall-Palmer
SEVERE_DBZ = 50.0           # Z-R becomes unreliable above this (hail / Mie scattering)
EXTREME_DBZ = 55.0          # hail core territory

# Motion detection. Operational TREC implementations (Vaisala IRIS, CHMI)
# use 0.5-0.7; vectors below MOTION_LOW_CONFIDENCE_MIN are dropped, vectors
# in [MOTION_LOW_CONFIDENCE_MIN, MOTION_MIN_CORRELATION] are kept but flagged.
MOTION_MIN_CORRELATION = 0.6
MOTION_LOW_CONFIDENCE_MIN = 0.4

# Cell persistence: a wet annulus is "confirmed" only if the same ring also
# had >= min_wet_pixels in the previous scan. Standard across SCIT/TITAN/
# KONRAD; single-frame detections are treated as candidates only.
PERSISTENCE_MIN_SCANS = 2

# Maximum rain rate to report (mm/h). Above dBZ ~50 the Marshall-Palmer
# relation breaks down (hail, Mie scattering) and produces wildly inflated
# values; cap so the UI doesn't claim "300 mm/h" for a hail core.
RAIN_RATE_CAP_MMH = 60.0

RAIN_DBZ_THRESHOLD = 20.0       # dBZ edge to define rain onset
NOWCAST_MIN_LIFETIME_MIN = 15.0  # Floor for decaying cell survival
NOWCAST_REACH_BUFFER_KM = 5.0    # Spatial buffer around Budva for a "hit"
P_APPROACH_THRESHOLD = 0.25      # Probability threshold to trigger 'approaching=True'
CELL_CORE_DBZ = 40.0            # Threshold to distinguish convective cores from stratiform
# Physical cap on storm motion (km/h). Real cells move ~10-90 km/h; squall lines
# rarely exceed ~100. The Europe-wide OPERA composite occasionally yields an
# absurd global motion vector (feature mismatch across the huge frame), which a
# brand-new far cell inherits and "arrives" at 500+ km/h. Cap it, and gate cells
# too far to reach within the lead window (see nowcast._cell_arrival).
NOWCAST_MAX_SPEED_KMH = 120.0

# Unscented Mini-Ensemble settings
NOWCAST_SPEED_FACTORS = [0.8, 0.9, 1.0, 1.1, 1.2] 
NOWCAST_LEAD_STEPS_MIN = 5      # Advection time step resolution
NOWCAST_LEAD_MAX_MIN = 120       # Maximum lookahead window (2 hours)

# Confidence Cone Spread Rates
NOWCAST_DIR_SPREAD_CONVECTIVE_DEG = 15.0 # Erratic movement base spread
NOWCAST_DIR_SPREAD_STRATIFORM_DEG = 5.0   # Steady movement base spread
NOWCAST_DIR_GROWTH_DEG_PER_MIN = 0.1     # Cone widening factor over time
