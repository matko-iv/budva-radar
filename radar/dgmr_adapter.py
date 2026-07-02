"""DeepMind DGMR (Ravuri et al. 2021, Nature 597) as a second nowcast engine,
run side-by-side with LINDA for comparison.

DGMR's fixed contract — input (4, 256, 256, 1) at 1 km / 5 min, output 18 lead
frames — matches the ORD (hrulj) grid natively, which is why it is offered for
the ORD path and not the 4 km OPERA composite.

TensorFlow and the pysteps-dgmr-nowcasts plugin are imported lazily; when
missing, available() is False and forecast() returns (None, reason) so the
comparison omits the DGMR column instead of breaking. To enable:
    pip install tensorflow wradlib xarray pyproj
    pip install git+https://github.com/pySTEPS/pysteps-dgmr-nowcasts.git

DGMR was trained on UK radar, so the Adriatic is out-of-distribution — verify
against the local archive (verify_nowcast.py) rather than assuming it wins.
"""

import numpy as np

DGMR_KM_PER_PX = 1.0          # DGMR's native grid resolution
DGMR_TILE = 256               # fixed input/output spatial size
DGMR_LEADS = 18               # fixed number of output frames (90 min @ 5 min)
DGMR_TIMESTEP_MIN = 5.0


def _dgmr_cache_dir():
    """Where the plugin caches its weights (matches dgmr_module_plugin.dgmr)."""
    import os
    sub = "pysteps" if os.name == "nt" else ".pysteps"
    return os.path.join(os.path.expanduser("~"), sub, "pystepscache")


def _heal_dgmr_cache():
    """The plugin downloads weights at import time; an interrupted first
    download leaves the cache dir without a 'models--' folder and every later
    import dies with StopIteration. Clear that state so the next import
    re-downloads; a healthy cache is untouched."""
    import os
    import shutil
    cache = _dgmr_cache_dir()
    try:
        if os.path.isdir(cache) and not any("models--" in d for d in os.listdir(cache)):
            shutil.rmtree(cache, ignore_errors=True)
    except Exception:
        pass


def _import_forecast():
    """Return the plugin's forecast callable, or raise ImportError. Tries every
    name the plugin is known to expose (the package module and the pysteps plugin
    entry point) so an install that registers only one of them still works."""
    _heal_dgmr_cache()
    errs = []
    try:
        from dgmr_module_plugin.dgmr import forecast
        return forecast
    except Exception as e:
        errs.append(f"dgmr_module_plugin.dgmr: {type(e).__name__}")
    try:
        from pysteps.nowcasts import dgmr as _d
        if hasattr(_d, "forecast"):
            return _d.forecast
    except Exception as e:
        errs.append(f"pysteps.nowcasts.dgmr: {type(e).__name__}")
    try:
        from pysteps import nowcasts
        m = nowcasts.get_method("dgmr")
        return getattr(m, "forecast", m)
    except Exception as e:
        errs.append(f"nowcasts.get_method('dgmr'): {type(e).__name__}")
    raise ImportError("; ".join(errs))


def available():
    """True iff TensorFlow + the DGMR plugin import (weights downloaded lazily on
    first forecast)."""
    try:
        import tensorflow  # noqa: F401
        _import_forecast()
        return True
    except Exception:
        return False


def unavailable_reason():
    """Human-readable reason DGMR can't run (for the page/CLI), or None if it can.
    Names the Python so a wrong-environment install (the usual Windows cause) is
    obvious."""
    import sys
    pyexe = sys.executable
    try:
        import tensorflow  # noqa: F401
    except Exception as e:
        return f"TensorFlow not importable in {pyexe} ({type(e).__name__})"
    try:
        _import_forecast()
    except Exception as e:
        return (f"pysteps-dgmr-nowcasts plugin not importable in {pyexe} ({e}). "
                f"Run the pip install in THIS Python, and ensure 'git' is on PATH.")
    return None


def center_tile(field, cx, cy, size=DGMR_TILE):
    """Crop/pad `field` to size x size with source pixel (cx,cy) at the tile
    centre. Zero-filled outside the source. Returns (tile, (center_x, center_y))
    where the centre is size//2."""
    field = np.asarray(field, dtype="float64")
    h, w = field.shape
    c = size // 2
    out = np.zeros((size, size), dtype="float64")
    icx, icy = int(round(cx)), int(round(cy))
    # source rect that maps into the tile
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
    return out, (c, c)


def forecast(R_stack, info, n_leadtimes, *, timestep_min=DGMR_TIMESTEP_MIN,
             num_samples=1, _forecast_fn=None):
    """Run DGMR on the last 4 frames of a 1 km mm/h rain-rate stack.

    Returns (fc, meta) where fc is (n_leadtimes, 256, 256) mm/h on a Budva-centred
    256x256 / 1 km tile (Budva at pixel (128,128)), or (None, {"reason": ...}) when
    DGMR is unavailable or the input is unusable.

    `_forecast_fn` is an injection seam for tests (a stub standing in for the real
    plugin); production passes None and the real plugin is imported.
    """
    if R_stack.shape[0] < 4:
        return None, {"reason": "DGMR needs >= 4 input frames"}
    kmpp = info.get("km_per_px", DGMR_KM_PER_PX)
    if abs(kmpp - DGMR_KM_PER_PX) > 0.25:
        return None, {"reason": f"DGMR needs ~1 km/px input, got {kmpp:.2f} "
                                f"(use the ORD/H5 path, not the {kmpp:.0f} km composite)"}
    fn = _forecast_fn
    if fn is None:
        reason = unavailable_reason()
        if reason:
            return None, {"reason": reason}
        try:
            fn = _import_forecast()
        except Exception as e:                       # pragma: no cover - import guard
            return None, {"reason": f"DGMR import failed ({e})"}

    cx, cy = info["budva_crop_xy"]
    tiles = [center_tile(R_stack[-4 + k], cx, cy)[0] for k in range(4)]
    inp = np.stack(tiles, axis=0)[..., None].astype("float32")   # (4,256,256,1)

    try:
        out = fn(inp, num_samples=num_samples)
    except Exception as e:                           # pragma: no cover - runtime guard
        return None, {"reason": f"DGMR forecast failed ({type(e).__name__}: {e})"}
    out = np.asarray(out, dtype="float64")
    out = np.squeeze(out)                            # -> (18,256,256) (drops sample/chan)
    if out.ndim == 4:                               # (samples,18,256,256) -> mean over samples
        out = out.mean(axis=0)
    if out.ndim != 3:
        return None, {"reason": f"DGMR returned unexpected shape {out.shape}"}
    n = min(n_leadtimes, out.shape[0])
    fc = np.clip(np.nan_to_num(out[:n], nan=0.0), 0.0, None)
    meta = {"method": "dgmr", "native_leads": int(out.shape[0]),
            "tile": DGMR_TILE, "km_per_px": DGMR_KM_PER_PX,
            "budva_tile_xy": [DGMR_TILE // 2, DGMR_TILE // 2]}
    return fc, meta
