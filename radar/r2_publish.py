"""Mirror pipeline outputs to Cloudflare R2.

GitHub Pages rebuilds on every push and pins a 10-min CDN cache, so pushed
data goes stale for minutes; R2 has no build step and honours our cache
headers, so mirroring docs/ there and fetching client-side (cache-busted)
makes updates near-instant. R2 is S3-compatible, hence boto3.

Credentials come from the environment (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY); bucket, public base URL, and cache header live in
config.R2. Missing creds make this a silent no-op. Quick check:
    python -m radar.r2_publish --test
"""

import mimetypes
import os
import sys
from pathlib import Path

import config

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"


def available():
    """True iff R2 is enabled in config AND all three creds are in the environment."""
    cfg = getattr(config, "R2", {}) or {}
    return bool(cfg.get("enabled")
                and os.environ.get("R2_ACCOUNT_ID")
                and os.environ.get("R2_ACCESS_KEY_ID")
                and os.environ.get("R2_SECRET_ACCESS_KEY"))


def _client():
    import boto3  # lazy: only needed when actually publishing
    acct = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def publish(relpaths, base_dir=DOCS):
    """Upload docs-relative paths to R2 under the SAME key (so the bucket mirrors
    docs/ and a page fetches <public_base>/<relpath>). Missing files are skipped.
    Returns the number uploaded; silent no-op when R2 isn't configured."""
    if not available():
        return 0
    cfg = config.R2
    try:
        s3 = _client()
    except Exception as e:                                   # boto3 missing / bad creds
        print(f"  R2: client init failed ({type(e).__name__}: {e}); skipping",
              file=sys.stderr)
        return 0
    cache = cfg.get("cache_control", "no-cache")
    n = 0
    for rel in relpaths:
        rel = str(rel).replace("\\", "/")
        p = Path(base_dir) / rel
        if not p.is_file():
            continue
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            s3.upload_file(str(p), cfg["bucket"], rel,
                           ExtraArgs={"ContentType": ctype, "CacheControl": cache})
            n += 1
        except Exception as e:                              # one bad file mustn't stop the rest
            print(f"  R2: upload {rel} failed ({type(e).__name__}: {e})", file=sys.stderr)
    if n:
        print(f"  R2: published {n} file(s) -> {cfg['public_base']}")
    return n


def publish_glob(patterns, base_dir=DOCS):
    """publish() for docs-relative glob patterns, e.g. ['compare_frames/**/*.png']."""
    rels = []
    for pat in patterns:
        for p in Path(base_dir).glob(pat):
            if p.is_file():
                rels.append(str(p.relative_to(base_dir)))
    return publish(rels, base_dir)


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("R2 enabled in config:", bool(getattr(config, "R2", {}).get("enabled")))
        print("creds present:", available(), "| public_base:", config.R2.get("public_base"))
        if available():
            probe = DOCS / "r2_probe.txt"
            DOCS.mkdir(exist_ok=True)
            probe.write_text("skala r2 ok\n", encoding="utf-8")
            if publish(["r2_probe.txt"]):
                print("OK -> open:", config.R2["public_base"] + "/r2_probe.txt?v=1")
            probe.unlink(missing_ok=True)
        else:
            print("Set R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY first.")
