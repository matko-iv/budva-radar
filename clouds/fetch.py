"""Download the latest EUMETSAT cloud products and normalize them to the
CloudField format (clouds/grid.py). This is the ONLY module that knows about
EUMETSAT product specifics — everything downstream is product-agnostic.

The collection ids in config.CLOUDS["collections"] and the variable names in
_VARMAP MUST be confirmed against the live catalogue with `clouds/discover.py`
before the first live run. eumdac / xarray are imported lazily so the rest of
the package (and the tests) import without them installed.

Cache layout mirrors the radar one: data/cloud_frames/YYYYMMDD_HHMMSS_<hash>.npz
(normalized field) + a matching .png preview; the last KEEP_FRAMES are kept.
"""

import datetime
import hashlib
import tempfile
from pathlib import Path

import numpy as np

import config
from clouds import render
from clouds.grid import CloudField

BASE_DIR = Path(__file__).resolve().parent.parent
FRAMES_DIR = BASE_DIR / "data" / "cloud_frames"

# Variable names per normalized layer, confirmed against the live MTG L2 products
# via clouds/discover.py (2026-06-19): CLM 0678, CTTH 0681, OCA 0684.
_VARMAP = {
    "mask":  ["cloud_state", "cloud_mask", "cma", "clm"],            # CLM
    "frac":  ["effective_cloudiness", "cloud_fraction"],            # CTTH (0..1)
    "ctt":   ["cloud_top_temperature", "retrieved_cloud_top_temperature", "ctt"],
    "cth":   ["cloud_top_height", "retrieved_cloud_top_height", "cth", "height"],
    "ctp":   ["cloud_top_pressure", "retrieved_cloud_top_pressure", "ctp"],
    "cot":   ["retrieved_cloud_optical_thickness", "cloud_optical_thickness", "cot"],
    "phase": ["retrieved_cloud_phase", "cloud_phase", "phase", "cph"],
}


# --------------------------------------------------------------------------
# Cache helpers (no eumdac needed)
# --------------------------------------------------------------------------
def _frame_dir() -> Path:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    return FRAMES_DIR


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _prune(keep: int) -> None:
    d = _frame_dir()
    frames = sorted(d.glob("*.npz"))
    for old in frames[:-keep] if len(frames) > keep else []:
        for p in (old, old.with_suffix(".png")):
            try:
                p.unlink()
            except Exception:
                pass


def list_cached_frames() -> list:
    return sorted(_frame_dir().glob("*.npz"))


def latest_field():
    frames = list_cached_frames()
    return CloudField.load(frames[-1]) if frames else None


def latest_two_fields():
    """(prev, curr) for motion; (None, curr) or (None, None) if too few."""
    frames = list_cached_frames()
    if not frames:
        return None, None
    curr = CloudField.load(frames[-1])
    prev = CloudField.load(frames[-2]) if len(frames) >= 2 else None
    return prev, curr


def save_field(field: CloudField, sensing_time: str) -> dict:
    """Persist a normalized field (npz + png) using the radar filename idiom."""
    ts = datetime.datetime.fromisoformat(sensing_time).strftime("%Y%m%d_%H%M%S")
    sha = _hash_str(sensing_time)
    existing = {p.stem.split("_")[-1] for p in _frame_dir().glob("*.npz")}
    if sha in existing:
        return {"fetched": False, "reason": "no_change", "sensing_time": sensing_time}
    base = _frame_dir() / f"{ts}_{sha}"
    field.save(base.with_suffix(".npz"))
    render.to_png(field, base.with_suffix(".png"))
    _prune(config.CLOUDS["keep_frames"])
    return {"fetched": True, "path": str(base.with_suffix(".npz")),
            "sensing_time": sensing_time}


# --------------------------------------------------------------------------
# Grid + normalization
# --------------------------------------------------------------------------
def target_grid(cfg):
    b, step = cfg["bbox"], cfg["grid_step_deg"]
    lats = np.arange(b["lat_max"], b["lat_min"] - 1e-9, -step)   # north-up
    lons = np.arange(b["lon_min"], b["lon_max"] + 1e-9, step)
    return lats, lons


def _pick(ds, key):
    for name in _VARMAP[key]:
        if name in ds.variables:
            return ds[name]
    return None


