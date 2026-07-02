"""Verification harness: the quantitative model comparison.

Given a time-ordered archive of ORD ODIM HDF5 frames, hindcast each model from
every origin time t and score against the observed frames at t+lead, using
pysteps verification: FSS at neighbourhood scales 2-32 km (displacement-
tolerant, unlike pixel-exact detection) and CSI/POD/FAR vs lead time at a rain
threshold.

Models: extrapolation (baseline), LINDA-D (headline), and DGMR when installed.
Writes output/verify.json + docs/verify_data.js, rendered as a skill table by
docs/nowcast-compare.html. This is the real LINDA-vs-DGMR accuracy answer;
the side-by-side maps are qualitative.

    python verify_nowcast.py --h5 archive/*.h5
    python verify_nowcast.py --h5 archive/*.h5 --leads 12 --stride 3 --max-origins 200

When FSS at the smallest useful scale falls below ~0.5 at a lead, that lead is
unskillful — stop alerting on it.
"""

import datetime
import json
import sys
from pathlib import Path

import config

BASE = Path(__file__).resolve().parent
DOCS = BASE / "docs"
OUT_JS = DOCS / "verify_data.js"
OUT_JSON = BASE / "output" / "verify.json"

N_INPUT = 4                       # frames used to seed a nowcast
DEFAULT_LEADS = 6                 # 6 x 5 min = 30 min (raise for longer archives)
SCALES_KM = [2, 4, 8, 16, 32]     # FSS neighbourhood sizes (1 km/px -> px == km)
FSS_THR_MMH = 1.0                 # rain threshold for FSS + CSI/POD/FAR
TILE = 256


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0, tzinfo=None).isoformat() + "Z"


def _load_tiles(paths):
    """ORD H5 archive -> (R [T,256,256] mm/h on the Budva tile, cal, timestep_min,
    scenario). All frames share one Budva-centred 256x256 / 1 km geometry."""
    import numpy as np
    from radar import ord as ordmod, pysteps_nowcast as pn, colormap
    from compare_nowcast import _force_tile
    grids = [ordmod.load_grid(p) for p in paths]
    cal = grids[-1]["cal"]
    loc = config.LOCATION
    mx = max((float(np.nanmax(g["dbz"])) for g in grids
              if np.isfinite(g["dbz"]).any()), default=None)
    scenario = colormap.pick_zr_scenario(mx)        # one Z-R for the whole archive
    R, info = pn.build_rainrate_stack_from_grids(
        [g["dbz"] for g in grids], cal, grids[-1]["km_per_px"],
        loc["lat"], loc["lon"], half_km=140.0, scenario=scenario)
    R, info = _force_tile(R, info, cal)
    ts = [g.get("nominal_utc") for g in grids if g.get("nominal_utc")]
    dt = 5.0
    if len(ts) >= 2:
        deltas = [(ts[i + 1] - ts[i]).total_seconds() / 60.0 for i in range(len(ts) - 1)]
        deltas = [d for d in deltas if 0 < d < 60]
        if deltas:
            dt = float(np.median(deltas))
    return R, info, cal, dt, scenario


def _forecasts(seed, info, velocity, n_leads):
    """Run each available model on one seed stack -> {key: (label, fc)}."""
    import numpy as np
    from radar import pysteps_nowcast as pn, dgmr_adapter as dg
    out = {}
    for key, req, label in (("extrapolation", "extrapolation", "Ekstrapolacija"),
                            ("linda", "linda", "LINDA-D")):
        fc, _, m = pn.nowcast_fields(seed, n_leads, velocity=velocity, method=req,
                                     scenario=info["scenario"], kmperpixel=1.0)
        out[key] = (label, np.asarray(fc))
    dgmr_fc, _ = dg.forecast(seed, info, n_leads)
    if dgmr_fc is not None:
        out["dgmr"] = ("DGMR", np.asarray(dgmr_fc))
    return out


