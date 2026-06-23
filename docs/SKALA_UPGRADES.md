# SKALA PDF Upgrades — Implementation Status

Tracks the work from *"Fixing False Alarms in Skala"* (Budva radar/cloud research
report). Lightning (Part D) is explicitly **out of scope** per the owner. The
Gemini-narrative PDF is a **separate repo** (`weather-forecast`) and not touched
here.

All work is dependency-free (numpy / scipy / h5py / math — the repo's existing
stack); no pysteps / wradlib / Py-ART / cv2 were added (none are installable in
the project `.venv`, and the repo ethos is to re-implement in-house). Everything
below was built test-first; the suite is **16/16 green** (was 10/1 — the one
pre-existing failure, verdict parity, is now fixed). The whole PDF except the
lightning module (Part D) is now implemented.

## Done & verified

### Part E — Closest-point-of-approach (the "RAIN ETA ~0" false alarm)
- `nowcast.closest_point_of_approach(rx, ry, vx, vy) -> (t_cpa, d_min)` — exact
  time + miss distance of closest approach (replaces the discrete range-rate sign).
- `arrival_nowcast` now classifies the dominant cell **HIT / BYPASS / RECEDING**
  and exposes `bypassing` + `dominant.{classification, t_cpa_min, miss_km}`.
- Mirrored verbatim in `docs/nowcast-browser.js`; `tests/test_nowcast_parity.py`
  extended (tangential-bypass case) — Python↔JS parity holds.
- Tests: `tests/test_cpa.py` (geometry + HIT/BYPASS/RECEDING classification).

### Part E — Severe re-gate (fixes the pre-existing verdict-parity failure)
- **Decision:** the PDF says *re-gate* severe on CPA; the JS had *removed* the
  SEVERE point-alert ("false-triggered on distant cells") while Python kept it
  (the red parity test). Per owner: **revive SEVERE in JS, CPA-HIT-gated.**
- `severe_approaching` now requires the dominant threat to be a CPA `HIT`
  (`radar/verdict.py` + `docs/skala-text.js`); `docs/skala-sections.js` carries
  `threat.cpaClass`. A distant bypassing/receding severe cell no longer raises a
  point alert. `tests/test_verdict_parity.py` adds HIT and BYPASS cases. **Verdict
  parity is now GREEN.**

### Part A2 — Cloud Modification Factor (CMF)
- `clouds.solar.cmf(cot, sza)` implements the published Papachristopoulou et al.
  (2024) form `1 - tanh(b·COTᵃ)` with the SZA polynomials. Exposed as the
  `cmfDiag` fact.
