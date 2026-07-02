"""Settings for budva-radar. Change LOCATION to point at a different city."""

LOCATION = {
    "name": "Budva",
    "lat": 42.2864,
    "lon": 18.8400,
}

# Ring radii (km) sampled around the location.
SAMPLE_RADII_KM = [10, 25, 50, 100, 150]

SOURCES = {
    "dhmz": {
        "name": "DHMZ MRC Uljenje",
        "url": "https://vrijeme.hr/uljenje-stat.png",
        "format": "png_static",
        "expected_size": (720, 751),
        "coverage": "Adriatic Sea region (Croatia + Montenegro + parts of Italy/BiH)",
        # Strict inner rectangle of the map area; the top edge of the drawn map
        # slants ~12 px, and this excludes it plus the dBZ scale column.
        "valid_area": (3, 96, 657, 716),
        # (lat, lon, range_km) from hrulj ODIM metadata; range clipped 1 km
        # inside the drawn rim so the rim line never reads as echo.
        "radar_site": (42.8944, 17.4783, 248.0),
        "calibration": None,
    },
    "opera": {
        "name": "OPERA Odyssey Composite (FMI)",
        "list_url": "https://cdn.fmi.fi/demos/eumetnet-web-site-radar-animator/list-images/",
        "format": "json_listing",
        "expected_size": (950, 1100),
        "coverage": "All of Europe",
        # Excludes the legend and timestamp in the upper-right corner.
        "valid_area": (10, 50, 900, 1080),
        "radar_site": None,
        "calibration": None,
    },
}

# Use the hrulj raw polar volume from MeteoGate ORD for cells + rings instead
# of colour-classifying the PNG. The PNG stays the display layer and the
# fallback when the ORD fetch fails. See radar/ord.py.
ORD_ENABLED = True

FETCH_INTERVAL_MIN = 5
KEEP_FRAMES = 16  # ~80 min of history
USER_AGENT = "budva-radar/0.1 (local precipitation analysis; non-commercial)"

# dBZ thresholds, per NOAA JetStream and the DHMZ operational scale.
NOISE_DBZ = 5.0
RAIN_DBZ_THRESHOLD = 20.0
MODERATE_DBZ = 30.0
HEAVY_DBZ_THRESHOLD = 40.0
SEVERE_DBZ = 50.0  # Z-R unreliable above this (hail / Mie scattering)
EXTREME_DBZ = 55.0

# Vectors below the low-confidence floor are dropped; between the two values
# they are kept but flagged. Operational TREC uses 0.5-0.7.
MOTION_MIN_CORRELATION = 0.6
MOTION_LOW_CONFIDENCE_MIN = 0.4

# A wet annulus counts as confirmed only if the previous scan also had it.
PERSISTENCE_MIN_SCANS = 2

# Marshall-Palmer breaks down above ~50 dBZ; cap so a hail core doesn't
# report 300 mm/h.
RAIN_RATE_CAP_MMH = 60.0

NOWCAST_MIN_LIFETIME_MIN = 15.0
NOWCAST_REACH_BUFFER_KM = 5.0
P_APPROACH_THRESHOLD = 0.25
# Tuned on the 2026-06-11 verification replay (1133 scans / 73 onsets,
# _far_sweep.py): 60-min bucket + 50 km gate kept POD 0.973, cut FAR to 0.601.
APPROACH_LEAD_MIN = 60
APPROACH_MAX_DIST_KM = 50.0
CELL_CORE_DBZ = 40.0
# The Europe-wide OPERA composite occasionally produces an absurd global
# motion vector; cap so a far cell can't "arrive" at 500 km/h.
NOWCAST_MAX_SPEED_KMH = 120.0

NOWCAST_SPEED_FACTORS = [0.8, 0.9, 1.0, 1.1, 1.2]
NOWCAST_LEAD_STEPS_MIN = 5
NOWCAST_LEAD_MAX_MIN = 120

NOWCAST_DIR_SPREAD_CONVECTIVE_DEG = 15.0
NOWCAST_DIR_SPREAD_STRATIFORM_DEG = 5.0
NOWCAST_DIR_GROWTH_DEG_PER_MIN = 0.1

