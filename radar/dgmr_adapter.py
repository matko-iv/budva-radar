"""DeepMind DGMR (Deep Generative Model of Radar) as a second nowcast engine,
to run side-by-side with LINDA-D for accuracy comparison (Skala PDF asks for a
two-track view; the user asked specifically for pySTEPS/pysteps-dgmr-nowcasts).

DGMR is a pretrained generative nowcaster from Ravuri et al. 2021 (Nature 597),
trained on UK 1 km radar. Its fixed contract is:
    input  (4, 256, 256, 1)  -- 4 past frames, 1 km grid, 5 min cadence, mm/h
    output (num_samples, 18, 256, 256, 1) -- 18 lead frames = 90 min @ 5 min

That 1 km / 5 min / 256 px contract is a NATIVE fit for the ORD (hrulj) ODIM
grid (radar/ord.py load_grid is 1 km/px), which is exactly why it is offered for
the ORD path and NOT for the 4 km OPERA composite.

Heavy deps (TensorFlow + the pysteps-dgmr-nowcasts plugin + its pretrained
weights) are imported lazily and the whole thing is GATED on availability: if the
plugin/weights are not installed, `available()` is False and `forecast()` returns
(None, reason) so the comparison simply omits the DGMR column instead of
breaking. Install to enable (on a machine with internet):
    pip install tensorflow wradlib xarray pyproj
    pip install git+https://github.com/pySTEPS/pysteps-dgmr-nowcasts.git
The plugin downloads + caches the pretrained weights on first use.

NOTE on transfer: DGMR was trained on UK radar; applied to the Adriatic it is an
out-of-distribution test. That is the point of the comparison -- verify it on the
local archive (verify_nowcast.py), do not assume it wins.
"""

import numpy as np

DGMR_KM_PER_PX = 1.0          # DGMR's native grid resolution
DGMR_TILE = 256               # fixed input/output spatial size
DGMR_LEADS = 18               # fixed number of output frames (90 min @ 5 min)
DGMR_TIMESTEP_MIN = 5.0


def _import_forecast():
    """Return the plugin's forecast callable, or raise ImportError. Tries every
    name the plugin is known to expose (the package module and the pysteps plugin
    entry point) so an install that registers only one of them still works."""
    errs = []
    # 1) the package module (what example.py uses)
    try:
        from dgmr_module_plugin.dgmr import forecast
        return forecast
    except Exception as e:
        errs.append(f"dgmr_module_plugin.dgmr: {type(e).__name__}")
    # 2) the documented pysteps plugin path: from pysteps.nowcasts import dgmr
    try:
        from pysteps.nowcasts import dgmr as _d
        if hasattr(_d, "forecast"):
            return _d.forecast
    except Exception as e:
        errs.append(f"pysteps.nowcasts.dgmr: {type(e).__name__}")
    # 3) pysteps method registry
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
