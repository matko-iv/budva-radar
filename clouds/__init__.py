"""budva-radar cloud-cover module (EUMETSAT satellite).

A parallel to the radar precipitation module: instead of "is it raining / will
it rain", it answers "are clouds over Budva now / are they approaching or
clearing", plus cloud type / height / thickness and a near-term sun outlook.

Pipeline (mirrors the radar one, but for a geostationary satellite sensor):

    discover.py   one-off: pin the live EUMETSAT collection ids + variable names
    fetch.py      eumdac download + ROI subset -> NORMALIZED cloud field (.npz)
    grid.py       geolocation + point/disc sampling on the regular lat/lon grid
    interpret.py  facts at the location + rings (fraction, type, height, ...)
    motion.py     whole-field advection vector (cross-correlation)
    nowcast.py    semi-Lagrangian field advection -> approaching / clearing / ETA
    verdict.py    THE canonical cloud verdict (state machine + wording)
    ../run_clouds.py   orchestration -> output/ + docs/ artifacts

------------------------------------------------------------------------------
NORMALIZED CLOUD FIELD
------------------------------------------------------------------------------
Everything downstream of fetch.py consumes one normalized representation, so the
EUMETSAT product specifics live ONLY in fetch.py (and are pinned by discover.py).

A frame is a regular lat/lon grid saved as an .npz (see clouds/grid.py
CloudField.save/load):

    lats   float32 [H]     latitudes of each grid row   (monotonic)
    lons   float32 [W]     longitudes of each grid col   (monotonic)
    mask   float32 [H,W]   cloud mask: 1.0 cloudy, 0.0 clear (NaN = no data)
    frac   float32 [H,W]   cloud fraction 0..1 (== mask if product is binary)
    ctt    float32 [H,W]   cloud-top temperature [K]   (NaN where clear)
    cth    float32 [H,W]   cloud-top height [m]        (NaN where clear)
    cot    float32 [H,W]   cloud optical thickness     (NaN where clear)
    phase  float32 [H,W]   0 none / 1 water / 2 ice    (NaN where clear)
    meta   0-d str         JSON: {"sensing_time": iso, "source": "...", ...}

Cached frames are named YYYYMMDD_HHMMSS_<hash>.npz (+ a display .png), exactly
like the radar frames, so motion history and pruning reuse the radar idiom.
"""
