# SKALA design notes

Decisions behind the false-alarm fixes, kept here so threshold changes and
rewrites don't silently undo them. Lightning integration is out of scope.

## Closest point of approach

`nowcast.closest_point_of_approach` gives the exact time and miss distance of
a cell's closest approach, replacing the instantaneous range-rate sign test
that called a tangential pass an on-location hit. The dominant cell is
classified HIT / BYPASS / RECEDING, mirrored in `docs/nowcast-browser.js`,
with Python-JS parity enforced by `tests/test_nowcast_parity.py`.

The SEVERE point alert is gated on the dominant threat being a CPA HIT
(`radar/verdict.py` + `docs/skala-text.js`): a distant bypassing or receding
severe cell no longer raises the alert. This resolved the long-standing
verdict-parity test failure — the JS had removed SEVERE entirely while
Python kept it ungated.

## Cloud Modification Factor

`clouds.solar.cmf` implements Papachristopoulou et al. (2024) Eq. 2,
CMF = 1 - tanh(b * COT^a). The paper's published coefficient polynomials are
degenerate as transcribed (CMF ~ 0 for every COT above ~0.1, the opposite of
the stated behaviour), so the coefficients are re-fit to the paper's own
anchors; see the docstring. Verify against the typeset equation before
trusting the coefficients further.

Cloud presence and sun-blocking cover are separate axes throughout:
`cloudFracNow` (CLM presence, thin cirrus counts) vs `skyCoverEff`
(slant-COT-gated opaque cover, which drives the clear/partly/overcast state).

## Full-volume products

`radar/volume.py` reads all 9 sweeps of the ORD PVOL: 4/3-earth beam height,
Greene-Clark VIL (18 dBZ floor, 56 dBZ hail cap), interpolated 18-dBZ echo
top, VIL density, ZDR columns above the freezing level (updraft proxy), and
a surface-rain confidence cue — the lowest beam is ~2.5 km over Budva at
130 km, so echo aloft may never reach ground. Shipped under `source.volume`.

The survival model (`nowcast._lifetime_min`) prefers the VIL trend over the
2-D dBZ trend; `tracking.update_summaries` computes `vil_trend_per_min` when
both frames carry a volume column. Mirrored in `docs/nowcast-browser.js`.

## Coastal arrival

`radar/coastal.py` down-weights arrival for inland cells steered over the
seaward Dinaric slope (subsidence drying) and folds in the growth/decay
trend. Heuristic starting points — tune against the verification log.

## Motion field

`radar/motion.py` carries both the global cross-correlation vector and a
per-tile TREC field (`trec_field` / `field_median` / `motion_field`) for
scenes with differential motion. The pysteps Lucas-Kanade dense flow is used
by the nowcast path (`radar/pysteps_nowcast.py`); TREC remains the
no-OpenCV fallback.

## Cloud false alarms

Budva is a coastal pixel next to sun-glint sea — the worst case for the
clear-conservative CLM. Mitigations: N-adjacent spatial coherence on the
mask (`clm.spatial_coherence`), and glint-zone suppression gated on missing
CTTH/OCA corroboration (`clouds/contamination.py`). Both conservative:
genuine cloud, including thin cirrus, always survives.

## Honest ETAs

Deterministic cell-arrival skill is ~30-60 min, so any ETA beyond
`DETERMINISTIC_ETA_MAX_MIN` (30) is labelled "(probabilistic)" in both
`radar/verdict.py` and `docs/skala-text.js`.

## Running the tests

Standalone scripts, exit 0 = pass; node is needed for the JS parity tests:

```
for t in tests/test_*.py; do .venv/Scripts/python.exe "$t"; done
node tests/test_cloud_js.js
```
