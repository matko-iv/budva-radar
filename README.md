# budva-radar

A program that **reads radar so you don't have to** — for Budva (or any
configurable location).

Pulls from two sources:

| Source | Coverage | Update | Format |
|---|---|---|---|
| **DHMZ Uljenje** (`https://vrijeme.hr/uljenje-stat.png`) | Adriatic / Croatia / Montenegro | ~5-10 min | PNG 720×751 |
| **OPERA Odyssey** (FMI CDN) | All of Europe | 5 min | GIF 950×1100, JSON listing |

## What it does

1. Downloads radar images every 5-10 min, caches them under `data/frames/`.
2. Maps pixels to lat/lon coordinates (affine calibration anchored on Budva).
3. Maps colors to precipitation intensity (RGB → dBZ → mm/h).
4. Samples **concentric rings** (10, 25, 50, 100, 150 km) around the location.
5. Detects **motion** by cross-correlating two consecutive frames.
6. Extrapolates: if rain is moving toward us at X km/h from Y km away,
   **ETA = Y / X**.
7. Writes output:
   - `output/status.json` (for machines)
   - `docs/data.js` (inline for the local HTML preview)

## Quickstart

```powershell
# One-time setup
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Single run (fetch + interpret + write outputs)
python run.py

# Background loop (every 5 min)
python loop.py
```

Open `docs/index.html` in a browser to see the status.

## Layout

```
budva-radar/
├── config.py                  # Location, source URLs, valid-area masks
├── radar/
│   ├── fetch.py               # Download + cache
│   ├── calibration.py         # Pixel <-> lat/lon (affine + anchor)
│   ├── colormap.py            # RGB -> dBZ -> mm/h (Marshall-Palmer)
│   ├── sampling.py            # Concentric ring sampling
│   ├── motion.py              # Frame-to-frame motion vector
│   └── interpret.py           # High-level summary
├── run.py                     # One full cycle
├── loop.py                    # Background loop
├── data/frames/{dhmz,opera}/  # Cached image frames
├── output/status.json         # Current interpretation
└── docs/                      # Static HTML / CSS preview
```

## Calibration

If the radar source changes layout (e.g. DHMZ redesigns the map), the
hardcoded pixel landmarks in `radar/calibration.py` need to be updated.
Budva is treated as the "anchor" — its user-verified pixel position is
preserved exactly, while the affine fit handles local geometry around it.

## GitHub Actions (automatic updates)

A workflow in `.github/workflows/update.yml` runs `run.py` every ~15 minutes
on GitHub's free-tier runner. It commits these outputs back to the repo:
- `docs/radar_status.json` (served via GitHub Pages, consumed by the main
  weather-forecast page)
- `docs/data.js` (used by `docs/index.html` for the local preview)
- `docs/latest_dhmz.png` and `docs/latest_opera.gif` (latest frames for the preview)
- `data/frames/{dhmz,opera}/...` (full motion history, kept up to KEEP_FRAMES)

### One-time setup

1. **Create the GitHub repo (public)**
   ```powershell
   cd c:\Users\Matija\budva-radar
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/matko-iv/budva-radar.git
   git push -u origin main
   ```

2. **Enable GitHub Pages**
   - Repo → Settings → Pages
   - Source: **Deploy from a branch**
   - Branch: **main**, Folder: **/docs**
   - Save. URL becomes `https://matko-iv.github.io/budva-radar/`

3. **The workflow runs automatically** every ~15 minutes (cron) once the repo
   is pushed. First run can be triggered manually under Actions tab.

### Integration with weather-forecast

The main XGBoost forecast page (`weather-forecast/docs/forecast.html`) fetches
radar status from this repo's GitHub Pages URL with a local-file fallback:

```javascript
const RADAR_STATUS_URLS = [
    'radar_status.json',                                    // local fallback
    'https://matko-iv.github.io/budva-radar/radar_status.json',  // GH Pages
];
```

GH Pages serves with `Access-Control-Allow-Origin: *`, so cross-origin fetch
works without any extra configuration.

## Disclaimer

DHMZ images are public radar imagery. Polite fetching (every 5+ min) follows
best practice. Don't use this for commercial / mass-distribution purposes
without permission from DHMZ (vrijeme.hr) or EUMETNET (OPERA).
