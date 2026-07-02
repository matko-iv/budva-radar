"""HighSight true-colour satellite tiles -> normalized CloudField + display image.

The interim SKALA CLOUD source while the L2/OCA verdict is being repaired:
read the visible satellite picture from HighSight's XYZ tiles the way SKALA
RAIN reads radar PNG frames. Cloud = bright + near-neutral (white) against
dark sea / green land; the cloud-fraction field drives the same advection
nowcast and per-point disc reads as the L2 path, so every clicked point reads
the picture rather than a phantom L2 retrieval.

Tiles:  GET https://api.highsight.dev/v1/satellite/{z}/{x}/{y}?date=YYYY/MM/DD/HHmm
        JPEG 512x512, Web-Mercator XYZ, 10-min cadence. All tiles are pinned
        to one explicit UTC slot so the mosaic is one coherent frame and
        sensing_time is that slot's true time; a request without `date`
        silently serves a ~30-min-old default and can mix frames across
        tiles. Auth: Bearer HIGHSIGHT_KEY from the environment (also a
        GitHub Actions secret).

The geometry / brightness helpers take plain numpy arrays so they test
without the network; only fetch_field touches HTTP.
"""

import datetime
import hashlib
import io
import math
import os
import urllib.error
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


# Cloud = bright and near-neutral (white/grey); sun-blocking cloud = very
# bright. Sea and land stay clear.
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


# Frame cache mirrors clouds/fetch.py so the nowcast has prev + curr frames.
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


def _ts_sha(sensing_time):
    """(timestamp, short-hash) used to name + dedup a cached frame. Robust to a
    trailing 'Z' on any Python (normalize before parsing)."""
    dt = datetime.datetime.fromisoformat(sensing_time.replace("Z", "+00:00"))
    return dt.strftime("%Y%m%d_%H%M%S"), hashlib.sha256(
        sensing_time.encode("utf-8")).hexdigest()[:12]


def _frame_paths(sensing_time):
    """(npz_path, png_path) for a cached frame of this sensing_time."""
    ts, sha = _ts_sha(sensing_time)
    base = _frame_dir() / f"{ts}_{sha}"
    return base.with_suffix(".npz"), base.with_suffix(".png")


def save_frame(field, sensing_time, rgb_image=None):
    """Persist a field frame (npz) + optional display RGB (png). Deduped by
    sensing_time so re-runs in the same 10-min slot don't pile up frames."""
    npz, png = _frame_paths(sensing_time)
    if npz.exists():
        return {"fetched": False, "reason": "no_change", "sensing_time": sensing_time}
    field.save(npz)
    if rgb_image is not None:
        try:
            rgb_image.save(png)
        except Exception:
            pass
    _prune(int((config.CLOUDS or {}).get("keep_frames", 12)))
    return {"fetched": True, "path": str(npz), "sensing_time": sensing_time}


def _load_cached(sensing_time):
    """Return (field, rgb_PIL_or_None) for an already-cached slot, else None — so
    fetch_field can skip the tile download when we already hold this slot. The
    HighSight free tier is tile-quota-limited, so re-downloading a slot we already
    have would burn quota for nothing."""
    npz, png = _frame_paths(sensing_time)
    if not npz.exists():
        return None
    field = CloudField.load(npz)
    rgb = None
    if png.exists():
        try:
            from PIL import Image
            rgb = Image.open(png)
        except Exception:
            rgb = None
    return field, rgb


def _parse_iso(s):
    """ISO sensing_time (with or without trailing 'Z') -> naive-UTC datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _within_interval(nominal, last, min_interval_min):
    """True when a new slot should be skipped (cache reused) because less than
    min_interval_min has elapsed since the last download — the tile-quota
    throttle. 0 or no last frame: never skip."""
    if not min_interval_min or last is None:
        return False
    return (nominal - last) < datetime.timedelta(minutes=float(min_interval_min))


def _newest_cached():
    """(field, rgb_PIL_or_None, sensing_time_iso) of the newest cached frame, or
    None. Used by the throttle to reuse the latest frame between downloads."""
    frames = sorted(_frame_dir().glob("*.npz"))
    if not frames:
        return None
    npz = frames[-1]
    field = CloudField.load(npz)
    st = field.meta.get("sensing_time")
    if not st:                                   # fall back to the filename slot
        try:
            ts = "_".join(npz.stem.split("_")[:2])
            st = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S").isoformat() + "Z"
        except Exception:
            st = None
    rgb = None
    png = npz.with_suffix(".png")
    if png.exists():
        try:
            from PIL import Image
            rgb = Image.open(png)
        except Exception:
            rgb = None
    return field, rgb, st


def latest_two_fields():
    """(prev, curr) CloudFields for the motion estimate, or (None, curr)/(None,
    None) when too few frames are cached yet."""
    frames = sorted(_frame_dir().glob("*.npz"))
    if not frames:
        return None, None
    curr = CloudField.load(frames[-1])
    prev = CloudField.load(frames[-2]) if len(frames) >= 2 else None
    return prev, curr


def _api_key(cfg):
    """HighSight key, in order: env HIGHSIGHT_KEY -> gitignored local file
    (highsight_key.txt / .highsight_key at repo root) -> cfg. Never hardcoded so
    it isn't committed to the public repo."""
    env = cfg.get("highsight_key_env", "HIGHSIGHT_KEY")
    key = os.environ.get(env)
    if key:
        return key.strip()
    for name in ("highsight_key.txt", ".highsight_key"):
        p = BASE_DIR / name
        if p.exists():
            try:
                k = p.read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass
    return (cfg.get("highsight_key") or "").strip()