# Seasonal placeholder for the environmental freezing level (Adriatic summer
# ~3.5-4 km, winter ~1-2 km); ZDR columns are counted above this height.
FREEZING_LEVEL_M = 3500.0

# VIL (kg/m2) floor a collapsing cell decays toward; 3-D analogue of
# RAIN_DBZ_THRESHOLD in nowcast._lifetime_min.
VIL_RAIN_FLOOR = 0.5

# Budva-specific: open sea to the SW, Dinaric/Rumija ridge to the NE. Cells
# descending the seaward slope tend to dissipate; down-weight their arrival.
# Starting points to tune against verification, not validated climatology.
COASTAL_SEAWARD_AZIMUTH_DEG = 225.0
COASTAL_RIDGE_DISSIPATION = 0.5

# Satellite cloud module (clouds/ package). Independent of the radar pipeline;
# writes output/cloud_status.json, docs/cloud_data.js, docs/cloud_status.json.
# Everything downstream of clouds/fetch.py consumes a normalized regular
# lat/lon grid with named layers (mask/fraction/ctt/cth/cot/phase).
CLOUDS = {
    # Bounding box around LOCATION, sized so cloud can be seen advecting in
    # from any side within the 2 h horizon.
    "bbox": {
        "lat_min": 40.3, "lat_max": 44.3,
        "lon_min": 16.4, "lon_max": 21.4,
    },
    "grid_step_deg": 0.03,  # ~3 km, near MTG-FCI L2 native

    # Map rendering: show EUMETSAT GeoColour (what EUMETView shows); the L2
    # overlay is the fallback when the WMS fetch fails.
    "use_geocolour_map": True,
    "geocolour_wms": "https://view.eumetsat.int/geoserver/wms",
    "geocolour_layer": "mtg_fd:rgb_geocolour",

    # HighSight visible tiles drive the cloud verdict: cloud = bright/neutral
    # pixels against dark sea / green land, nowcast by advecting the frames.
    # Overrides the L2/GeoColour verdict while the L2 path is being repaired.
    # Tiles need an API key in the HIGHSIGHT_KEY env var.
    "use_highsight": True,
    "highsight_key_env": "HIGHSIGHT_KEY",
    "highsight_zoom": 5,              # 2 tiles over the bbox (~1.8 km/px @ 42N)
    "highsight_display_width": 1000,
    "highsight_bright_min": 150,
    "highsight_sat_max": 40,
    "highsight_thick_min": 205,
    # HighSight runs ~20 min late and rejects too-recent slots; request this
    # many minutes behind now, floored to the 10-min cadence. The resolved
    # slot's true time becomes sensing_time.
    "highsight_lag_min": 30,
    "highsight_max_lookback_slots": 3,
    # Tile quota throttle (free tier = 5,000 tiles/month). Download a new
    # frame at most every N minutes; monthly tiles ~ tiles_per_frame * 44640/N
    # (2/4/9/25 tiles per frame at zoom 5/6/7/8). Zoom 5 @ 20 min ~ 4,460/mo.
    # 0 disables the throttle.
    "highsight_min_interval_min": 20,
    "highsight_history_hours": 2.0,  # scrubbable frame history on the page

    # GeoColour is a rendered picture, not a measurement: brightness misreads
    # sun-glint, snow, and low sun, and at night encodes temperature. Off by
    # default; when on it is day-only with the sun high enough.
    "use_geocolour_verdict": False,
    # Daytime cross-check that vetoes the OCA COT over-read (phantom thick
    # ice shield driving a false "sun blocked"). Caps L2 cloud downward only,
    # never adds cloud, and only by day with the sun high enough.
    "use_geocolour_crosscheck": True,
    "geocolour_verdict_day_only": True,
    "geocolour_max_sza": 70.0,
    "geocolour_sample_km": 6.0,
    "geocolour_bright_min": 150,
    "geocolour_sat_max": 40,
    "geocolour_thick_min": 205,

    # EUMETSAT Data Store collection IDs, pinned from the live catalogue
    # (clouds/discover.py, 2026-06-19). MTG 0-deg disk covers Budva.
    "collections": {
        "clm": "EO:EUM:DAT:0678",   # Cloud Mask
        "ctth": "EO:EUM:DAT:0681",  # Cloud Top Temperature and Height
        "oca": "EO:EUM:DAT:0684",   # Optimal Cloud Analysis (COT + phase)
    },
    # Data Tailor subsets + reprojects to a regular lat/lon grid, which keeps
    # files small and makes clouds/grid.py a plain index.
    "use_data_tailor": True,

    # Cloud fraction at the location: <= clear_max -> clear,
    # >= overcast_min -> overcast, otherwise partly.
    "frac_clear_max": 0.20,
    "frac_overcast_min": 0.80,

    # Disc radius for the point read. ~3 km (one grid cell) so a clicked small
    # cloud registers instead of averaging away into the surrounding clear.
    "point_read_radius_km": 3.0,

    # A cloudy CLM pixel counts only if >= N of its 8 neighbours are cloudy;
    # drops isolated coastline false-cloud speckle. 0 = off.
    "coherence_min_neighbors": 2,

    # The CLM over-detects over sun-glint sea and coastline (Budva is both).
    # A "cloudy" pixel is dropped only when it is in the glint zone AND has no
    # corroborating CTTH/OCA retrieval; genuine cloud, including thin cirrus,
    # always survives.
    "glint_suppress": True,
    "glint_max_deg": 25.0,

    # Cloud-top height bands (m): low / mid / high.
    "height_low_max_m": 2000.0,
    "height_mid_max_m": 6000.0,

    # COT ~3 = cirrus boundary where the solar disc is still clearly visible.
    "cot_thin_max": 3.0,

    # Two separate axes: PRESENCE comes from the CLM only (thin cirrus is
    # cloud, never COT-gated); SUN-BLOCKING is slant optical depth
    # (COT / cos SZA) crossing cot_block_min, with ice cloud needing
    # sun_ice_factor more. At night OCA COT is unusable: no sun claim.
    "cot_block_min": 5.0,
    "sun_night_sza": 80.0,
    "sun_ice_factor": 1.5,

    # Sun/shade word comes from the Cloud Modification Factor
    # (GHI_cloudy/GHI_clear, clouds/solar.cmf), not direct-beam transmittance:
    # thin forward-scattering cloud keeps the sky bright.
    "cmf_sunny_min": 0.80,
    "cmf_blocked_max": 0.40,

    # MTG sits at 0N,0E; cloud over Budva appears shifted ~1.3x its height NE.
    # The sampling disc already absorbs this.
    "parallax_correct": False,

    # Sky-cover weight for semi-transparent cloud: 0 = only sun-blocking cloud
    # sets the clear/partly/overcast level; thin cirrus shows as a note.
    "semi_sky_weight": 0.0,

    "nowcast_lead_max_min": 120,
    "nowcast_lead_step_min": 10,
    "nowcast_dir_spread_deg": 12.0,
    "nowcast_dir_growth_deg_per_min": 0.08,
    # One global cross-correlation vector can lock onto a spurious far peak
    # ("SW @ 408 km/h"). Real jet-level cirrus tops out ~200 km/h; faster
    # estimates are treated as unreliable and never drive the nowcast.
    "motion_max_speed_kmh": 250.0,

    "keep_frames": 12,  # ~2 h at the 10-min cadence
}

# Public NWP forecast JSON used for the 2-48 h cloud outlook band on
# cloud-map.html. Observed (satellite) and modeled (NWP) stay in separate bands.
NWP_FORECAST_URL = "https://matko-iv.github.io/vrijeme/forecast_data/forecast_48h.json"

# GitHub Pages rebuilds on push and pins a 10-min CDN cache, so pushed data
# goes stale. The pipeline mirrors docs/ outputs to Cloudflare R2 and the pages
# fetch from R2_PUBLIC_BASE cache-busted. Credentials come from the environment
# (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY); if unset, publishing
# is a silent no-op. See radar/r2_publish.py.
R2 = {
    "enabled": True,
    "bucket": "skala-data",
    "public_base": "https://pub-3d539da10a4c4aa8a3f0048f8dcb067c.r2.dev",
    "cache_control": "public, max-age=15",
}
