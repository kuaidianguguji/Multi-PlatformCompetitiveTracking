from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import re
import time
from urllib.parse import urlsplit

from competitive_tracking.browser import BrowserSession
from .parser import ID, pagination, parse_html

log = logging.getLogger(__name__)


class ShopeeCollector:
    def __init__(self, config, session_factory=BrowserSession):
        self.config = config
        self.cfg = config['shopee']
        self.sel = self.cfg['selectors']
        self.session_factory = session_factory
        self.page = None
        self.session = None
        self.mode = None
        self.trends = {}

    def _wait(self, check, description, timeout=None):
        end = time.monotonic() + (timeout or self.cfg['result_timeout_seconds'])
        while time.monotonic() < end:
            value = check()
            if value:
                return value
            time.sleep(self.cfg['poll_seconds'])
        raise TimeoutError(description)

    def _ele(self, key, *, xpath=False):
        element = self.page.ele(('xpath:' if xpath else 'css:') + self.sel[key], timeout=self.config['browser']['element_timeout_seconds'])
        if not element:
            raise RuntimeError(f'Shopdora 缺少 {key} 元素')
        return element

    def _logged_in(self):
        if urlsplit(self.page.url).path.rstrip('/') == '/login':
            return False
        element = self.page.ele('css:' + self.sel['logged_in'], timeout=0.2)
        return bool(element and element.states.is_displayed and element.text.strip() and not re.search('登录|注册', element.text))

    def _login(self):
        self.page.get(self.cfg['home_url'], show_errmsg=True)
        time.sleep(self.cfg['page_wait_seconds'])
        if not self._logged_in():
            if not self.cfg['username'] or not self.cfg['password']:
                raise ValueError('请配置 shopee.username/password 或 SHOPDORA_USERNAME/SHOPDORA_PASSWORD')
            self.page.get(self.cfg['login_url'], show_errmsg=True)
            password_tab = self.page.ele('xpath://*[normalize-space(.)="密码登录" and not(*)]', timeout=1)
            if password_tab:
                password_tab.click()
            self._ele('username').input(self.cfg['username'], clear=True)
            self._ele('password').input(self.cfg['password'], clear=True)
            self._ele('login_submit').click()
            log.info('Shopdora 已提交登录；若出现验证，请在浏览器中完成，最长等待 %s 秒', self.cfg['login_timeout_seconds'])
            self._wait(self._logged_in, 'Shopdora 登录未成功，请检查账号密码或完成页面验证', self.cfg['login_timeout_seconds'])
            time.sleep(self.cfg['login_settle_seconds'])
        log.info('Shopdora 登录状态已确认，复用 shopee 独立会话')

    def _open(self, mode):
        if not self._logged_in():
            raise RuntimeError('Shopdora 会话已失效')
        previous = self.page
        tab_ids = set(previous.browser.tab_ids)
        self._ele('product_menu', xpath=True).hover()
        menu = self._ele('favorites_menu' if mode == 'favorite' else 'search_menu', xpath=True)
        self._wait(lambda: menu.states.is_displayed, 'Shopdora 产品菜单未展开')
        time.sleep(0.5)  # Let the menu animation finish before clicking its text.
        menu.click(by_js=True)
        expected = '/my/collect' if mode == 'favorite' else '/my/product'
        def target_tab():
            if urlsplit(previous.url).path.rstrip('/') == expected:
                return previous
            for tab_id in set(previous.browser.tab_ids) - tab_ids:
                tab = previous.browser.get_tab(tab_id)
                if urlsplit(tab.url).path.rstrip('/') == expected:
                    return tab
            return None
        self.page = self._wait(target_tab, f'Shopdora 菜单未进入 {expected}')
        self.session.page = self.page
        self._ele('filters')
        time.sleep(self.cfg['page_wait_seconds'])
        self.mode = mode
        if previous.tab_id != self.page.tab_id:
            previous.close()

    def _brazil(self):
        self._ele('brazil').click()
        self._wait(lambda: self.page.run_js('return !!document.querySelector(arguments[0])?.querySelector("input")?.checked;', self.sel['brazil']),
                   'Shopdora 筛选区未选中巴西')

    def _reset(self):
        self._ele('reset').click()
        time.sleep(self.cfg['poll_seconds'])
        self._brazil()
        # Reset may restore a default minimum price, which must not constrain an ID query.
        for field in self.page.eles('css:' + self.sel['filters'] + ' input[placeholder="最小值"], ' + self.sel['filters'] + ' input[placeholder="最大值"]'):
            if field.states.is_displayed and field.value:
                field.input('', clear=True)
        if self.mode == 'favorite':
            self._ele('favorite_input').input('', clear=True)

    def _html(self):
        value = self.page.run_js('return document.querySelector(arguments[0])?.outerHTML || "";', self.sel['results'])
        if not value:
            raise RuntimeError('Shopdora 结果区域不存在')
        return value

    @staticmethod
    def _response(packet):
        body = packet.response.body
        if packet.response.status != 200 or not isinstance(body, dict) or body.get('ok') is not True:
            code = body.get('code') if isinstance(body, dict) else None
            raise RuntimeError(f'Shopdora 查询失败 HTTP={packet.response.status} code={code}')
        return body.get('data')

    def _query(self, action=None):
        if not self._logged_in():
            raise RuntimeError('Shopdora 查询前登录已失效')
        if not self.page.run_js('return !!document.querySelector(arguments[0])?.querySelector("input")?.checked;', self.sel['brazil']):
            raise RuntimeError('Shopdora 查询前巴西站点校验失败')
        path = '/api/user/collect/list' if self.mode == 'favorite' else '/api/product/search'
        # Observe only page-initiated product queries, never login requests or auth headers.
        self.page.listen.start(re.escape(path) + r'(?:\?|$)|/api/product/monthTrend(?:\?|$)', is_regex=True)
        self.trends = {}
        (action or (lambda: self._ele('query').click()))()
        deadline = time.monotonic() + self.cfg['result_timeout_seconds']
        data = None
        while time.monotonic() < deadline:
            packet = self.page.listen.wait(timeout=min(1, max(0.01, deadline-time.monotonic())), raise_err=False)
            if not packet:
                continue
            if urlsplit(packet.url).path == path:
                data = self._response(packet)
                break
            trend = self._response(packet)
            self.trends.update((trend or {}).get('trendDates', {}))
        if not isinstance(data, dict) or not isinstance(data.get('list'), list) or not isinstance(data.get('totalCount'), int):
            raise RuntimeError('Shopdora 未获得完整查询响应，不能把旧表格或空白页面当作结果')
        raw_items = {str(item['itemId']): item for item in data['list'] if isinstance(item, dict) and ID.fullmatch(str(item.get('itemId', '')))}
        if len(raw_items) != len(data['list']):
            raise RuntimeError('Shopdora 查询响应存在缺失或重复的商品 ID')
        expected = set(raw_items)
        def rendered():
            if not self._logged_in():
                raise RuntimeError('Shopdora 查询后登录已失效')
            html = self._html()
            try:
                info = pagination(html)
            except ValueError:
                return False
            return (html if set(parse_html(html)) == expected and info['total'] == data['totalCount']
                    and info['current'] == max(1, data.get('currentPage', 1)) else False)
        self._wait(rendered, 'Shopdora 查询响应与表格 ID、分页未同步')
        time.sleep(self.cfg['result_settle_seconds'])
        if expected and not expected <= self.trends.keys():
            trend_end = time.monotonic() + min(10, self.cfg['result_timeout_seconds'])
            while time.monotonic() < trend_end:
                packet = self.page.listen.wait(timeout=0.5, raise_err=False)
                if packet and urlsplit(packet.url).path == '/api/product/monthTrend':
                    try:
                        self.trends.update(self._response(packet).get('trendDates', {}))
                    except RuntimeError:
                        break
                if expected <= self.trends.keys():
                    break
        self.page.listen.stop()
        html = rendered()
        if not html:
            raise RuntimeError('Shopdora 表格在采集前发生变化')
        products = parse_html(html, raw_items=raw_items)
        for pid, p in products.items():
            p['sales_trend'] = self.trends.get(pid)
            p['raw_data_units'] = '接口原值，价格/比例/评分可能缩放；页面标准值见 prices 等字段'
            p['query_period'] = self.page.run_js('return document.querySelector(".filter-containter .t-form-item__month input")?.value || null;')
            if p['sales_trend'] is None:
                p['warnings'].append('本次未取得该商品的销量趋势响应')
            p['captured_at'] = datetime.now(timezone.utc).isoformat()
        return products, pagination(html)

    def _record(self, pid, product, results, origin, favorite_status):
        product.update(origin=origin, favorite_status=favorite_status)
        results['shopee:' + pid] = {'status': 'partial' if product['warnings'] else 'ok', 'product': product}
        log.info('商品数据 shopee:%s %s', pid, json.dumps(product, ensure_ascii=False))

    def _favorites(self, wanted, results):
        self._open('favorite')
        self._reset()
        batch, info = self._query()
        seen, pages = set(), set()
        initial_total = info['total']
        for _ in range(self.cfg['max_pages']):
            if info['current'] in pages or (not pages and info['current'] != 1):
                raise RuntimeError('Shopdora 收藏未从第一页开始或分页重复')
            pages.add(info['current'])
            if seen.intersection(batch):
                raise RuntimeError('Shopdora 收藏分页重复商品，无法确认扫描完整')
            seen.update(batch)
            for pid in wanted.intersection(batch):
                self._record(pid, batch[pid], results, 'favorite', 'existing')
            missing = wanted - {key.split(':',1)[1] for key in results if results[key].get('product')}
            log.info('Shopee 收藏第 %s 页：扫描 %s，目标剩余 %s', info['current'], len(batch), len(missing))
            if not missing:
                return set()
            if info['total'] != initial_total:
                raise RuntimeError('Shopdora 收藏总数在扫描期间变化，请重跑以免漏抓')
            if not info['has_next']:
                if len(seen) != info['total']:
                    raise RuntimeError('Shopdora 收藏解析数量与分页总数不一致')
                return missing
            previous = info['current']
            batch, info = self._query(lambda: self._ele('next_button').click())
            if info['current'] != previous + 1:
                raise RuntimeError('Shopdora 收藏翻页未前进')
        raise RuntimeError('Shopdora 收藏扫描超过 max_pages')

    def _search(self, pid):
        self._open('search')
        self._reset()
        self._ele('search_input').input(pid, clear=True)
        batch, info = self._query()
        seen = set()
        for _ in range(self.cfg['max_pages']):
            if pid in batch:
                return batch[pid]
            if seen.intersection(batch):
                raise RuntimeError('Shopdora 搜索结果翻页重复')
            seen.update(batch)
            if not info['has_next']:
                if len(seen) != info['total']:
                    raise RuntimeError('Shopdora 搜索结果未扫描完整')
                return None
            before = info['current']
            batch, info = self._query(lambda: self._ele('next_button').click())
            if info['current'] != before + 1:
                raise RuntimeError('Shopdora 搜索翻页未前进')
        raise RuntimeError('Shopdora 搜索超过 max_pages')

    def _favorite(self, pid):
        # pid has already been restricted to digits; act only inside its exact product row.
        xpath = '//tr[contains(@class,"row-main")][.//div[contains(@class,"link-info")]//a[normalize-space(.)="' + pid + '"]]'
        def button(label):
            return self.page.ele('xpath:' + xpath + '//a[normalize-space(.)="' + label + '"]', timeout=0.2)
        if button('取消收藏'):
            return 'existing'
        add = button('加入收藏')
        if not add:
            raise RuntimeError('精确商品行中没有加入收藏按钮')
        add.click()
        self._wait(lambda: button('取消收藏'), 'Shopdora 加入收藏未确认成功')
        return 'added'

    def collect(self, targets):
        results = {}
        valid = {t.product_id for t in targets if ID.fullmatch(t.product_id)}
        for target in targets:
            if target.product_id not in valid:
                results[target.key] = {'status': 'error', 'error': 'Shopee 产品 ID 必须为完整数字字符串'}
        if not valid:
            return results
        cfg = deepcopy(self.config)
        cfg['browser']['close_after_run'] = True  # User requested browser exit after the query.
        self.session = self.session_factory(cfg, 'shopee', origin_url=self.cfg['home_url'])
        with self.session as self.page:
            self.page.set.window.max()
            self._login()
            try:
                missing = self._favorites(valid, results)
            except Exception as exc:
                log.error('Shopee 收藏扫描失败：%s', exc)
                for pid in valid:
                    results.setdefault('shopee:'+pid, {'status':'error', 'error':str(exc)})
                return results
            for pid in sorted(missing):
                try:
                    product = self._search(pid)
                    if product is None:
                        results['shopee:'+pid] = {'status':'not_found', 'error':'巴西选产品结果中无精确匹配的产品 ID'}
                        continue
                    try:
                        favorite_status = self._favorite(pid)
                    except Exception as exc:
                        favorite_status = 'failed'
                        product['warnings'].append('加入收藏失败：' + str(exc))
                    self._record(pid, product, results, 'search', favorite_status)
                except Exception as exc:
                    results['shopee:'+pid] = {'status':'error', 'error':str(exc)}
                    log.error('Shopee 商品 %s 查询失败：%s', pid, exc)
        return results
