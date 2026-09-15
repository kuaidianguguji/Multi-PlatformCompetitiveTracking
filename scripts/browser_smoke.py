"""Optional real Chromium integration test. Uses only a synthetic local HTML page."""
from pathlib import Path
import socket
import tempfile

from competitive_tracking.config import load_config
from competitive_tracking.models import TrackingTarget
from competitive_tracking.platforms.mercado.collector import MercadoCollector


def main():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    with tempfile.TemporaryDirectory(prefix="competitive_tracking_test_") as temp:
        cfg["app"]["session_dir"] = Path(temp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cfg["browser"]["port"] = sock.getsockname()[1]
        cfg["browser"]["headless"] = True
        cfg["browser"]["element_timeout_seconds"] = 5
        cfg["mercado"].update(page_wait_seconds=0.1, result_settle_seconds=0.1,
                              scroll_wait_seconds=0.05, poll_seconds=0.05, favorite_dialog_wait_seconds=0.1)
        url = (root / "tests/fixtures/browser.html").as_uri()
        for key, route in [("favorite_url", "favorite"), ("login_url", "login"), ("home_url", "home"), ("search_url", "searchItems")]:
            cfg["mercado"][key] = url + "#/" + route
        result = MercadoCollector(cfg).collect([TrackingTarget("mercado", "MLB106"), TrackingTarget("mercado", "MLB999")])
        for pid, origin in [("MLB106", "favorite"), ("MLB999", "search")]:
            item = result["mercado:" + pid]
            assert item["status"] == "ok", item
            assert item["product"]["origin"] == origin, item
            assert item["product"]["conversion_rate_percent"] == 9.99, item
            assert item["product"]["sales"]["7d"] == int(pid[3:]) * 10, item
        assert result["mercado:MLB999"]["product"]["favorite_status"] == "added", result
        print("PASS: real Chromium + Canvas + virtual rows/columns + pagination + exact-ID search + group favorite")


if __name__ == "__main__":
    main()
