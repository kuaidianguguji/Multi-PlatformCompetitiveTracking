from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from competitive_tracking.browser import BrowserSession
from .parser import ID, pagination, parse_html, attach_raw_product

log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    pass


class TikTokCollector:
    def __init__(self, config, session_factory=BrowserSession):
        self.config = config
        self.cfg = config['tiktok']
        self.sel = self.cfg['selectors']
        self.session_factory = session_factory
        self.page = None

    def search_url(self, pid, page=1):
        if not ID.fullmatch(pid):
            raise ValueError('TikTok 商品 ID 必须为完整数字字符串')
        return self.cfg['search_url'] + '?' + urlencode([
            ('region', self.cfg['region']), ('page', page), ('words', pid)
        ])

    def _visible(self, key, *, xpath=False):
        for ele in self.page.eles(('xpath:' if xpath else 'css:') + self.sel[key], timeout=0.2):
            if ele.states.is_displayed:
                return ele
        return None

    def _wait(self, check, message, timeout=None):
        end = time.monotonic() + (timeout or self.cfg['result_timeout_seconds'])
        while time.monotonic() < end:
            found = check()
            if found:
                return found
            time.sleep(self.cfg['poll_seconds'])
        raise TimeoutError(message)

    def _needs_login(self):
        return bool(self._visible('login_modal') or self._visible('guest_login', xpath=True) or self._visible('header_login', xpath=True))

    def _logged_in(self):
        return bool(self._visible('logged_in') and not self._needs_login())

    def _login(self):
        guest = self._visible('guest_login', xpath=True)
        if guest:
            guest.click()
        elif not self._visible('login_modal'):
            header = self._visible('header_login', xpath=True)
            if not header:
                raise LoginError('FastMoss 登录入口不可用')
            header.click()
        self._wait(lambda: self._visible('phone_tab', xpath=True), '未找到手机号登录页签').click()
        mode = self._wait(lambda: self._visible('password_mode', xpath=True) or self._visible('password'), '未找到密码登录入口')
        if not self._visible('password'):
            mode.click()
        password = self._wait(lambda: self._visible('password'), '密码登录表单未出现')
        if self.cfg['username'] and self.cfg['password']:
            country = self._visible('phone_country')
            if not country or country.text.strip() != self.cfg['phone_country_code']:
                raise LoginError('FastMoss 手机区号与配置不一致，请在浏览器中选择正确区号')
            try:
                self._visible('username').input(self.cfg['username'], clear=True)
                password.input(self.cfg['password'], clear=True)
                self._wait(lambda: self._visible('login_submit', xpath=True), '未找到密码提交按钮').click()
            except Exception:
                raise LoginError('FastMoss 密码登录输入或提交失败，请在浏览器检查表单') from None
            log.info('FastMoss 已提交密码登录；如有验证码请在浏览器完成，最长等待 %s 秒', self.cfg['login_timeout_seconds'])
        else:
            log.warning('FastMoss 未配置账号密码，请在最大化项目浏览器中手动登录，最长等待 %s 秒', self.cfg['login_timeout_seconds'])
        try:
            self._wait(self._logged_in, 'FastMoss 登录未成功，请检查账号密码或手动完成验证', self.cfg['login_timeout_seconds'])
        except TimeoutError as exc:
            raise LoginError(str(exc)) from None
        time.sleep(self.cfg['login_settle_seconds'])

    def _result(self, pid, page_number, expected_ids=None):
        """Require stable DOM plus matching query and pagination.

        FastMoss can render a usable result table while a guest/login modal is
        still visible.  Login state is therefore deliberately not part of the
        result acceptance condition.
        """
        previous, stable_since = None, time.monotonic()
        deadline = time.monotonic() + self.cfg['result_timeout_seconds']
        while time.monotonic() < deadline:
            state = self.page.run_js('''
                const root = document.querySelector(arguments[0]);
                const input = document.querySelector(arguments[1]);
                return {html:root?.outerHTML || '', word:input?.value || '',
                  busy:!!root?.querySelector('.ant-spin-spinning'), url:location.href};
            ''', self.sel['results'], self.sel['search_input'])
            params = parse_qs(urlsplit(state['url']).query)
            correct_query = (params.get('words') == [pid] and params.get('region') == [self.cfg['region']] and state['word'] == pid)
            if correct_query and state['html'] and not state['busy']:
                try:
                    products, info = parse_html(state['html']), pagination(state['html'])
                except ValueError:
                    previous = None
                else:
                    if (info['current'] == page_number and (products or info['empty'])
                            and (expected_ids is None or set(products) == expected_ids)):
                        # Ignore tooltip movement; compare product data and page, not changing HTML attributes.
                        signature = json.dumps([products, info], sort_keys=True, ensure_ascii=False)
                        if signature != previous:
                            previous, stable_since = signature, time.monotonic()
                        elif time.monotonic() - stable_since >= self.cfg['result_settle_seconds']:
                            return products, info
                    else:
                        previous = None
            else:
                previous = None
            time.sleep(self.cfg['poll_seconds'])
        raise TimeoutError('FastMoss 本次查询的登录、商品 ID、分页或表格未稳定，拒绝读取旧结果')

    def _decode_response(self, packet, pid, page_number):
        body = packet.response.body
        if not isinstance(body, dict):
            raise RuntimeError('FastMoss 商品搜索未返回 JSON')
        # Only inspect correlation metadata in memory; never persist the response envelope,
        # which also contains login identifiers and IP information unrelated to products.
        params = (body.get('ext') or {}).get('params') or {}
        if str(params.get('words')) != pid or str(params.get('page')) != str(page_number) or params.get('region') != self.cfg['region']:
            return None
        if packet.response.status != 200 or body.get('code') != 200:
            raise RuntimeError(f"FastMoss 商品查询失败 HTTP={packet.response.status} code={body.get('code')}")
        data = body.get('data') or {}
        if not isinstance(data.get('product_list'), list) or (data.get('ext') or {}).get('is_ok') is not True:
            raise RuntimeError('FastMoss 商品查询响应不完整或查询失败')
        items = {}
        for item in data['product_list']:
            key = str(item.get('product_id', '')) if isinstance(item, dict) else ''
            if not ID.fullmatch(key) or key in items:
                raise RuntimeError('FastMoss 商品响应 ID 缺失或重复')
            items[key] = item
        return items

    def _await_response(self, pid, page_number):
        deadline = time.monotonic() + self.cfg['result_timeout_seconds']
        while time.monotonic() < deadline:
            packet = self.page.listen.wait(timeout=1, raise_err=False)
            if packet:
                items = self._decode_response(packet, pid, page_number)
                if items is not None:
                    return items
        raise TimeoutError('FastMoss 未取得与当前 ID、国家、页码相符的查询响应')

    def _query(self, pid, page_number):
        for attempt in range(2):
            # A fresh document avoids retaining the preceding SPA query's rows after failed navigation.
            self.page.get('about:blank', show_errmsg=True)
            self.page.listen.start(re.escape(self.cfg['search_response_path'])+r'(?:\?|$)', is_regex=True)
            self.page.get(self.search_url(pid, page_number), show_errmsg=True)
            time.sleep(self.cfg['page_wait_seconds'])
            login_visible = self._needs_login()
            if login_visible:
                # Do not log in merely because a modal is visible.  The page
                # may already contain a valid result table behind it.
                pass
            try:
                raw = self._await_response(pid, page_number)
                products, info = self._result(pid, page_number, set(raw))
                if not products and login_visible:
                    raise LoginError('FastMoss 登录后才能读取空结果')
                return {key: attach_raw_product(product, raw[key]) for key, product in products.items()}, info
            except (LoginError, TimeoutError):
                if attempt or not (login_visible or self._needs_login()):
                    raise
                self._login()
            finally:
                self.page.listen.stop()
        raise LoginError('FastMoss 登录后无法读取本次商品结果')

    def _search(self, pid):
        seen = set()
        for page_number in range(1, self.cfg['max_pages'] + 1):
            products, info = self._query(pid, page_number)
            if seen.intersection(products):
                raise RuntimeError('FastMoss 分页出现重复商品，无法确认结果完整')
            seen.update(products)
            if pid in products:
                product = products[pid]
                if product['region'] != self.cfg['region']:
                    raise RuntimeError('FastMoss 命中商品国家不是配置的巴西 BR')
                product.update(search_url=self.search_url(pid, page_number), captured_at=datetime.now(timezone.utc).isoformat(), origin='search')
                return product
            if not info['has_next']:
                return None
        raise RuntimeError('FastMoss 搜索超过 max_pages，不能判为未找到')

    def _error(self, exc):
        message = f'{type(exc).__name__}: {exc}'
        for key in ('username', 'password'):
            if self.cfg.get(key):
                message = message.replace(self.cfg[key], '[REDACTED]')
        return message[:700]

    def collect(self, targets):
        results = {}
        valid = [target for target in targets if ID.fullmatch(target.product_id)]
        for target in targets:
            if target not in valid:
                results[target.key] = {'status': 'error', 'error': 'TikTok 商品 ID 必须为完整数字字符串'}
        if not valid:
            return results
        cfg = deepcopy(self.config)
        cfg['browser']['close_after_run'] = True
        with self.session_factory(cfg, 'tiktok', origin_url=self.cfg['search_url']) as self.page:
            self.page.set.window.max()
            for index, target in enumerate(valid):
                try:
                    product = self._search(target.product_id)
                    if product is None:
                        results[target.key] = {'status': 'not_found', 'error': '本次搜索所有页均无精确匹配的商品 ID'}
                    else:
                        results[target.key] = {'status': 'partial' if product['warnings'] else 'ok', 'product': product}
                        log.info('商品数据 %s %s', target.key, json.dumps(product, ensure_ascii=False))
                except Exception as exc:
                    message = self._error(exc)
                    results[target.key] = {'status': 'error', 'error': message}
                    log.error('FastMoss 商品 %s 采集失败：%s', target.product_id, message)
                    if isinstance(exc, LoginError):
                        for remaining in valid[index+1:]:
                            results[remaining.key] = {'status': 'error', 'error': 'FastMoss 登录失败，未继续查询后续商品'}
                        break
        return results
