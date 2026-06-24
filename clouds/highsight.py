"""HighSight true-colour satellite tiles -> normalized CloudField + display image.

The SKALA CLOUD interim source (while the L2/OCA verdict is being repaired): read
the actual visible satellite PICTURE from HighSight's XYZ tiles, exactly like
SKALA RAIN reads radar PNG frames. Cloud = bright + near-neutral (white) against
the dark sea / green land; the resulting cloud-fraction field drives the SAME
advection nowcast (clouds/motion.py + clouds/nowcast.py) and the same per-point
disc reads as the L2 path — so every clicked point reads the picture, not a
phantom L2 retrieval (the "only Budva is right" bug).

Tiles:  GET https://api.highsight.dev/v1/satellite/{z}/{x}/{y}
        JPEG 512x512, Web-Mercator XYZ, ~10 min cadence (latest frame only).
        Auth: `Authorization: Bearer <HIGHSIGHT_KEY>` (key from env, never
        hardcoded). Set HIGHSIGHT_KEY locally and as a GitHub Actions secret.

The pure geometry / brightness helpers take plain numpy arrays so they are
unit-testable without the network; only `fetch_field` touches HTTP.
"""

import datetime
import hashlib
import io
import math
import os
import urllib.request
from pathlib import Path

import numpy as np

import config
from clouds.fetch import target_grid
from clouds.grid import CloudField

BASE_DIR = Path(__file__).resolve().parent.parent
FRAMES_DIR = BASE_DIR / "data" / "highsight_frames"
TILE_PX = 512
_BASE_URL = "https://api.highsight.dev/v1/satellite"


# --------------------------------------------------------------------------
# Web-Mercator tile geometry (pure)
# --------------------------------------------------------------------------
def merc_norm(lat, lon):
    """(lon,lat) deg -> normalized Web-Mercator (x, y) in [0,1] (y grows south).
    Vectorized: accepts scalars or numpy arrays."""
    lat = np.asarray(lat, dtype="float64")
    lon = np.asarray(lon, dtype="float64")
    x = (lon + 180.0) / 360.0
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0
    return x, y


def tile_index(lat, lon, z):
    """Integer (x, y) XYZ tile containing (lat, lon) at zoom z."""
    n = 2 ** z
    x, y = merc_norm(lat, lon)
    return int(x * n), int(y * n)


def tile_range(bbox, z):
    """Inclusive (x_min, x_max, y_min, y_max) tile range covering bbox at zoom z."""
    x0, _ = tile_index(bbox["lat_max"], bbox["lon_min"], z)   # west
    x1, _ = tile_index(bbox["lat_min"], bbox["lon_max"], z)   # east
    _, y0 = tile_index(bbox["lat_max"], bbox["lon_min"], z)   # north (smaller y)
    _, y1 = tile_index(bbox["lat_min"], bbox["lon_max"], z)   # south (larger y)
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)


# --------------------------------------------------------------------------
# Reprojection mosaic(Web-Mercator) -> regular lat/lon (plate carree) (pure)
# --------------------------------------------------------------------------
def reproject(mosaic, origin_px, z, lats, lons):
    """Sample a Web-Mercator `mosaic` (HxWx3, top-left at `origin_px`=(px,py) in
    world pixels at zoom z) onto a regular lat/lon grid. `lats` descending (north
    up), `lons` ascending. Returns an (len(lats), len(lons), 3) uint8 array."""
    world_px = TILE_PX * (2 ** z)
    LON, LAT = np.meshgrid(np.asarray(lons, "float64"), np.asarray(lats, "float64"))
    xn, yn = merc_norm(LAT, LON)
    gx = np.round(xn * world_px - origin_px[0]).astype(int)
    gy = np.round(yn * world_px - origin_px[1]).astype(int)
    gx = np.clip(gx, 0, mosaic.shape[1] - 1)
    gy = np.clip(gy, 0, mosaic.shape[0] - 1)
    return mosaic[gy, gx]


# --------------------------------------------------------------------------
# Brightness -> cloud (pure). Cloud = bright AND near-neutral (white/grey);
# optically-thick (sun-blocking) cloud = very bright. Sea/land stay clear.
# --------------------------------------------------------------------------
def cloud_fields(rgb, cfg=None):
    """(H,W,3) RGB -> (cloud, thick) float arrays in {0.,1.}. Mirrors
    clouds/visible._cloud_masks so the picture is read the same way everywhere."""
    cfg = cfg or config.CLOUDS
    a = np.asarray(rgb, dtype="float64")
    mx = a.max(axis=-1)
    sat = mx - a.min(axis=-1)
    bright_min = cfg.get("highsight_bright_min", cfg.get("geocolour_bright_min", 150))
    sat_max = cfg.get("highsight_sat_max", cfg.get("geocolour_sat_max", 40))
    thick_min = cfg.get("highsight_thick_min", cfg.get("geocolour_thick_min", 205))
    cloud = ((mx >= bright_min) & (sat <= sat_max)).astype("float64")
    thick = ((mx >= thick_min) & (sat <= sat_max)).astype("float64")
    return cloud, thick


def build_field(rgb_grid, lats, lons, sensing_time, cfg=None):
    """Build a normalized CloudField from a plate-carree RGB grid on (lats, lons).
    Only the picture-derived axes are populated (presence/opaque from brightness);
    cot/cth/ctt/phase are NaN — a picture carries no optical thickness or height."""
    cfg = cfg or config.CLOUDS
    cloud, thick = cloud_fields(rgb_grid, cfg)
    nan = np.full(cloud.shape, np.nan)
    return CloudField(lats, lons, {
        "mask": cloud, "frac": cloud, "opaque": thick,
        "ctt": nan, "cth": nan, "cot": nan, "phase": nan,
    }, meta={"sensing_time": sensing_time, "source": "HighSight"})


