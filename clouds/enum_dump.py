"""Dump the authoritative netCDF enum definition of cloud_state (the CLM cloud
mask categories) + value counts over the Budva bbox, and the effective_cloudiness
fill/scale. Run with creds:  python -m clouds.enum_dump
"""
import tempfile

import numpy as np

import config
from clouds import fetch


def main():
    import eumdac
    import netCDF4
    from clouds.discover import get_token
    store = eumdac.DataStore(get_token())
    cols = config.CLOUDS["collections"]
    with tempfile.TemporaryDirectory() as d:
        # ---- CLM cloud_state enum -----------------------------------------
        p = fetch._search_latest(store, cols["clm"])
        path = fetch._download_nc(p, d)
        nc = netCDF4.Dataset(path)
        var = nc.variables["cloud_state"]
        print("cloud_state dtype:", var.datatype)
        try:
            print("ENUM (name -> value):", dict(var.datatype.enum_dict))
        except Exception as e:
            print("no enum_dict:", e)
        print("attrs:", {k: var.getncattr(k) for k in var.ncattrs()})
        vals, counts = np.unique(np.asarray(var[:]).ravel(), return_counts=True)
        tot = counts.sum()
        print("whole-disk value counts:",
              {int(v): f"{100*c/tot:.1f}%" for v, c in zip(vals, counts)})
        nc.close()

        # ---- CTTH effective_cloudiness fill/scale -------------------------
        pc = fetch._search_latest(store, cols["ctth"])
        ncc = netCDF4.Dataset(fetch._download_nc(pc, d))
        if "effective_cloudiness" in ncc.variables:
            ev = ncc.variables["effective_cloudiness"]
            print("\neffective_cloudiness dtype:", ev.dtype,
                  "attrs:", {k: ev.getncattr(k) for k in ev.ncattrs()})
        ncc.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