def verify(paths, n_leads=DEFAULT_LEADS, stride=1, max_origins=None):
    import numpy as np
    from pysteps import verification as V
    from radar import pysteps_nowcast as pn
    fss = V.get_method("FSS")
    R, info, cal, dt, scenario = _load_tiles(paths)
    T = R.shape[0]
    origins = [i for i in range(N_INPUT - 1, T - n_leads)][::stride]
    if max_origins:
        origins = origins[:max_origins]
    if not origins:
        raise RuntimeError(f"need >= {N_INPUT + n_leads} frames; got {T}")

    # accumulators: per model/key -> per lead -> sums
    acc = {}

    def _slot(key, label):
        if key not in acc:
            acc[key] = {"label": label,
                        "fss": [[0.0] * len(SCALES_KM) for _ in range(n_leads)],
                        "csi": [0.0] * n_leads, "pod": [0.0] * n_leads,
                        "far": [0.0] * n_leads, "n": [0] * n_leads}
        return acc[key]

    for oi in origins:
        seed = R[oi - N_INPUT + 1:oi + 1]
        obs = R[oi + 1:oi + 1 + n_leads]
        velocity = pn.motion_field(seed)
        for key, (label, fc) in _forecasts(seed, info, velocity, n_leads).items():
            s = _slot(key, label)
            for k in range(min(n_leads, fc.shape[0], obs.shape[0])):
                f2, o2 = fc[k], obs[k]
                if not (np.isfinite(f2).any() and np.isfinite(o2).any()):
                    continue
                for si, sc in enumerate(SCALES_KM):
                    s["fss"][k][si] += float(fss(f2, o2, FSS_THR_MMH, sc))
                cat = V.det_cat_fct(f2, o2, FSS_THR_MMH, scores=["CSI", "POD", "FAR"])
                s["csi"][k] += float(cat["CSI"]); s["pod"][k] += float(cat["POD"])
                s["far"][k] += float(cat["FAR"]); s["n"][k] += 1

    models = []
    for key, s in acc.items():
        by_lead = []

        def _mean(x, n):
            v = x / n
            return None if (v != v) else round(v, 3)        # nan -> null for the page

        for k in range(n_leads):
            n = max(s["n"][k], 1)
            by_lead.append({
                "lead_min": round((k + 1) * dt, 1),
                "fss": [_mean(s["fss"][k][si], n) for si in range(len(SCALES_KM))],
                "csi": _mean(s["csi"][k], n), "pod": _mean(s["pod"][k], n),
                "far": _mean(s["far"][k], n), "n": s["n"][k]})
        models.append({"key": key, "label": s["label"], "by_lead": by_lead})
    # headline first
    order = {"linda": 0, "dgmr": 1, "extrapolation": 2}
    models.sort(key=lambda m: order.get(m["key"], 9))
    return {
        "ok": True, "generated": _utc_now(), "scenario": scenario,
        "n_cases": len(origins), "timestep_min": dt, "scales": SCALES_KM,
        "fss_threshold_mmh": FSS_THR_MMH, "leads": n_leads, "models": models,
    }


def write(data):
    DOCS.mkdir(exist_ok=True); OUT_JSON.parent.mkdir(exist_ok=True)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("// Auto-generated by verify_nowcast.py; do not edit by hand.\n")
        f.write("window.VERIFY_DATA = ")
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.write(";\n")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _arg(argv, name, cast, default):
    if name in argv:
        try:
            return cast(argv[argv.index(name) + 1])
        except Exception:
            pass
    return default


def main(argv):
    if "--h5" not in argv:
        print("usage: verify_nowcast.py --h5 <archive *.h5> [--leads N --stride N --max-origins N]")
        return 2
    i = argv.index("--h5")
    paths = sorted(a for a in argv[i + 1:] if a.endswith(".h5") or a.endswith(".hdf"))
    try:
        data = verify(paths, n_leads=_arg(argv, "--leads", int, DEFAULT_LEADS),
                      stride=_arg(argv, "--stride", int, 1),
                      max_origins=_arg(argv, "--max-origins", int, None))
    except Exception as e:
        write({"ok": False, "error": str(e), "generated": _utc_now()})
        import traceback; traceback.print_exc()
        return 1
    write(data)
    print(f"verified {data['n_cases']} cases, {data['leads']} leads, {len(data['models'])} models:")
    for m in data["models"]:
        last = m["by_lead"][-1]
        print(f"  {m['label']:14} FSS@8km +{last['lead_min']:.0f}min = {last['fss'][2]:.2f} | "
              f"CSI = {last['csi']:.2f} (n={last['n']})")
    print("Saved:", OUT_JS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
