"""Inspect the downloaded ODIM HDF5 polar volume from MeteoGate ORD (hrulj)."""
import h5py
import numpy as np

f = h5py.File("_ord_probe.h5", "r")

w = f["what"].attrs
where = f["where"].attrs
print("object:", w["object"].decode(), "| version:", w["version"].decode())
print("source:", w["source"].decode())
print("nominal time:", w["date"].decode(), w["time"].decode(), "UTC")
print(f"radar site: lat {where['lat']:.4f}, lon {where['lon']:.4f}, alt {where['height']:.0f} m")

# enumerate sweeps
n = 0
for k in sorted(f.keys()):
    if not k.startswith("dataset"):
        continue
    n += 1
    dw = f[k]["where"].attrs
    quants = []
    for dk in f[k]:
        if dk.startswith("data"):
            quants.append(f[k][dk]["what"].attrs["quantity"].decode())
    if n <= 3 or n == 9:
        print(f"  {k}: elev {dw['elangle']:.1f} deg, {int(dw['nbins'])} bins x {int(dw['nrays'])} rays, "
              f"rscale {dw['rscale']:.0f} m, range {dw['rscale']*dw['nbins']/1000:.0f} km | {','.join(sorted(quants))}")
print(f"sweeps total: {n}")

# pull lowest-sweep DBZH and compute simple stats (unpack gain/offset/nodata)
ds = f["dataset1"]
for dk in ds:
    if dk.startswith("data") and ds[dk]["what"].attrs["quantity"] == b"DBZH":
        a = ds[dk]["what"].attrs
        raw = ds[dk]["data"][:]
        gain, offset = a["gain"], a["offset"]
        nodata, undetect = a["nodata"], a["undetect"]
        valid = (raw != nodata) & (raw != undetect)
        dbz = raw[valid] * gain + offset
        print(f"DBZH sweep1: {raw.shape}, valid {valid.mean()*100:.1f}%, "
              f"max {dbz.max():.1f} dBZ, >=20dBZ pixels: {(dbz >= 20).sum()}")
        break

# Budva beam geometry sanity: distance from radar to Budva
BUDVA = (42.2864, 18.8400)
lat0, lon0 = float(where["lat"]), float(where["lon"])
import math
dy = (BUDVA[0] - lat0) * 110.57
dx = (BUDVA[1] - lon0) * 111.32 * math.cos(math.radians(lat0))
d = math.hypot(dx, dy)
# 4/3-earth beam height at Budva range for the lowest elevation
Re = 4.0 / 3.0 * 6371.0
h = math.sqrt(d**2 + Re**2 + 2 * d * Re * math.sin(math.radians(0.5))) - Re + float(where["height"]) / 1000.0
print(f"radar->Budva: {d:.1f} km; 0.5deg beam height over Budva: ~{h*1000:.0f} m")
f.close()
