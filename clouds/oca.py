"""OCA (Optimal Cloud Analysis) optical-thickness unpacking.

OCA does not store COT linearly: it stores log10(COT) in two variables,
`cloud_optical_depth_log` (upper/only layer) and
`cloud_optical_depth_log_lower_layer`, and the total is

    COT = 10^upper + 10^lower      (summed in linear space, not log space)

Rules enforced here: fill is masked to NaN before de-logging (de-logging a
raw -32768 fill gives nonsense); the two layers are summed in linear space
(satpy's get_total_cot does the same); failed retrievals
(scene_classification == 10) are dropped; and COT ~= 257 (10^2.41, the
documented log upper limit) is saturated thick cloud, not fill — it passes
through unchanged.

Pure numpy, unit-testable without xarray or a live netCDF file.
"""

import numpy as np

FAILED_RETRIEVAL = 10   # OCA scene_classification code for a failed retrieval
SATURATION_COT = 10.0 ** 2.41   # ~257; documented upper limit of the log scale


def delog(log_arr):
    """10 ** log_arr, preserving NaN (fill must already be NaN, not a raw fill)."""
    a = np.asarray(log_arr, dtype="float64")
    return np.power(10.0, a)


def total_cot(upper_log, lower_log=None):
    """Total linear COT from the two log10 layers: 10^upper + 10^lower.

    NaN-aware: a layer that has no retrieval (NaN) contributes nothing, but if
    BOTH layers are NaN the result is NaN (no cloud / no retrieval there)."""
    u = delog(upper_log)
    if lower_log is None:
        return u
    l = delog(lower_log)
    u_nan, l_nan = np.isnan(u), np.isnan(l)
    total = np.where(u_nan, 0.0, u) + np.where(l_nan, 0.0, l)
    return np.where(u_nan & l_nan, np.nan, total)


def apply_scene_filter(cot, scene_class, failed_code=FAILED_RETRIEVAL):
    """NaN out COT where the OCA scene_classification flags a failed retrieval."""
    if scene_class is None:
        return np.asarray(cot, dtype="float64")
    cot = np.asarray(cot, dtype="float64").copy()
    sc = np.asarray(scene_class, dtype="float64")
    cot[sc == failed_code] = np.nan
    return cot


def is_log_variable(name="", attrs=None):
    """True if a variable holds log10(COT) (so it must be de-logged before use).
    Keys off the variable NAME (`..._log`) or its long_name, since the FCI NRT
    `cloud_optical_depth_log` long_name does not always literally say 'log10'."""
    attrs = attrs or {}
    name = (name or "").lower()
    long_name = str(attrs.get("long_name", "")).lower()
    return name.endswith("_log") or "_log_" in name or "log10" in long_name
