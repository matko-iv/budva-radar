"""Visible / GeoColour map layer.

The MTG L2 cloud retrievals (CLM/OCA) are weak for small, sub-pixel fair-weather
cumulus and over-detect at night, so they make a poor *map*. Instead we draw the
map from EUMETSAT's high-resolution GeoColour RGB — exactly what you see on
EUMETView (view.eumetsat.int) — fetched through its public WMS. Real cumulus show
up because they are bright against the dark sea/land, and clear stays clear. The
Budva point verdict still comes from the COT logic in interpret.py / verdict.py;
this module only produces the picture.

GeoColour is a day/night blended product, so the map stays meaningful after dark
(clouds rendered via IR), though small-cumulus fidelity is best in daylight.

Endpoint + layer confirmed from the EUMETView product viewer:
    https://view.eumetsat.int/productviewer?v=mtg_fd:rgb_geocolour
"""
import datetime
import io
import re
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image, ImageDraw

from clouds import solar

# WMS defaults (overridable via config.CLOUDS["geocolour_wms"/"geocolour_layer"]).
_WMS = "https://view.eumetsat.int/geoserver/wms"
_LAYER = "mtg_fd:rgb_geocolour"


def geocolour_verdict_ok(cfg, loc, when=None):
    """Is the GeoColour RGB a usable cloud proxy for the verdict right now?

    Only by day with the sun high enough: GeoColour brightness reads as false
    "cloud" over sun-glint on the sea, on snow, at low sun and at night (where
    brightness is cloud-top temperature, not albedo). When `geocolour_verdict_day_only`
    is set we require a daytime frame with SZA <= geocolour_max_sza; otherwise the
    caller falls back to the L2 verdict (PDF Section 5)."""
    if not cfg.get("geocolour_verdict_day_only", True):
        return True
    if not when:
        return False
    try:
        sza = solar.solar_zenith_deg(when, loc["lat"], loc["lon"])
    except Exception:
        return False
    return sza <= float(cfg.get("geocolour_max_sza", 70.0))


