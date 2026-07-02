"""MeteoGate ORD (EUMETNET Open Radar Data) — raw ODIM HDF5 polar volumes for
the DHMZ Uljenje radar (NOD:hrulj), replacing colour-classified PNG pixels with
the radar's actual dBZ measurements.

Access is anonymous (rate-limited) via the CloudFerro S3 bucket behind the
MeteoGate gateway; verified live 2026-06-11. hrulj publishes a full polar
volume every 5 minutes (~4 min latency): 9 elevations, 400 m gates, with
DBZH + RHOHV + TH + VRADH + ZDR.

fetch_latest() downloads the newest PVOL; load_grid() turns its lowest sweep
into a RHOHV-filtered cartesian dBZ grid; sample_rings() reproduces the
sampling.sample_concentric annulus statistics from raw dBZ.
"""

import datetime
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import requests

import config

BASE_DIR = Path(__file__).resolve().parent.parent
ORD_FRAMES_DIR = BASE_DIR / "data" / "frames" / "ord"
# Range downloads land here, NOT in ORD_FRAMES_DIR: the live loop prunes that to the
# newest KEEP_FRAMES volumes, which would silently delete an archived case.
ORD_ARCHIVE_DIR = BASE_DIR / "data" / "ord_archive"

S3_BASE = "https://s3.waw3-1.cloudferro.com/openradar-24h"
NODE_PREFIX = "{date}/HR/hrulj/PVOL/"

# RHOHV (co-polar correlation) below this is non-meteorological echo (clutter,
# chaff, birds/insects, sea spikes). Rain/snow sit at 0.97+; hail can dip to
# ~0.85, so 0.80 keeps every hydrometeor and drops the junk.
RHOHV_MIN = 0.80

GRID_KM = 1.0  # cartesian grid resolution (km/pixel)

# Gate-to-gate azimuthal shear at/above this is the operational mesocyclone
# couplet signature (NSSL/SCIT practice, ~20-25 m/s).
MESO_COUPLET_MS = 20.0

_TS_RE = re.compile(r"@(\d{8}T\d{4})@")


def _utc_date_str(offset_days=0):
    d = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=offset_days)
    return d.strftime("%Y/%m/%d")


