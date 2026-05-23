"""Run run.py every N minutes until killed with Ctrl+C."""

import time
import subprocess
import sys
from datetime import datetime

import config

INTERVAL_SEC = config.FETCH_INTERVAL_MIN * 60


def main():
    print(f"budva-radar loop — interval {config.FETCH_INTERVAL_MIN} min. Press Ctrl+C to stop.")
    while True:
        t0 = datetime.now()
        print(f"\n=== {t0.isoformat(timespec='seconds')} ===")
        try:
            subprocess.run([sys.executable, "run.py"], check=False)
        except Exception as e:
            print(f"run.py crashed: {e}")
        elapsed = (datetime.now() - t0).total_seconds()
        sleep_for = max(10, INTERVAL_SEC - elapsed)
        print(f"sleeping {sleep_for:.0f}s ...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