- **Decision (owner): DIAGNOSTIC ONLY — `sun_state` stays Beer-Lambert.** The
  published coefficients, as transcribed *and as fetched from the AMT paper HTML*,
  are degenerate with the literal grouping (CMF≈0 for every COT≳0.1 at all SZA —
  the opposite of the paper's stated behaviour). **Action for owner:** confirm the
  exponent grouping against the *typeset* Eq. 2 / Fig. 2a, then flip `cmf` to drive
  the sun-state. Until then it ships only as a labelled diagnostic.
- Part A3 (separate cloud *presence* from sun-blocking *cover*) was already done
  in the codebase (`skyCoverEff` vs `cloudFracNow`).

### Part C2/C1 — Full-volume radar products
- New `radar/volume.py` (h5py + numpy): `beam_height_m` (4/3-earth),
  `vil_from_profile` (Greene–Clark, 18-dBZ floor / 56-dBZ hail cap),
  `echo_top_m` (18-dBZ, interpolated), `vil_density_g_m3`, `column_products`,
  `zdr_column` (ZDR≥1 dB above the 0 °C level — updraft proxy),
  `surface_rain_confidence` (low when the lowest beam overshoots, e.g. ~2.5 km
  over Budva at 130 km), plus polar-volume I/O (`read_volume`,
  `column_profile_at`, `column_products_at`) reading **all 9 sweeps** of the ORD
  PVOL.
- Wired into `radar/interpret.py` (STAGE 4b) — shipped under `source.volume`.
  Real-data-verified against the cached ORD volume (lowest beam over Budva ≈ 2.5 km
  confirmed). Config: `FREEZING_LEVEL_M` (seasonal placeholder; ideally NWP-fed).
- Tests: `tests/test_volume.py` (pure column math + real-data column).

### Part C2/B2 — 3-D trend survival ("ANVIL intent")
- `nowcast._lifetime_min` now PREFERS the full-volume VIL trend over the 2-D dBZ
  trend (falls back when no volume). `tracking.update_summaries` computes
  `vil_trend_per_min` across frames; `radar/interpret.py` attaches per-cell VIL
  (one volume read, reused by the Budva column) and ships `vil_kg_m2` /
  `vil_trend_per_min` in the cell catalog. Mirrored in `docs/nowcast-browser.js`
  (+ `VIL_RAIN_FLOOR`); `tests/test_nowcast_parity.py` adds a VIL-trend case.
  Config: `VIL_RAIN_FLOOR`. Tests: `tests/test_survival.py`. *Not literal ANVIL*
  (pysteps unavailable — see "Deferred").

### Part C3 — Coastal-arrival score
- New `radar/coastal.py`: `coastal_arrival_score = f(base CPA prob, VIL/dBZ
  trend, Dinaric-ridge dissipation)` + `descends_seaward` (inland cell steered
  over the seaward slope → down-weighted). Wired into `radar/interpret.py` for
  the dominant cell (`nowcast_details.coastal_arrival`). Config:
  `COASTAL_SEAWARD_AZIMUTH_DEG`, `COASTAL_RIDGE_DISSIPATION`. Lightning term
  omitted. Tests: `tests/test_coastal.py`. Heuristic — tune against the log.

### Part B1 — Block/TREC dense motion field
- `radar/motion.py`: `trec_field` (tile + cross-correlate → local vectors),
  `field_median` (outlier-robust scene motion), `motion_field` (geo-located
  field from two RGB frames). Shipped under `source.motion_field`; the existing
  global vector is kept for back-compat. The dependency-free stand-in for pysteps
  Lucas–Kanade. Tests: `tests/test_motion_field.py` (incl. differential motion).

### Part A1 — Coastal + sun-glint masking
- `clm.spatial_coherence` (N-adjacent test) wired into `clm.categorize` +
  `clouds/fetch.py` (config `CLOUDS.coherence_min_neighbors`): isolated
  coastline false-cloud is dropped so PRESENCE isn't inflated at the Budva
  coastal pixel. `solar.solar_azimuth_deg` + `solar.glint_angle` add the
  sun-glint geometry (flag glint zone < ~25-30 deg). Tests in
  `tests/test_cloud_clm.py` + `tests/test_cloud_solar.py`.

### Part C4/E — Honest-expectations ETA capping
- `radar/verdict.py` `_eta_text` (+ `DETERMINISTIC_ETA_MAX_MIN = 30`) flags any
  ETA beyond the deterministic skill horizon as "(probabilistic)"; mirrored in
  `docs/skala-text.js` (`etaText`), verdict parity green. The probabilistic
  `p_by_lead` cone already existed.

## Deferred (require new deps / external feeds / another repo)
- **Lightning (Part D)** — out of scope per owner (Blitzortung MQTT + EUMETSAT
  MTG-LI `eumdac`).
- **Gemini narrative rephrase-and-validate PDF** — lives in the separate
  `weather-forecast` repo; not in `budva-radar`.

### Literal ANVIL / pysteps optical flow / wradlib — why deferred, and how to enable
The items above are dependency-free **stand-ins** that realise the same INTENT
(VIL trends for growth/decay; block/TREC for a motion field; hand-rolled VIL on
the ODIM volume). The owner wants the *literal* library implementations too;
here is exactly what blocks them and the path to add them.

**Why they aren't in right now**
1. **Not installed** — none of `pysteps`, `wradlib`, `Py-ART`, or `cv2`
   (OpenCV) is in the project `.venv` (verified by import probe). `pysteps`
   needs OpenCV; `wradlib`/`Py-ART` pull in GDAL/PROJ and (for Py-ART) a C/Cython
   build. On this Windows box those are non-trivial wheels.
2. **Repo ethos** — the codebase deliberately re-implements geometry/algorithms
   in-house (`solar.py` reimplements NOAA to avoid pyorbital; `nowcast.py`/
   `ord.py` are "numpy/math/h5py only") and keeps `status.json` byte-reproducible
   for clean git diffs. Adding heavy native deps cuts against that and the
   GitHub-Actions free-tier runner the pipeline runs on.
3. **They are libraries, not data** — adding them does not, by itself, change a
   single user-facing number; they would *replace* the stand-ins. So they are a
   quality/robustness upgrade, sequenced after the intent was delivered.

**How to enable (a clean follow-up)**
1. Pin deps in `requirements.txt` and install into `.venv`
   (`pip install opencv-python-headless pysteps wradlib arm-pyart`). Confirm the
   GitHub Actions runner can install them (or gate the heavy path behind a
   `try/except ImportError` so CI/the free runner falls back to the stand-ins).
2. **pysteps ANVIL** (replaces Part C2/B2 stand-in): grid the ODIM volume to a
   cartesian VIL field across the last N frames (`radar/volume.py` already gives
   per-column VIL — extend to a 2-D VIL grid), run `pysteps.nowcasts.anvil`
   (`ar_order` 1–2, 6 cascade levels) for the growth/decay nowcast, and blend
   its per-cell tendency into `nowcast._lifetime_min` behind a feature flag.
3. **pysteps Lucas–Kanade / Farnebäck** (replaces Part B1 stand-in): swap
   `radar/motion.py`'s TREC field for `pysteps.motion.get_method('LK')` and feed
   the dense field into both the radar cell advection and the cloud advection
   (`clouds/motion.py`). Keep `trec_field` as the no-OpenCV fallback.
4. **wradlib / Py-ART** (hardens Part C2): replace the hand-rolled `read_volume`
   /`column_profile_at` with `wradlib.georef` + `vpr.CAPPI` and
   `qual.beam_block_frac`/`cum_beam_block_frac` (true beam-blockage from a DEM,
   which the current beam-height model doesn't do), and use `xradar`/Py-ART for
   ODIM ingest. Validate VIL/echo-top against the current numbers before cutting
   over.

Keep each behind an `ImportError` fallback so the dependency-free path remains
the default on the free CI runner.

## Verification
Run the suite (standalone scripts, exit 0 = pass; node required for the two JS
parity tests):

```
for t in tests/test_*.py; do .venv/Scripts/python.exe "$t"; done
```
