"""Self-verification + feature log for SKALA (PDF Stage 3).

Two jobs:
  1) append_log(): append ONE slim scalar row per run to a year-partitioned CSV.
     This is the empirical record that lets us (a) score SKALA's own skill over
     time and (b) feed the weather-forecast onset model with a live radar
     feature archive. It is tiny — ~120 bytes/row, a few MB/year — so it is
     GitHub-safe (NEVER log the full status.json or the images).
  2) score_log(): read the accumulated log, compare each prediction to what the
     radar actually observed at Budva in the following window, and write
     POD/FAR/CSI/HSS + Brier to docs/skala_verification.json. This is the
     "deliverable that makes high confidence defensible" — it empirically tells
     us whether the thresholds are any good.

No third-party deps (csv/json/datetime only).
"""

import csv
import json
import datetime
from pathlib import Path

# Window over which a single scan's prediction is verified against later scans.
VERIFY_HORIZON_MIN = 60
# Map the categorical scenario state to a rough P(rain at Budva within horizon),
# so we can compute a Brier score / reliability against the observed outcome.
STATE_PROB = {
    "RAINING": 1.0, "SEVERE": 0.85, "IMMINENT": 0.85, "LIKELY": 0.60,
    "POSSIBLE": 0.30, "LIKELY_NO_RAIN": 0.10, "BIO_NOISE": 0.03,
    "CLEAR": 0.02, "UNAVAILABLE": None,
}

LOG_FIELDS = [
    "generated", "scenario_state", "scenario_source",
    "rain_at_location", "rain_approaching", "rain_in_vicinity",
    "closest_eta_minutes", "severe_present",
    "dhmz_rain_at_location", "opera_rain_at_location",
    "closest_rain_km", "closest_rain_dbz", "motion_speed_kmh", "p_rain",
    # Continuous nowcast probabilities (added 2026-06-11). `p_rain` above is
    # only the categorical STATE_PROB mapping; these are the actual model
    # outputs, so future threshold tuning can replay the log empirically.
    "nowcast_p_rain",   # 120-min cumulative P(rain) from the driving source
    "nowcast_p60",      # 60-min bucket — what the approaching verdict keys off
]


def _log_path(docs_dir, year):
    return Path(docs_dir) / f"skala_log_{year}.csv"