def latest_time(cfg, timeout=30):
    """Read the GeoColour layer's newest available TIME from GetCapabilities.

    Without a pinned TIME the WMS returns its default frame, which near the
    day/night terminator comes back as a stale, day+night MOSAIC. Pinning the
    latest time gives one coherent, current frame. Returns an ISO time string or
    None if it can't be determined (caller falls back to a clock estimate)."""
    wms = cfg.get("geocolour_wms", _WMS)
    layer = cfg.get("geocolour_layer", _LAYER)
    url = wms + "?" + urllib.parse.urlencode(
        {"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"})
    req = urllib.request.Request(url, headers={"User-Agent": "budva-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        xml = r.read().decode("utf-8", "replace")
    i = xml.find(f"<Name>{layer}</Name>")
    if i < 0:
        return None
    seg = xml[i:]
    end = seg.find("</Layer>")
    if end > 0:
        seg = seg[:end]
    m = re.search(r'name="time"[^>]*>(.*?)</(?:Dimension|Extent)>', seg, re.S)
    if not m:
        return None
    val = m.group(1).strip()
    if "," in val:                       # explicit list -> newest is last
        return val.split(",")[-1].strip()
    if "/" in val:                       # start/end/period -> end is newest
        parts = val.split("/")
        return parts[1].strip() if len(parts) >= 2 else parts[0].strip()
    return val or None


def _fallback_time():
    """Best-effort 'latest' when GetCapabilities is unavailable: current UTC
    minus ~20 min (dissemination latency), floored to a 10-minute slot."""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
    t = t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_time(cfg, time=None):
    """Pick the TIME for GetMap: explicit > newest from GetCapabilities > clock
    estimate. Never raises."""
    if time:
        return time
    try:
        t = latest_time(cfg)
        if t:
            return t
    except Exception:
        pass
    return _fallback_time()


def getmap_url(cfg, width, height, time=None, wms=None, layer=None):
    """Build a WMS 1.3.0 GetMap URL for GeoColour over the config bbox.

    WMS 1.3.0 + EPSG:4326 uses (lat, lon) axis order, so BBOX is
    min_lat,min_lon,max_lat,max_lon (NOT lon-first).
    """
    b = cfg["bbox"]
    wms = wms or cfg.get("geocolour_wms", _WMS)
    layer = layer or cfg.get("geocolour_layer", _LAYER)
    params = {
        "service": "WMS", "version": "1.3.0", "request": "GetMap",
        "layers": layer, "styles": "",
        "crs": "EPSG:4326",
        "bbox": f'{b["lat_min"]},{b["lon_min"]},{b["lat_max"]},{b["lon_max"]}',
        "width": str(int(width)), "height": str(int(height)),
        "format": "image/png", "transparent": "false",
    }
    if time:
        params["time"] = time
    return wms + "?" + urllib.parse.urlencode(params)


def fetch_geocolour(cfg, width=1000, time=None, timeout=30):
    """Fetch the GeoColour RGB for the bbox at the latest available time.

    Returns (PIL RGB Image, time_used). Raises on network error or a non-image
    response (geoserver returns an XML ServiceException on failure) — the caller
    should catch and fall back. Height is derived from the bbox so degrees map
    linearly (plate carree), keeping the lat/lon -> pixel marker math exact.
    """
    b = cfg["bbox"]
    span_lat = b["lat_max"] - b["lat_min"]
    span_lon = b["lon_max"] - b["lon_min"]
    height = max(1, round(width * span_lat / span_lon))
    t = resolve_time(cfg, time)          # pin TIME or we get a stale day/night mosaic
    url = getmap_url(cfg, width, height, time=t)
    req = urllib.request.Request(url, headers={"User-Agent": "budva-radar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read()
    if "image" not in ctype.lower():
        raise RuntimeError(
            f"WMS did not return an image (Content-Type={ctype!r}): {data[:300]!r}")
    return Image.open(io.BytesIO(data)).convert("RGB"), t


def _marker_xy(lat, lon, b, W, H):
    """Pixel (x, y) of a lat/lon in a plate-carree image spanning bbox b
    (north-up: y grows southward)."""
    x = (lon - b["lon_min"]) / (b["lon_max"] - b["lon_min"]) * W
    y = (b["lat_max"] - lat) / (b["lat_max"] - b["lat_min"]) * H
    return x, y


def render_map_png(cfg, loc, out_path, width=1000, time=None, source_image=None):
    """Render the GeoColour map for the bbox with a Budva marker, save to
    out_path, return (width, height) of the written PNG.

    Pass source_image (a PIL Image already covering the bbox) to skip the network
    fetch — used for offline/testing. Otherwise fetches from the EUMETView WMS.
    """
    if source_image is not None:
        img = source_image.convert("RGB")
    else:
        img, _ = fetch_geocolour(cfg, width=width, time=time)
    W, H = img.size
    b = cfg["bbox"]
    draw = ImageDraw.Draw(img, "RGBA")

    # Budva marker: dark halo + white ring + red dot, so it reads on cloud or sea.
    x, y = _marker_xy(loc["lat"], loc["lon"], b, W, H)
    draw.ellipse([x - 8, y - 8, x + 8, y + 8], outline=(0, 0, 0, 140), width=3)
    draw.ellipse([x - 7, y - 7, x + 7, y + 7], outline=(255, 255, 255, 255), width=2)
    draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 60, 60, 255))
    name = loc.get("name", "")
    if name:
        draw.text((x + 11, y - 5), name, fill=(0, 0, 0, 160))
        draw.text((x + 10, y - 6), name, fill=(255, 255, 255, 255))

    draw.text((4, H - 14), "GeoColour \u00a9 EUMETSAT/NASA", fill=(255, 255, 255, 175))
    img.save(out_path)
    return W, H


# --------------------------------------------------------------------------
# GeoColour-driven verdict: read the sky over the point straight off the same
# GeoColour image. Cloud = bright + near-neutral (white/grey); optically-thick
# (sun-blocking) cloud = very bright. No cloud-top height / type — the RGB does
# not carry those — so the verdict is state + %, which is what was wrong before.
# --------------------------------------------------------------------------
def _cloud_masks(rgb, cfg):
    a = np.asarray(rgb, dtype="float64")
    mx = a.max(2)
    sat = mx - a.min(2)
    bmin = cfg.get("geocolour_bright_min", 150)
    smax = cfg.get("geocolour_sat_max", 40)
    tmin = cfg.get("geocolour_thick_min", 205)
    cloud = (mx >= bmin) & (sat <= smax)
    thick = (mx >= tmin) & (sat <= smax)
    return cloud, thick


def budva_sky_from_geocolour(rgb, cfg, loc, radius_km=None):
    """Sky cover over the point from the GeoColour image: fraction of cloud-bright
    pixels in a small disc around it, plus the optically-thick (sun-blocking)
    sub-fraction. Returns {cloudFrac, blockFrac, n}. Uses the SAME bbox->pixel
    mapping as the marker, so the marker sits exactly on what is measured."""
    b = cfg["bbox"]
    W, H = rgb.size
    cloud, thick = _cloud_masks(rgb, cfg)
    radius_km = radius_km or cfg.get("geocolour_sample_km", 6.0)
    lat0, lon0 = loc["lat"], loc["lon"]

    xs = b["lon_min"] + (np.arange(W) + 0.5) / W * (b["lon_max"] - b["lon_min"])
    ys = b["lat_max"] - (np.arange(H) + 0.5) / H * (b["lat_max"] - b["lat_min"])
    LON, LAT = np.meshgrid(xs, ys)
    dlat = (LAT - lat0) * 111.0
    dlon = (LON - lon0) * 111.0 * np.cos(np.radians(lat0))
    within = (dlat * dlat + dlon * dlon) <= radius_km * radius_km
    n = int(within.sum())
    if n == 0:                                   # disc smaller than a pixel: nearest
        x, y = _marker_xy(lat0, lon0, b, W, H)
        yy = int(min(max(y, 0), H - 1)); xx = int(min(max(x, 0), W - 1))
        within = np.zeros((H, W), bool); within[yy, xx] = True
        n = 1
    return {"cloudFrac": float(cloud[within].mean()),
            "blockFrac": float(thick[within].mean()), "n": n}


def geocolour_facts(sky, loc, cfg, motion=None):
    """Minimal verdict-contract facts dict from a GeoColour sky estimate, so the
    existing verdict.interpret / cloud_verdict can render it unchanged."""
    clear_max = cfg.get("frac_clear_max", 0.2)
    frac = round(sky["cloudFrac"], 3)
    block = round(sky["blockFrac"], 3)
    return {
        "locationName": loc.get("name", "Budva"),
        "cloudFracNow": frac,
        "cloudAtLocation": bool(frac > clear_max),
        "skyCoverEff": block,            # sun-blocking = optically-thick (very bright)
        "opaqueFracNow": block,
        "thinVeil": bool(frac > clear_max and block <= clear_max),
        "cloudTypeLabel": None, "heightBand": None, "thickness": None,
        "phase": None, "cloudTopHeightM": None,
        "approaching": False, "clearing": False, "etaMin": None,
        "motionCardinal": (motion or {}).get("direction_cardinal"),
        "motionSpeedKmh": (motion or {}).get("speed_kmh"),
        "sunOutlook": "",
        "source": "GeoColour",
        "sampleRadiusKm": cfg.get("geocolour_sample_km", 6.0),
        "samplePixels": sky["n"],
    }
