"""MTG FCI CLM `cloud_state` classification by meaning, read from the file.

The integer<->category mapping is stored as a netCDF4 enum inside each CLM
file and is not hardcoded publicly. The five official categories: cloud-free,
cloud contaminated (partial / semi-transparent), cloud filled (opaque),
snow/ice contaminated, undefined/non-processed. Dust and volcanic ash are
separate flags, not cloud_state values.

Each pixel is classified by the name of its enum category
(cloud_state.datatype.enum_dict, or the flag_values / flag_meanings attrs
satpy/xarray expose), never by an assumed integer; the documented FCI
heritage integers are only the no-enum fallback.

cloud_any = contaminated OR filled — this was the original bug. Optically
thin cirrus lands in "contaminated" and is cloud; presence must keep it.
Whether it blocks the sun is a separate question (clouds/solar.py).

Pure numpy, unit-testable without a live netCDF file.
"""

import numpy as np

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
    """Keep a cloudy pixel only if at least min_neighbors of its 8 neighbours
    are also cloudy. A lone cloudy pixel on the coast is almost always a
    false alarm (the negative coastline effect)."""
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

    With coherence_min_neighbors > 0, isolated cloudy pixels (coastline false
    alarms) are dropped and reclassified as clear, so presence isn't inflated
    by speckle; thin cirrus in coherent regions is kept.
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
