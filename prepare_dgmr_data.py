#!/usr/bin/env python3
r"""Stage 1 of the DGMR transfer-learning pipeline: turn a folder of raw ODIM
HDF5 radar volumes (e.g. your 2023 hrulj/Uljenje archive) into clean, Budva-
calibrated, rain-only training sequences ready to shard and upload to Colab.

This is the LOCAL preprocessing step the PDF (Part 1c / 1e, "the complete ordered
pipeline") asks you to run BEFORE touching Colab. It does, per file:

  1. CALIBRATE FOR BUDVA. Auto-detects the ODIM type and reads either:
       * 2-D PRODUCT IMAGE (the hrulj 2023 archive: .MAX. column-max reflectivity
         on a 940x940 ~558 m azimuthal-equidistant grid centred on the radar).
         Georeferenced via the file's own `projdef` (pyproj) + the IRIS centred-
         grid convention, then RESAMPLED 558 m -> 1 km by max-reduction (preserves
         convective cell peaks). ODIM `undetect` (measured, no echo) -> dry;
         `nodata` (outside coverage) -> NaN. This is the path your files take --
         radar/ord.py only reads polar PVOL, which is why it raised "can't locate
         attribute: 'lat'".
       * POLAR PVOL / SCAN (the live-loop path) via radar/ord.py load_grid --
         lowest-sweep DBZH, RHOHV < 0.80 clutter filter, polar -> Cartesian.
     Either way the result is a 256x256 @ 1 km/px tile CENTRED ON BUDVA
     (42.2864 N, 18.8400 E, at pixel 128,128) -- byte-for-byte the domain
     dgmr_adapter.py feeds the model at inference. Out-of-coverage cells stay NaN
     for the wet test, then are zero-filled for storage (DGMR wants mm/h, dry=0).
     dBZ -> mm/h via Marshall-Palmer Z = 200 R^1.6, clipped to [0,128] (PDF 1e).

  2. FILTER OUT NON-RAIN FILES. Per-frame wet fraction = share of in-range pixels
     above --rain-thr mm/h. Two policies for KEEPING a 22-frame window:
       * simple  (default): keep the window if >= --min-wet-frames of its frames
                  clear --keep-frac wet pixels;
       * importance (--importance): DeepMind's sequence acceptance probability
                  q_n = min{1, q_min + (m/C) * sum x_sat}, x_sat = 1 - exp(-rr/s)
                  (Ravuri et al. 2021, Supp. Table 1) -- keeps a controlled
                  fraction of low-rain windows so the model still learns NOT to
                  hallucinate rain, instead of dropping every dry case.

  3. BUILD GAP-FREE 22-FRAME WINDOWS. 21 consecutive ~300 s deltas (--win/--dt/
     --tol). Windows are cut over the FULL contiguous time series, NOT over only
     the rainy frames -- a rainy sequence legitimately contains its own onset and
     decay frames -- and the rain filter is then applied at the WINDOW level.
     22 = 4 context + 18 lead frames, the DGMR contract.

  4. SHARD. Write kept (22, 256, 256) sequences to WebDataset .tar shards
     (budva-000000.tar ...) using only the stdlib tarfile, so you do NOT need the
     `webdataset` package locally -- the shards are still read by
     `webdataset.WebDataset(...)` in Colab exactly as the PDF's reader shows
     (each sample is one `<key>.seq.npy`). Windows are NON-OVERLAPPING by default
     (--stride defaults to win=22) so each frame is stored once: overlapping
     stride-1 windows would store every frame ~22x (~1.5 TB here). Stored as
     float16 by default (--shard-dtype; ~36 GB vs ~73 GB float32 on this archive --
     cast to float32 in the Colab Dataset). --resume continues an interrupted run
     (keeps complete shards, redoes the last partial one). --report-only skips
     writing and just reports what the archive yields.

For the hrulj 2023 archive, select the 5-min .MAX. reflectivity product (the
.RN1./.RNN. files are 1 h / 24 h rain ACCUMULATIONS, not 5-min frames, and are
skipped anyway). Point it at the parent folder; it recurses into the dated
subfolders (20230523/, 20230524/, ...).

First, just look at what you have (no shards written):

    python prepare_dgmr_data.py E:\uljenje --glob "*.MAX.*.h5" --report-only

Then build the shards:

    python prepare_dgmr_data.py E:\uljenje --glob "*.MAX.*.h5" --out-dir data/dgmr_train

If a run is interrupted (e.g. out of disk), free space and re-run with --resume
(point --out-dir / --cache-dir at the SAME paths): the frame cache is reused so
nothing is re-decoded, and shard writing continues after the last complete shard:

    python prepare_dgmr_data.py E:\uljenje --glob "*.MAX.*.h5" --out-dir E:\dgmr_train --resume

Outputs in --out-dir:
    frames_manifest.json   per-frame timestamp + wet fraction + cache path
    dataset_manifest.json   run parameters + window/shard summary
    frames_cache/*.npy      one Budva tile per frame, float16 (re-used across
                            runs so a re-run / crash resumes; delete after shards)
    budva-XXXXXX.tar        WebDataset shards (omitted with --report-only)

Depends on numpy + h5py + pyproj (the IMAGE georeferencing) and the stdlib.
pysteps / tensorflow are NOT needed for this stage. Install pyproj if missing:
    pip install pyproj
"""

