"""Downloads radar images from DHMZ and OPERA and stores them in data/frames/{source}/."""

import os
import time
import hashlib
import datetime
from pathlib import Path

import requests
from PIL import Image
import io

import config

BASE_DIR = Path(__file__).resolve().parent.parent
FRAMES_DIR = BASE_DIR / "data" / "frames"

_HEADERS = {"User-Agent": config.USER_AGENT}


def _frame_dir(source_id: str) -> Path:
    d = FRAMES_DIR / source_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def _prune_old_frames(source_id: str, keep: int) -> None:
    """Keep only the last `keep` PNG/GIF files in sorted order."""
    d = _frame_dir(source_id)
    files = sorted([p for p in d.iterdir() if p.suffix in (".png", ".gif")])
    if len(files) > keep:
        for old in files[:-keep]:
            try:
                old.unlink()
            except Exception:
                pass


# --------------------------------------------------------------------------
# DHMZ Uljenje (single static PNG, overwritten on update)
# --------------------------------------------------------------------------
def fetch_dhmz() -> dict:
    """Download the DHMZ Uljenje radar image. Returns metadata dict or raises.

    Since the URL is "static" (always the same URL), we use a content hash to
    detect whether the image has changed. We save each new one as
    YYYYMMDD_HHMMSS_<hash>.png so we have motion history.
    """
    src = config.SOURCES["dhmz"]
    r = requests.get(src["url"], timeout=30, headers=_HEADERS)
    r.raise_for_status()
    img_bytes = r.content
    sha = _hash_bytes(img_bytes)

    d = _frame_dir("dhmz")
    # Check whether we already have the same hash
    existing_hashes = {p.stem.split("_")[-1] for p in d.glob("*.png")}
    if sha in existing_hashes:
        return {"source": "dhmz", "fetched": False, "reason": "no_change", "hash": sha}

    # Validate that it's a parseable image
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = d / f"{ts}_{sha}.png"
    with open(out, "wb") as f:
        f.write(img_bytes)
    _prune_old_frames("dhmz", config.KEEP_FRAMES)
    return {
        "source": "dhmz", "fetched": True, "path": str(out),
        "hash": sha, "size_bytes": len(img_bytes),
        "image_size": img.size, "image_mode": img.mode,
    }


# --------------------------------------------------------------------------
# OPERA Odyssey (FMI CDN, JSON listing -> GIF files)
# --------------------------------------------------------------------------
def fetch_opera() -> dict:
    """Download the latest OPERA Odyssey composite image.

    The listing is in JSON with epoch+url per frame, ~5 min step,
    keeping ~36 frames (3 hours of history).
    """
    src = config.SOURCES["opera"]
    r = requests.get(src["list_url"], timeout=30, headers=_HEADERS)
    r.raise_for_status()
    data = r.json()
    images = data.get("images", [])
    if not images:
        return {"source": "opera", "fetched": False, "reason": "empty_list"}

    # Download the most recent (last in the list).
    latest = images[-1]
    url, epoch = latest["url"], latest["epoch"]
    ts = datetime.datetime.fromtimestamp(epoch / 1000).strftime("%Y%m%d_%H%M%S")
    d = _frame_dir("opera")

    # Skip if we already have this timestamp
    if any(ts in p.stem for p in d.glob("*.gif")):
        return {"source": "opera", "fetched": False, "reason": "no_change",
                "epoch": epoch, "ts": ts}

    r2 = requests.get(url, timeout=60, headers=_HEADERS)
    r2.raise_for_status()
    img_bytes = r2.content
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    out = d / f"{ts}.gif"
    with open(out, "wb") as f:
        f.write(img_bytes)
    _prune_old_frames("opera", config.KEEP_FRAMES)
    return {
        "source": "opera", "fetched": True, "path": str(out),
        "epoch": epoch, "ts": ts, "size_bytes": len(img_bytes),
        "image_size": img.size, "image_mode": img.mode,
    }


def fetch_all() -> list:
    """Fetch both images. Returns a list of metadata dicts; errors are captured."""
    results = []
    for fn, label in [(fetch_dhmz, "dhmz"), (fetch_opera, "opera")]:
        try:
            results.append(fn())
        except Exception as e:
            results.append({"source": label, "fetched": False, "error": str(e)})
    return results


def list_cached_frames(source_id: str) -> list:
    """Return a sorted list of cached frame paths for a given source."""
    d = _frame_dir(source_id)
    suffix = ".png" if source_id == "dhmz" else ".gif"
    return sorted(d.glob(f"*{suffix}"))


if __name__ == "__main__":
    import json
    res = fetch_all()
    print(json.dumps(res, indent=2, default=str))
