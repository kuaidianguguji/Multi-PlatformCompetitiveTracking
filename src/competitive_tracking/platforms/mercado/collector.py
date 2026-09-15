from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from competitive_tracking.browser import BrowserSession
from competitive_tracking.models import TrackingTarget
from .parser import ID, merge_product, pagination, parse_html

log = logging.getLogger(__name__)
SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
SCROLLER = "document.querySelector('.vxe-table--main-wrapper .vxe-table--body-wrapper')"


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ")"


class MercadoCollector:
    def __init__(self, config: dict, session_factory=BrowserSession):
        self.config = config
        self.cfg = config["mercado"]
        self.selectors = self.cfg["selectors"]
        self.timeout = config["browser"]["element_timeout_seconds"]
        self.session_factory = session_factory
        self.page = None

    def _wait(self, predicate, description: str, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(self.cfg["poll_seconds"])
        raise TimeoutError(description)

    def _route_is(self, url: str) -> bool:
        current, target = urlsplit(self.page.url), urlsplit(url)
        return (current.scheme, current.netloc, current.fragment.split("?")[0].rstrip("/")) == (
            target.scheme, target.netloc, target.fragment.split("?")[0].rstrip("/"))

    def _navigate(self, url: str):
        if not self.page.get(url, show_errmsg=True):
            raise RuntimeError("页面导航失败")
        if not self.page.wait.doc_loaded(timeout=self.config["browser"]["page_load_timeout_seconds"]):
            raise TimeoutError("文档加载超时")
        time.sleep(self.cfg["page_wait_seconds"])

    def _element(self, xpath):
        element = self.page.ele("xpath:" + xpath, timeout=self.timeout)
        if not element:
            raise TimeoutError(f"未找到页面元素：{xpath}")
        return element

    def _login(self):
        self._navigate(self.cfg["favorite_url"])
        if self._route_is(self.cfg["login_url"]):
            if self.config["browser"]["headless"]:
                raise RuntimeError("登录已过期；将 browser.headless 设为 false 后重新运行并手动登录")
            self._element(self.selectors["login_button"]).click()
            log.warning("请在项目浏览器中手动登录蓝鲸，最长等待 %s 秒", self.cfg["manual_login_timeout_seconds"])
            self._wait(lambda: self._route_is(self.cfg["home_url"]), "手动登录超时，未检测到 /home", self.cfg["manual_login_timeout_seconds"])
            log.info("已检测到 /home，等待登录状态稳定 %s 秒后进入收藏页", self.cfg["login_settle_seconds"])
            time.sleep(self.cfg["login_settle_seconds"])
        if not self._route_is(self.cfg["favorite_url"]):
            self._navigate(self.cfg["favorite_url"])

    def _busy(self) -> bool:
        if self._route_is(self.cfg["login_url"]):
            raise RuntimeError("采集期间登录失效，请重新登录")
        return bool(self.page.run_js("""
          return [...document.querySelectorAll('.el-loading-mask,.vxe-loading')]
            .some(e => getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden'
              && e.getBoundingClientRect().height > 0);
        """))

    def _snapshot(self):
        return self.page.run_js(SNAPSHOT_JS)

    def _ready(self):
        def ready():
            if self._busy():
                return False
            # Empty states must be explicit, not just a transient absence of rows.
            html = self.page.html
            soup = BeautifulSoup(html, "lxml")
            if soup.select_one(".vxe-table tr.vxe-body--row"):
                return True
            total = soup.select_one(".el-pagination__total")
            return bool(total and re.search(r"(?:共计|共)\s*0\s*条", total.get_text()))
        self._wait(ready, "表格结果加载超时或页面结构变化")
        time.sleep(self.cfg["result_settle_seconds"])

    def _scroll(self, x, y):
        return self.page.run_js(f"""
          const e = {SCROLLER};
          if (!e) return null;
          e.scrollLeft = arguments[0]; e.scrollTop = arguments[1];
          e.dispatchEvent(new Event('scroll', {{bubbles: true}}));
          return {{x: e.scrollLeft, y: e.scrollTop, width: e.clientWidth, height: e.clientHeight,
                   maxX: e.scrollWidth - e.clientWidth, maxY: e.scrollHeight - e.clientHeight}};
        """, x, y)

    def _scan_page(self) -> dict[str, dict]:
        self._ready()
        products, snapshots, seen_positions = {}, [], set()
        x, y, steps = 0, 0, 0
        while True:
            dimensions = self._scroll(x, y)
            if dimensions is None:
                # Explicit empty table still has a pager.
                if pagination(self.page.html)["total"] == 0:
                    return {}
                raise RuntimeError("找不到虚拟表格滚动容器")
            if dimensions["height"] <= 0 or dimensions["width"] <= 0:
                raise RuntimeError("表格滚动容器不可见")
            position = (dimensions["x"], dimensions["y"])
            if position in seen_positions:
                raise RuntimeError("虚拟滚动没有前进，停止以免漏抓")
            seen_positions.add(position)
            time.sleep(self.cfg["scroll_wait_seconds"])
            self._wait(lambda: not self._busy(), "表格滚动后加载超时")
            # Accumulate row fragments so a horizontally virtualized ID column may disappear.
            snapshots.append(self._snapshot())
            steps += 1
            if dimensions["x"] < dimensions["maxX"]:
                x = min(dimensions["maxX"], dimensions["x"] + max(1, int(dimensions["width"] * self.cfg["scroll_fraction"])))
            else:
                # Combine copies into one table, allowing fixed and virtual columns to merge by rowid.
                combined = BeautifulSoup('<div class="vxe-table"></div>', "lxml")
                root = combined.select_one(".vxe-table")
                for sample in snapshots:
                    soup = BeautifulSoup(sample, "lxml")
                    for node in soup.select("tr.vxe-header--row, tr.vxe-body--row"):
                        root.append(node.extract())
                for pid, product in parse_html(str(combined)).items():
                    products[pid] = merge_product(products[pid], product) if pid in products else product
                snapshots = []
                if dimensions["y"] >= dimensions["maxY"]:
                    break
                x = 0
                y = min(dimensions["maxY"], dimensions["y"] + max(1, int(dimensions["height"] * self.cfg["scroll_fraction"])))
            if steps >= self.cfg["max_scroll_steps"]:
                raise RuntimeError("虚拟滚动达到安全上限，收藏扫描不完整")
        return products

    def _favorites(self, wanted: set[str]) -> dict[str, dict]:
        found, seen_pages, scanned_ids = {}, set(), set()
        self._ready()
        # A remembered SPA page may not be the first page.
        initial = pagination(self.page.html)
        if initial["current"] != 1:
            self._element('//div[contains(@class,"el-pagination")]//li[contains(@class,"number") and normalize-space(.)="1"]').click()
            self._wait(lambda: pagination(self.page.html)["current"] == 1 and not self._busy(), "返回收藏第一页超时")
            time.sleep(self.cfg["result_settle_seconds"])
        for _ in range(self.cfg["max_pages"]):
            info = pagination(self.page.html)
            if info["current"] in seen_pages:
                raise RuntimeError("收藏分页重复，停止采集")
            seen_pages.add(info["current"])
            batch = self._scan_page()
            scanned_ids.update(batch)
            for pid in wanted.intersection(batch):
                found[pid] = batch[pid]
            log.info("收藏第 %d 页：扫描 %d 个商品，累计命中 %d/%d", info["current"], len(batch), len(found), len(wanted))
            if wanted <= found.keys():
                return found
            if not info["has_next"]:
                if info["total"] is not None and len(scanned_ids) < info["total"]:
                    raise RuntimeError(f"收藏扫描不完整：分页显示 {info['total']} 条，仅解析 {len(scanned_ids)} 个唯一商品")
                return found
            before_ids = set(batch)
            button = self.page.ele("css:" + self.selectors["next_button"], timeout=self.timeout)
            if not button:
                raise RuntimeError("下一页按钮不存在")
            button.click()
            self._wait(lambda: pagination(self.page.html)["current"] > info["current"] and not self._busy(), "收藏翻页未前进")
            # Pagination highlight may update before rows; require actual product change too.
            self._wait(lambda: bool(set(parse_html(self._snapshot())) - before_ids), "收藏页码已变但商品列表未更新")
            time.sleep(self.cfg["result_settle_seconds"])
        raise RuntimeError("收藏分页达到 max_pages，仍有下一页")

    def _search(self, pid: str) -> dict | None:
        self._navigate(self.cfg["search_url"])
        field = self._element(self.selectors["search_input"])
        field.input(pid, clear=True)
        previous = self._snapshot()
        self._element(self.selectors["search_button"]).click()
        def changed():
            if self._busy():
                return False
            snapshot = self._snapshot()
            rows = parse_html(snapshot)
            return pid in rows or snapshot != previous and bool(rows) or (
                snapshot != previous and "共计 0 条" in self.page.html)
        self._wait(changed, f"搜索 {pid} 结果未完成更新")
        time.sleep(self.cfg["result_settle_seconds"])
        # Only accept exact 商品ID; never blindly take the first search result.
        results = self._scan_page()
        return results.get(pid)

    def _row(self, pid: str, *, timeout=None):
        # Scope favorite actions to the matched product row.
        query = ('//tr[contains(@class,"vxe-body--row")][.//span[starts-with(normalize-space(text()),"商品ID")]/'
                 'span[contains(@class,"color-primary-copy") and normalize-space(.)=' + xpath_literal(pid) + ']]')
        if timeout is not None:
            return self.page.ele("xpath:" + query, timeout=timeout)
        return self._element(query)

    def _favorite(self, pid: str) -> str:
        y = 0
        for _ in range(self.cfg["max_scroll_steps"]):
            dim = self._scroll(0, y)
            time.sleep(self.cfg["scroll_wait_seconds"])
            row = self._row(pid, timeout=0.2)
            if row:
                break
            if dim is None or dim["y"] >= dim["maxY"]:
                raise RuntimeError("搜索命中商品已离开结果列表，停止收藏")
            y = min(dim["maxY"], dim["y"] + max(1, int(dim["height"] * self.cfg["scroll_fraction"])))
        else:
            raise RuntimeError("重新定位收藏商品达到滚动上限")
        if row.ele('xpath:.//button[normalize-space(.)="取消收藏"]', timeout=0.2):
            return "already_favorited"
        button = row.ele('xpath:.//button[normalize-space(.)="加入收藏"]', timeout=self.timeout)
        if not button:
            raise RuntimeError("精确匹配的商品行没有加入收藏按钮")
        button.click()
        time.sleep(self.cfg["favorite_dialog_wait_seconds"])
        def dialog_or_done():
            dropdown = self.page.ele("xpath:" + self.selectors["group_input"], timeout=0.2)
            if dropdown:
                return dropdown
            return "done" if self._row(pid).ele('xpath:.//button[normalize-space(.)="取消收藏"]', timeout=0.2) else None
        dropdown = self._wait(dialog_or_done, "加入收藏后未出现分组弹窗或成功状态")
        if dropdown == "done":
            return "added_without_dialog"
        dropdown.click()
        self._element('//li[contains(@class,"el-select-dropdown__item")]/span[normalize-space(.)=' + xpath_literal(self.cfg["favorite_group"]) + ']').click()
        self._element(self.selectors["group_confirm"]).click()
        self._wait(lambda: not self.page.ele("xpath:" + self.selectors["group_input"], timeout=0.2)
                   and self._row(pid).ele('xpath:.//button[normalize-space(.)="取消收藏"]', timeout=0.2), "收藏未确认成功")
        return "added"

    def collect(self, targets: list[TrackingTarget]) -> dict[str, dict]:
        if not targets:
            return {}
        results, valid = {}, []
        for target in targets:
            if ID.fullmatch(target.product_id):
                valid.append(target)
            else:
                results[target.key] = {"status": "error", "error": "无效 Mercado 商品 ID，需为 ML站点前缀+数字"}
        if not valid:
            return results
        with self.session_factory(self.config, "mercado", origin_url=self.cfg["favorite_url"],
                                  init_scripts=[Path(__file__).with_name("canvas.js").read_text(encoding="utf-8")]) as self.page:
            self._login()
            favorites = self._favorites({t.product_id for t in valid})
            for target in valid:
                pid = target.product_id
                try:
                    product = favorites.get(pid)
                    origin = "favorite" if product else "search"
                    if product is None:
                        product = self._search(pid)
                    if product is None:
                        results[target.key] = {"status": "not_found", "error": "搜索结果中没有精确匹配的商品ID"}
                        log.warning("未找到 %s", target.key)
                        continue
                    favorite_status = "existing"
                    if origin == "search":
                        try:
                            favorite_status = self._favorite(pid)
                        except Exception as exc:
                            favorite_status = "failed"
                            product["warnings"].append(f"加入收藏失败：{type(exc).__name__}: {exc}")
                            # Leave a failed modal behind neither for the next query nor for the next ID.
                            self.page.refresh()
                    product.update({"origin": origin, "favorite_status": favorite_status,
                                    "captured_at": datetime.now(timezone.utc).isoformat()})
                    results[target.key] = {"status": "partial" if product["warnings"] else "ok", "product": product}
                    log.info("商品数据 %s %s", target.key, json.dumps(product, ensure_ascii=False))
                except Exception as exc:
                    results[target.key] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    log.error("商品 %s 采集失败：%s", target.key, exc)
        return results