import argparse
import io
import json
import math
import random
import sys
import tarfile
import warnings
from pathlib import Path

import numpy as np

# Repo imports: Budva location + the exact ODIM reader / tile-cropper the rest of
# the project already trusts, so the training tiles match the inference tiles.
import config
from radar import ord as ord_radar

BUDVA_LAT = config.LOCATION["lat"]
BUDVA_LON = config.LOCATION["lon"]

TILE = 256              # DGMR fixed spatial size
TILE_KM_PER_PX = 1.0    # DGMR native resolution
TILE_M_PER_PX = 1000.0  # target tile resolution in metres (1 km/px)
DGMR_WINDOW = 22        # 4 context + 18 lead frames
CADENCE_S = 300         # nominal 5 min scan cadence
CADENCE_TOL_S = 60      # allowed deviation per delta
RAIN_THR_MMH = 0.2      # a pixel is "wet" above this rain rate (PDF 1c)
KEEP_FRAC = 0.01        # a frame is "rainy" with >= 1% wet pixels (PDF 1c)
# ODIM "undetect" = measured, no echo = DRY (NOT missing). Map it to a low finite
# dBZ so it converts to ~0 mm/h yet still counts as a valid (non-NaN) pixel in the
# wet-fraction denominator. Only "nodata" (out of radar coverage) becomes NaN.
DRY_DBZ = -32.0

# Marshall-Palmer Z = a R^b -> mm/h (PDF Part 1e: a=200, b=1.6, clip [0,128]).
ZR_A = 200.0
ZR_B = 1.6
RAIN_RATE_CLIP_MMH = 128.0


# ---------------------------------------------------------------------------
# dBZ -> rain rate, Budva tile crop, timestamps
# ---------------------------------------------------------------------------
def dbz_to_rainrate(dbz, a=ZR_A, b=ZR_B, clip=RAIN_RATE_CLIP_MMH):
    """ODIM dBZ -> mm/h via Z = a * R^b (Marshall-Palmer by default). NaN dBZ
    (no echo / out of range / clutter) -> NaN so the wet test can ignore it; the
    caller zero-fills for storage. Result clipped to [0, clip]."""
    dbz = np.asarray(dbz, dtype="float64")
    with np.errstate(invalid="ignore"):
        z = np.power(10.0, dbz / 10.0)
        rr = np.power(z / a, 1.0 / b)
    rr = np.clip(rr, 0.0, clip)
    rr[~np.isfinite(dbz)] = np.nan          # keep dry/out-of-range as NaN here
    return rr


def crop_centered(field, cx, cy, size=TILE, fill=np.nan):
    """Crop a size x size window of `field` with source pixel (cx, cy) at the tile
    centre (size//2). Pixels outside the source are `fill` (NaN, so they read as
    out-of-domain for the wet test). Mirrors dgmr_adapter.center_tile but NaN-
    fills instead of zero-filling so the wet fraction is computed over real data
    only."""
    field = np.asarray(field, dtype="float64")
    h, w = field.shape
    c = size // 2
    out = np.full((size, size), fill, dtype="float64")
    icx, icy = int(round(cx)), int(round(cy))
    sx0, sy0 = icx - c, icy - c
    for ty in range(size):
        sy = sy0 + ty
        if sy < 0 or sy >= h:
            continue
        sx_lo = max(0, sx0)
        sx_hi = min(w, sx0 + size)
        if sx_hi <= sx_lo:
            continue
        tx_lo = sx_lo - sx0
        out[ty, tx_lo:tx_lo + (sx_hi - sx_lo)] = field[sy, sx_lo:sx_hi]
    return out


import re as _re
# hrulj product filename: hrulj.YYYYmmddHHMMSS.<PRODUCT>.<n>.h5
_HRULJ_TS = _re.compile(r"\.(\d{14})\.")


