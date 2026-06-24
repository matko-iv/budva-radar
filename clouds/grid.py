"""Geolocation + sampling on the NORMALIZED cloud field (a regular lat/lon grid).

CloudField wraps the regular-grid arrays produced by clouds/fetch.py and offers
point / disc sampling. Geometry helpers (haversine, bearing) are REUSED from
radar/calibration.py — not re-implemented.
"""

import json
from pathlib import Path

import numpy as np

from radar import calibration

LAYERS = ("mask", "frac", "opaque", "ctt", "cth", "cot", "phase")


class CloudField:
    """A regular lat/lon grid of cloud layers. See clouds/__init__.py for the
    normalized format. `lats`/`lons` are 1-D, monotonic (any direction)."""

    def __init__(self, lats, lons, layers, meta=None):
        self.lats = np.asarray(lats, dtype="float64")
        self.lons = np.asarray(lons, dtype="float64")
        self.layers = {k: np.asarray(v, dtype="float64") for k, v in layers.items()}
        self.meta = dict(meta or {})
        H, W = len(self.lats), len(self.lons)
        for k, v in self.layers.items():
            if v.shape != (H, W):
                raise ValueError(f"layer {k!r} shape {v.shape} != grid ({H},{W})")
        # Cache a meshgrid for disc sampling (cheap for our ~150x170 subset).
        self._lon2d, self._lat2d = np.meshgrid(self.lons, self.lats)

    # -- basic geometry ------------------------------------------------------
    @property
    def shape(self):
        return (len(self.lats), len(self.lons))

    @property
    def sensing_time(self):
        return self.meta.get("sensing_time")

    def _nearest_idx(self, lat, lon):
        return int(np.argmin(np.abs(self.lats - lat))), \
               int(np.argmin(np.abs(self.lons - lon)))

    def contains(self, lat, lon):
        return (min(self.lats) <= lat <= max(self.lats)
                and min(self.lons) <= lon <= max(self.lons))

    # -- sampling ------------------------------------------------------------
    def value_at(self, layer, lat, lon):
        """Nearest-cell value of `layer` at (lat, lon), or None if NaN/missing."""
        i, j = self._nearest_idx(lat, lon)
        v = self.layers[layer][i, j]
        return None if np.isnan(v) else float(v)

    def _disc_mask(self, lat, lon, radius_km):
        d = calibration.haversine_km(lat, lon, self._lat2d, self._lon2d)
        return d <= radius_km

    def cloud_fraction(self, lat, lon, radius_km, layer="frac"):
        """Disc-mean of `layer` (default 'frac' = total cloud amount; pass
        'opaque' for opaque-only) within radius_km.

        Falls back to the NEAREST cell when the disc catches no valid cell (radius
        smaller than a grid cell — e.g. a tight point read on a coarse grid), so a
        point inside the grid always reads a value instead of None. Returns None
        only when the point is outside the grid or the nearest cell is NaN."""
        arr = self.layers.get(layer)
        if arr is None:
            arr = self.layers["mask"]
        sel = self._disc_mask(lat, lon, radius_km)
        vals = arr[sel] if sel.any() else np.array([])
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            if not self.contains(lat, lon):
                return None
            i, j = self._nearest_idx(lat, lon)
            v = arr[i, j]
            return None if np.isnan(v) else float(np.clip(v, 0.0, 1.0))
        return float(np.clip(vals.mean(), 0.0, 1.0))

    def sample_cloudy(self, layer, lat, lon, radius_km, reducer="mean"):
        """Reduce `layer` over CLOUDY cells (mask>=0.5) within radius_km.
        Returns None if there are no cloudy cells with valid values."""
        sel = self._disc_mask(lat, lon, radius_km) & (self.layers["mask"] >= 0.5)
        if not sel.any():
            return None
        vals = self.layers[layer][sel]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            return None
        if reducer == "median":
            return float(np.median(vals))
        if reducer == "max":
            return float(np.max(vals))
        return float(np.mean(vals))

    def dominant_phase(self, lat, lon, radius_km):
        """Most common phase over cloudy cells: 'water' / 'ice' / None."""
        sel = self._disc_mask(lat, lon, radius_km) & (self.layers["mask"] >= 0.5)
        vals = self.layers["phase"][sel]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            return None
        n_ice = int((vals >= 1.5).sum())
        n_water = int((vals >= 0.5).sum()) - n_ice
        if n_ice == 0 and n_water == 0:
            return None
        return "ice" if n_ice >= n_water else "water"

    # -- persistence ---------------------------------------------------------
    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path, lats=self.lats.astype("float32"), lons=self.lons.astype("float32"),
            meta=np.array(json.dumps(self.meta)),
            **{k: v.astype("float32") for k, v in self.layers.items()})

    @classmethod
    def load(cls, path):
        with np.load(path, allow_pickle=False) as z:
            lats, lons = z["lats"], z["lons"]
            meta = json.loads(str(z["meta"])) if "meta" in z else {}
            layers = {k: z[k] for k in LAYERS if k in z}
        return cls(lats, lons, layers, meta)


def downsample_for_browser(field, max_dim=200):
    """A compact dict for cloud_data.js so the JS port can replay the nowcast
    (frac) and read any clicked point.

    frac/opaque are MEAN-pooled (NOT strided): strided subsampling (`a[::step]`)
    silently drops clouds smaller than the stride, so clicking a small cloud read
    "clear". Mean-pooling keeps a non-zero fraction wherever any cloud falls, and
    the default max_dim ships frac at (near) full resolution so the per-point read
    is faithful to the picture. All-NaN layers (cth/cot for the HighSight picture)
    are omitted to keep the payload small."""
    import warnings
    H, W = field.shape
    step = max(1, (max(H, W) + max_dim - 1) // max_dim)
    if step == 1:
        lats, lons = field.lats, field.lons
    else:
        Hc, Wc = H // step, W // step
        lats = field.lats[:Hc * step].reshape(Hc, step).mean(1)
        lons = field.lons[:Wc * step].reshape(Wc, step).mean(1)

    def _pool(a):
        if step == 1:
            return a
        Hc, Wc = H // step, W // step
        block = a[:Hc * step, :Wc * step].reshape(Hc, step, Wc, step)
        with warnings.catch_warnings():            # all-NaN block -> NaN, silently
            warnings.simplefilter("ignore", category=RuntimeWarning)
            return np.nanmean(block, axis=(1, 3))

    def _layer(name, nd, default=None):
        a = field.layers.get(name)
        if a is None and default is not None:
            a = field.layers.get(default)
        if a is None or not np.isfinite(a).any():   # omit all-NaN layers (cth/cot)
            return None
        sub = _pool(a)
        return np.where(np.isnan(sub), None, np.round(sub, nd)).tolist()

    return {
        "lats": [round(float(x), 4) for x in lats],
        "lons": [round(float(x), 4) for x in lons],
        "frac": _layer("frac", 2, default="mask"),
        "opaque": _layer("opaque", 2),
        "cth": _layer("cth", 0),
        "cot": _layer("cot", 1),
    }
