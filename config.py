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
        # The radar's own coverage disc — everything drawn OUTSIDE it on the
        # image is basemap/frame, never echo. Site coords + range read from the
        # station's ODIM PVOL metadata (MeteoGate ORD, hrulj: 623 bins x 400 m
        # = 249.2 km; we clip 1 km inside so the drawn rim line can't leak in).
        "radar_site": (42.8944, 17.4783, 248.0),  # (lat, lon, range_km)
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
        "radar_site": None,  # composite of many radars — no single coverage disc
        "calibration": None,
    },
}

# ============================================================================
# MeteoGate ORD (raw ODIM volumes) — Stage 4
# ============================================================================
# When True, the dhmz source's cells + rings come from the hrulj (Uljenje)
# raw polar volume on MeteoGate ORD (anonymous S3, 5-min cadence, RHOHV
# clutter-filtered) instead of colour-classifying the PNG. The PNG is still
# fetched: it stays the display layer and the automatic fallback whenever the
# ORD fetch/decode fails. See radar/ord.py.
ORD_ENABLED = True

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
# 'approaching' verdict horizon + distance gate, tuned on the verification log
# (2026-06-11 replay of 1133 matured scans / 73 onsets, _far_sweep.py):
#   * the verdict is scored on a 60-min horizon, so it must use the 60-min
#     cumulative bucket, NOT the 120-min p_rain (built-in over-trigger);
#   * gating on the dominant cell being <= 50 km kept POD 0.973 while cutting
#     FAR 0.721 -> 0.601 (median dominant distance: hits 11 km vs FA 43 km).
# Re-tune both as the log grows.
APPROACH_LEAD_MIN = 60           # Lead bucket (min) the approaching verdict keys off
APPROACH_MAX_DIST_KM = 50.0      # Dominant cell farther than this is "watch", not "approaching"
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

# Full-volume radar products (PDF Part C2/C1, radar/volume.py). Environmental
# 0 C (freezing) level used as the base of the ZDR-column updraft proxy. This is
# a SEASONAL placeholder (Adriatic summer ~3.5-4 km, winter ~1-2 km); ideally
# fed from NWP. ZDR columns are only the part of ZDR>=1 dB ABOVE this level.
FREEZING_LEVEL_M = 3500.0

# VIL (kg/m2) below which a column carries negligible rain — the floor the
# survival model decays a collapsing cell's VIL toward (PDF Part C2/B2). Used as
# the 3-D analogue of RAIN_DBZ_THRESHOLD in nowcast._lifetime_min.
VIL_RAIN_FLOOR = 0.5

# Coastal-arrival score (PDF Part C3, radar/coastal.py). Budva-specific: open
# sea lies to the SW (~225 deg), the Dinaric/Rumija ridge inland to the NE. A
# cell that is inland AND steered seaward must DESCEND the seaward slope, where
# subsidence warming/drying promotes dissipation -> down-weight its arrival.
# These are physically-motivated starting points to TUNE against verification,
# not validated local climatology.
COASTAL_SEAWARD_AZIMUTH_DEG = 225.0   # direction from Budva toward open sea
COASTAL_RIDGE_DISSIPATION = 0.5       # arrival multiplier for seaward-descending cells

