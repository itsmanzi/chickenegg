import logging
import os
import threading
import time

from income_radar.app import app
from income_radar.engine import refresh_all

logging.basicConfig(level=logging.INFO)

_HOST = os.getenv("INCOME_RADAR_HOST", "127.0.0.1")
_PORT = int(os.getenv("INCOME_RADAR_PORT", "5001"))


def _background_refresh_loop(interval_sec: int) -> None:
    while True:
        time.sleep(interval_sec)
        try:
            info = refresh_all()
            logging.info("Income Radar auto-refresh: %s", info)
        except Exception:
            logging.exception("Income Radar auto-refresh failed")


def main() -> None:
    if os.getenv("INCOME_RADAR_AUTO_REFRESH", "").strip().lower() in ("1", "true", "yes", "on"):
        interval = int(os.getenv("INCOME_RADAR_REFRESH_SEC", "3600"))
        t = threading.Thread(target=_background_refresh_loop, args=(max(300, interval),), daemon=True)
        t.start()
        logging.info("Background refresh every %ss", max(300, interval))
    app.run(host=_HOST, port=_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
