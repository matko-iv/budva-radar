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
        'opaque' for opaque-only) within radius_km. Returns None when no valid
        cells fall in the disc."""
        sel = self._disc_mask(lat, lon, radius_km)
        if not sel.any():
            return None
        arr = self.layers.get(layer)
        if arr is None:
            arr = self.layers["mask"]
        vals = arr[sel]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            return None
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


def downsample_for_browser(field, max_dim=80):
    """A compact dict for cloud_data.js so the JS port can replay the nowcast
    (frac) and label clicked points (cth/cot). Coarsened to keep the file small."""
    H, W = field.shape
    step = max(1, (max(H, W) + max_dim - 1) // max_dim)   # ceil -> actually shrinks
    lats = field.lats[::step]
    lons = field.lons[::step]

    def _coarse(name, nd, default=None):
        a = field.layers.get(name)
        if a is None:
            a = field.layers.get(default)
        if a is None:
            return None
        sub = a[::step, ::step]
        return np.where(np.isnan(sub), None, np.round(sub, nd)).tolist()

    return {
        "lats": [round(float(x), 4) for x in lats],
        "lons": [round(float(x), 4) for x in lons],
        "frac": _coarse("frac", 2, default="mask"),
        "opaque": _coarse("opaque", 2),
        "cth": _coarse("cth", 0),
        "cot": _coarse("cot", 1),
    }
