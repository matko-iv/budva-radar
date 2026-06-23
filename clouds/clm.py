"""MTG FCI CLM `cloud_state` classification — by MEANING, read from the file.

The PDF (Section 2.1 + Caveats): the integer<->category mapping is stored as a
netCDF4 ENUM inside each CLM file and is NOT hardcoded publicly. The five official
categories are: cloud-free, cloud contaminated (partial / semi-transparent),
cloud filled (opaque), snow/ice contaminated, undefined/non-processed. Dust and
volcanic ash are SEPARATE flags, NOT cloud_state values 4-7.

So we read the enum from the file (`cloud_state.datatype.enum_dict`, or the
`flag_values` / `flag_meanings` attributes satpy/xarray expose) and classify each
pixel by the NAME of its category, never by an assumed integer. Only when a file
carries no enum do we fall back to the documented FCI heritage integers.

CRITICAL (the original bug): `cloud_any` = contaminated OR filled. Optically thin
cirrus lands in "contaminated" and IS cloud — presence must keep it. Whether it
blocks the sun is a SEPARATE question (clouds/solar.py + OCA COT), never a reason
to drop it from the cloud mask.

Pure numpy so it is unit-testable without a live netCDF file.
"""

import numpy as np

# Documented FCI heritage integers — used ONLY when a file carries no enum.
HERITAGE_ENUM = {
    "no_data": 0, "cloud_free": 1, "cloud_contaminated": 2, "cloud_filled": 3,
    "snow_ice_contaminated": 8, "undefined": 9,
}


def enum_from_attrs(attrs):
    """Build a {name: value} enum from a variable's flag_values / flag_meanings
    attributes (how satpy/xarray surface a netCDF enum). Returns {} if absent."""
    attrs = attrs or {}
    values = attrs.get("flag_values")
    meanings = attrs.get("flag_meanings")
    if values is None or not meanings:
        return {}
    names = meanings.split() if isinstance(meanings, str) else list(meanings)
    vals = list(np.asarray(values).ravel())
    return {str(n): int(v) for n, v in zip(names, vals)}


def _category_of(name):
    """Map an enum entry NAME to one of our coarse categories (or None)."""
    n = name.lower()
    if "contaminat" in n and ("snow" in n or "ice" in n):
        return "snow_ice"
    if "snow" in n or "ice" in n:
        return "snow_ice"
    if "contaminat" in n:
        return "contaminated"
    if "fill" in n:
        return "filled"
    if "free" in n or "clear" in n:
        return "clear"
    if "no_data" in n or "no-data" in n or "nodata" in n or "non_proc" in n \
            or "non-proc" in n or "undefined" in n or "undef" in n:
        return "nodata"
    return None


def spatial_coherence(mask, min_neighbors=2):
    """N-adjacent spatial-coherence filter (PDF Part A1): keep a cloudy pixel
    only if at least `min_neighbors` of its 8 neighbours are also cloudy. A lone
    cloudy pixel adjacent to the coast is almost always a false alarm (the
    'negative coastline effect'); requiring more than one cloudy pixel removes it.
    Pure numpy (3x3 neighbour count via padded shifts)."""
    m = np.asarray(mask, dtype=bool)
    if not min_neighbors or min_neighbors <= 0:
        return m.copy()
    p = np.pad(m.astype(np.int16), 1)
    neigh = (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:]
             + p[1:-1, :-2] + p[1:-1, 2:]
             + p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:])
    return m & (neigh >= int(min_neighbors))


def categorize(codes, enum_dict=None, coherence_min_neighbors=0):
    """Classify a CLM `cloud_state` array into boolean masks by meaning.

    Returns a dict of boolean arrays: nodata, clear, contaminated, filled,
    snow_ice, plus cloud_any (= contaminated | filled). NaN -> nodata.

    When `coherence_min_neighbors` > 0, applies the N-adjacent spatial-coherence
    filter (PDF Part A1) to cloud_any: isolated cloudy pixels (coastline false
    alarms) are dropped and reclassified as clear, so the PRESENCE number is not
    inflated by speckle. Thin cirrus in coherent regions is still kept.
    """
    arr = np.asarray(codes, dtype="float64")
    enum = enum_dict or HERITAGE_ENUM

    nodata = np.isnan(arr)
    clear = np.zeros(arr.shape, dtype=bool)
    contaminated = np.zeros(arr.shape, dtype=bool)
    filled = np.zeros(arr.shape, dtype=bool)
    snow_ice = np.zeros(arr.shape, dtype=bool)
    masks = {"clear": clear, "contaminated": contaminated, "filled": filled,
             "snow_ice": snow_ice, "nodata": nodata}

    for name, value in enum.items():
        cat = _category_of(name)
        if cat is None:
            continue
        masks[cat] |= (arr == value)

    if coherence_min_neighbors and coherence_min_neighbors > 0:
        coherent = spatial_coherence(contaminated | filled, coherence_min_neighbors)
        dropped = (contaminated | filled) & ~coherent
        contaminated = contaminated & coherent
        filled = filled & coherent
        clear = clear | dropped            # isolated false cloud -> clear

    return {
        "nodata": nodata,
        "clear": clear,
        "contaminated": contaminated,
        "filled": filled,
        "snow_ice": snow_ice,
        "cloud_any": contaminated | filled,
    }
