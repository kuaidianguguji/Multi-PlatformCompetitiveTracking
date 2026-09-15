from __future__ import annotations

import json
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class BrowserSession:
    """Dedicated Chromium profile plus origin-scoped sessionStorage snapshot."""

    def __init__(self, cfg: dict, platform: str, *, origin_url: str, init_scripts=()):
        self.cfg = cfg
        self.origin_url = origin_url
        self.init_scripts = init_scripts
        self.path = cfg["app"]["session_dir"] / platform
        self.path.mkdir(parents=True, exist_ok=True)
        self.page = None

    def __enter__(self):
        from DrissionPage import ChromiumOptions, ChromiumPage

        cfg = self.cfg["browser"]
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", cfg["port"])) == 0:
                raise RuntimeError(f"项目浏览器端口 {cfg['port']} 已占用；请关闭旧项目浏览器或修改配置")
        options = ChromiumOptions(read_file=False)
        options.set_local_port(cfg["port"])
        options.set_user_data_path(str(self.path / "profile"))
        if cfg["executable"]:
            options.set_browser_path(cfg["executable"])
        options.headless(cfg["headless"])
        options.set_timeouts(base=cfg["element_timeout_seconds"], page_load=cfg["page_load_timeout_seconds"])
        self.page = ChromiumPage(options)
        try:
            for script in self.init_scripts:
                self.page.add_init_js(script)
            state_path = self.path / "session_storage.json"
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict):
                        raise ValueError("invalid saved session")
                except (ValueError, OSError):
                    state = {}
                    log.warning("sessionStorage 文件无效，继续使用 profile 中的 Cookie 登录")
                origin = self.origin
                values = state.get(origin, {})
                if not isinstance(values, dict):
                    values = {}
                self.page.add_init_js(
                    "if(location.origin === " + json.dumps(origin) + ") { const values = " +
                    json.dumps(values, ensure_ascii=True) +
                    "; for (const [k,v] of Object.entries(values)) { if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k,v); } }")
            log.info("浏览器会话目录：%s（profile 自动恢复 Cookie/localStorage）", self.path)
            return self.page
        except BaseException:
            self.page.quit()
            raise

    @property
    def origin(self):
        parts = urlsplit(self.origin_url)
        return f"{parts.scheme}://{parts.netloc}"

    def __exit__(self, *_):
        try:
            if self.page.run_js("return location.origin") == self.origin:
                state = self.page.run_js("return Object.fromEntries(Object.entries(sessionStorage))")
                path = self.path / "session_storage.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({self.origin: state}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
        except Exception:
            log.warning("无法保存 sessionStorage；下次仍会使用浏览器 profile 登录信息")
        finally:
            if self.cfg["browser"]["close_after_run"]:
                try:
                    # A platform may navigate into a new MixTab, which has no page.quit().
                    self.page.browser.quit()
                except Exception:
                    log.warning("关闭项目浏览器失败；请手动关闭后再运行")