def _list_keys(prefix):
    """Anonymous S3 ListObjectsV2; returns sorted object keys for a prefix."""
    url = f"{S3_BASE}?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
    r = requests.get(url, timeout=30, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    keys = [el.text for el in root.iter(f"{ns}Key") if el.text]
    return sorted(keys)


def nominal_time_utc(key_or_name):
    """Parse the nominal scan time (UTC) from an ORD key/filename."""
    m = _TS_RE.search(str(key_or_name))
    if not m:
        return None
    return datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(
        tzinfo=datetime.timezone.utc)


def fetch_latest():
    """Download the newest hrulj PVOL (skipping if already cached).
    Returns the local Path, or None when nothing is listed. Raises on
    network/HTTP errors so the caller can fall back to the PNG path."""
    keys = _list_keys(NODE_PREFIX.format(date=_utc_date_str()))
    if not keys:  # just after 00 UTC the new day's prefix may be empty
        keys = _list_keys(NODE_PREFIX.format(date=_utc_date_str(-1)))
    if not keys:
        return None
    latest = keys[-1]
    ORD_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    local = ORD_FRAMES_DIR / latest.rsplit("/", 1)[-1]
    if not local.exists():
        r = requests.get(f"{S3_BASE}/{urllib.parse.quote(latest)}", timeout=60,
                         headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        if len(r.content) < 10000 or r.content[:4] != b"\x89HDF":
            raise ValueError(f"ORD object is not an HDF5 volume ({len(r.content)} B)")
        local.write_bytes(r.content)
        # prune old volumes
        vols = sorted(ORD_FRAMES_DIR.glob("*.h5"))
        for old in vols[:-config.KEEP_FRAMES]:
            try:
                old.unlink()
            except Exception:
                pass
    return local


def available_window():
    """What the rolling bucket currently holds. Returns an ordered list of
    (date_str, count, first_utc, last_utc) for yesterday + today (UTC) — i.e. the
    boundaries of what fetch_range can still pull."""
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for off in (-1, 0):
        d = (now + datetime.timedelta(days=off)).strftime("%Y/%m/%d")
        try:
            keys = [k for k in _list_keys(NODE_PREFIX.format(date=d)) if k.endswith(".h5")]
        except Exception:
            keys = []
        if keys:
            out.append((d, len(keys), nominal_time_utc(keys[0]), nominal_time_utc(keys[-1])))
        else:
            out.append((d, 0, None, None))
    return out


def fetch_range(start_utc, end_utc, dest=None):
    """Download every hrulj PVOL whose nominal time is within [start_utc, end_utc]
    (tz-aware UTC) to `dest` (default ORD_ARCHIVE_DIR — which the loop never prunes).
    Skips already-cached files and silently skips objects that aren't valid HDF5.
    Returns the sorted list of local Paths.

    The upstream bucket keeps only a rolling ~24 h, so ranges older than that come
    back empty (use available_window() to see the boundaries)."""
    if end_utc < start_utc:
        start_utc, end_utc = end_utc, start_utc
    dest = Path(dest) if dest else ORD_ARCHIVE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    out, day = [], start_utc.date()
    while day <= end_utc.date():
        try:
            keys = _list_keys(NODE_PREFIX.format(date=day.strftime("%Y/%m/%d")))
        except Exception:
            keys = []
        for k in keys:
            if not k.endswith(".h5"):
                continue
            t = nominal_time_utc(k)
            if t is None or t < start_utc or t > end_utc:
                continue
            local = dest / k.rsplit("/", 1)[-1]
            if not local.exists():
                r = requests.get(f"{S3_BASE}/{urllib.parse.quote(k)}", timeout=60,
                                 headers={"User-Agent": config.USER_AGENT})
                r.raise_for_status()
                if len(r.content) < 10000 or r.content[:4] != b"\x89HDF":
                    continue
                local.write_bytes(r.content)
            out.append(local)
        day += datetime.timedelta(days=1)
    return sorted(out)


def _unpack(ds_group):
    """ODIM data group -> float array with gain/offset applied, nodata/undetect
    as NaN."""
    a = ds_group["what"].attrs
    raw = ds_group["data"][:].astype(np.float64)
    out = raw * float(a["gain"]) + float(a["offset"])
    out[(raw == float(a["nodata"])) | (raw == float(a["undetect"]))] = np.nan
    return out


class GridCal:
    """latlon<->pixel for the cartesian dBZ grid (equirectangular around the
    radar site — the same local-plane convention the rest of the repo uses)."""

    def __init__(self, site_lat, site_lon, half_px, km_per_px):
        self.site_lat = float(site_lat)
        self.site_lon = float(site_lon)
        self.half = half_px
        self.km_per_px = km_per_px
        self._kx = 111.32 * math.cos(math.radians(self.site_lat))

    def latlon_to_pixel(self, lat, lon):
        e_km = (lon - self.site_lon) * self._kx
        n_km = (lat - self.site_lat) * 110.57
        return (self.half + e_km / self.km_per_px,
                self.half - n_km / self.km_per_px)

    def pixel_to_latlon(self, x, y):
        e_km = (x - self.half) * self.km_per_px
        n_km = (self.half - y) * self.km_per_px
        return (self.site_lat + n_km / 110.57,
                self.site_lon + e_km / self._kx)


def load_grid(path):
    """Lowest-sweep DBZH -> cartesian grid. Returns
    {dbz, cal, km_per_px, nominal_utc, frame_timestamp_local, site, elangle}.
    RHOHV < RHOHV_MIN gates are removed before gridding."""
    import h5py
    with h5py.File(path, "r") as f:
        where = f["where"].attrs
        site_lat, site_lon = float(where["lat"]), float(where["lon"])
        # pick the sweep with the lowest elevation angle
        best, best_el = None, 1e9
        for k in f.keys():
            if k.startswith("dataset"):
                el = float(f[k]["where"].attrs["elangle"])
                if el < best_el:
                    best, best_el = k, el
        ds = f[best]
        dw = ds["where"].attrs
        nbins, nrays = int(dw["nbins"]), int(dw["nrays"])
        rscale_km = float(dw["rscale"]) / 1000.0
        rstart_km = float(dw.get("rstart", 0.0))
        dbz_polar = None
        rhohv = None
        for dk in ds.keys():
            if not dk.startswith("data"):
                continue
            q = ds[dk]["what"].attrs["quantity"]
            q = q.decode() if isinstance(q, bytes) else str(q)
            if q == "DBZH":
                dbz_polar = _unpack(ds[dk])
            elif q == "RHOHV":
                rhohv = _unpack(ds[dk])
        if dbz_polar is None:
            raise ValueError("volume has no DBZH quantity")

    if rhohv is not None and rhohv.shape == dbz_polar.shape:
        dbz_polar = dbz_polar.copy()
        dbz_polar[(~np.isnan(rhohv)) & (rhohv < RHOHV_MIN)] = np.nan

    # polar -> cartesian (nearest-neighbour; rays assumed uniform from north,
    # the ODIM PVOL row convention)
    range_km = rstart_km + nbins * rscale_km
    half = int(math.ceil(range_km / GRID_KM))
    size = 2 * half + 1
    ys, xs = np.mgrid[0:size, 0:size]
    dx_km = (xs - half) * GRID_KM
    dy_km = (half - ys) * GRID_KM  # north up
    r_km = np.hypot(dx_km, dy_km)
    az = (np.degrees(np.arctan2(dx_km, dy_km)) + 360.0) % 360.0
    ray = np.round(az / (360.0 / nrays)).astype(int) % nrays
    bin_idx = np.floor((r_km - rstart_km) / rscale_km).astype(int)
    ok = (bin_idx >= 0) & (bin_idx < nbins) & (r_km > 0.5)
    dbz = np.full((size, size), np.nan)
    dbz[ok] = dbz_polar[ray[ok], bin_idx[ok]]

    nominal = nominal_time_utc(path)
    local_naive = (nominal.astimezone().replace(tzinfo=None)
                   if nominal else None)
    return {
        "dbz": dbz,
        "cal": GridCal(site_lat, site_lon, half, GRID_KM),
        "km_per_px": GRID_KM,
        "nominal_utc": nominal,
        # naive LOCAL time, same convention as PNG frame timestamps, so the
        # frontend stalenessNotice keeps working unchanged
        "frame_timestamp_local": local_naive.isoformat(timespec="seconds") if local_naive else None,
        "site": {"lat": site_lat, "lon": site_lon},
        "elangle": best_el,
        "range_km": range_km,
        "n_gates_wet": int(np.nansum(dbz >= config.RAIN_DBZ_THRESHOLD)),
    }


def rotation_check(path, lat, lon, ray_halfwin=12, bin_halfwin=10):
    """Mesocyclone proxy from VRADH (Doppler radial velocity): max gate-to-gate
    AZIMUTHAL shear in a window around (lat, lon) on the lowest sweep. A
    velocity couplet (adjacent rays with opposite-sign radial velocity, each
    >= 5 m/s) at gate-to-gate shear >= ~20-25 m/s is the operational
    mesocyclone signature (NSSL/SCIT practice). This single-elevation check is
    a CONFIRMATION AID, not a warning criterion — aliasing near the Nyquist
    velocity can fake couplets, which we flag.

    Sweep choice: among sweeps whose range covers the cell and whose elevation
    is low enough to look at storm-relevant levels (<= ~5 deg), pick the one
    with the HIGHEST Nyquist velocity (hrulj: 0.5deg has NI +-6.1 m/s but
    1.3-4.8deg have +-8.3). Even so, a >=20 m/s couplet FOLDS at these NIs, so
    when 2*NI is below the couplet threshold the result is honestly marked
    `limited_nyquist` (inconclusive) instead of a false "not confirmed".

    Returns {max_shear_ms, couplet, couplet_shear_ms, nyquist_ms, elangle,
             limited_nyquist, aliasing_possible, n_valid_gates} or None when
    VRADH is missing.
    """
    import h5py

    def _ni_of(f, ds, dk):
        for grp in (ds[dk], ds, f):
            if "how" in grp and "NI" in grp["how"].attrs:
                return float(grp["how"].attrs["NI"])
        return None

    with h5py.File(path, "r") as f:
        where = f["where"].attrs
        site_lat, site_lon = float(where["lat"]), float(where["lon"])

        # target gate geometry first (needed to test range coverage per sweep)
        kx = 111.32 * math.cos(math.radians(site_lat))
        dx = (lon - site_lon) * kx
        dy = (lat - site_lat) * 110.57
        rng_km = math.hypot(dx, dy)

        # candidate sweeps with VRADH that cover the range, elevation <= 5 deg;
        # prefer highest NI, tie-break lowest elevation
        cands = []
        for k in f.keys():
            if not k.startswith("dataset"):
                continue
            ds = f[k]
            dw = ds["where"].attrs
            el = float(dw["elangle"])
            cover_km = float(dw.get("rstart", 0.0)) + int(dw["nbins"]) * float(dw["rscale"]) / 1000.0
            if el > 5.0 or cover_km < rng_km:
                continue
            for dk in ds.keys():
                if dk.startswith("data"):
                    q = ds[dk]["what"].attrs["quantity"]
                    q = q.decode() if isinstance(q, bytes) else str(q)
                    if q == "VRADH":
                        cands.append((_ni_of(f, ds, dk) or 0.0, -el, k, dk))
        if not cands:
            return None
        cands.sort(reverse=True)
        ni_val, neg_el, best, best_dk = cands[0]
        ds = f[best]
        dw = ds["where"].attrs
        nbins, nrays = int(dw["nbins"]), int(dw["nrays"])
        rscale_km = float(dw["rscale"]) / 1000.0
        rstart_km = float(dw.get("rstart", 0.0))
        vr = _unpack(ds[best_dk])
        ni = ni_val or None
        elangle = -neg_el

    az = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    ray0 = int(round(az / (360.0 / nrays))) % nrays
    bin0 = int((rng_km - rstart_km) / rscale_km)
    if not (0 <= bin0 < nbins):
        return None

    rays = [(ray0 + r) % nrays for r in range(-ray_halfwin, ray_halfwin + 1)]
    b0, b1 = max(0, bin0 - bin_halfwin), min(nbins, bin0 + bin_halfwin + 1)
    win = vr[np.array(rays)][:, b0:b1]          # (rays, bins) window
    v1, v2 = win[:-1, :], win[1:, :]            # adjacent-azimuth gate pairs
    both = ~(np.isnan(v1) | np.isnan(v2))
    n_valid = int(both.sum())
    limited = bool(ni is not None and 2.0 * ni < MESO_COUPLET_MS)
    if n_valid == 0:
        return {"max_shear_ms": None, "couplet": False, "couplet_shear_ms": None,
                "nyquist_ms": ni, "elangle": elangle, "limited_nyquist": limited,
                "aliasing_possible": False, "n_valid_gates": 0}
    dv = np.abs(v2 - v1)
    dv[~both] = np.nan
    max_shear = float(np.nanmax(dv))
    # couplet = opposite signs, both meaningful (>= 5 m/s)
    coup = both & (np.sign(v1) * np.sign(v2) < 0) & (np.abs(v1) >= 5.0) & (np.abs(v2) >= 5.0)
    couplet_shear = float(np.nanmax(np.where(coup, dv, np.nan))) if coup.any() else None
    couplet = bool(couplet_shear is not None and couplet_shear >= MESO_COUPLET_MS)
    aliasing = bool(ni is not None and couplet_shear is not None
                    and float(np.max(np.abs(win[~np.isnan(win)]))) > 0.8 * ni)
    return {
        "max_shear_ms": round(max_shear, 1),
        "couplet": couplet,
        "couplet_shear_ms": round(couplet_shear, 1) if couplet_shear is not None else None,
        "nyquist_ms": ni,
        "elangle": elangle,
        "limited_nyquist": limited,
        "aliasing_possible": aliasing,
        "n_valid_gates": n_valid,
    }


def cells_from_grid(grid, lat_c, lon_c):
    """Storm cells from the raw dBZ grid — same cell dicts as
    tracking.extract_cells, but from measured reflectivity instead of
    colour classification (and already RHOHV-filtered in load_grid)."""
    import tracking
    dbz = grid["dbz"]
    cal = grid["cal"]
    bx, by = cal.latlon_to_pixel(lat_c, lon_c)
    mask = (~np.isnan(dbz)) & (dbz >= config.RAIN_DBZ_THRESHOLD)
    return tracking.cells_from_dbz(dbz, mask, cal.pixel_to_latlon,
                                   bx, by, grid["km_per_px"])


def sample_rings(grid, lat_c, lon_c, radii_km):
    """Annulus ring statistics around (lat_c, lon_c) from the raw dBZ grid —
    the same fields sampling.sample_concentric returns from PNG colours, so
    every downstream consumer (facts, UI table) works unchanged."""
    from radar import calibration, sampling

    dbz = grid["dbz"]
    cal = grid["cal"]
    km_per_px = grid["km_per_px"]
    H, W = dbz.shape
    bx, by = cal.latlon_to_pixel(lat_c, lon_c)

    ys = np.arange(0, H)
    xs = np.arange(0, W)
    XX, YY = np.meshgrid(xs, ys)
    DX = (XX - bx)
    DY = (YY - by)
    DIST_KM = np.sqrt(DX * DX + DY * DY) * km_per_px
    BRG = (np.degrees(np.arctan2(DX, -DY)) + 360) % 360

    valid = ~np.isnan(dbz)
    wet_raw = valid & (dbz >= config.RAIN_DBZ_THRESHOLD)
    wet = sampling._apply_speckle_filter(wet_raw)

    results = []
    prev_r = 0.0
    for r_km in radii_km:
        ann = (DIST_KM > prev_r) & (DIST_KM <= r_km)
        n_pix = int(ann.sum())
        if n_pix == 0:
            results.append({"radius_km": r_km, "n_samples": 0, "out_of_image": True})
            prev_r = r_km
            continue
        ann_dbz = dbz[ann]
        ann_brg = BRG[ann]
        ann_dist = DIST_KM[ann]
        ann_wet = wet[ann]
        v = ~np.isnan(ann_dbz)
        n_valid = int(v.sum())
        echo = v & (ann_dbz >= config.NOISE_DBZ)
        trace = echo & (ann_dbz < config.RAIN_DBZ_THRESHOLD)
        wet_raw_ann = v & (ann_dbz >= config.RAIN_DBZ_THRESHOLD)
        n_wet = int(ann_wet.sum())
        if n_valid > 0:
            max_dbz = float(np.nanmax(ann_dbz))
            mean_dbz = float(np.nanmean(ann_dbz))
            bi = int(np.nanargmax(ann_dbz))
            sb, sd = float(ann_brg[bi]), float(ann_dbz[bi])
        else:
            max_dbz = mean_dbz = float("nan")
            sb, sd = None, float("nan")
        if n_wet > 0:
            wd = ann_dist[ann_wet]
            wb = ann_brg[ann_wet]
            wz = ann_dbz[ann_wet]
            ci = int(np.argmin(wd))
            cw_km, cw_brg, cw_dbz = float(wd[ci]), float(wb[ci]), float(wz[ci])
        else:
            cw_km = cw_brg = cw_dbz = None
        results.append({
            "radius_km": r_km,
            "km_per_pixel": round(km_per_px, 3),
            "min_wet_threshold": sampling.min_wet_for_distance(r_km, km_per_px),
            "n_pixels_in_annulus": n_pix,
            "n_valid_color": n_valid,
            "n_wet": n_wet,
            "n_wet_raw": int(wet_raw_ann.sum()),
            "n_echo": int(echo.sum()),
            "n_trace": int(trace.sum()),
            "frac_wet": round(n_wet / max(n_pix, 1), 5),
            "max_dbz": None if np.isnan(max_dbz) else round(max_dbz, 1),
            "mean_dbz": None if np.isnan(mean_dbz) else round(mean_dbz, 1),
            "strongest_bearing": round(sb, 1) if sb is not None else None,
            "strongest_bearing_cardinal": (calibration.bearing_to_cardinal(sb)
                                           if sb is not None else None),
            "strongest_dbz": None if (isinstance(sd, float) and np.isnan(sd)) else round(sd, 1),
            "closest_wet_km": round(cw_km, 2) if cw_km is not None else None,
            "closest_wet_bearing": round(cw_brg, 1) if cw_brg is not None else None,
            "closest_wet_bearing_cardinal": (calibration.bearing_to_cardinal(cw_brg)
                                             if cw_brg is not None else None),
            "closest_wet_dbz": round(cw_dbz, 1) if cw_dbz is not None else None,
            "n_samples": 0,
            "n_in_image": 0,
            "samples": [],
        })
        prev_r = r_km
    return results
