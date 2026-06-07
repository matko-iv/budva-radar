# Budva weather simulator — design

**Date:** 2026-06-05
**Status:** design approved (decisions locked); Stage 1 pending implementation

A self-contained, physics-based weather **sandbox** on a ~10 km map around Budva.
Real fluid + thermodynamic equations, computed in the browser, so weather *behaves*
like real life — NOT a forecast of the actual weather (no real-time data /
assimilation; that would be the forecast model we deliberately exclude). You can
seed it from the live radar ("Učitaj trenutni radar") and then physics takes over.

## Locked decisions

| Decision | Choice |
|---|---|
| Type | Interactive **sandbox** (place/seed cells, set wind, press play) |
| Home | New standalone page `docs/simulator.html` (reuses calibration + dBZ colormap) |
| Start state | Blank, with a **"Učitaj trenutni radar"** button that seeds from `data.js` |
| Wind | Global wind that varies **locally by real physics** (terrain-driven) |
| Physics | **Full**: wind (Navier–Stokes) + moisture/clouds/rain + solar/sea thermics |
| Domain / res | **~10 km box around Budva, 256×256 (~40 m/cell)** |
| Substrate | **CPU, Float32Array** — physics core is unit-testable in Node |
| Build | **Staged**: Stage 1 wind core → Stage 2 moisture → Stage 3 solar/thermics |

**Expectation (explicit):** physically realistic *behaviour*, not real-time
accuracy. Matching Budva's actual current weather needs assimilation = a forecast.

## Architecture

Small, single-purpose, testable units:

- **`docs/windsim.js`** — UMD (browser + Node) incompressible fluid solver on a
  collocated grid with a terrain obstacle mask. Pure numerics, no DOM → Node
  unit tests. (Stage 2/3 extend it with scalar transport + thermodynamics.)
- **`docs/terrain-budva.js`** — builds the coarse Budva heightmap + masks (sea /
  land / Lovćen) for the 10 km box. Hand-built now; swappable for a real DEM
  (SRTM) later. Pure → testable.
- **`docs/simulator.html`** — view: canvas rendering (terrain, wind field, cells,
  Budva marker), the animation loop, and the controls. Depends on the two modules
  above + the existing calibration (`PX_PER_KM`, `budva_pixel` from `data.js`) and
  a dBZ→RGB ramp derived from the DHMZ legend (`radar/colormap.py`).

Reused, not rebuilt: km-plane conventions (as in `nowcast-browser.js`), the dBZ
color ramp, and the `data.js` cell catalog shape for seeding.

---

## Stage 1 — CFD wind core (what we build first)

