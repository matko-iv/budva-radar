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
        "format": "png_static",   # Single static PNG (overwritten on update)
        "expected_size": (720, 751),
        "coverage": "Adriatic Sea region (Croatia + Montenegro + parts of Italy/BiH)",
        # Valid radar area (excludes legend bar at bottom + text in upper-left).
        # Format: (x_min, y_min, x_max, y_max). Pixels outside this box are ignored.
        "valid_area": (15, 15, 705, 700),
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
FETCH_INTERVAL_MIN = 5            # Fetch every N minutes (OPERA updates every 5 min)
KEEP_FRAMES = 16                  # Keep last N frames (16 = ~80 min of history)
USER_AGENT = "budva-radar/0.1 (local precipitation analysis; non-commercial)"

# ============================================================================
# Interpretation
# ============================================================================
# Threshold for "rain detected" in dBZ.
# 10 dBZ = light/visible echo. 20 dBZ = ~ 0.5 mm/h (light rain).
# Using 10 means trace echoes within range are flagged, so the user is aware
# something exists on the radar even if it's not yet actual rainfall.
RAIN_DBZ_THRESHOLD = 10.0   # ~ visible echo on radar
HEAVY_DBZ_THRESHOLD = 40.0  # ~ 12 mm/h (heavy rain)

# Motion detection: minimum cross-correlation for a valid motion vector
MOTION_MIN_CORRELATION = 0.4
