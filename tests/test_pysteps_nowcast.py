"""Tests for radar/pysteps_nowcast.py — the pysteps (ANVIL + Lucas-Kanade) rain
nowcast. Proves the things the old cross-correlation / dBZ-trend nowcast could
NOT do: a dense motion field, and GROWTH/DECAY extrapolation (ANVIL) that plain
advection cannot produce (PDF Part B/C).

Needs pysteps + opencv. Run from repo root with the pysteps venv:
    psenv/bin/python tests/test_pysteps_nowcast.py     (exit 0 = pass)
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

try:
    import pysteps  # noqa: F401
    from pysteps import nowcasts
except Exception as e:                                   # pragma: no cover
    print(f"SKIP — pysteps not available ({e}). Install it to run this test.")
    sys.exit(0)

import config  # noqa: E402
from radar import pysteps_nowcast as pn, calibration  # noqa: E402

H = W = 160


def _blob(cx, cy, amp, sig=12):
    yy, xx = np.mgrid[:H, :W]
    return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sig ** 2)))


def _stack(amps, dx=10, cy=80):
    return np.stack([_blob(40 + dx * k, cy, amps[k]) for k in range(4)], 0).astype(float)


def _info():
    cal = calibration.get_calibration("opera")
    lat, lon = config.LOCATION["lat"], config.LOCATION["lon"]
    kmpp, (px, py) = pn.km_per_pixel(cal, lat, lon)
    return {"km_per_px": kmpp, "budva_crop_xy": (80, 80),
            "budva_full_xy": (px, py), "shape": (H, W)}


# --- ANVIL growth/decay vs plain extrapolation -------------------------------
def test_anvil_extrapolates_growth():
    stack = _stack([4, 7, 11, 16])                       # intensifying
    fc, _, method = pn.nowcast_fields(stack, 6, ar_order=2)
    assert method == "anvil"
    obs_last = float(stack[-1].max())
    fc_peak_end = float(fc[-1].max())
    assert fc_peak_end > obs_last * 1.2, \
        f"ANVIL should keep growing past {obs_last:.0f}, got {fc_peak_end:.0f}"


def test_anvil_extrapolates_decay():
    stack = _stack([16, 11, 7, 4])                       # decaying
    fc, _, _ = pn.nowcast_fields(stack, 6, ar_order=2)
    obs_last = float(stack[-1].max())
    assert float(fc[0].max()) < obs_last, \
        f"ANVIL should keep decaying below {obs_last:.0f}, got {float(fc[0].max()):.0f}"


def test_extrapolation_is_variance_preserving():
    # plain semi-Lagrangian advection keeps the peak ~constant (no growth) — the
    # limitation ANVIL fixes.
    stack = _stack([4, 7, 11, 16])
    V = pn.motion_field(stack)
    fc = np.nan_to_num(np.asarray(nowcasts.get_method("extrapolation")(stack[-1], V, 6)))
    peaks = [float(fc[k].max()) for k in range(6)]
    assert max(peaks) - min(peaks) < 2.0, f"extrapolation peak should be flat, got {peaks}"


def test_fewer_frames_falls_back_to_extrapolation():
    two = np.stack([_blob(40, 80, 10), _blob(50, 80, 10)], 0).astype(float)  # 2 < ar+2
    _, _, method = pn.nowcast_fields(two, 4, ar_order=2)
    assert method == "extrapolation"


# --- LINDA (cell-based) for convective scenes --------------------------------
def _peak_x(f):
    return int(np.unravel_index(int(np.nanargmax(np.nan_to_num(f, nan=-1))), f.shape)[1])


def test_linda_tracks_and_grows_a_convective_cell():
    # LINDA detects the cell (blob), advects it and runs a localized ARI model;
    # on an intensifying cell it both GROWS and MOVES it -- what makes it better
    # than ANVIL's cascade for isolated convection.
    stack = _stack([4, 7, 11, 16], dx=4)                 # intensifying, moving east
    fc, _, method = pn.nowcast_fields(stack, 6, ar_order=2, method="linda",
                                      kmperpixel=1.5, timestep_min=5.0)
    assert method == "linda", f"explicit linda should run, got {method}"
    assert float(fc[-1].max()) > float(stack[-1].max()), \
        f"LINDA should grow the cell past {float(stack[-1].max()):.0f}, got {float(fc[-1].max()):.0f}"
    assert _peak_x(fc[-1]) > _peak_x(fc[0]) + 2, "LINDA cell should advect eastward"


def test_auto_picks_linda_when_convective():
    _, _, method = pn.nowcast_fields(_stack([4, 7, 11, 16], dx=4), 4,
                                     method="auto", scenario="convective",
                                     kmperpixel=1.5)
    assert method == "linda", f"convective scene should auto-select LINDA, got {method}"


def test_auto_picks_anvil_when_stratiform():
    _, _, method = pn.nowcast_fields(_stack([8, 8, 8, 8], dx=4), 4,
                                     method="auto", scenario="stratiform")
    assert method == "anvil", f"stratiform scene should auto-select ANVIL, got {method}"


# --- dense motion + product assembly -----------------------------------------
def test_motion_direction_is_eastward():
    stack = _stack([10, 10, 10, 10], dx=8)               # moving +x (east) at 8 px/frame
    V = pn.motion_field(stack)
    wet = stack[-1] > 0.1
    assert float(V[0][wet].mean()) > 5.0, "LK should recover the eastward motion"
    prod = pn.nowcast_product(stack, _info(), "opera", n_leadtimes=6)
    assert prod["motion_kmh"] > 0
    assert 45 <= prod["motion_dir_deg"] <= 135, \
        f"+x advection should read easterly, got {prod['motion_dir_deg']}"


def test_product_has_contract_and_trend():
    prod = pn.nowcast_product(_stack([4, 7, 11, 16], dx=8), _info(), "opera",
                              n_leadtimes=6, timestep_min=5.0)
    for k in ("method", "series", "eta_onset_min", "peak_mmh", "peak_lead_min",
              "trend", "motion_kmh", "motion_cardinal", "now_disc_mmh"):
        assert k in prod, f"missing key {k}"
    assert len(prod["series"]) == 6
    assert prod["series"][0]["lead_min"] == 5.0
    assert prod["trend"] in ("intensifying", "steady", "decaying")


# --- real composite decode + geolocation -------------------------------------
def test_real_opera_decode_and_geoloc():
    gif = ROOT / "docs" / "latest_opera.gif"
    if not gif.exists():
        print("  (skip real-frame decode: docs/latest_opera.gif absent)")
        return
    dbz = pn.decode_dbz(str(gif), "opera")
    assert dbz.ndim == 2 and np.isfinite(dbz).any(), "decode should yield some echo"
    cal = calibration.get_calibration("opera")
    kmpp, (px, py) = pn.km_per_pixel(cal, config.LOCATION["lat"], config.LOCATION["lon"])
    assert 1.0 < kmpp < 12.0, f"km/px {kmpp} implausible"
    assert 0 <= px < dbz.shape[1] and 0 <= py < dbz.shape[0], "Budva pixel off-grid"


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    fails = []
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            fails.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — pysteps ANVIL + LK nowcast OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
