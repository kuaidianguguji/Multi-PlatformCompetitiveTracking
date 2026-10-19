from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import time
from urllib.parse import urlsplit

import psutil

from competitive_tracking.credential_lock import CredentialDirectoryLock

log = logging.getLogger(__name__)


class BrowserSession:
    """Chromium profile with a shared directory lease and origin-scoped storage."""

    def __init__(self, cfg: dict, platform: str, *, origin_url: str, init_scripts=()):
        self.cfg = cfg
        self.origin_url = origin_url
        self.init_scripts = init_scripts
        directory = cfg.get(platform, {}).get("credentials_dir", "")
        if isinstance(directory, str):
            directory = directory.strip()
        self.shared = bool(directory)
        if self.shared:
            root = Path(cfg.get("root", Path.cwd()))
            self.path = (root / Path(directory).expanduser()).resolve()
            self.profile_path = self.path
        else:
            self.path = cfg["app"]["session_dir"] / platform
            self.profile_path = self.path / "profile"
        # Preserve the existing snapshot location when no override is set.
        self.state_path = self.path / "session_storage.json"
        self.page = None
        self._credentials_lock = None
        self._startup_attempted = False
        self._startup_started_at = None
        self._startup_existing_pids = set()

    def _profile_processes(self):
        """Find only browser processes using this profile; never log command lines."""
        expected = os.path.normcase(str(self.profile_path.resolve()))
        found = []
        names = {"chrome", "chrome.exe", "google chrome", "google-chrome", "chromium", "chromium-browser",
                 "msedge", "msedge.exe", "microsoft-edge", "chrome-headless-shell"}
        for process in psutil.process_iter(["name"]):
            if (process.info.get("name") or "").lower() not in names:
                continue
            try:
                args = process.cmdline()
                directory = None
                for index, arg in enumerate(args):
                    if arg.startswith("--user-data-dir="):
                        directory = arg.split("=", 1)[1]
                        break
                    if arg == "--user-data-dir" and index + 1 < len(args):
                        directory = args[index + 1]
                        break
                if directory and os.path.normcase(str(Path(directory.strip('"')).resolve())) == expected:
                    found.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
                continue
        return found

    def _owns_startup_process(self, process):
        """Identify a failed launch by new PID, creation time, profile and port."""
        if process.pid in self._startup_existing_pids or self._startup_started_at is None:
            return False
        # Windows can round process creation timestamps. A PID absent from the
        # pre-launch inventory remains mandatory even within this tolerance.
        if process.create_time() < self._startup_started_at - 1:
            return False
        args = process.cmdline()
        options = {}
        for index, arg in enumerate(args):
            for flag in ("--user-data-dir", "--remote-debugging-port"):
                if arg.startswith(flag + "="):
                    options[flag] = arg.split("=", 1)[1]
                elif arg == flag and index + 1 < len(args):
                    options[flag] = args[index + 1]
        directory = options.get("--user-data-dir")
        return bool(directory and
                    os.path.normcase(str(Path(directory.strip('"')).resolve())) ==
                    os.path.normcase(str(self.profile_path.resolve())) and
                    options.get("--remote-debugging-port") == str(self.cfg["browser"]["port"]))

    def _cleanup_failed_startup(self):
        """Bounded cleanup when Chromium launched but no CDP page was obtained."""
        candidates, uncertain = [], False
        for process in self._profile_processes():
            try:
                if not self._owns_startup_process(process):
                    uncertain = True
                    continue
                candidates.append(process)
                process.terminate()
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError, ValueError):
                uncertain = True
        alive = []
        if candidates:
            _, alive = psutil.wait_procs(candidates, timeout=3)
        killable = []
        for process in alive:
            try:
                # Revalidate before escalation; never kill a reused PID or a
                # browser whose profile/port cannot be confirmed as ours.
                if not self._owns_startup_process(process):
                    uncertain = True
                    continue
                process.kill()
                killable.append(process)
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError, ValueError):
                uncertain = True
        if killable:
            _, remaining = psutil.wait_procs(killable, timeout=2)
            uncertain |= bool(remaining)
        if uncertain:
            raise RuntimeError(f"浏览器启动失败，无法安全确认或关闭残留进程；请手动关闭使用该凭证目录的浏览器：{self.profile_path}")

    def _read_storage(self):
        try:
            if not self.state_path.exists():
                return {}
            state = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            if not isinstance(state, dict):
                raise ValueError("invalid storage snapshot")
        except (OSError, ValueError, UnicodeError):
            log.warning("sessionStorage 文件无法读取或格式无效，继续使用浏览器配置中的 Cookie 登录")
            return {}
        clean, invalid = {}, False
        for origin, values in state.items():
            if not isinstance(values, dict):
                invalid = True
                continue
            clean[origin] = {key: value for key, value in values.items() if isinstance(key, str) and isinstance(value, str)}
            invalid |= len(clean[origin]) != len(values)
        if invalid:
            log.warning("sessionStorage 文件含无效项，已忽略；浏览器配置中的 Cookie 保持可用")
        return clean

    def _restore_storage(self):
        values = self._read_storage().get(self.origin, {})
        if values:
            self.page.add_init_js(
                "if(location.origin === " + json.dumps(self.origin) + ") { const values = " +
                json.dumps(values, ensure_ascii=True) +
                "; for (const [k,v] of Object.entries(values)) { if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k,v); } }")

    def _save_storage(self):
        if self.page.run_js("return location.origin") != self.origin:
            return
        current = self.page.run_js("return Object.fromEntries(Object.entries(sessionStorage))")
        if not isinstance(current, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in current.items()):
            raise ValueError("invalid browser sessionStorage snapshot")
        state = self._read_storage()
        state[self.origin] = current
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=".session_storage.", suffix=".tmp", dir=self.state_path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def __enter__(self):
        from DrissionPage import ChromiumOptions, ChromiumPage

        if self._credentials_lock is not None:
            raise RuntimeError("浏览器会话不能重复进入")
        cfg = self.cfg["browser"]
        self.page = None
        self._startup_attempted = False
        self._startup_started_at = None
        self._startup_existing_pids = set()
        self._credentials_lock = CredentialDirectoryLock(self.profile_path)
        try:
            self._credentials_lock.acquire(timeout_seconds=cfg.get("credentials_lock_timeout_seconds", 60), poll_seconds=0.5)
            self.path.mkdir(parents=True, exist_ok=True)
            if self._profile_processes():
                raise RuntimeError(f"登录凭证目录正被其他浏览器占用，请先关闭使用该目录的浏览器：{self.profile_path}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", cfg["port"])) == 0:
                    raise RuntimeError(f"项目浏览器端口 {cfg['port']} 已占用；请关闭旧项目浏览器或修改配置")
            options = ChromiumOptions(read_file=False)
            options.set_local_port(cfg["port"])
            options.set_user_data_path(str(self.profile_path))
            if cfg["executable"]:
                options.set_browser_path(cfg["executable"])
            options.headless(cfg["headless"])
            options.set_timeouts(base=cfg["element_timeout_seconds"], page_load=cfg["page_load_timeout_seconds"])
            self._startup_existing_pids = set(psutil.pids())
            self._startup_started_at = time.time()
            self._startup_attempted = True
            self.page = ChromiumPage(options)
            for script in self.init_scripts:
                self.page.add_init_js(script)
            self._restore_storage()
            log.info("浏览器凭证目录：%s（%s，Cookie/localStorage 由浏览器读写）", self.profile_path,
                     "指定共享目录" if self.shared else "默认独立目录")
            return self.page
        except BaseException:
            try:
                if self._startup_attempted:
                    if self.page is None:
                        self._cleanup_failed_startup()
                    else:
                        self._finish_browser(close=True)
            finally:
                self._credentials_lock.release()
                self._credentials_lock = None
            raise

    @property
    def origin(self):
        parts = urlsplit(self.origin_url)
        return f"{parts.scheme}://{parts.netloc}"

    def _finish_browser(self, *, close):
        if close and self.page is not None:
            try:
                # Works for both ChromiumPage and a platform's new MixTab.
                # Graceful close lets Chromium flush its Cookie database.
                self.page.browser.quit()
            except Exception:
                log.warning("关闭项目浏览器失败，请手动关闭；关闭前保持凭证目录锁")
        elif not close:
            log.info("close_after_run=false：等待手动关闭项目浏览器后释放凭证目录锁")
        waiting_logged = False
        while self._profile_processes():
            if not waiting_logged:
                log.info("等待使用 %s 的浏览器进程完全退出，凭证目录锁保持占用", self.profile_path)
                waiting_logged = True
            time.sleep(0.5)

    def __exit__(self, *_):
        try:
            try:
                self._save_storage()
            except Exception:
                log.warning("无法保存 sessionStorage；已有快照保持不变，下次仍可使用浏览器配置中的 Cookie")
            if self.shared and not self.cfg["browser"]["close_after_run"]:
                log.info("共享凭证目录在本轮结束时关闭浏览器，确保落盘后交给其他项目")
            self._finish_browser(close=self.shared or self.cfg["browser"]["close_after_run"])
        finally:
            if self._credentials_lock is not None:
                self._credentials_lock.release()
                self._credentials_lock = None