def frame_time_utc(path):
    """Nominal scan time (tz-aware UTC). Tries the repo's live-loop filename
    convention (ord.nominal_time_utc, @YYYYmmddTHHMM@), then the hrulj product
    filename (`...<14-digit YYYYmmddHHMMSS>...`), then the ODIM root /what
    `date` + `time` attributes (every ODIM file carries them). Filename parses
    avoid opening the H5 just for a timestamp on the cache-hit path."""
    import datetime
    t = ord_radar.nominal_time_utc(path)
    if t is not None:
        return t
    m = _HRULJ_TS.search(Path(path).name)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    import h5py
    try:
        with h5py.File(path, "r") as f:
            a = f["what"].attrs
            d = a["date"]; tm = a["time"]
            d = d.decode() if isinstance(d, bytes) else str(d)
            tm = tm.decode() if isinstance(tm, bytes) else str(tm)
        tm = (tm + "000000")[:6]
        return datetime.datetime.strptime(d + tm, "%Y%m%d%H%M%S").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ODIM 2-D PRODUCT IMAGE reader (the hrulj 2023 archive: .MAX. column-max dBZ on
# an azimuthal-equidistant grid centred on the radar). radar/ord.py only reads
# polar PVOL volumes, hence the "can't locate attribute: 'lat'" error -- product
# IMAGE files store a projection in /where, not a radar site lat/lon.
# ---------------------------------------------------------------------------
def odim_object(path):
    """The ODIM /what `object` (PVOL, SCAN, IMAGE, COMP, ...), upper-cased, or ''."""
    import h5py
    try:
        with h5py.File(path, "r") as f:
            o = f["what"].attrs["object"]
        return (o.decode() if isinstance(o, bytes) else str(o)).strip().upper()
    except Exception:
        return ""


# Cache one resampler per (projdef, xscale, yscale, xsize, ysize): the Budva
# projected coordinate and the native->tile index mapping are CONSTANT across
# every frame of a product, so we build them once and reuse for all ~100k files.
_RESAMPLER_CACHE = {}


def _image_resampler(projdef, xscale, yscale, xsize, ysize):
    key = (projdef, round(xscale, 3), round(yscale, 3), xsize, ysize)
    r = _RESAMPLER_CACHE.get(key)
    if r is not None:
        return r
    from pyproj import CRS, Transformer
    tf = Transformer.from_crs("EPSG:4326", CRS.from_proj4(projdef), always_xy=True)
    xb, yb = tf.transform(BUDVA_LON, BUDVA_LAT)        # Budva in the aeqd plane (m)
    # IRIS convention: projection origin (lat_0/lon_0 = radar) is the GRID CENTRE.
    # Native pixel (col,row) centre -> projected metres:
    #   x = (col + 0.5 - xsize/2) * xscale ;  y = (ysize/2 - row - 0.5) * yscale
    # Crop only the native window that can fall inside the 256 km tile (+margin).
    half_m = (TILE / 2 + 2) * TILE_M_PER_PX
    nwin = int(math.ceil(half_m / min(xscale, yscale))) + 2
    cb = xb / xscale + xsize / 2.0 - 0.5               # Budva native col/row
    rb = ysize / 2.0 - 0.5 - yb / yscale
    c0 = max(0, int(math.floor(cb - nwin))); c1 = min(xsize, int(math.ceil(cb + nwin)) + 1)
    r0 = max(0, int(math.floor(rb - nwin))); r1 = min(ysize, int(math.ceil(rb + nwin)) + 1)
    cc, rr_ = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
    xm = (cc + 0.5 - xsize / 2.0) * xscale
    ym = (ysize / 2.0 - rr_ - 0.5) * yscale
    it = np.round((xm - xb) / TILE_M_PER_PX).astype(np.int64) + TILE // 2
    jt = np.round((yb - ym) / TILE_M_PER_PX).astype(np.int64) + TILE // 2
    inside = (it >= 0) & (it < TILE) & (jt >= 0) & (jt < TILE)
    r = {"slice": (r0, r1, c0, c1),
         "flat_target": (jt[inside] * TILE + it[inside]),
         "inside": inside,
         "budva_native": (round(float(cb), 1), round(float(rb), 1))}
    _RESAMPLER_CACHE[key] = r
    return r


def load_image_tile_dbz(path):
    """ODIM 2-D product IMAGE (.MAX. column-max reflectivity) -> (256,256) Budva-
    centred dBZ tile at 1 km/px, NaN where outside radar coverage. Resamples the
    native ~558 m aeqd grid to 1 km by MAX-reduction (right for a reflectivity
    MAX product: it preserves cell peaks instead of smearing them). Raises if the
    file is not a DBZH product so accidental RN1/RNN (rain-accumulation) files are
    skipped with a clear reason."""
    import h5py

    def _s(v):
        return v.decode() if isinstance(v, bytes) else str(v)

    def _attr(key, *managers, default=None):
        # first manager (data-level /what, then dataset-level /what) that has the key
        for m in managers:
            if m is not None and key in m:
                return m[key]
        return default

    with h5py.File(path, "r") as f:
        if "where" not in f or "projdef" not in f["where"].attrs:
            raise ValueError("no /where projdef (not a projected IMAGE); skipping")
        wh = f["where"].attrs
        projdef = _s(wh["projdef"])
        xscale = float(wh["xscale"]); yscale = float(wh["yscale"])
        xsize = int(wh["xsize"]); ysize = int(wh["ysize"])
        # Scan EVERY datasetN/dataM group. The hrulj product metadata
        # (quantity/gain/offset/nodata/undetect) lives at the DATASET level
        # (dataset1/what), while data1 holds only the raw array -- but other ODIM
        # writers put a /what under each dataN, so read whichever exists (data-level
        # first, then dataset-level). Membership tests throughout so malformed /
        # empty / truncated products skip cleanly instead of raising.
        target, seen_q = None, []
        for dsk in sorted(f.keys()):
            if not dsk.startswith("dataset") or not isinstance(f[dsk], h5py.Group):
                continue
            g = f[dsk]
            ds_what = g["what"].attrs if ("what" in g and isinstance(g["what"], h5py.Group)) else None
            for dk in sorted(g.keys()):
                if not dk.startswith("data") or not isinstance(g[dk], h5py.Group):
                    continue
                sub = g[dk]
                if "data" not in sub:
                    continue
                d_what = sub["what"].attrs if ("what" in sub and isinstance(sub["what"], h5py.Group)) else None
                if d_what is None and ds_what is None:
                    continue
                q = _s(_attr("quantity", d_what, ds_what, default="?"))
                seen_q.append(q)
                if q != "DBZH":
                    continue
                gain = float(_attr("gain", d_what, ds_what, default=1.0))
                offset = float(_attr("offset", d_what, ds_what, default=0.0))
                nodata = float(_attr("nodata", d_what, ds_what, default=np.nan))
                undetect = float(_attr("undetect", d_what, ds_what, default=np.nan))
                raw = sub["data"][:].astype(np.float64)
                target = (raw, gain, offset, nodata, undetect)
                break
            if target is not None:
                break
        if target is None:
            raise ValueError(f"no usable DBZH data group "
                             f"(quantities seen: {sorted(set(seen_q)) or 'none'}); skipping")
        raw, gain, offset, nodata, undetect = target

    rs = _image_resampler(projdef, xscale, yscale, xsize, ysize)
    r0, r1, c0, c1 = rs["slice"]
    win = raw[r0:r1, c0:c1]
    dbz = win * gain + offset
    dbz[win == undetect] = DRY_DBZ          # measured, no echo -> dry (finite)
    dbz[win == nodata] = np.nan             # out of coverage -> unknown
    inside = rs["inside"]
    vals = dbz[inside]
    # MAX-reduce native pixels into tile cells. Seed with -inf (NOT NaN: np.maximum
    # propagates NaN, which would blank the whole tile); cells that receive no
    # finite contributor stay -inf and become NaN = "outside radar coverage".
    tile = np.full(TILE * TILE, -np.inf, dtype=np.float64)
    finite = np.isfinite(vals)
    if finite.any():
        np.maximum.at(tile, rs["flat_target"][finite], vals[finite])
    tile[~np.isfinite(tile)] = np.nan
    return tile.reshape(TILE, TILE), rs["budva_native"], (xscale, yscale, xsize)


def budva_tile_rainrate(path, zr_a=ZR_A, zr_b=ZR_B, clip=RAIN_RATE_CLIP_MMH,
                        rain_thr=RAIN_THR_MMH):
    """Raw ODIM file -> (256,256) Budva-centred rain-rate tile (mm/h, NaN where
    out of domain) + wet fraction over the in-range pixels. Auto-detects the ODIM
    type: 2-D product IMAGE (the hrulj 2023 .MAX. archive) via the projection
    reader, or polar PVOL/SCAN via radar/ord.py load_grid. Returns (tile_nan,
    wet_fraction, meta). Raises on unreadable / non-reflectivity files (caller
    skips)."""
    obj = odim_object(path)
    if obj not in ("PVOL", "SCAN"):             # IMAGE/COMP/CVOL or unknown -> 2-D product
        tile_dbz, bnative, (xscale, _ys, _xs) = load_image_tile_dbz(path)
        rr = dbz_to_rainrate(tile_dbz, zr_a, zr_b, clip)
        meta = {"km_per_px": TILE_KM_PER_PX, "odim_object": obj or "IMAGE",
                "native_m_per_px": round(float(xscale), 1),
                "budva_native_px": list(bnative), "resample": "max 558m->1km"}
    else:                                       # polar PVOL/SCAN (live-loop path)
        grid = ord_radar.load_grid(path)
        dbz = grid["dbz"]
        cal = grid["cal"]
        bx, by = cal.latlon_to_pixel(BUDVA_LAT, BUDVA_LON)
        tile_dbz = crop_centered(dbz, bx, by, TILE, fill=np.nan)
        rr = dbz_to_rainrate(tile_dbz, zr_a, zr_b, clip)
        meta = {"km_per_px": grid["km_per_px"], "odim_object": obj or "PVOL",
                "elangle": grid.get("elangle"),
                "budva_px": [round(float(bx), 1), round(float(by), 1)],
                "grid_shape": list(dbz.shape)}
    valid = np.isfinite(rr)
    wet = float(np.mean(rr[valid] > rain_thr)) if valid.any() else 0.0
    return rr, wet, meta


# ---------------------------------------------------------------------------
# Gap-free windows + DeepMind importance acceptance (PDF 1c)
# ---------------------------------------------------------------------------
def gap_free_windows(times_s, win=DGMR_WINDOW, dt=CADENCE_S, tol=CADENCE_TOL_S,
                     stride=1):
    """Indices of length-`win` windows whose `win-1` consecutive deltas are all
    within `tol` of `dt` (gap-free). `times_s` is the SORTED unix-second list.

    `stride` is the hop between consecutive KEPT windows:
      * stride=1   -> every overlapping window. There are ~N of them and each
                     shares win-1 frames with the next, so MATERIALISING them all
                     stores every frame ~win times (a 22x data blow-up -> ~1.5 TB
                     on this archive). Use only to ENUMERATE/validate.
      * stride=win -> NON-OVERLAPPING windows (each frame used once) -- the right
                     choice for a training set; ~N/win windows, ~tens of GB.
    Returns [(start, end_exclusive), ...]."""
    ts = np.asarray(times_s, dtype="float64")
    out, i, n = [], 0, len(ts)
    while i <= n - win:
        d = np.diff(ts[i:i + win])
        if np.all(np.abs(d - dt) <= tol):
            out.append((i, i + win))
            i += stride                      # skip the overlapping starts
        else:
            i += 1                           # not gap-free here -> try next start
    return out


def x_sat(rr, s=1.0):
    """Saturation transform x_sat = 1 - exp(-rr/s) (Ravuri et al. 2021)."""
    return 1.0 - np.exp(-np.clip(rr, 0.0, None) / s)


def acceptance_prob(rr_seq, q_min=0.05, m=2.0, s=1.0):
    """DeepMind per-sequence acceptance probability
    q_n = min{1, q_min + (m / C) * sum_c x_sat}, C = number of cells. rr_seq is a
    (T, H, W) rain-rate crop with NaN treated as 0."""
    xs = np.nan_to_num(x_sat(rr_seq, s), nan=0.0)
    C = xs.size
    return float(min(1.0, q_min + (m / C) * xs.sum()))


# ---------------------------------------------------------------------------
# stdlib WebDataset (.tar) shard writer -- no `webdataset` dependency locally
# ---------------------------------------------------------------------------
class ShardWriter:
    """Writes one float32 sequence per sample as `<key>.seq.npy` into rotating
    `<pattern % shard>.tar` files (default 2000 samples/shard). Byte-compatible
    with webdataset.ShardWriter, so Colab reads them with
    `webdataset.WebDataset(urls).decode()` and `np.load(io.BytesIO(s["seq.npy"]))`
    exactly as the PDF shows."""

    def __init__(self, pattern, maxcount=2000, dtype="float16", start_shard=0):
        self.pattern = str(pattern)
        self.maxcount = int(maxcount)
        self.dtype = dtype
        self.shard = int(start_shard)
        self.count = 0
        self.total = 0
        self.tar = None
        self.shards = []
        self._open()

    def _open(self):
        path = self.pattern % self.shard
        self.tar = tarfile.open(path, "w")
        self._path = path
        self.count = 0

    def write(self, key, seq):
        if self.count >= self.maxcount:
            self._close_current()
            self.shard += 1
            self._open()
        buf = io.BytesIO()
        np.save(buf, np.ascontiguousarray(seq, dtype=self.dtype))
        data = buf.getvalue()
        ti = tarfile.TarInfo(name=f"{key}.seq.npy")
        ti.size = len(data)
        ti.mtime = 0
        self.tar.addfile(ti, io.BytesIO(data))
        self.count += 1
        self.total += 1

    def _close_current(self):
        if self.tar is not None:
            self.tar.close()
            self.shards.append({"path": self._path, "samples": self.count})

    def close(self):
        self._close_current()
        self.tar = None


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
def stage_decode(files, cache_dir, args):
    """Decode every H5 -> Budva tile, cache it as <unixts>.npy, and record
    (unixts, iso, wet_fraction, src, cache). De-duplicates on nominal time
    (keeps the first). Returns the sorted-by-time frame manifest list."""
    import datetime
    cache_dir.mkdir(parents=True, exist_ok=True)
    seen, frames = set(), []
    skips, n_fail, first_examples = {}, 0, {}

    def _skip(reason, name):
        nonlocal n_fail
        n_fail += 1
        skips[reason] = skips.get(reason, 0) + 1
        first_examples.setdefault(reason, name)      # remember one example per reason

    for i, p in enumerate(files):
        t = frame_time_utc(p)
        if t is None:
            _skip("no timestamp", p.name)
            continue
        ts = int(t.timestamp())
        if ts in seen:
            continue
        cache = cache_dir / f"{ts}.npy"
        try:
            if cache.exists() and not args.refresh:
                rr = np.load(cache)
                wet = float(np.mean(np.nan_to_num(rr) > args.rain_thr))
            else:
                rr, wet, _meta = budva_tile_rainrate(
                    p, args.zr_a, args.zr_b, args.clip, args.rain_thr)
                # cache as float16 (rain rate [0,128] mm/h needs no more) to halve
                # the intermediate cache (~13 GB vs ~26 GB for ~100k MAX frames);
                # cast back to float32 for the DGMR shards at write time
                np.save(cache, np.nan_to_num(rr, nan=0.0).astype("float16"))
        except Exception as e:
            # group skips by a short reason (the message up to the first ';' or '(')
            reason = str(e).split(";")[0].split("(")[0].strip() or type(e).__name__
            _skip(reason, p.name)
            continue
        seen.add(ts)
        frames.append({"unixts": ts,
                       "iso": datetime.datetime.fromtimestamp(
                           ts, datetime.timezone.utc).isoformat(),
                       "wet_fraction": round(wet, 5),
                       "src": p.name,
                       "cache": cache.name})
        if (i + 1) % 1000 == 0:
            print(f"  scanned {i + 1}/{len(files)} files "
                  f"({len(frames)} frames, {n_fail} skipped)", flush=True)
    frames.sort(key=lambda r: r["unixts"])
    print(f"Decoded {len(frames)} unique frames from {len(files)} files "
          f"({n_fail} skipped).")
    if skips:
        print("Skips by reason (one example each):")
        for reason, n in sorted(skips.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>7}  {reason}   e.g. {first_examples[reason]}")
    return frames, skips


def stage_window_and_shard(frames, cache_dir, args):
    """Build gap-free windows over the frame time series, apply the rain filter at
    the window level, and (unless --report-only) write kept windows to shards.
    Honours --stride (non-overlapping by default), --shard-dtype, and --resume."""
    times = [f["unixts"] for f in frames]
    wet_by_ts = {f["unixts"]: f["wet_fraction"] for f in frames}
    stride = args.stride if args.stride > 0 else args.win
    windows = gap_free_windows(times, args.win, args.dt, args.tol, stride)
    overlap = "overlapping" if stride < args.win else "non-overlapping"
    print(f"Found {len(windows)} gap-free {args.win}-frame windows "
          f"(stride={stride}, {overlap}).")

    out_dir = Path(args.out_dir)
    pattern = str(out_dir / "budva-%06d.tar")

    # --resume: keep the COMPLETE shards already written, drop the last one (which
    # may be a half-written shard from the crash) and continue after it. Shards
    # other than the last are always full (maxcount) by construction, so the count
    # already written = (#kept shards) * maxcount. The kept-window order is
    # deterministic, so we re-enumerate and skip that many.
    already, start_shard = 0, 0
    if args.resume and not args.report_only:
        existing = sorted(out_dir.glob("budva-*.tar"))
        if existing:
            partial = existing[-1]
            print(f"resume: found {len(existing)} shard(s); discarding possibly-"
                  f"partial {partial.name} and continuing.")
            partial.unlink()
            full = existing[:-1]
            start_shard = len(full)
            already = len(full) * args.maxcount

    writer = None
    if not args.report_only:
        writer = ShardWriter(pattern, args.maxcount, args.shard_dtype, start_shard)

    tile_memo = {}

    def load_tile(ts):
        t = tile_memo.get(ts)
        if t is None:
            t = np.load(cache_dir / f"{ts}.npy").astype("float32")
            tile_memo[ts] = t
        return t

    kept, written, kept_windows = 0, 0, []
    for (a, b) in windows:
        win_ts = times[a:b]
        n_wet = sum(1 for ts in win_ts if wet_by_ts.get(ts, 0.0) >= args.keep_frac)
        if args.importance:
            seq = np.stack([load_tile(ts) for ts in win_ts], axis=0)
            q = acceptance_prob(seq, args.q_min, args.m, args.s)
            # deterministic per-window draw keyed by (seed, start_ts) so the keep
            # decision is independent of order -> --resume is exact. str seed is
            # reproducible across runs (unaffected by PYTHONHASHSEED).
            keep = random.Random(f"{args.seed}-{win_ts[0]}").random() < q
        else:
            seq, q = None, None
            keep = n_wet >= args.min_wet_frames
        if not keep:
            continue
        idx = kept
        kept += 1
        if idx < already:                    # already written in a previous run
            continue
        if seq is None:
            seq = np.stack([load_tile(ts) for ts in win_ts], axis=0)
        if writer is not None:
            writer.write(f"{idx:08d}", seq)
            written += 1
        if len(kept_windows) < 50:
            kept_windows.append({"start_iso": frames[a]["iso"],
                                 "start_unixts": win_ts[0],
                                 "n_wet_frames": n_wet,
                                 "acceptance_q": None if q is None else round(q, 4)})
        if writer is not None and written % 500 == 0:
            print(f"  wrote {written} sequences ({kept} kept so far)...", flush=True)
        # evict tiles no longer needed by upcoming windows
        if len(tile_memo) > args.win * 4:
            keep_set = set(times[a:min(len(times), a + args.win * 2)])
            for k in list(tile_memo):
                if k not in keep_set:
                    del tile_memo[k]

    shards = []
    if writer is not None:
        writer.close()
        shards = writer.shards
        print(f"Wrote {written} new sequences ({kept} kept total) to "
              f"{len(shards)} new shard(s) in {out_dir}.")
    else:
        print(f"--report-only: {kept} windows WOULD be kept (no shards written).")
    return windows, kept, kept_windows, shards


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Stage 1 DGMR data prep: Budva-calibrate ODIM H5, drop "
                    "non-rain, build gap-free 22-frame windows, shard.")
    ap.add_argument("input_dir", help="folder of ODIM .h5 volumes (searched recursively)")
    ap.add_argument("--out-dir", default="data/dgmr_train", help="output dir for shards + manifests")
    ap.add_argument("--cache-dir", default=None, help="per-frame tile cache (default <out-dir>/frames_cache)")
    ap.add_argument("--glob", default="*.h5", help="filename pattern (default *.h5)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N files (debug)")
    ap.add_argument("--refresh", action="store_true", help="re-decode even if a cached tile exists")
    ap.add_argument("--report-only", action="store_true",
                    help="scan + write manifests but DO NOT write shards")
    # rain / calibration knobs
    ap.add_argument("--rain-thr", type=float, default=RAIN_THR_MMH, help="mm/h wet-pixel threshold")
    ap.add_argument("--keep-frac", type=float, default=KEEP_FRAC, help="min wet fraction for a 'rainy' frame")
    ap.add_argument("--min-wet-frames", type=int, default=1,
                    help="simple filter: min rainy frames per window to keep it")
    ap.add_argument("--zr-a", type=float, default=ZR_A, help="Z-R 'a' (Marshall-Palmer 200)")
    ap.add_argument("--zr-b", type=float, default=ZR_B, help="Z-R 'b' (Marshall-Palmer 1.6)")
    ap.add_argument("--clip", type=float, default=RAIN_RATE_CLIP_MMH, help="rain-rate clip (mm/h)")
    # windowing
    ap.add_argument("--win", type=int, default=DGMR_WINDOW, help="frames per window (22)")
    ap.add_argument("--stride", type=int, default=0,
                    help="hop between kept windows; 0 -> win (non-overlapping, the "
                         "sane default). Smaller = more overlap = MUCH bigger output.")
    ap.add_argument("--dt", type=int, default=CADENCE_S, help="nominal cadence seconds (300)")
    ap.add_argument("--tol", type=int, default=CADENCE_TOL_S, help="per-delta tolerance seconds (60)")
    # DeepMind importance sampling
    ap.add_argument("--importance", action="store_true",
                    help="use DeepMind sequence acceptance instead of the simple wet filter")
    ap.add_argument("--q-min", type=float, default=0.05, help="importance: min inclusion prob")
    ap.add_argument("--m", type=float, default=2.0, help="importance: inclusion multiplier")
    ap.add_argument("--s", type=float, default=1.0, help="importance: saturation constant")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for importance acceptance")
    # sharding
    ap.add_argument("--maxcount", type=int, default=2000, help="sequences per shard")
    ap.add_argument("--shard-dtype", choices=["float16", "float32"], default="float16",
                    help="dtype stored in shards (float16 halves disk; cast to "
                         "float32 in Colab. float32 matches the PDF verbatim).")
    ap.add_argument("--resume", action="store_true",
                    help="continue shard writing after an interrupted run "
                         "(keeps complete shards, redoes the last partial one)")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else (out_dir / "frames_cache")
    manifest_path = out_dir / "frames_manifest.json"

    # Reuse the decoded frame manifest if it already exists (so tweaking the rain
    # selection / writing shards does NOT re-scan all ~285k files off disk). The
    # per-frame wet fractions in it were computed at the decode-time --rain-thr;
    # pass --refresh to rebuild if you change --rain-thr or the input set.
    if manifest_path.exists() and not args.refresh:
        frames = json.loads(manifest_path.read_text())
        skips = {}
        n_files = len(frames)
        print(f"Reusing {len(frames)} decoded frames from {manifest_path.name} "
              f"(no re-scan; --refresh to rebuild from {args.input_dir}).")
    else:
        in_dir = Path(args.input_dir)
        if not in_dir.is_dir():
            print(f"error: {in_dir} is not a directory", file=sys.stderr)
            return 2
        files = sorted(in_dir.rglob(args.glob))
        if args.limit:
            files = files[:args.limit]
        if not files:
            print(f"error: no files matching {args.glob} under {in_dir}", file=sys.stderr)
            return 2
        print(f"Found {len(files)} candidate files under {in_dir}.")
        frames, skips = stage_decode(files, cache_dir, args)
        n_files = len(files)
        manifest_path.write_text(json.dumps(frames, indent=1))

    n_rainy = sum(1 for f in frames if f["wet_fraction"] >= args.keep_frac)
    print(f"Rainy frames (>= {args.keep_frac:.0%} wet): {n_rainy} / {len(frames)}.")

    windows, kept, kept_windows, shards = stage_window_and_shard(frames, cache_dir, args)

    manifest = {
        "budva": {"lat": BUDVA_LAT, "lon": BUDVA_LON},
        "tile": TILE, "km_per_px": TILE_KM_PER_PX,
        "zr": {"a": args.zr_a, "b": args.zr_b, "clip_mmh": args.clip,
               "relation": "Z = a R^b (Marshall-Palmer)"},
        "rain_thr_mmh": args.rain_thr, "keep_frac": args.keep_frac,
        "window": {"frames": args.win, "stride": (args.stride or args.win),
                   "cadence_s": args.dt, "tol_s": args.tol,
                   "context": 4, "lead": args.win - 4},
        "shard_dtype": args.shard_dtype,
        "filter": ("importance" if args.importance else "simple"),
        "importance": ({"q_min": args.q_min, "m": args.m, "s": args.s, "seed": args.seed}
                       if args.importance else None),
        "counts": {"files": n_files, "frames": len(frames), "rainy_frames": n_rainy,
                   "gap_free_windows": len(windows), "kept_windows": kept,
                   "skipped": sum(skips.values())},
        "skips_by_reason": skips,
        # all shards currently on disk (resume-aware), not just those this run wrote
        "shards": sorted(p.name for p in out_dir.glob("budva-*.tar")),
        "report_only": args.report_only,
        "kept_windows_head": kept_windows[:20],
    }
    (out_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2))

    n_shards_total = len(list(out_dir.glob("budva-*.tar")))
    print("\n--- summary ---")
    print(f"files={n_files}  frames={len(frames)}  rainy={n_rainy}  "
          f"windows={len(windows)}  kept={kept}  shards={n_shards_total}")
    print(f"manifests -> {out_dir}/frames_manifest.json, {out_dir}/dataset_manifest.json")
    if not args.report_only:
        bytes_per = 2 if args.shard_dtype == "float16" else 4
        total_samples = kept                 # full dataset size, written + already there
        approx_gb = total_samples * args.win * TILE * TILE * bytes_per / 1e9
        print(f"shards     -> {out_dir}/budva-XXXXXX.tar  "
              f"(~{approx_gb:.1f} GB total, {args.shard_dtype}, {kept} sequences)")
        print("Next: upload the shards to Drive/GCS, then we configure Colab "
              "(DGMR.from_pretrained + fine-tuning LRs) per the PDF.")
    elif args.report_only:
        print("Re-run without --report-only to write the shards once the counts look right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
