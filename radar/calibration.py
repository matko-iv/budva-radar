"""Pixel <-> lat/lon mapping for radar images.

An affine transform is fitted from >= 3 known city landmarks. Good enough for
the DHMZ Adriatic image; OPERA's Europe-wide projection is stereographic, so
affine drifts at the edges but stays accurate around Budva. Budva is the
anchor: after fitting, a translation forces it onto its verified pixel.
"""

import numpy as np

CITIES_LATLON = {
    "Split":      (43.5081, 16.4402),
    "Dubrovnik":  (42.6507, 18.0944),
    "Pula":       (44.8666, 13.8496),
    "Rijeka":     (45.3271, 14.4422),
    "Zagreb":     (45.8150, 15.9819),
    "Budva":      (42.2864, 18.8400),
    "Podgorica":  (42.4304, 19.2594),
    "Sarajevo":   (43.8563, 18.4131),
    "Mostar":     (43.3438, 17.8078),
    "Bari":       (41.1171, 16.8719),
    "Pescara":    (42.4584, 14.2081),
    "Venice":     (45.4408, 12.3155),
    "Tirana":     (41.3275, 19.8187),
    "Vlore":      (40.4686, 19.4914),
    "Berlin":     (52.5200, 13.4050),
    "Paris":      (48.8566,  2.3522),
    "Madrid":     (40.4168, -3.7038),
    "London":     (51.5074, -0.1278),
    "Stockholm":  (59.3293, 18.0686),
    "Athens":     (37.9838, 23.7275),
    "Helsinki":   (60.1699, 24.9384),
    "Rome":       (41.9028, 12.4964),
}

# Pixel positions measured by hand off the source images (neither DHMZ nor FMI
# publish projection strings). Re-derive via `python -m radar.calibration`.
PIXEL_LANDMARKS_DHMZ = {
    "Split":     (217, 292),
    "Dubrovnik": (400, 425),
    "Sarajevo":  (434, 241),
    "Podgorica": (532, 455),
    "Budva":     (485, 480),
}

PIXEL_LANDMARKS_OPERA = {
    "London":    (312, 610),
    "Paris":     (349, 686),
    "Madrid":    (189, 907),
    "Berlin":    (537, 595),
    "Rome":      (537, 887),
    "Athens":    (779, 967),
    "Budva":     (669, 866),
}


class AffineCalibration:
    """Least-squares affine fit (lat, lon) <-> (px, py).

    With `anchor` set, a translation is added after the fit so the anchor
    city maps exactly onto its landmark pixel; the fit still supplies the
    local geometry around it.
    """

    def __init__(self, landmarks_px: dict, latlon: dict, anchor: str = None):
        names = sorted(set(landmarks_px.keys()) & set(latlon.keys()))
        if len(names) < 3:
            raise ValueError(f"Need at least 3 landmarks, got {len(names)}: {names}")
        src = np.array([[latlon[n][1], latlon[n][0]] for n in names])  # lon, lat
        dst = np.array([landmarks_px[n] for n in names])
        A_in = np.hstack([src, np.ones((len(src), 1))])
        self._fwd, *_ = np.linalg.lstsq(A_in, dst, rcond=None)
        A_in2 = np.hstack([dst, np.ones((len(dst), 1))])
        self._inv, *_ = np.linalg.lstsq(A_in2, src, rcond=None)
        self.landmarks = names
        pred = A_in @ self._fwd
        self.fit_rmse_px = float(np.sqrt(np.mean((pred - dst) ** 2)))

        self._dx, self._dy = 0.0, 0.0
        if anchor and anchor in landmarks_px and anchor in latlon:
            lat_a, lon_a = latlon[anchor]
            px_a, py_a = landmarks_px[anchor]
            fit_x, fit_y = self.latlon_to_pixel(lat_a, lon_a)
            self._dx = px_a - fit_x
            self._dy = py_a - fit_y
            self.anchor = anchor

    def latlon_to_pixel(self, lat: float, lon: float) -> tuple:
        v = np.array([lon, lat, 1.0]) @ self._fwd
        return float(v[0] + getattr(self, "_dx", 0)), float(v[1] + getattr(self, "_dy", 0))

    def pixel_to_latlon(self, px: float, py: float) -> tuple:
        px_adj = px - getattr(self, "_dx", 0)
        py_adj = py - getattr(self, "_dy", 0)
        v = np.array([px_adj, py_adj, 1.0]) @ self._inv
        return float(v[1]), float(v[0])


_CALIB_CACHE = {}


def get_calibration(source: str) -> AffineCalibration:
    """Cached Budva-anchored calibration for 'dhmz' or 'opera'."""
    if source in _CALIB_CACHE:
        return _CALIB_CACHE[source]
    if source == "dhmz":
        pl = PIXEL_LANDMARKS_DHMZ
    elif source == "opera":
        pl = PIXEL_LANDMARKS_OPERA
    else:
        raise KeyError(f"Unknown source: {source}")
    cal = AffineCalibration(pl, CITIES_LATLON, anchor="Budva")
    _CALIB_CACHE[source] = cal
    return cal


EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    r1, r2 = np.radians(lat1), np.radians(lat2)
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(r1) * np.cos(r2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Bearing from point 1 to point 2; 0 = N, 90 = E, range [0, 360)."""
    r1, r2 = np.radians(lat1), np.radians(lat2)
    dlon = np.radians(lon2 - lon1)
    y = np.sin(dlon) * np.cos(r2)
    x = np.cos(r1) * np.sin(r2) - np.sin(r1) * np.cos(r2) * np.cos(dlon)
    return (np.degrees(np.arctan2(y, x)) + 360) % 360


def bearing_to_cardinal(deg: float) -> str:
    deg = float(deg) % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) // 45) % 8]


def _self_test():
    for src in ("dhmz", "opera"):
        cal = get_calibration(src)
        bx, by = cal.latlon_to_pixel(*config.LOCATION_LATLON)  # noqa
        print(f"[{src}] fit RMSE = {cal.fit_rmse_px:.2f} px  "
              f"({len(cal.landmarks)} landmarks)")
        print(f"  Budva pixel: ({bx:.1f}, {by:.1f})")


if __name__ == "__main__":
    import config
    config.LOCATION_LATLON = (config.LOCATION["lat"], config.LOCATION["lon"])
    _self_test()
