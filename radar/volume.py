"""Full-volume radar products (PDF Part C2): Vertically Integrated Liquid (VIL),
18-dBZ echo-top height, and VIL density, from the full ODIM polar volume the
DHMZ Uljenje radar publishes (9 elevations; see radar/ord.py).

The PDF's key upgrade for mountainous Montenegro: the 0.5-deg beam is ~2.5 km up
over Budva at 130 km, so the lowest sweep overshoots shallow convection. Using
the FULL column (VIL / echo-top / VIL-density) and, above all, their TIME TRENDS
is the realistic growth/decay signal the 2-D dBZ-trend model approximates.

This module is split in two:
  * pure column math (beam_height_m, vil_from_profile, echo_top_m,
    vil_density_g_m3, column_products) — numpy/math only, fully unit-tested;
  * polar-volume I/O (read_volume, column_profile_at, column_products_at) —
    h5py, sampling the vertical column over a point across all sweeps.

VIL (Greene-Clark 1972): M = 3.44e-3 * Z^(4/7) g/m3 (Z in mm^6/m^3), integrated
over height -> kg/m2. Reflectivity is capped at 56 dBZ (hail) and floored at
18 dBZ (noise) before integrating.
"""

import math

import numpy as np

# 4/3-earth effective radius (m): k=4/3, a=6371 km -> k*a ~ 8494.7 km.
_KA_M = 8_494_700.0

DBZ_FLOOR = 18.0   # below -> no liquid (noise)
DBZ_CAP = 56.0     # above -> capped (hail contamination)
ECHO_TOP_DBZ = 18.0


def beam_height_m(slant_range_m, elevation_deg, antenna_height_m=0.0):
    """Height (m) of the radar beam centre above the surface at a given slant
    range and elevation, under the standard 4/3-earth refraction model:

        h = sqrt(r^2 + (k a)^2 + 2 r (k a) sin(theta)) - k a + h0
    """
    r = float(slant_range_m)
    th = math.radians(float(elevation_deg))
    h = (math.sqrt(r * r + _KA_M * _KA_M + 2.0 * r * _KA_M * math.sin(th))
         - _KA_M + float(antenna_height_m))
    return h


def _eff_z(dbz, floor=DBZ_FLOOR, cap=DBZ_CAP):
    """Reflectivity factor Z (mm^6/m^3) after the hail cap + noise floor: gates
    below the floor contribute no liquid (Z=0), gates above the cap are capped."""
    d = float(dbz)
    if math.isnan(d) or d < floor:
        return 0.0
    if d > cap:
        d = cap
    return 10.0 ** (d / 10.0)


def vil_from_profile(heights_m, dbz, floor=DBZ_FLOOR, cap=DBZ_CAP):
    """Vertically Integrated Liquid (kg/m^2) from a vertical (height, dBZ)
    column, trapezoidal over layers (PDF Part C2):

        VIL = sum 3.44e-6 * ((Z_i + Z_{i+1})/2)^(4/7) * dh
    """
    h = np.asarray(heights_m, dtype=float)
    z = np.asarray(dbz, dtype=float)
    if h.size < 2:
        return 0.0
    order = np.argsort(h)
    h = h[order]
    z = z[order]
    zeff = np.array([_eff_z(v, floor, cap) for v in z])
    vil = 0.0
    for i in range(len(h) - 1):
        dh = h[i + 1] - h[i]
        if dh <= 0:
            continue
        zavg = 0.5 * (zeff[i] + zeff[i + 1])
        if zavg > 0.0:
            vil += 3.44e-6 * zavg ** (4.0 / 7.0) * dh
    return vil


def echo_top_m(heights_m, dbz, threshold=ECHO_TOP_DBZ):
    """Height (m) of the highest `threshold`-dBZ echo top, linearly interpolated
    to the threshold crossing above the topmost gate that meets it. None if no
    gate reaches the threshold."""
    h = np.asarray(heights_m, dtype=float)
    z = np.asarray(dbz, dtype=float)
    order = np.argsort(h)
    h = h[order]
    z = z[order]
    idx = [i for i in range(len(z)) if not math.isnan(z[i]) and z[i] >= threshold]
    if not idx:
        return None
    top = idx[-1]
    if top == len(z) - 1:
        return float(h[top])                     # echo reaches the top sampled gate
    z0, z1 = z[top], z[top + 1]
    if z0 == z1:
        return float(h[top])
    frac = (z0 - threshold) / (z0 - z1)
    return float(h[top] + (h[top + 1] - h[top]) * frac)


