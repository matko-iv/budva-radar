# docs/

Static pages served by GitHub Pages, plus the data files the pipelines
generate for them. See the repository [README](../README.md) for how the
pipelines work and how to run them.

## Pages

| Page | Shows |
|---|---|
| `index.html` | SKALA RAIN status: verdict, ring table, per-source detail |
| `radar-map.html` | radar map with cells, rings, and a per-point nowcast on click |
| `cloud-map.html` | SKALA CLOUD: satellite picture, verdict, 2 h history loop |
| `nowcast-compare.html` | SKALA NOWCAST: DGMR forecast frames + skill table |
| `nowcast.html` | pysteps (ANVIL/LINDA) nowcast page |
| `nowcast-compare.legacy.html`, `nowcastcompare.html` | older comparison layouts |

## Generated files — do not edit

`data.js`, `cloud_data.js`, `nowcast_data.js`, `compare_data.js`,
`verify_data.js`, `radar_status.json`, `cloud_status.json`,
`nowcast_status.json`, `compare.json`, `latest_*.png/gif`,
`cloud_history/`, `compare_frames/`, `nowcast_frames/`, and
`skala_log_*.csv` are all written by the pipelines (`run.py`,
`run_clouds.py`, `run_nowcast.py`, `compare_nowcast.py`,
`verify_nowcast.py`).

The hand-written sources are the HTML pages, `style.css`, and the JS
interpreters (`skala-*.js`, `cloud-*.js`, `nowcast-browser.js`,
`cloud-nowcast-browser.js`, `skala-cells-viz.js`, `skala-r2.js`). The
`skala-text.js` / `cloud-text.js` interpreters are kept in lockstep with
their Python ports (`radar/verdict.py`, `clouds/verdict.py`) by parity tests.
