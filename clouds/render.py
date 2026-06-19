"""Render a normalized cloud field to a preview PNG: a topographic basemap of the
region with the cloud field composited on top (clear/thin -> map shows through,
overcast/thick -> opaque white/grey). The display layer for cloud-map.html,
analogous to docs/latest_dhmz.png for the radar.

The basemap is a static WMS image for the exact bbox (terrestris OSM, no key),
fetched once and cached in docs/ so repeated runs / CI don't refetch. Both the
basemap and the cloud grid are equal-degree (plate carrée) over the same bbox,
so they align 1:1 and the Budva marker placement in cloud-map.html stays correct.
"""

import hashlib
import io
from pathlib import Path

import numpy as np
import requests
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "docs"

_BASEMAP_WMS = "https://ows.terrestris.de/osm/service"
_BASEMAP_LAYER = "TOPO-OSM-WMS"          # topo relief + coastline + place names
_HEADERS = {"User-Agent": "budva-radar-clouds/0.1 (cloud basemap)"}

_SKY = np.array([150, 197, 255], dtype=float)     # fallback clear background
_CLOUD = np.array([250, 250, 250], dtype=float)    # bright (thin) cloud
_THICK = np.array([150, 156, 165], dtype=float)    # grey (thick / dense) cloud


def _bbox_of(field):
    return {"lon_min": float(field.lons.min()), "lon_max": float(field.lons.max()),
            "lat_min": float(field.lats.min()), "lat_max": float(field.lats.max())}


def _basemap(bbox, size):
    """Fetch + cache a topographic basemap for the bbox at the given pixel size.
    Returns an RGB PIL image, or None if the service is unreachable."""
    w, h = size
    key = hashlib.sha1(
        f"{bbox['lon_min']},{bbox['lat_min']},{bbox['lon_max']},{bbox['lat_max']}|"
        f"{w}x{h}|{_BASEMAP_LAYER}".encode()).hexdigest()[:10]
    cache = DOCS_DIR / f"cloud_basemap_{key}.png"
    if cache.exists():
        try:
            return Image.open(cache).convert("RGB")
        except Exception:
            pass
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": _BASEMAP_LAYER, "STYLES": "", "SRS": "EPSG:4326",
        # WMS 1.1.1 EPSG:4326 bbox order = minx,miny,maxx,maxy (lon,lat)
        "BBOX": f"{bbox['lon_min']},{bbox['lat_min']},{bbox['lon_max']},{bbox['lat_max']}",
        "WIDTH": w, "HEIGHT": h, "FORMAT": "image/png",
    }
    try:
        r = requests.get(_BASEMAP_WMS, params=params, headers=_HEADERS, timeout=40)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        DOCS_DIR.mkdir(exist_ok=True)
        img.save(cache)
        return img
    except Exception as e:
        print(f"  [clouds.render] basemap fetch failed ({e}); plain background")
        return None


def to_png(field, path, scale=4):
    """Composite the cloud field over a topographic basemap, north-up."""
    frac = field.layers.get("frac")
    if frac is None:
        frac = field.layers["mask"]
    H, W = frac.shape
    f = np.clip(np.nan_to_num(frac, nan=0.0), 0.0, 1.0)

    cot = field.layers.get("cot")
    thickness = (np.clip(np.nan_to_num(cot, nan=0.0) / 20.0, 0.0, 1.0)
                 if cot is not None else np.zeros_like(f))
    cloud_col = _CLOUD * (1.0 - thickness[..., None]) + _THICK * thickness[..., None]

    # cloud opacity = cloud fraction; transparent where clear or no-data
    alpha = f.copy()
    alpha[np.isnan(frac)] = 0.0

    if field.lats[0] < field.lats[-1]:           # ensure north is on top
        cloud_col = cloud_col[::-1]
        alpha = alpha[::-1]

    out_w, out_h = W * scale, H * scale
    cloud_img = Image.fromarray(cloud_col.astype("uint8"), "RGB").resize(
        (out_w, out_h), Image.BILINEAR)
    alpha_img = Image.fromarray((alpha * 255).astype("uint8"), "L").resize(
        (out_w, out_h), Image.BILINEAR)

    base = _basemap(_bbox_of(field), (out_w, out_h))
    if base is None:
        base = Image.new("RGB", (out_w, out_h), tuple(_SKY.astype(int)))
    base = base.convert("RGB")
    base.paste(cloud_img, (0, 0), alpha_img)     # alpha-composite clouds over map
    base.save(path)
    return path