def _grid_mapping(ds):
    for name in ("mtg_geos_projection", "geostationary", "projection", "mtg_geos"):
        if name in ds.variables:
            return ds[name]
    for v in ds.data_vars:                      # else follow a data var's pointer
        gm = ds[v].attrs.get("grid_mapping")
        if gm and gm in ds.variables:
            return ds[gm]
    return None


def _geos_indices(ds, lats, lons):
    """Map the regular target (lats, lons) onto nearest (row, col) indices of the
    product's GEOSTATIONARY grid via pyproj. The MTG L2 products carry x/y in the
    geos projection (no lat/lon arrays), so we invert: lon/lat -> geos x/y ->
    integer index on the regular x/y axes. Returns (i, j, valid)."""
    import pyproj
    gm = _grid_mapping(ds)
    if gm is None:
        raise ValueError("no geostationary grid_mapping variable found (check product)")
    crs = pyproj.CRS.from_cf(dict(gm.attrs))
    x = np.asarray(ds["x"].values, dtype="float64")
    y = np.asarray(ds["y"].values, dtype="float64")
    h = gm.attrs.get("perspective_point_height")
    if h and np.nanmax(np.abs(x)) < 1.5:        # x/y in radians (scan angle) -> metres
        x = x * float(h); y = y * float(h)
    tf = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lon2d, lat2d = np.meshgrid(lons, lats)
    X, Y = tf.transform(lon2d.ravel(), lat2d.ravel())
    X = np.asarray(X, dtype="float64").reshape(lat2d.shape)
    Y = np.asarray(Y, dtype="float64").reshape(lat2d.shape)
    ny, nx = len(y), len(x)
    dx = (x[-1] - x[0]) / (nx - 1)
    dy = (y[-1] - y[0]) / (ny - 1)
    j = np.round((X - x[0]) / dx)
    i = np.round((Y - y[0]) / dy)
    valid = (np.isfinite(X) & np.isfinite(Y)
             & (i >= 0) & (i < ny) & (j >= 0) & (j < nx))
    # *** Row order: the data array rows are stored N->S, OPPOSITE to the
    # ascending y coordinate (verified 8/8 against EUMETView via debug_flip).
    # So flip the row index into the data array. Columns (x) are already aligned. ***
    i = (ny - 1) - i
    return np.where(valid, i, 0).astype(int), np.where(valid, j, 0).astype(int), valid


def _sample(da, idx):
    """Pull a DataArray onto the target grid using geos indices; NaN off-disk."""
    i, j, valid = idx
    arr = np.asarray(da.values, dtype="float64")
    out = np.full(i.shape, np.nan)
    out[valid] = arr[i[valid], j[valid]]
    return out


def _sample_cot(da, idx):
    """OCA optical thickness: 3D (rows, cols, layers) and stored as log10(COT).
    Take the upper layer (0) and de-log if the metadata says so."""
    arr = np.asarray(da.values, dtype="float64")
    if arr.ndim == 3:
        arr = arr[..., 0]
    i, j, valid = idx
    out = np.full(i.shape, np.nan)
    out[valid] = arr[i[valid], j[valid]]
    if "log10" in str(da.attrs.get("long_name", "")).lower():
        out = np.power(10.0, out)
    return out


def _pressure_to_height_m(p_pa):
    """Crude standard-atmosphere pressure(Pa) -> geopotential height(m)."""
    p = np.asarray(p_pa, dtype="float64")
    return 44330.0 * (1.0 - np.power(np.clip(p, 1.0, 1.1e5) / 101325.0, 0.190284))


