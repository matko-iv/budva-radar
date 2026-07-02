# budva-radar (SKALA)

Radar and satellite monitoring for Budva, Montenegro. The pipeline answers
three questions and publishes them as static pages: is it raining here now
(SKALA RAIN), will it rain within the next two hours (SKALA NOWCAST), and is
the sky clear or clouded (SKALA CLOUD). Change `LOCATION` in `config.py` to
point it at another town.

## Data sources

| Source | Used for | Cadence |
|---|---|---|
| MeteoGate ORD — hrulj (Uljenje) ODIM HDF5 polar volumes | primary radar data: measured dBZ, RHOHV clutter filter, Doppler, ZDR | 5 min |
| DHMZ Uljenje PNG (`vrijeme.hr/uljenje-stat.png`) | display layer + fallback when ORD is down | 5-10 min |
| OPERA Odyssey composite (FMI CDN) | Europe-wide context | 5 min |
| HighSight visible tiles | cloud verdict + cloud map | 10 min |
| EUMETSAT MTG L2 (CLM / CTTH / OCA) | cloud retrievals; kept behind config flags while the OCA over-read is being fixed | ~10 min |

## How it works

The radar path decodes each frame to dBZ (raw values from ORD, colour
classification for the PNGs), samples concentric rings around the location,
extracts and tracks storm cells, and runs a probabilistic arrival nowcast:
each cell is advected over a deterministic speed x direction grid, weighted
by a survival model, and combined into P(rain) per lead bucket. Full-volume
products (VIL, echo top, ZDR columns) sharpen the growth/decay signal, and a
CPA classification separates cells that will hit from those that bypass.

SKALA NOWCAST runs DeepMind DGMR on a Budva-centred 256 x 256 / 1 km tile of
ORD frames; `verify_nowcast.py` scores it against LINDA and plain
extrapolation on archived cases (FSS / CSI vs lead time).

The cloud path reads the HighSight visible picture (cloud = bright, neutral
pixels), advects successive frames for a two-hour outlook, and derives a
sun/shade verdict. The EUMETSAT L2 path — CLM presence, OCA optical
thickness, CMF-based sun state — stays in the codebase behind config flags.

Each pipeline computes its verdict once, in Python, and every page renders
that same result; parity tests replay the JS interpreters against the Python
ports so wording can't drift.

## Running it

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python run.py            # radar: fetch + interpret + write outputs
python run_clouds.py     # clouds (needs HIGHSIGHT_KEY)
python compare_nowcast.py --ord-latest   # DGMR nowcast (needs the plugin)

python loop.py           # keep everything updated; each module waits for its
                         # own source to publish a new frame
```

Open `docs/index.html` for the radar status, `docs/radar-map.html` for the
map, `docs/cloud-map.html` for clouds, `docs/nowcast-compare.html` for the
DGMR nowcast.

Tests are standalone scripts (no pytest):

```powershell
.venv\Scripts\python.exe tests\test_volume.py
node tests\test_cloud_js.js
```

## Layout

```
config.py            location, sources, thresholds, cloud + R2 settings
radar/               fetch, calibration, colormap, sampling, motion, ord,
                     volume, interpret, verdict, pysteps/dgmr adapters
clouds/              highsight, fetch (EUMETSAT), clm, oca, solar, interpret,
                     nowcast, verdict
nowcast.py           probabilistic arrival nowcast + storm-mode classifier
tracking.py          cell extraction + frame-to-frame tracking
run.py / run_clouds.py / compare_nowcast.py / loop.py
verify_nowcast.py    model skill scoring on archived ORD cases
fetch_ord.py         pull ODIM volumes for a chosen time window
docs/                static pages + generated data files (GitHub Pages)
tests/               standalone test scripts (python / node)
```

## Publishing

Outputs go to Cloudflare R2 (`radar/r2_publish.py`; credentials via
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`), which the pages
fetch cache-busted, so updates appear in seconds. Without R2 credentials the
pipeline falls back to committing `docs/` and letting GitHub Pages serve it;
`.github/workflows/update.yml` runs the radar pipeline on a cron for that
mode.

## Calibration

The PNG sources have no published projection. `radar/calibration.py` fits an
affine transform through hand-measured city landmarks, with Budva as the
anchor: its verified pixel is exact and the fit supplies the geometry around
it. If DHMZ or FMI redesign their images, re-measure the landmarks. The ORD
grid needs none of this — its geometry comes from the ODIM metadata.

## Verification

Every radar run appends one row to `docs/skala_log_<year>.csv`. `radar/
verification.py` replays the log against what the radar later observed and
writes POD / FAR / CSI / HSS and Brier scores to
`docs/skala_verification.json`. Threshold changes (for example the
"approaching" gates in `config.py`) are tuned against this log, not by eye.

## Terms of use

DHMZ imagery, OPERA composites, EUMETSAT products, and HighSight tiles all
have their own terms. This project fetches politely (5+ min intervals,
quota-throttled tiles) for personal, non-commercial use; get permission from
the providers before redistributing.