A working sandbox that shows **wind streaming around Lovćen / the coast** and
**passive cells drifting with it**. No moisture yet (that's Stage 2); cells are
passive colored blobs advected by the wind so motion is visible.

### Grid & domain
- `N = 256`, domain `L = 10 km`, `dx = L/N ≈ 39 m`. Budva at the box center.
- Collocated grid (u, v, p, terrain all cell-centered). Float32Array length `N*N`.
- World mapping: cell `(i,j)` → km offset from Budva `((i+0.5)*dx − L/2, …)`; →
  pixels via `PX_PER_KM` for rendering and for seeding real radar cells.

### Terrain (the local-variation driver)
- `terrain-budva.js` returns `height[N*N]` (m) and a boolean `solid[N*N]`.
- Coarse, geographically aligned: sea = 0 to the SW (Adriatic); land rises toward
  the NE; the Lovćen massif as a raised ridge in the NE. A cell is `solid` when
  `height > H_solid` (e.g. 600 m) — the flow must go around it.
- Coastline orientation ≈ NW–SE through Budva. Parameterized so it's easy to tune
  / later replace with a DEM. Documented as approximate.

### Fluid solver (Stam "stable fluids", incompressible Navier–Stokes)
Per step `dt`:
1. **Forcing** — nudge velocity toward the global wind `U₀=(Ux,Uy)` in open air:
   `u += k·(Ux−u)·dt` (small `k`), so the large-scale flow is `U₀` but terrain
   reshapes it locally. Solid cells: `u=v=0`.
2. **Advect** velocity by semi-Lagrangian backtrace (unconditionally stable):
   `u*(x) = u(x − u·dt)` with bilinear sampling; skip/zero inside solids.
3. **Project** to divergence-free (mass conservation `∇·V=0`): solve the pressure
   Poisson `∇²p = (1/dt)∇·V*` by **red–black Gauss–Seidel** (~20–40 sweeps),
   then `V = V* − ∇p`. Obstacle boundary = **no-penetration** (zero normal
   velocity on solid faces; Neumann `∂p/∂n=0` there). Domain boundary: inflow =
   `U₀` on the upwind side, zero-gradient outflow downwind.
4. Optional tiny viscosity (numerical smoothing) — off by default.

Result, from the continuity equation (not hand rules): acceleration through gaps
/ over ridges, deflection around the massif, a weak lee wake.

### Passive cells (visible motion)
- State: `{x_km, y_km, dbz, r_km}`. Each step: bilinear-sample `V` at the cell,
  `x += V·dt`. Rendered as a soft blob colored by the dBZ ramp. Cells leaving the
  box are dropped.
- **Add**: click the map → cell at a preset intensity/size (light / heavy / storm).
- **Seed**: "Učitaj trenutni radar" maps `data.js` `sources.dhmz.cells` (abs
  lat/lon → km offset from Budva) into cells inside the box.

### Rendering (canvas)
- Layers: terrain (sea/land/ridge shading + coastline), wind field (speed as
  color and/or a sparse arrow/streamline overlay, toggleable), cells (blobs),
  Budva marker. A small readout: wind at Budva, # cells.

### UI controls
Wind direction (compass) + speed (slider); Add-cell preset; Play / Pause; sim
speed; Clear; "Učitaj trenutni radar"; toggles for field-arrows and terrain.

### `windsim.js` public API
`createSim({N, L, terrain})` → `{ setWind(dirDeg, speedMs), step(dt),
sampleVel(xKm,yKm)→[u,v], u, v, solid, divergence()→Float32Array }`.

### Stage 1 tests (Node, `tests/test_windsim.js`)
1. **Divergence-free**: after `project`, `max|∇·V| < ε` on open cells.
2. **No-penetration**: normal velocity into `solid` cells ≈ 0.
3. **Uniform flow, flat terrain** → field stays ≈ `U₀` (no spurious vorticity),
   divergence ≈ 0.
4. **Obstacle** → deflection: speed-up on the flanks, stagnation upwind, weaker
   lee; assert these inequalities on a known ridge.
5. **Stability**: fields stay finite/bounded over many steps.
6. Bilinear `sampleVel` correctness on a linear field.

---

## Stage 2 — moisture, clouds, rain (deferred; own spec)
Add advected scalars `T` (potential temperature), `qv` (vapor), `qc` (cloud),
`qr` (rain). Saturation `qvs(T,p)`; condensation `qv↔qc` releases latent heat
(buoyancy source in the momentum eq); autoconversion `qc→qr`; rain falls + can
re-evaporate. Cells become real moisture, not passive blobs. New tests: latent-heat
energy bookkeeping, condensation only above saturation, mass conservation of water.

## Stage 3 — solar / sea thermics (deferred; own spec)
Surface energy: diurnal solar heating of land vs. sea (heat capacity contrast) →
sea-breeze circulation; buoyant thermals → afternoon convective clouds. A day-night
clock. Tests: sea-breeze onset direction, thermal plume rises.

## Out of scope (all stages)
Real-time accuracy / assimilation; 3-D (this is 2-D plan-view, terrain via mask +
depth weighting); microphysics beyond warm-rain; GPU/WebGL (possible later
optimization if 256² CPU is slow on weak hardware — physics core stays the same).

## File list (Stage 1)
- new `docs/windsim.js`, `docs/terrain-budva.js`, `docs/simulator.html`
- new `tests/test_windsim.js`
- link to `simulator.html` from `docs/index.html` and `docs/radar-map.html`