def normalize(ds_clm, ds_ctth, ds_oca, cfg, sensing_time):
    """Build a CloudField on the target grid from the MTG cloud products:
    CLM (mask), CTTH (cloud-top temp + height), OCA (optical thickness + phase).
    Variable mapping uses _VARMAP — confirm names via discover.py."""
    lats, lons = target_grid(cfg)
    H, W = len(lats), len(lons)
    nan = np.full((H, W), np.nan)

    clm = _pick(ds_clm, "mask")
    if clm is None:
        raise ValueError("no cloud-mask variable found in CLM product (check _VARMAP)")
    idx_clm = _geos_indices(ds_clm, lats, lons)
    cs = np.round(_sample(clm, idx_clm))        # CLM cloud_state codes (see enum)

    # CLM cloud_state enum (MTG FCI L2 CLM):
    #   0 no-data, 1 cloud-free, 2 cloud CONTAMINATED (partial/semitransparent),
    #   3 cloud FILLED (opaque), 4-7 dust/ash, 8 snow/ice, 9 undefined.
    # NOTE: code 3 ("opaque") is NOT trustworthy on its own — the FCI CLM marks
    # optically thin cloud as code 3 too. We keep the codes here but let OCA
    # optical thickness gate what really counts as cloud / sun-blocking (below).
    nodata = np.isnan(cs) | (cs == 0) | (cs == 9)
    clearish = (cs == 1) | ((cs >= 4) & (cs <= 8))     # clear / dust / ash / snow
    semi = (cs == 2)
    opaque_px = (cs == 3)
    cloud_any = semi | opaque_px

    ctt = cth = cot = phase = nan

    # --- OCA optical thickness (COT) + phase, sampled FIRST ----------------
    # The CLM mask over-detects (flags optically thin cloud as code-3 "opaque"),
    # so COT — not the CLM flag — decides what actually blocks the sun. Sampled
    # up front so the frac/opaque layers below can be gated on it.
    if ds_oca is not None:
        idx_oca = _geos_indices(ds_oca, lats, lons)
        vcot = _pick(ds_oca, "cot")
        if vcot is not None:
            cot = _sample_cot(vcot, idx_oca)
        vph = _pick(ds_oca, "phase")
        if vph is not None:
            phase = _sample(vph, idx_oca)
    cot_ok = (cot is not nan) and bool(np.isfinite(cot).any())

    # Cloud amount from CTTH effective_cloudiness (0..1); fall back per category.
    idx_ctth = _geos_indices(ds_ctth, lats, lons) if ds_ctth is not None else None
    eff = None
    if ds_ctth is not None:
        vfrac = _pick(ds_ctth, "frac")
        if vfrac is not None:
            eff = _sample(vfrac, idx_ctth)
    if eff is not None:
        fin = eff[~np.isnan(eff)]
        if fin.size and np.nanmax(fin) > 1.5:   # percent -> fraction
            eff = eff / 100.0
        default_amt = np.where(opaque_px, 1.0, np.where(semi, 0.5, 0.0))
        amt = np.where(np.isnan(eff), default_amt, np.clip(eff, 0.0, 1.0))
    else:
        amt = np.where(opaque_px, 1.0, np.where(semi, 0.5, 0.0))

    # Optical-thickness gating. When OCA COT is available, a pixel is
    #   * SUN-BLOCKING (opaque layer) only if COT >= cot_block_min, and
    #   * counted as cloud at all (frac) only if COT >= cot_cloud_min,
    # so the CLM's optically thin false "opaque" detections drop out and the
    # field matches the visible sky. With no OCA we keep the raw CLM flags.
    cot_block = float(cfg.get("cot_block_min", 5.0))
    cot_cloud = float(cfg.get("cot_cloud_min", 1.0))
    if cot_ok:
        cot_g = np.where(np.isnan(cot), 0.0, cot)   # no retrieval -> treat as thin
        # COT is the arbiter, NOT the CLM 2/3 split: a pixel blocks the sun if any
        # cloud is present AND it is optically thick (so thick cloud the CLM mis-
        # labelled "contaminated" still counts, and thin "opaque" no longer does).
        block_px = cloud_any & (cot_g >= cot_block)
        cloud_keep = cloud_any & (cot_g >= cot_cloud)
    else:
        block_px = opaque_px           # no OCA -> fall back to the raw CLM flag
        cloud_keep = cloud_any

    frac = np.where(nodata, np.nan, np.where(cloud_keep, amt, 0.0))   # any (real) cloud
    opaque = np.where(nodata, np.nan, np.where(block_px, amt, 0.0))   # sun-blocking only
    mask = np.where(nodata, np.nan, cloud_keep.astype(float))

    if ds_ctth is not None:
        v = _pick(ds_ctth, "ctt")
        if v is not None:
            ctt = _sample(v, idx_ctth)
        vcth = _pick(ds_ctth, "cth")
        if vcth is not None:
            cth = _sample(vcth, idx_ctth)
        else:
            vctp = _pick(ds_ctth, "ctp")
            if vctp is not None:
                cth = _pressure_to_height_m(_sample(vctp, idx_ctth))

    # Mask cloud-only layers to where there is cloud.
    cloudy = mask >= 0.5
    for arr in (ctt, cth, cot, phase):
        if arr is not nan:
            arr[~cloudy] = np.nan

    return CloudField(lats, lons,
                      {"mask": mask, "frac": frac, "opaque": opaque, "ctt": ctt,
                       "cth": cth, "cot": cot, "phase": phase},
                      meta={"sensing_time": sensing_time, "source": "EUMETSAT"})


