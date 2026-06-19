"""Diagnostic: fetch latest CLM + CTTH, and report the ACTUAL values of
cloud_state and effective_cloudiness over the Budva bbox, so we can fix the
cloud-amount interpretation in fetch.normalize.

    python -m clouds.diag      (needs EUMETSAT_KEY / EUMETSAT_SECRET)
"""
import sys
import tempfile

import numpy as np

import config
from clouds import fetch


def _stats(name, a):
    a = np.asarray(a, dtype="float64")
    fin = a[~np.isnan(a)]
    if fin.size == 0:
        print(f"  {name}: all-NaN"); return
    qs = np.percentile(fin, [0, 10, 25, 50, 75, 90, 100])
    print(f"  {name}: n={fin.size} NaN={np.mean(np.isnan(a))*100:.0f}%  "
          f"min={qs[0]:.3g} p10={qs[1]:.3g} p25={qs[2]:.3g} med={qs[3]:.3g} "
          f"p75={qs[4]:.3g} p90={qs[5]:.3g} max={qs[6]:.3g} mean={fin.mean():.3g}")


def main():
    import eumdac
    import xarray as xr
    from clouds.discover import get_token
    cfg = config.CLOUDS
    lats, lons = fetch.target_grid(cfg)
    store = eumdac.DataStore(get_token())

    with tempfile.TemporaryDirectory() as d:
        # ---- CLM cloud_state ------------------------------------------------
        clm_p = fetch._search_latest(store, cfg["collections"]["clm"])
        ds = xr.open_dataset(fetch._download_nc(clm_p, d))
        try:
            cs_var = ds["cloud_state"]
            print("cloud_state attrs:", dict(cs_var.attrs))
            idx = fetch._geos_indices(ds, lats, lons)
            cs = fetch._sample(cs_var, idx)
            vals, counts = np.unique(cs[~np.isnan(cs)].round().astype(int), return_counts=True)
            tot = counts.sum()
            print("cloud_state values over bbox (value: count, %):")
            for v, c in zip(vals, counts):
                print(f"   {v}: {c}  ({100*c/tot:.1f}%)")
        finally:
            ds.close()

        # ---- CTTH effective_cloudiness -------------------------------------
        ctth_p = fetch._search_latest(store, cfg["collections"]["ctth"])
        ds = xr.open_dataset(fetch._download_nc(ctth_p, d))
        try:
            idx = fetch._geos_indices(ds, lats, lons)
            for vn in ("effective_cloudiness", "cloud_top_height", "cloud_top_temperature"):
                if vn in ds.variables:
                    print(f"\n{vn} attrs:", dict(ds[vn].attrs))
                    _stats(vn + " over bbox", fetch._sample(ds[vn], idx))
        finally:
            ds.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
