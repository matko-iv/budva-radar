"""Decisive geolocation flip/transpose test. Run with creds:
    python -m clouds.debug_flip

The pixel COORDINATES are correct, but the verdict disagrees with EUMETView, so
the data ARRAY may be ordered differently from the x/y coordinate arrays. For
each landmark (known cloudy/clear from EUMETView), this reads cloud_state at the
computed (i,j) AND at i-flip / j-flip / transpose, and reports which indexing
variant matches reality. Whichever scores best is the correct one.
"""
import tempfile

import numpy as np
import pyproj

import config
from clouds import fetch

# truth from EUMETView GeoColour 17Z: 1 = cloudy, 0 = clear
LANDMARKS = [
    ("Bosnia interior", 44.0, 17.8, 1), ("Split/Dalmatia", 43.5, 16.4, 1),
    ("Sarajevo", 43.85, 18.41, 1), ("Central Adriatic", 43.0, 16.5, 0),
    ("Budva", 42.29, 18.84, 0), ("South Adriatic", 41.6, 18.2, 0),
    ("E Montenegro", 42.6, 19.5, 1), ("Albania SE", 41.3, 20.0, 1),
]


def _product_at(store, col_id, target):
    """Pick the product whose sensing_start is nearest `target` (datetime)."""
    import datetime
    col = store.get_collection(col_id)
    lo = target - datetime.timedelta(minutes=20)
    hi = target + datetime.timedelta(minutes=20)
    prods = list(col.search(dtstart=lo, dtend=hi))
    if not prods:
        return None
    def key(p):
        t = getattr(p, "sensing_start", None)
        try:
            t = t if hasattr(t, "year") else datetime.datetime.fromisoformat(str(t))
            return abs((t - target).total_seconds())
        except Exception:
            return 1e9
    return min(prods, key=key)


def main():
    import datetime
    import eumdac
    import xarray as xr
    from clouds.discover import get_token
    store = eumdac.DataStore(get_token())
    # Match the user's EUMETView truth screenshot time (17:00 UTC, 2026-06-19).
    target = datetime.datetime(2026, 6, 19, 17, 0, 0)
    with tempfile.TemporaryDirectory() as d:
        p = _product_at(store, config.CLOUDS["collections"]["clm"], target)
        if p is None:
            print("no product near", target); return 1
        print("frame:", getattr(p, "sensing_start", "?"), "(target 17:00Z)")
        ds = xr.open_dataset(fetch._download_nc(p, d))
        try:
            gm = fetch._grid_mapping(ds)
            h = float(gm.attrs["perspective_point_height"])
            x = np.asarray(ds["x"].values, "float64") * h
            y = np.asarray(ds["y"].values, "float64") * h
            dx = (x[-1] - x[0]) / (len(x) - 1); dy = (y[-1] - y[0]) / (len(y) - 1)
            crs = pyproj.CRS.from_cf(dict(gm.attrs))
            tf = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            cs = np.asarray(ds["cloud_state"].values)
            N = cs.shape[0]

            def cloudy(v):
                v = int(round(v)) if not np.isnan(v) else 0
                return 1 if v == 3 else 0   # OPAQUE (cloud filled) only = what GeoColour shows

            variants = {"normal": lambda i, j: cs[i, j],
                        "i-flip (N-S)": lambda i, j: cs[N - 1 - i, j],
                        "j-flip (E-W)": lambda i, j: cs[i, N - 1 - j],
                        "both-flip": lambda i, j: cs[N - 1 - i, N - 1 - j],
                        "transpose": lambda i, j: cs[j, i]}
            scores = {k: 0 for k in variants}
            print(f"\n{'landmark':16} truth | " + " ".join(f"{k:12}" for k in variants))
            for name, la, lo, truth in LANDMARKS:
                X, Y = tf.transform(lo, la)
                j = int(round((X - x[0]) / dx)); i = int(round((Y - y[0]) / dy))
                row = f"{name:16} {truth:5} | "
                for k, fn in variants.items():
                    try:
                        c = cloudy(fn(i, j))
                    except Exception:
                        c = -1
                    scores[k] += (c == truth)
                    row += f"{('CLD' if c==1 else 'clr' if c==0 else '?'):12} "
                print(row)
            print("\nMatch scores (/8):", {k: scores[k] for k in variants})
            best = max(scores, key=scores.get)
            print("BEST indexing:", best, "->", scores[best], "/8")
        finally:
            ds.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
