# Per-point probabilistic nowcast via shipped cell catalog

**Date:** 2026-06-05
**Status:** approved (approach + fidelity), pending implementation

## Problem

`radar-map.html` interprets *clicked* (non-Budva) points with single-frame motion
geometry (`assessApproach` → `classifyScenario`). That path has known blind spots:

- 15 km range gate (`APPROACHING_MAX_KM`) dismisses a strong cell farther out but
  heading straight in.
- Requires scene `motion.confidence ≥ 0.6`; on low confidence (e.g. 0.178) it
  gives up and never flags approaching.
- One global motion vector + straight-line extrapolation; no per-cell vectors, no
  growth/decay, no probability.

Budva (both pages) instead uses the precomputed **probabilistic cell-tracking
nowcast** (`nowcast.arrival_nowcast`), which is per-cell, multi-frame and
probabilistic. The goal: give clicked points that **same** model.

## Decision

Ship the DHMZ **cell catalog** in `data.js`; port `arrival_nowcast` faithfully to
JS; run it in-browser for any clicked point. Budva stays pinned to the precomputed
`budvaHeadline` (so `index.html` and the DHMZ detail line can never drift).

- **Source:** DHMZ only (radar-map is DHMZ-pixel based: `PX_PER_KM=1.3648`,
  `IMG_W=720`). DHMZ cells are inherently local (frame radius ≈150 km).
- **Fidelity:** faithful probabilistic port (5×5 unscented cone + survival), not a
  simplified geometric approximation. Accepted cost: a second copy of the model in
  JS that must track `nowcast.py`/`config.py`. A parity test guards drift.

## Data contract (`data.js`, per source — emitted for DHMZ)

`sources.dhmz.cells`: list of tracked cells, each:

```
{ id, lat, lon, equiv_diam_km, max_dbz, cell_type,
  speed_kmh, direction_deg, dbz_trend_per_min, trend }
```

- `lat`/`lon`: **absolute** centroid (location-independent). JS recomputes
  edge/bearing relative to any point.
- `speed_kmh`/`direction_deg`: the cell's **own** track velocity (compass deg).
- **All** DHMZ cells shipped (no distance bound). A 150 km-from-Budva bound was
  rejected: a point clicked near the frame edge can have a relevant cell far from
  Budva but close to that point, and the nowcast's own ~240 km reach gate already
  handles distance. DHMZ cells are few (~7) and local, so cost is negligible.
- Source built in `interpret_source` from the `cell_summaries` it already holds
  (radar/interpret.py:81-85). Added as a new key on the returned source dict
  (interpret.py:135-146). Purely additive — no existing field changes.

## JS port — `docs/nowcast-browser.js` (UMD: browser `window.NOWCAST` + Node `module.exports`)

Faithful mirror of `nowcast.py`:

- `lifetimeMin(cell)` ← `_lifetime_min`
- `cellArrival(cell, latP, lonP)` ← `_cell_arrival`
- `arrivalNowcast(cells, latP, lonP)` ← `arrival_nowcast`
- `classifyIntensity(dbz)` ← `colormap.classify_intensity` (for `dominant.intensity_label`)

Constants hardcoded to match `config.py` (single `const C = {…}` block, documented
as "must match config.py"):

```
RAIN_DBZ_THRESHOLD=20.0  CELL_CORE_DBZ=40.0  NOWCAST_MIN_LIFETIME_MIN=15.0
NOWCAST_REACH_BUFFER_KM=5.0  NOWCAST_MAX_SPEED_KMH=120.0
NOWCAST_SPEED_FACTORS=[0.8,0.9,1.0,1.1,1.2]  NOWCAST_LEAD_STEPS_MIN=5
NOWCAST_LEAD_MAX_MIN=120  NOWCAST_DIR_SPREAD_CONVECTIVE_DEG=15.0
NOWCAST_DIR_SPREAD_STRATIFORM_DEG=5.0  NOWCAST_DIR_GROWTH_DEG_PER_MIN=0.1
P_APPROACH_THRESHOLD=0.25  LEAD_BUCKETS=[15,30,60,120]
```

Direction weights: speed `sw = exp(-0.5*((f-1)/0.2)^2)` normalized; direction
`doff=[-2,-1,0,1,2]`, `dw = exp(-0.5*(off/1)^2)` normalized. km-plane
`kx=111.32*cos(latP)`, `ky=110.57`, `px=(lon-lonP)*kx`, `py=(lat-latP)*ky`.
Output shape identical to Python (`p_rain`, `eta_minutes`, `dominant{…}`,
`p_by_lead`, `approaching`, `n_cells_considered`).

## Wiring — `radar-map.html`

1. `<script src="nowcast-browser.js"></script>` before the page script.
2. Click → pixel `(cx,cy)` already known. Reconstruct point lat/lon (isotropic
   `PX_PER_KM`, same scalar the rings use):
   - `eKm=(cx-budvaX)/PX_PER_KM`, `nKm=(budvaY-cy)/PX_PER_KM` (y down → north up)
   - `latP=budva.lat + nKm/110.57`, `lonP=budva.lon + eKm/(111.32*cos(budva.lat))`
3. In `classifyScenario`, replace the `assessApproach` path with
   `NOWCAST.arrivalNowcast(dhmz.cells, latP, lonP)`. Map its output into the same
   `facts` shape `SKALA.interpret` expects — mirroring `factsFromSource`:
   - `approaching = nc.approaching`, `eta = nc.eta_minutes`
   - `threat` = `nc.dominant` when approaching (drives SEVERE), bounded to vicinity
   - **local** `rainAtLocation`/`anyWet`/`anyEcho`/`km`/`cardinal`/`dbz` stay from
     the live pixel scan (local intensity at the point), exactly as today.
   - `dominant.dist_km`/`bearing_cardinal` computed relative to the clicked point.
4. Wording still flows through shared `SKALA.interpret` — output style unchanged.
5. Budva click still routes to `budvaHeadline` (existing `isBudva` branch); the JS
   port is for non-Budva points. `assessApproach` and its 3 constants are removed
   if nothing else uses them (verify first).

## Known bounds (documented, accepted)

- Isotropic `PX_PER_KM` for click→km (same assumption the rings already make).
- `dominant.dist_km` uses equirectangular km in the browser vs pixel-calibration
  km in Python — a ~1% gap on a *display* label only; all probabilities (which use
  equirectangular advection in both) are exact. (Confirmed by the parity test:
  probabilities match; dist_km within 3% at the extraction point.)
- Two implementations of the model (Python authoritative, JS mirror) → parity test.

## Verification

- **Parity test** (`tests/test_nowcast_parity.py` or a Node+Python pair): run the
  real pipeline offline on the 4 cached DHMZ frames → Python `nowcast_details` for
  Budva + the emitted `cells`. Feed those same cells to the JS port at Budva
  (latP/lonP = Budva). Assert `p_rain`, `p_by_lead`, `eta_minutes`, and
  `dominant.track_id` match within rounding (±0.001 on probs, ±0.1 on eta).
- Regenerate `data.js` offline (`interpret.interpret_all()`, cached frames) and
  confirm `sources.dhmz.cells` is present and well-formed.
- `node --check nowcast-browser.js`; spot-check a non-Budva click in the browser.

## Out of scope

- OPERA catalog (Europe-wide, huge) — DHMZ only for now.
- The stale-image notice (already shipped in commit c47b26fd).
