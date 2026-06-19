"""HARD geolocation/data debug. Run with creds:  python -m clouds.debug_geo

Settles whether the cloud field is correctly geolocated by (1) printing the real
x/y axis order, (2) detecting N/S row order, (3) comparing from_cf vs explicit
+sweep=y, (4) showing cloud_state at Budva + neighbourhood, and (5) printing a
WIDE cloud_state map to eyeball against EUMETView (flip/offset show instantly).
"""
import tempfile

import numpy as np
import pyproj

import config
from clouds import fetch


def _axis_m(arr, h):
    a = np.asarray(arr, dtype="float64")
    return a * h if np.nanmax(np.abs(a)) < 1.5 else a


def main():
    import eumdac
    import xarray as xr
    from clouds.discover import get_token
    store = eumdac.DataStore(get_token())
    cols = config.CLOUDS["collections"]
    with tempfile.TemporaryDirectory() as d:
        p = fetch._search_latest(store, cols["clm"])
        print("CLM product:", p)
        for a in ("sensing_start", "sensing_end"):
            print(f"  {a}:", getattr(p, a, None))
        ds = xr.open_dataset(fetch._download_nc(p, d))
        try:
            gm = fetch._grid_mapping(ds)
            h = float(gm.attrs["perspective_point_height"])
            a_, b_ = gm.attrs["semi_major_axis"], gm.attrs["semi_minor_axis"]
            x = np.asarray(ds["x"].values, "float64"); y = np.asarray(ds["y"].values, "float64")
            print(f"x: first={x[0]:.6f} last={x[-1]:.6f} n={x.size}")
            print(f"y: first={y[0]:.6f} last={y[-1]:.6f} n={y.size}")
            xm, ym = _axis_m(x, h), _axis_m(y, h)
            dx = (xm[-1] - xm[0]) / (len(xm) - 1)
            dy = (ym[-1] - ym[0]) / (len(ym) - 1)

            crs_cf = pyproj.CRS.from_cf(dict(gm.attrs))
            crs_y = pyproj.CRS.from_proj4(
                f"+proj=geos +lon_0=0 +h={h} +a={a_} +b={b_} +sweep=y +units=m +no_defs")
            inv = pyproj.Transformer.from_crs(crs_cf, "EPSG:4326", always_xy=True)

            jc = len(xm) // 2
            lat0 = inv.transform(xm[jc], ym[0])[1]
            latN = inv.transform(xm[jc], ym[-1])[1]
            print(f"row0 lat={lat0:.2f}  lastrow lat={latN:.2f}  -> row0 is "
                  f"{'NORTH' if lat0 > latN else 'SOUTH'}")

            cs = ds["cloud_state"].values
            eff = ds_eff = None
            print("cloud_state shape:", cs.shape)

            def idx(lat, lon, crs):
                X, Y = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
                return int(round((Y - ym[0]) / dy)), int(round((X - xm[0]) / dx))

            for name, la, lo in [("Budva", 42.286, 18.84), ("Rome", 41.9, 12.5),
                                 ("Sarajevo", 43.85, 18.41)]:
                i, j = idx(la, lo, crs_cf)
                iy, jy = idx(la, lo, crs_y)
                lon2, lat2 = inv.transform(xm[j], ym[i])
                print(f"\n{name}: from_cf idx=({i},{j})  +sweep=y idx=({iy},{jy})  "
                      f"recovered=({lat2:.3f},{lon2:.3f})  cloud_state={cs[i, j]}")
                print("  cloud_state 7x7 around it:\n", cs[i-3:i+4, j-3:j+4])

            print("\nWIDE cloud_state map  (rows lat 50->34 N->S, cols lon 6->30 W->E)")
            print("  '.'=1(clear)  'o'=2  '#'=3   (B=Budva ~ 42.3N,18.8E)")
            fwd = pyproj.Transformer.from_crs("EPSG:4326", crs_cf, always_xy=True)
            lats = np.linspace(50, 34, 24); lons = np.linspace(6, 30, 60)
            for la in lats:
                row = ""
                for lo in lons:
                    X, Y = fwd.transform(lo, la)
                    i = int(round((Y - ym[0]) / dy)); j = int(round((X - xm[0]) / dx))
                    if abs(la - 42.3) < 0.4 and abs(lo - 18.8) < 0.25:
                        row += "B"; continue
                    if 0 <= i < cs.shape[0] and 0 <= j < cs.shape[1]:
                        v = int(round(cs[i, j])) if not np.isnan(cs[i, j]) else 0
                        row += {1: ".", 2: "o", 3: "#"}.get(v, " ")
                    else:
                        row += " "
                print("  " + row)
        finally:
            ds.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