def _ensure_header(path):
    """One-time, in-place migration when LOG_FIELDS gains columns: rewrite the
    file with the new header, padding old rows with ''. No-op when the header
    already matches. Atomic (tmp + replace) so a crash can't truncate the log."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
        if header == LOG_FIELDS:
            return
        with open(path, newline="", encoding="utf-8") as f:
            old_rows = list(csv.DictReader(f))
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in old_rows:
                w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in LOG_FIELDS})
        tmp.replace(path)
        print(f"  [verify] migrated {path.name} header to {len(LOG_FIELDS)} columns")
    except Exception as e:
        print(f"  [verify] header migration failed: {e}")


def append_log(status, docs_dir):
    """Append one slim row summarising this run. Creates the (year) file with a
    header on first write. Returns the row dict (or None on failure)."""
    try:
        summary = status.get("summary", {}) or {}
        sources = status.get("sources", {}) or {}

        def _src_at_loc(sid):
            s = sources.get(sid, {})
            app = (s or {}).get("approaching") or {}
            return 1 if app.get("rain_at_location") else 0

        # Pull closest-cell numbers from the source that drove the composite.
        drv = sources.get(summary.get("scenario_source"), {}) or {}
        app = (drv.get("approaching") or {}) if drv else {}
        state = summary.get("scenario_state", "CLEAR")
        prob = STATE_PROB.get(state)
        nd = app.get("nowcast_details") or {}
        p_by_lead = nd.get("p_by_lead") or {}

        row = {
            "generated": status.get("generated"),
            "scenario_state": state,
            "scenario_source": summary.get("scenario_source", ""),
            "rain_at_location": 1 if summary.get("rain_at_location") else 0,
            "rain_approaching": 1 if summary.get("rain_approaching") else 0,
            "rain_in_vicinity": 1 if summary.get("rain_in_vicinity") else 0,
            "closest_eta_minutes": summary.get("closest_eta_minutes"),
            "severe_present": 1 if summary.get("severe_present") else 0,
            "dhmz_rain_at_location": _src_at_loc("dhmz"),
            "opera_rain_at_location": _src_at_loc("opera"),
            "closest_rain_km": app.get("closest_rain_km"),
            "closest_rain_dbz": app.get("closest_rain_intensity_dbz"),
            "motion_speed_kmh": app.get("motion_speed_kmh"),
            "p_rain": prob,
            "nowcast_p_rain": nd.get("p_rain"),
            "nowcast_p60": p_by_lead.get("60"),
        }
        gen = row["generated"] or datetime.datetime.now().isoformat()
        year = str(gen)[:4]
        path = _log_path(docs_dir, year)
        path.parent.mkdir(exist_ok=True)
        new_file = not path.exists()
        if not new_file:
            _ensure_header(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        return row
    except Exception as e:
        print(f"  [verify] log append failed: {e}")
        return None


def _parse_dt(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:
        return None


def score_log(docs_dir, horizon_min=VERIFY_HORIZON_MIN):
    """Read all skala_log_*.csv, and for each scan decide:
      predicted_rain = scenario said rain is at/approaching the location;
      observed_rain  = ANY later scan within `horizon_min` reported rain at the
                       location (rain_at_location True).
    Compute POD/FAR/CSI/HSS over the matured scans + a Brier score from p_rain.
    Writes docs/skala_verification.json. Returns the metrics dict (or None)."""
    try:
        docs = Path(docs_dir)
        rows = []
        for p in sorted(docs.glob("skala_log_*.csv")):
            with open(p, newline="", encoding="utf-8") as f:
                rows.extend(list(csv.DictReader(f)))
        rows = [r for r in rows if _parse_dt(r.get("generated"))]
        rows.sort(key=lambda r: _parse_dt(r["generated"]))
        if len(rows) < 10:
            return {"n_scans": len(rows), "note": "not enough history yet"}

        times = [_parse_dt(r["generated"]) for r in rows]
        at_loc = [r.get("rain_at_location") == "1" for r in rows]
        last_t = times[-1]

        H = M = F = C = 0
        brier_sum = 0.0
        brier_n = 0
        nc_brier_sum = 0.0
        nc_brier_n = 0
        n_eval = 0
        for i, r in enumerate(rows):
            # Only score scans whose horizon has fully matured.
            if (last_t - times[i]).total_seconds() < horizon_min * 60:
                continue
            # Only score DRY-state onset predictions: a scan that is already
            # raining is a nowcast of the present, not a prediction of future
            # onset, so it doesn't belong in the onset contingency table.
            if at_loc[i]:
                continue
            # Observed: did rain reach the location within the horizon?
            observed = False
            j = i + 1
            while j < len(rows) and (times[j] - times[i]).total_seconds() <= horizon_min * 60:
                if at_loc[j]:
                    observed = True
                    break
                j += 1
            predicted = (r.get("rain_at_location") == "1"
                         or r.get("rain_approaching") == "1")
            n_eval += 1
            if predicted and observed:
                H += 1
            elif predicted and not observed:
                F += 1
            elif (not predicted) and observed:
                M += 1
            else:
                C += 1
            # Brier from the categorical probability mapping
            p = r.get("p_rain")
            if p not in (None, "", "None"):
                try:
                    pv = float(p)
                    brier_sum += (pv - (1.0 if observed else 0.0)) ** 2
                    brier_n += 1
                except ValueError:
                    pass
            # Brier from the continuous 60-min nowcast probability (the value
            # the approaching verdict keys off; column exists from 2026-06-11).
            p60 = r.get("nowcast_p60")
            if p60 not in (None, "", "None"):
                try:
                    pv60 = float(p60)
                    nc_brier_sum += (pv60 - (1.0 if observed else 0.0)) ** 2
                    nc_brier_n += 1
                except ValueError:
                    pass

        pod = H / (H + M) if (H + M) else None
        far = F / (H + F) if (H + F) else None
        csi = H / (H + M + F) if (H + M + F) else None
        n = H + M + F + C
        # Heidke skill score
        if n:
            exp = ((H + M) * (H + F) + (C + M) * (C + F)) / n
            hss = (H + C - exp) / (n - exp) if (n - exp) else None
        else:
            hss = None
        metrics = {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "horizon_min": horizon_min,
            "n_scans_total": len(rows),
            "n_scans_scored": n_eval,
            "hits": H, "misses": M, "false_alarms": F, "correct_rejections": C,
            "POD": round(pod, 3) if pod is not None else None,
            "FAR": round(far, 3) if far is not None else None,
            "CSI": round(csi, 3) if csi is not None else None,
            "HSS": round(hss, 3) if hss is not None else None,
            "brier": round(brier_sum / brier_n, 4) if brier_n else None,
            "brier_n": brier_n,
            "brier_nowcast_p60": round(nc_brier_sum / nc_brier_n, 4) if nc_brier_n else None,
            "brier_nowcast_p60_n": nc_brier_n,
        }
        with open(docs / "skala_verification.json", "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)
        return metrics
    except Exception as e:
        print(f"  [verify] scoring failed: {e}")
        return None