def _fetch_tile(z, x, y, key, date=None, timeout=30):
    """Fetch one JPEG tile -> (TILE_PX, TILE_PX, 3) uint8. The `date` query (UTC
    YYYY/MM/DD/HHmm) pins the frame so the whole mosaic is coherent. Raises
    urllib.error.HTTPError on an HTTP error (e.g. 400 for a too-recent slot), which
    `_resolve_slot` uses to step back to an available slot."""
    from PIL import Image
    url = f"{_BASE_URL}/{z}/{x}/{y}"
    if date:
        url += f"?date={date}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "User-Agent": "budva-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(img, dtype="uint8")


def _freshest_slot(cfg=None):
    """The freshest slot we can reliably request: now (UTC) minus the publish
    lag, floored to the 10-min cadence. Imagery runs up to ~20 min behind
    real-time and fresher requests may fail or serve older tiles, so pin a
    known slot and report its true time — never 'now'."""
    cfg = cfg or config.CLOUDS
    lag = int(cfg.get("highsight_lag_min", 30))
    t = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
         - datetime.timedelta(minutes=lag))
    return t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0)


def _date_param(slot):
    """HighSight `date` query value: UTC YYYY/MM/DD/HHmm (10-min cadence)."""
    return slot.strftime("%Y/%m/%d/%H%M")


def _slot_iso(slot):
    """Canonical sensing_time for a slot: UTC ISO with a 'Z', so the page reads the
    age in UTC (not local) and the L2 / HighSight time formats stay uniform."""
    return slot.replace(second=0, microsecond=0).isoformat() + "Z"


def _resolve_slot(z, x, y, key, slot, cfg=None):
    """Find the freshest slot whose tile actually exists, stepping back from `slot`
    in 10-min steps (HighSight occasionally hasn't published the newest slot yet).
    Returns (slot_datetime, first_tile_array); the fetched tile is reused so this
    costs no extra quota."""
    steps = int((cfg or config.CLOUDS).get("highsight_max_lookback_slots", 3))
    for _ in range(steps + 1):
        try:
            return slot, _fetch_tile(z, x, y, key, date=_date_param(slot))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 416):            # too recent / not available
                slot = slot - datetime.timedelta(minutes=10)
                continue
            raise
    raise RuntimeError(
        f"HighSight: no frame available in the lookback window (back to "
        f"{_date_param(slot)} UTC)")


def fetch_field(cfg=None, *, key=None, zoom=None, display_width=None):
    """Download the HighSight tiles for the bbox, stitch + reproject to the
    regular lat/lon grid, and return (field, display_rgb_PIL, sensing_time).

    `field` is a normalized CloudField (presence/opaque from brightness);
    `display_rgb_PIL` is a north-up plate-carree picture for the map. All tiles are
    pinned to ONE resolved UTC slot, and that slot's true time is returned as
    sensing_time. Raises on a missing key or a failed download (run_clouds catches
    and falls back)."""
    cfg = cfg or config.CLOUDS
    z = int(zoom or cfg.get("highsight_zoom", 7))

    # Quota guard: if we already hold the freshest slot, reuse it (no download).
    nominal = _freshest_slot(cfg)
    cached = _load_cached(_slot_iso(nominal))
    if cached is not None:
        return cached[0], cached[1], _slot_iso(nominal)

    # Quota throttle: only download a NEW slot every highsight_min_interval_min;
    # between downloads reuse the newest cached frame (its honest age just grows).
    # This is what keeps the monthly tile count under the HighSight free quota.
    min_interval = float(cfg.get("highsight_min_interval_min", 0) or 0)
    if min_interval > 0:
        newest = _newest_cached()
        if newest is not None and _within_interval(nominal, _parse_iso(newest[2]), min_interval):
            return newest

    key = key or _api_key(cfg)
    if not key:
        raise RuntimeError(
            "HighSight API key missing — set HIGHSIGHT_KEY env var, or put the key "
            "in highsight_key.txt at the repo root (gitignored).")
    from PIL import Image

    bbox = cfg["bbox"]
    x0, x1, y0, y1 = tile_range(bbox, z)
    # Resolve to a slot that actually exists; the returned NW tile is reused below.
    slot, first_tile = _resolve_slot(z, x0, y0, key, nominal, cfg)
    sensing_time = _slot_iso(slot)
    cached = _load_cached(sensing_time)          # resolution may land on a cached slot
    if cached is not None:
        return cached[0], cached[1], sensing_time

    date = _date_param(slot)
    nx, ny = (x1 - x0 + 1), (y1 - y0 + 1)
    mosaic = np.zeros((ny * TILE_PX, nx * TILE_PX, 3), dtype="uint8")
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = (first_tile if (tx == x0 and ty == y0)
                    else _fetch_tile(z, tx, ty, key, date=date))
            r = (ty - y0) * TILE_PX
            c = (tx - x0) * TILE_PX
            mosaic[r:r + TILE_PX, c:c + TILE_PX] = tile
    origin_px = (x0 * TILE_PX, y0 * TILE_PX)

    # Frac grid on the analysis grid (drives nowcast + per-point reads).
    lats, lons = target_grid(cfg)
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