# --------------------------------------------------------------------------
# Frame cache (mirrors clouds/fetch.py so the nowcast has prev + curr frames)
# --------------------------------------------------------------------------
def _frame_dir():
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    return FRAMES_DIR


def _prune(keep):
    frames = sorted(_frame_dir().glob("*.npz"))
    for old in frames[:-keep] if len(frames) > keep else []:
        for p in (old, old.with_suffix(".png")):
            try:
                p.unlink()
            except Exception:
                pass


def save_frame(field, sensing_time, rgb_image=None):
    """Persist a field frame (npz) + optional display RGB (png). Deduped by
    sensing_time so re-runs in the same ~10-min slot don't pile up frames."""
    ts = datetime.datetime.fromisoformat(sensing_time).strftime("%Y%m%d_%H%M%S")
    sha = hashlib.sha256(sensing_time.encode("utf-8")).hexdigest()[:12]
    existing = {p.stem.split("_")[-1] for p in _frame_dir().glob("*.npz")}
    if sha in existing:
        return {"fetched": False, "reason": "no_change", "sensing_time": sensing_time}
    base = _frame_dir() / f"{ts}_{sha}"
    field.save(base.with_suffix(".npz"))
    if rgb_image is not None:
        try:
            rgb_image.save(base.with_suffix(".png"))
        except Exception:
            pass
    _prune(int((config.CLOUDS or {}).get("keep_frames", 12)))
    return {"fetched": True, "path": str(base.with_suffix(".npz")),
            "sensing_time": sensing_time}


def latest_two_fields():
    """(prev, curr) CloudFields for the motion estimate, or (None, curr)/(None,
    None) when too few frames are cached yet."""
    frames = sorted(_frame_dir().glob("*.npz"))
    if not frames:
        return None, None
    curr = CloudField.load(frames[-1])
    prev = CloudField.load(frames[-2]) if len(frames) >= 2 else None
    return prev, curr


# --------------------------------------------------------------------------
# Live fetch (network)
# --------------------------------------------------------------------------
def _api_key(cfg):
    env = cfg.get("highsight_key_env", "HIGHSIGHT_KEY")
    return os.environ.get(env) or cfg.get("highsight_key") or ""


def _fetch_tile(z, x, y, key, timeout=30):
    """Fetch one JPEG tile -> (TILE_PX, TILE_PX, 3) uint8. None on failure."""
    from PIL import Image
    url = f"{_BASE_URL}/{z}/{x}/{y}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "User-Agent": "budva-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(img, dtype="uint8")


def _sensing_time():
    """HighSight serves the latest frame on a ~10-min cadence; floor 'now' (UTC)
    to a 10-min slot so re-runs in the same slot dedup and successive slots make
    distinct nowcast frames."""
    t = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    t = t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0)
    return t.isoformat()


def fetch_field(cfg=None, *, key=None, zoom=None, display_width=None):
    """Download the HighSight tiles for the bbox, stitch + reproject to the
    regular lat/lon grid, and return (field, display_rgb_PIL, sensing_time).

    `field` is a normalized CloudField (presence/opaque from brightness);
    `display_rgb_PIL` is a north-up plate-carree picture for the map. Raises on a
    missing key or a failed download (run_clouds catches and falls back)."""
    cfg = cfg or config.CLOUDS
    key = key or _api_key(cfg)
    if not key:
        raise RuntimeError("HighSight API key missing — set HIGHSIGHT_KEY")
    from PIL import Image

    z = int(zoom or cfg.get("highsight_zoom", 7))
    bbox = cfg["bbox"]
    x0, x1, y0, y1 = tile_range(bbox, z)
    nx, ny = (x1 - x0 + 1), (y1 - y0 + 1)
    mosaic = np.zeros((ny * TILE_PX, nx * TILE_PX, 3), dtype="uint8")
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = _fetch_tile(z, tx, ty, key)
            r = (ty - y0) * TILE_PX
            c = (tx - x0) * TILE_PX
            mosaic[r:r + TILE_PX, c:c + TILE_PX] = tile
    origin_px = (x0 * TILE_PX, y0 * TILE_PX)

    # Frac grid on the analysis grid (drives nowcast + per-point reads).
    lats, lons = target_grid(cfg)
    sensing_time = _sensing_time()
    rgb_analysis = reproject(mosaic, origin_px, z, lats, lons)
    field = build_field(rgb_analysis, lats, lons, sensing_time, cfg)

    # Higher-res plate-carree picture for the map (degrees map linearly).
    Wd = int(display_width or cfg.get("highsight_display_width", 1000))
    span_lat = bbox["lat_max"] - bbox["lat_min"]
    span_lon = bbox["lon_max"] - bbox["lon_min"]
    Hd = max(1, round(Wd * span_lat / span_lon))
    d_lats = np.linspace(bbox["lat_max"], bbox["lat_min"], Hd)
    d_lons = np.linspace(bbox["lon_min"], bbox["lon_max"], Wd)
    disp = reproject(mosaic, origin_px, z, d_lats, d_lons)
    display_rgb = Image.fromarray(disp, "RGB")
    return field, display_rgb, sensing_time