def vil_density_g_m3(vil_kg_m2, echo_top_height_m):
    """VIL density (g/m^3) = 1000 * VIL / echo-top height (Amburn & Wolf 1997).
    Much less sensitive to echo-top truncation than VIL alone; ~3.5 g/m3 flags
    severe hail. None when there is no echo top to normalise by."""
    if echo_top_height_m is None or echo_top_height_m <= 0:
        return None
    return 1000.0 * float(vil_kg_m2) / float(echo_top_height_m)


def column_products(heights_m, dbz, floor=DBZ_FLOOR, cap=DBZ_CAP,
                    echo_top_threshold=ECHO_TOP_DBZ):
    """VIL + echo-top + VIL-density for one (height, dBZ) column."""
    vil = vil_from_profile(heights_m, dbz, floor, cap)
    eth = echo_top_m(heights_m, dbz, echo_top_threshold)
    return {
        "vil_kg_m2": round(vil, 4),
        "echo_top_m": None if eth is None else round(eth, 1),
        "vil_density_g_m3": (None if vil_density_g_m3(vil, eth) is None
                             else round(vil_density_g_m3(vil, eth), 4)),
    }


ZDR_COLUMN_MIN_DB = 1.0       # ZDR >= this marks lofted liquid (updraft proxy)
ZDR_COLUMN_DBZ_MIN = 20.0     # ...within real echo, not noise
# Lowest beam above this height (m) over a point -> a radar echo aloft does NOT
# guarantee surface rain (overshoot / virga risk); flag low confidence (PDF C1).
SURFACE_RAIN_BEAM_MAX_M = 2000.0


def zdr_column(heights_m, zdr, dbz, freezing_level_m,
               zdr_min=ZDR_COLUMN_MIN_DB, dbz_min=ZDR_COLUMN_DBZ_MIN):
    """ZDR column: depth of ZDR >= `zdr_min` (in real echo) extending ABOVE the
    environmental 0 C level (PDF Part C2) — a documented updraft/intensification
    proxy whose depth changes can precede low-level reflectivity by 5-15 min.

    The caller supplies the freezing level (NWP/seasonal); at 130 km the 0.5deg
    beam is already ~2.5 km up, so only DEEP cells' columns are visible here.
    Returns {present, top_m, depth_m}.
    """
    h = np.asarray(heights_m, dtype=float)
    z = np.asarray([np.nan if v is None else v for v in zdr], dtype=float)
    d = np.asarray(dbz, dtype=float)
    above = [h[i] for i in range(len(h))
             if not np.isnan(z[i]) and z[i] >= zdr_min
             and not np.isnan(d[i]) and d[i] >= dbz_min
             and h[i] > freezing_level_m]
    if not above:
        return {"present": False, "top_m": None, "depth_m": 0.0}
    top = max(above)
    return {"present": True, "top_m": round(top, 1),
            "depth_m": round(top - freezing_level_m, 1)}


def surface_rain_confidence(lowest_beam_m, beam_max_m=SURFACE_RAIN_BEAM_MAX_M):
    """Whether an echo over the point can be trusted as SURFACE rain (PDF C1).
    With the lowest beam high above ground (overshoot), an aloft echo may be
    virga that evaporates before reaching the surface -> low confidence."""
    if lowest_beam_m is None or lowest_beam_m > beam_max_m:
        return {"confidence": "low",
                "reason": ("radar beam overshoots the low levels here "
                           f"(lowest beam {('?' if lowest_beam_m is None else round(lowest_beam_m))} m "
                           "AGL) — an echo aloft may not reach the surface")}
    return {"confidence": "high",
            "reason": f"lowest beam {round(lowest_beam_m)} m AGL — near the surface"}