# --------------------------------------------------------------------------
# Live fetch (needs eumdac + xarray + credentials)
# --------------------------------------------------------------------------
def latest_product(col, hours=6):
    """Newest product in a collection, searching only a recent time window so we
    don't enumerate the whole (huge) catalogue. Widens the window if empty."""
    end = datetime.datetime.utcnow()
    for win_h in (hours, 24, 72):
        start = end - datetime.timedelta(hours=win_h)
        prods = list(col.search(dtstart=start, dtend=end))
        if prods:
            prods.sort(key=lambda p: str(getattr(p, "sensing_start", "") or p), reverse=True)
            return prods[0]
    return None


def _search_latest(store, collection_id):
    return latest_product(store.get_collection(collection_id))


def _download_nc(product, dest_dir):
    """Download ONLY the netCDF entry of the product; return its local path."""
    import os
    entries = list(product.entries or [str(product)])
    nc_entry = next((e for e in entries if str(e).endswith((".nc", ".nc4"))), None)
    if nc_entry is None:
        return None
    local = os.path.join(dest_dir, os.path.basename(str(nc_entry)))
    try:
        with product.open(entry=str(nc_entry)) as fsrc, open(local, "wb") as fdst:
            fdst.write(fsrc.read())
    except TypeError:                            # some products: open() w/o entry
        with product.open() as fsrc, open(local, "wb") as fdst:
            fdst.write(fsrc.read())
    return local


def _sensing_time(product):
    for attr in ("sensing_start", "sensing_end"):
        t = getattr(product, attr, None)
        if t is not None:
            return (t if isinstance(t, str) else t.isoformat())
    return datetime.datetime.now().isoformat(timespec="seconds")


def fetch_latest(cfg=None) -> dict:
    """Download + normalize the latest cloud frame. Raises on failure (so
    run_clouds.py can report it). Returns a metadata dict."""
    cfg = cfg or config.CLOUDS
    cols = cfg["collections"]
    if not cols.get("clm"):
        raise RuntimeError(
            "config.CLOUDS['collections']['clm'] is unset — run "
            "`python -m clouds.discover` to pin the EUMETSAT collection ids first.")

    import xarray as xr
    import eumdac
    from clouds.discover import get_token

    store = eumdac.DataStore(get_token())
    clm_product = _search_latest(store, cols["clm"])
    if clm_product is None:
        raise RuntimeError(f"no products in CLM collection {cols['clm']}")
    sensing = _sensing_time(clm_product)

    def _open(col_id, tmp):
        if not col_id:
            return None
        p = _search_latest(store, col_id)
        if p is None:
            return None
        nc = _download_nc(p, tmp)
        return xr.open_dataset(nc) if nc else None

    with tempfile.TemporaryDirectory() as d:
        clm_nc = _download_nc(clm_product, d)
        ds_clm = xr.open_dataset(clm_nc)
        ds_ctth = _open(cols.get("ctth"), d)
        ds_oca = _open(cols.get("oca"), d)
        try:
            field = normalize(ds_clm, ds_ctth, ds_oca, cfg, sensing)
        finally:
            for ds in (ds_clm, ds_ctth, ds_oca):  # release file handles before
                if ds is not None:                # the temp dir is removed (Windows)
                    try:
                        ds.close()
                    except Exception:
                        pass

    meta = save_field(field, sensing)
    meta["source"] = "EUMETSAT"
    return meta


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_latest(), indent=2, default=str))
