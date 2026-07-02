"""Dump the structure of your ODIM HDF5 files so we can wire up the right reader.

Your hrulj 2023 files are evidently 2-D Cartesian PRODUCT images (filenames like
.MAX. = column-max reflectivity, .RN1. = 1 h rain accumulation), NOT the polar
PVOL volumes radar/ord.py expects -- that's why `where["lat"]` is missing. This
prints each product type's groups, attributes, dataset shape and value range so I
can write a reader that georeferences them onto a Budva-centred 256x256 1 km tile.

Usage:
    python inspect_h5.py "path\\to\\one_file.h5"
    python inspect_h5.py "path\\to\\folder"     # one sample per product token

Only needs h5py + numpy. Paste the whole output back.
"""
import glob
import os
import sys

import numpy as np

try:
    import h5py
except Exception as e:                       # pragma: no cover
    print("need h5py:", e); sys.exit(1)


def fmt(v):
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    if isinstance(v, np.ndarray):
        return f"ndarray shape={v.shape} dtype={v.dtype} -> {v.ravel()[:8]}"
    return v


def dump(name, obj, indent=1):
    pad = "  " * indent
    if isinstance(obj, h5py.Group):
        print(f"{pad}{name}/  (group)")
        for k, v in obj.attrs.items():
            print(f"{pad}    .{k} = {fmt(v)}")
        for k in obj.keys():
            dump(k, obj[k], indent + 1)
    else:
        print(f"{pad}{name}  dataset shape={obj.shape} dtype={obj.dtype}")
        for k, v in obj.attrs.items():
            print(f"{pad}    .{k} = {fmt(v)}")
        try:
            a = obj[()]
            if isinstance(a, np.ndarray) and a.size:
                print(f"{pad}    raw min={np.min(a)} max={np.max(a)} "
                      f"mean={np.mean(a):.3f}")
        except Exception:
            pass


def inspect(path):
    print("=" * 72)
    print("FILE:", os.path.basename(path), f"({os.path.getsize(path)} bytes)")
    print("=" * 72)
    with h5py.File(path, "r") as f:
        for grp in ("what", "where", "how"):
            if grp in f:
                print(f"/{grp}:")
                for k, v in f[grp].attrs.items():
                    print(f"    .{k} = {fmt(v)}")
        for k in sorted(f.keys()):
            if k.startswith("dataset"):
                dump(k, f[k], 1)
    print()


def main():
    if len(sys.argv) < 2:
        print("usage: python inspect_h5.py <file.h5 | folder>")
        return 1
    p = sys.argv[1]
    if os.path.isdir(p):
        files = sorted(glob.glob(os.path.join(p, "**", "*.h5"), recursive=True))
        if not files:
            print("no .h5 under", p)
            return 1
        # one sample per product token (the 3rd dot-field: hrulj.<date>.<TOKEN>.<n>.h5)
        seen = {}
        for fp in files:
            parts = os.path.basename(fp).split(".")
            tok = parts[2] if len(parts) > 2 else "?"
            seen.setdefault(tok, fp)
        print(f"{len(files)} files; product tokens found: {sorted(seen)}\n")
        for tok, fp in sorted(seen.items()):
            print(f"\n##################  product '{tok}'  ##################")
            inspect(fp)
    else:
        inspect(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())