# ============================================================================
# Clouds (EUMETSAT satellite cloud-cover module) — parallel to the radar module
# ============================================================================
# The cloud module fetches EUMETSAT Data Store cloud products (via eumdac),
# subsets them to a REGULAR lat/lon grid over the region below, and runs a
# field-advection nowcast for the cloud verdict (see the clouds/ package). It is
# fully independent of the radar pipeline above and writes its own outputs
# (output/cloud_status.json, docs/cloud_data.js, docs/cloud_status.json).
#
# Everything downstream of clouds/fetch.py consumes a NORMALIZED cloud field
# (a regular lat/lon grid with named layers: mask / fraction / cloud-top temp /
# cloud-top height / optical thickness / phase). fetch.py is the only adapter
# that touches EUMETSAT product specifics; discover.py pins the exact ids/vars.
CLOUDS = {
    # Region of interest (bounding box) for the satellite subset, around
    # LOCATION. Big enough to see cloud fields advecting in from any side
    # within the ~2 h nowcast horizon (a ~100 km/h jet covers ~200 km).
    "bbox": {  # degrees
        "lat_min": 40.3, "lat_max": 44.3,
        "lon_min": 16.4, "lon_max": 21.4,
    },
    # Target subset grid resolution (degrees per cell on the regular lat/lon
    # grid produced by Data Tailor). ~0.03 deg ~ 3 km, near MTG-FCI L2 native.
    "grid_step_deg": 0.03,

    # --- Map rendering: use EUMETSAT GeoColour as the picture (clouds/visible.py).
    # The L2 retrievals miss small sub-pixel cumulus and over-detect at night, so
    # for the MAP we show GeoColour — exactly what EUMETView shows — while the
    # Budva point verdict still uses the COT logic. Set False to draw the L2
    # overlay instead. Fetch failures (offline) fall back to the L2 render.
    "use_geocolour_map": True,
    "geocolour_wms": "https://view.eumetsat.int/geoserver/wms",
    "geocolour_layer": "mtg_fd:rgb_geocolour",
    # GeoColour is a RENDERED RGB picture, not a measurement: its brightness reads
    # as "cloud" over sun-glint on the sea, snow, and low sun, and at night means
    # cloud-top temperature, not albedo (PDF Section 5). So by default the verdict
    # comes from the L2 retrievals (CLM presence + OCA COT + solar zenith) and
    # GeoColour is the display MAP only. Set True to drive the verdict from RGB
    # brightness again — but then it is only used by day with the sun high enough
    # (see geocolour_verdict_day_only / geocolour_max_sza); otherwise it falls
    # back to L2 so glint/twilight/night can't produce a false "cloudy".
    "use_geocolour_verdict": False,
    "geocolour_verdict_day_only": True,  # never trust RGB brightness at night
    "geocolour_max_sza": 70.0,           # ...nor when the sun is low (glint/shadow)
    "geocolour_sample_km": 6.0,     # disc radius around Budva for the read
    "geocolour_bright_min": 150,    # max(R,G,B) >= this & near-neutral = cloud
    "geocolour_sat_max": 40,        # max(R,G,B)-min(R,G,B) <= this = near-neutral
    "geocolour_thick_min": 205,     # very bright = optically thick / sun-blocking

    # EUMETSAT Data Store collection IDs — pinned from the live catalogue
    # (clouds/discover.py, 2026-06-19). MTG (Meteosat Third Generation, 0 deg
    # disk) covers Budva; these are the dedicated netCDF L2 cloud products.
    "collections": {
        # Cloud Mask (netCDF) - MTG - 0 deg: clear/cloudy -> presence + advection
        "clm": "EO:EUM:DAT:0678",
        # Cloud Top Temperature and Height - MTG: cloud-top temp + height
        "ctth": "EO:EUM:DAT:0681",
        # Optimal Cloud Analysis - MTG: optical thickness + phase
        "oca": "EO:EUM:DAT:0684",
        # (Cloud Type - MTG = EO:EUM:DAT:0680 — could replace the derived
        #  band/thickness label later; not used yet.)
    },
    # Prefer EUMETSAT Data Tailor to ROI-subset + reproject to a regular lat/lon
    # grid (keeps files small, makes clouds/grid.py a trivial lat/lon index).
    "use_data_tailor": True,

    # Cloud fraction thresholds at the location (fraction of cloudy pixels in
    # the innermost ring): <= clear_max -> clear, >= overcast_min -> overcast,
    # otherwise partly.
    "frac_clear_max": 0.20,
    "frac_overcast_min": 0.80,

    # N-adjacent spatial-coherence on the CLM presence mask (PDF Part A1): a
    # cloudy pixel is only counted if >= this many of its 8 neighbours are also
    # cloudy. Drops isolated coastline false-cloud (Budva is a coastal pixel —
    # the textbook worst case) so PRESENCE isn't inflated by speckle. 0 = off.
    "coherence_min_neighbors": 2,

    # Cloud-top height bands (m): low <2 km, mid 2-6 km, high >6 km (WMO-ish).
    "height_low_max_m": 2000.0,
    "height_mid_max_m": 6000.0,

    # Optical thickness: thin vs thick for the SUN/SHADE call (COT ~3 = the cirrus
    # boundary where the disc is still clearly visible -> "sun gets through").
    "cot_thin_max": 3.0,

    # --- TWO SEPARATE AXES (PDF Section 3) ----------------------------------
    # 1) PRESENCE ("is there cloud") comes from the CLM mask ONLY and is NEVER
    #    gated on COT — optically thin cirrus IS cloud and must be counted. (This
    #    is the bug the PDF flags: a COT cutoff on presence deletes real cirrus.)
    # 2) SUN-BLOCKING ("is the sun blocked") = optical thickness AND sun geometry:
    #    a pixel blocks the sun when its SLANT optical depth (COT / cos SZA)
    #    crosses cot_block_min, so the same cloud blocks more when the sun is low.
    #    Ice cloud forward-scatters, so its blocking threshold is raised by
    #    sun_ice_factor. At night (SZA >= sun_night_sza) OCA COT is unusable, so we
    #    fall back to CLM presence + CTTH and make NO sun claim.
    # cot_block_min ~5 reproduces the clear/cloudy split closely at high sun; the
    # /cos(SZA) slant term lowers it automatically toward sunrise/sunset.
    "cot_block_min": 5.0,
    "sun_night_sza": 80.0,       # SZA at/above which we report no sun verdict
    "sun_ice_factor": 1.5,       # ice cloud needs ~50% more COT to block the sun

    # Parallax: MTG sits at 0N,0E so cloud over Budva (satellite zenith ~52 deg)
    # appears shifted ~1.3x its height toward the NE. The sampling disc (>=10 km)
    # already absorbs this; set True to additionally shift the sun/shade COT
    # sample toward where overhead cloud appears (uses the cloud-top height).
    "parallax_correct": False,

    # Sky-blocking weight for CONTAMINATED (semi-transparent) cloud in the
    # clear/partly/overcast level. Effective sky cover = opaque + semi_sky_weight
    # *(presence - opaque). 0 = only genuinely sun-blocking cloud sets the level;
    # thin cirrus shows as a "thin veil / sun gets through" note, never overcast.
    "semi_sky_weight": 0.0,

    # Advection nowcast horizon + step (mirror the radar nowcast windows).
    "nowcast_lead_max_min": 120,
    "nowcast_lead_step_min": 10,
    # Directional cone spread for field motion (deg), grows with lead time.
    "nowcast_dir_spread_deg": 12.0,
    "nowcast_dir_growth_deg_per_min": 0.08,

    # Frame cache retention (mirror KEEP_FRAMES). 12 frames @ ~10 min ~ 2 h.
    "keep_frames": 12,
}

# Public NWP forecast JSON (weather-forecast / "vrijeme" project) used for the
# 2-48 h cloud OUTLOOK band on cloud-map.html. Observed (satellite) vs modeled
# (NWP) are rendered in separate bands — never conflated.
NWP_FORECAST_URL = "https://matko-iv.github.io/vrijeme/forecast_data/forecast_48h.json"