# ---------------------------------------------------------------------------
# Polar-volume I/O (h5py) — sample the vertical column over a point
# ---------------------------------------------------------------------------
def read_volume(path):
    """Read every sweep of an ODIM PVOL into memory (h5py): DBZH + ZDR, RHOHV-
    filtered (non-meteorological gates -> NaN). Returns
    {site:{lat,lon}, sweeps:[{elangle, nbins, nrays, rscale_km, rstart_km,
    dbz, zdr}, ...]} sorted by elevation."""
    import h5py
    from radar import ord as _ord
    sweeps = []
    with h5py.File(path, "r") as f:
        where = f["where"].attrs
        site = {"lat": float(where["lat"]), "lon": float(where["lon"])}
        for k in sorted(f.keys()):
            if not k.startswith("dataset"):
                continue
            ds = f[k]
            dw = ds["where"].attrs
            quants = {}
            for dk in ds.keys():
                if not dk.startswith("data"):
                    continue
                q = ds[dk]["what"].attrs["quantity"]
                q = q.decode() if isinstance(q, bytes) else str(q)
                if q in ("DBZH", "RHOHV", "ZDR"):
                    quants[q] = _ord._unpack(ds[dk])
            dbz = quants.get("DBZH")
            if dbz is None:
                continue
            rhohv = quants.get("RHOHV")
            zdr = quants.get("ZDR")
            if rhohv is not None and rhohv.shape == dbz.shape:
                bad = (~np.isnan(rhohv)) & (rhohv < _ord.RHOHV_MIN)
                dbz = dbz.copy(); dbz[bad] = np.nan
                if zdr is not None and zdr.shape == dbz.shape:
                    zdr = zdr.copy(); zdr[bad] = np.nan
            sweeps.append({
                "elangle": float(dw["elangle"]),
                "nbins": int(dw["nbins"]), "nrays": int(dw["nrays"]),
                "rscale_km": float(dw["rscale"]) / 1000.0,
                "rstart_km": float(dw.get("rstart", 0.0)),
                "dbz": dbz, "zdr": zdr,
            })
    sweeps.sort(key=lambda s: s["elangle"])
    return {"site": site, "sweeps": sweeps}


def _ground_range_az(site, lat, lon):
    """Ground range (km) and azimuth (deg, 0=N 90=E) from the radar site to a
    point — the same local-plane convention radar/ord.py uses."""
    kx = 111.32 * math.cos(math.radians(site["lat"]))
    dx = (lon - site["lon"]) * kx          # east km
    dy = (lat - site["lat"]) * 110.57      # north km
    return math.hypot(dx, dy), (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def column_profile_at(volume, lat, lon):
    """Vertical (height, dBZ, ZDR) profile over a point: for each sweep, sample
    the gate at the point's azimuth whose GROUND range matches the point
    (slant r = ground / cos(elev)), at the 4/3-earth beam height. Only valid
    (non-NaN, RHOHV-passing) gates are kept. Sorted by height ascending."""
    site = volume["site"]
    s_km, az = _ground_range_az(site, lat, lon)
    levels = []
    for sw in volume["sweeps"]:
        el = sw["elangle"]
        cos_el = math.cos(math.radians(el))
        if cos_el <= 0:
            continue
        r_km = s_km / cos_el
        if r_km < sw["rstart_km"] or r_km > sw["rstart_km"] + sw["nbins"] * sw["rscale_km"]:
            continue
        ray = int(round(az / (360.0 / sw["nrays"]))) % sw["nrays"]
        b = int(math.floor((r_km - sw["rstart_km"]) / sw["rscale_km"]))
        if not (0 <= b < sw["nbins"]):
            continue
        d = sw["dbz"][ray, b]
        if d is None or (isinstance(d, float) and math.isnan(d)) or np.isnan(d):
            continue
        z = None
        if sw["zdr"] is not None:
            zv = sw["zdr"][ray, b]
            z = None if np.isnan(zv) else float(zv)
        levels.append((beam_height_m(r_km * 1000.0, el), float(d), z, el))
    levels.sort(key=lambda t: t[0])
    return {
        "ground_range_km": round(s_km, 2),
        "azimuth_deg": round(az, 1),
        "heights_m": [round(t[0], 1) for t in levels],
        "dbz": [round(t[1], 1) for t in levels],
        "zdr": [None if t[2] is None else round(t[2], 2) for t in levels],
        "elangles": [t[3] for t in levels],
    }


def column_products_at(volume_or_path, lat, lon):
    """Full-volume products (VIL / echo-top / VIL-density) for the column over a
    point, plus the lowest sampled beam height (the overshoot/confidence cue)."""
    vol = read_volume(volume_or_path) if isinstance(volume_or_path, str) else volume_or_path
    prof = column_profile_at(vol, lat, lon)
    out = column_products(prof["heights_m"], prof["dbz"])
    out["n_levels"] = len(prof["heights_m"])
    out["lowest_beam_m"] = prof["heights_m"][0] if prof["heights_m"] else None
    out["ground_range_km"] = prof["ground_range_km"]
    return out
