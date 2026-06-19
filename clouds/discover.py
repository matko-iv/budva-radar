"""Step-0 discovery: pin the LIVE EUMETSAT Data Store collection ids + the
variable names of one cloud product, so config.CLOUDS["collections"] and the
fetch.py normalization can be set against reality instead of guesses.

Prerequisites: a free EUMETSAT EO Portal account -> consumer key + secret,
provided via env vars EUMETSAT_KEY / EUMETSAT_SECRET (or ~/.eumdac/credentials).

Usage (from repo root):
    python -m clouds.discover                 # list cloud-related collections
    python -m clouds.discover EO:EUM:DAT:0662  # inspect that collection's latest product
"""

import os
import sys

# Keywords that flag the collections we care about (cloud mask / OCA / imager).
_KEYWORDS = ("cloud", "clm", "oca", "optimal cloud", "fci", "seviri", "mask",
             "cma", "ct ", "cloud type", "cloud top")


def get_token():
    import eumdac
    key = os.environ.get("EUMETSAT_KEY")
    secret = os.environ.get("EUMETSAT_SECRET")
    if key and secret:
        return eumdac.AccessToken((key, secret))
    # eumdac can also read credentials from ~/.eumdac/credentials
    try:
        return eumdac.AccessToken()
    except Exception as e:
        raise SystemExit(
            "No EUMETSAT credentials. Set EUMETSAT_KEY / EUMETSAT_SECRET "
            "(or run `eumdac set-credentials`).\n" + str(e))


def list_collections():
    import eumdac
    store = eumdac.DataStore(get_token())
    print("Cloud-relevant collections (id  —  title):\n")
    n = 0
    for col in store.collections:
        try:
            title = str(col.title)
        except Exception:
            title = ""
        hay = f"{col} {title}".lower()
        if any(k in hay for k in _KEYWORDS):
            print(f"  {col}  —  {title}")
            n += 1
    print(f"\n{n} matching collection(s). Pin the CLM + OCA ids into "
          f"config.CLOUDS['collections'].")


def inspect(collection_id):
    """Download the latest product of a collection and print its variables, so
    fetch.py's normalization can map them to the normalized layers."""
    import eumdac
    from clouds.fetch import latest_product
    store = eumdac.DataStore(get_token())
    col = store.get_collection(collection_id)
    latest = latest_product(col)
    if latest is None:
        print(f"No recent products found in {collection_id}.")
        return
    print(f"Latest product: {latest}")
    print("Entries:")
    for e in (latest.entries or []):
        print("  ", e)

    # Try to open + read variables with xarray (works for netCDF entries).
    nc = next((e for e in (latest.entries or []) if str(e).endswith((".nc", ".nc4"))),
              None)
    if nc is None:
        print("\nNo .nc entry to introspect; inspect entries above manually.")
        return
    import tempfile
    import numpy as np
    import xarray as xr
    with tempfile.TemporaryDirectory() as d:
        local = os.path.join(d, os.path.basename(str(nc)))
        with latest.open(entry=str(nc)) as fsrc, open(local, "wb") as fdst:
            fdst.write(fsrc.read())
        ds = xr.open_dataset(local)
        try:
            print("\nVariables:")
            for v in ds.variables:
                var = ds[v]
                print(f"  {v}  dims={var.dims}  units={var.attrs.get('units', '')}  "
                      f"long_name={var.attrs.get('long_name', '')}")

            # grid_mapping (projection definition) attributes
            for gm in ("mtg_geos_projection", "geostationary", "projection", "mtg_geos"):
                if gm in ds.variables:
                    print(f"\ngrid_mapping '{gm}' attrs:")
                    for k, val in ds[gm].attrs.items():
                        print(f"  {k} = {val}")
                    break

            # x / y coordinate units + range (radians vs metres)
            for c in ("x", "y"):
                if c in ds.variables:
                    a = ds[c]
                    vals = np.asarray(a.values, dtype="float64")
                    print(f"\ncoord {c}: units='{a.attrs.get('units', '')}' dtype={a.dtype} "
                          f"min={np.nanmin(vals):.6g} max={np.nanmax(vals):.6g} n={vals.size}")

            # data-var attrs that drive normalization (flags / scale / fill)
            print("\nData-var attrs (flags/scale/fill):")
            for v in ds.data_vars:
                a = ds[v]
                if not a.dims:
                    continue
                keys = ("flag_values", "flag_meanings", "scale_factor", "add_offset",
                        "_FillValue", "units", "standard_name")
                shown = {k: a.attrs[k] for k in keys if k in a.attrs}
                if shown:
                    print(f"  {v}: {shown}")
        finally:
            ds.close()


def main(argv):
    if len(argv) > 1:
        inspect(argv[1])
    else:
        list_collections()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
