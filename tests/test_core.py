from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests

from competitive_tracking.config import load_config
from competitive_tracking.platforms.mercado.parser import parse_html, pagination
from competitive_tracking.platforms.mercado.collector import MercadoCollector, xpath_literal
from competitive_tracking.runner import next_run, run_once
from competitive_tracking.sources.feishu import FeishuSource, select_targets
from competitive_tracking.storage import JsonSink, single_instance

ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(ROOT / "config.example.toml")


def record(pid="MLB123", **kwargs):
    fields = {"平台": "mercado", "商品ID": [{"text": pid, "type": "text"}], "监控开关": "开启",
              "数据推送人": [{"id": "ou_test", "name": "测试运营"}], "推送开关": "关闭"}
    fields.update(kwargs)
    return {"record_id": "rec_test", "fields": fields}


class FilteringTests(unittest.TestCase):
    def test_exact_monitor_and_nonempty_recipients(self):
        rows = [record(), record("MLB124", 监控开关=True), record("MLB125", 监控开关="关闭"),
                record("", 商品ID=[]), record("MLB126", 数据推送人=[]), record("MLB127", 平台="shopee"),
                record("MLB128", 数据推送人=[{}]), record("MLB129", 监控开关=["开启", "关闭"])]
        result = select_targets(rows, config()["feishu"]["fields"], "mercado")
        self.assertEqual([r.product_id for r in result], ["MLB123"])
        self.assertFalse(result[0].records[0]["push_enabled"])

    def test_duplicate_preserves_distinct_recipients_and_push(self):
        a, b = record(), record("mlb123", 推送开关="开启", 数据推送人=[{"id": "ou_second"}])
        b["record_id"] = "rec_second"
        result = select_targets([a, b], config()["feishu"]["fields"], "mercado")
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].records), 2)
        self.assertTrue(result[0].records[1]["push_enabled"])


def response(body, status=200):
    r = Mock(status_code=status, ok=status < 400)
    r.json.return_value = body
    return r


class FeishuTests(unittest.TestCase):
    def make(self, responses):
        cfg = config()["feishu"]
        cfg.update(app_id="test", app_secret="test", app_token="test", table_id="test", retries=1)
        session = Mock()
        session.request.side_effect = responses
        return FeishuSource(cfg, session), session

    def test_all_pages_and_token_reuse(self):
        client, session = self.make([
            response({"code": 0, "tenant_access_token": "test", "expire": 7200}),
            response({"code": 0, "data": {"items": [record()], "has_more": True, "page_token": "next"}}),
            response({"code": 0, "data": {"items": [record("MLB124")], "has_more": False}}),
        ])
        self.assertEqual(len(client.read_records()), 2)
        self.assertEqual(session.request.call_count, 3)
        self.assertEqual(session.request.call_args.kwargs["params"]["page_token"], "next")

    def test_repeated_cursor_rejected(self):
        client, _ = self.make([
            response({"code": 0, "tenant_access_token": "test", "expire": 7200}),
            *[response({"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}}) for _ in range(2)],
        ])
        with self.assertRaisesRegex(RuntimeError, "游标"):
            client.read_records()

    @patch("competitive_tracking.sources.feishu.time.sleep")
    def test_retry_network_and_http_errors(self, sleep):
        for failure in [requests.ConnectionError(), response({}, 429), response({}, 503)]:
            client, session = self.make([failure, response({"code": 0})])
            self.assertEqual(client._request("GET", "/test")["code"], 0)
            self.assertEqual(session.request.call_count, 2)

    def test_permissions_do_not_retry(self):
        client, session = self.make([response({}, 403)])
        with self.assertRaisesRegex(RuntimeError, "403"):
            client._request("GET", "/test")
        self.assertEqual(session.request.call_count, 1)

    def test_expired_token_is_refreshed_once(self):
        client, session = self.make([
            response({"code": 0, "tenant_access_token": "old", "expire": 7200}), response({"code": 99991663}),
            response({"code": 0, "tenant_access_token": "new", "expire": 7200}),
            response({"code": 0, "data": {"items": [], "has_more": False}}),
        ])
        self.assertEqual(client.read_records(), [])
        self.assertEqual(session.request.call_args.kwargs["headers"]["Authorization"], "Bearer new")


class ParserTests(unittest.TestCase):
    def test_canvas_and_fixed_columns(self):
        products = parse_html((ROOT / "tests/fixtures/table.html").read_text(encoding="utf-8"))
        self.assertEqual(set(products), {"MLB123"})
        p = products["MLB123"]
        self.assertEqual(p["prices"]["BRL"], 24.06)
        self.assertEqual(p["sales"]["30d"], 17986)
        self.assertEqual(p["sales"]["7d"], 0)
        self.assertEqual(p["revenue"]["近30天销售额($BRL)"]["amount"], 432743.16)
        self.assertEqual(p["images"], ["https://example.com/product.webp"])
        self.assertEqual(p["review_count"], 43877)
        self.assertEqual(p["conversion_rate_percent"], 8.11)
        self.assertEqual(p["raw_fields"]["新指标"]["text"], "保留扩展字段")
        self.assertEqual(p["warnings"], [])

    def test_offline_canvas_is_not_zero_or_next_label(self):
        html = (ROOT / "tests/fixtures/table.html").read_text(encoding="utf-8")
        html = html.replace('data-ct-text="0"', '').replace('data-ct-text="17,986"', '')
        p = parse_html(html)["MLB123"]
        self.assertIsNone(p["sales"]["7d"])
        self.assertIsNone(p["sales"]["30d"])
        self.assertTrue(p["warnings"])

    def test_catalog_id_and_followed_id_never_match(self):
        html = '<tr class="vxe-body--row"><td><span>被跟卖商品ID: <span class="color-primary-copy">MLB123</span></span></td></tr>'
        self.assertEqual(parse_html(html), {})

    def test_horizontal_samples_merge_by_rowid(self):
        html = (ROOT / "tests/fixtures/table.html").read_text(encoding="utf-8")
        p = parse_html(html)["MLB123"]
        self.assertEqual(p["raw_fields"]["新指标"]["text"], "保留扩展字段")

    def test_pager_disabled_attribute_false_aria_still_disabled(self):
        for disabled in ['class="btn-next" disabled=""', 'class="btn-next" aria-disabled="true"', 'class="btn-next is-disabled"']:
            state = pagination(f'<div class="el-pagination"><li class="el-pager"><b class="is-active">2</b></li><span class="el-pagination__total">共计 63 条</span><button {disabled}></button></div>')
            self.assertFalse(state["has_next"])
            self.assertEqual(state["total"], 63)

    def test_missing_pager_fails_closed(self):
        with self.assertRaises(RuntimeError):
            pagination("<div></div>")


class CollectorTests(unittest.TestCase):
    def make(self):
        cfg = config()
        cfg["mercado"].update(page_wait_seconds=0, result_settle_seconds=0, scroll_wait_seconds=0, poll_seconds=0.001)
        cfg["browser"]["element_timeout_seconds"] = 0.01
        collector = MercadoCollector(cfg)
        collector.page = Mock()
        return collector

    def test_no_targets_never_starts_browser(self):
        factory = Mock()
        self.assertEqual(MercadoCollector(config(), factory).collect([]), {})
        factory.assert_not_called()

    def test_manual_login_then_favorite(self):
        collector = self.make()
        state = {"url": collector.cfg["login_url"]}
        collector._navigate = Mock(side_effect=lambda url: None)
        collector._route_is = lambda url: state["url"] == url
        collector._element = Mock()
        def wait(predicate, *_):
            state["url"] = collector.cfg["home_url"]
            self.assertTrue(predicate())
        collector._wait = wait
        collector._login()
        collector._element.return_value.click.assert_called_once()
        self.assertEqual(collector._navigate.call_count, 2)

    def test_headless_requires_login_is_explicit_error(self):
        collector = self.make()
        collector.config["browser"]["headless"] = True
        collector._navigate = Mock()
        collector._route_is = lambda url: url == collector.cfg["login_url"]
        with self.assertRaisesRegex(RuntimeError, "headless"):
            collector._login()

    def test_search_does_not_accept_first_mismatched_product(self):
        collector = self.make()
        collector._navigate = Mock()
        collector._element = Mock()
        collector._snapshot = Mock(return_value="")
        collector._wait = Mock()
        collector._scan_page = Mock(return_value={"MLB999": {"product_id": "MLB999"}})
        self.assertIsNone(collector._search("MLB123"))

    def test_wait_timeout(self):
        with self.assertRaisesRegex(TimeoutError, "not ready"):
            self.make()._wait(lambda: False, "not ready")

    def test_favorite_failure_preserves_data_and_continues(self):
        collector = self.make()
        collector.session_factory = Mock()
        collector.session_factory.return_value.__enter__ = Mock(return_value=collector.page)
        collector.session_factory.return_value.__exit__ = Mock(return_value=False)
        collector._login = Mock()
        collector._favorites = Mock(return_value={})
        collector._search = lambda pid: {"product_id": pid, "warnings": []}
        collector._favorite = Mock(side_effect=[RuntimeError("分组不存在"), "added"])
        from competitive_tracking.models import TrackingTarget
        result = collector.collect([TrackingTarget("mercado", "MLB123"), TrackingTarget("mercado", "MLB124")])
        self.assertEqual(result["mercado:MLB123"]["status"], "partial")
        self.assertEqual(result["mercado:MLB123"]["product"]["favorite_status"], "failed")
        self.assertEqual(result["mercado:MLB124"]["status"], "ok")

    def test_xpath_escaping(self):
        from lxml import etree
        for value in ["每日查询分组", "a'b", 'a"b', 'a\'b"c']:
            self.assertEqual(etree.XML("<x/>").xpath("string(" + xpath_literal(value) + ")"), value)


class RunnerTests(unittest.TestCase):
    def test_empty_platform_then_next(self):
        cfg = config()
        cfg["app"]["platforms"] = ["mercado", "shopee"]
        source, sink, factory = Mock(), Mock(), Mock()
        source.read_records.return_value = [record(平台="shopee")]
        factory.return_value.collect.return_value = {"shopee:MLB123": {"status": "ok"}}
        result = run_once(cfg, source, {"mercado": Mock(), "shopee": factory}, sink)
        self.assertEqual(result["platforms"]["mercado"]["status"], "skipped")
        factory.assert_called_once()
        sink.write.assert_called_once()

    def test_source_error_is_saved(self):
        source, sink = Mock(), Mock()
        source.read_records.side_effect = RuntimeError("read failed")
        result = run_once(config(), source, {}, sink)
        self.assertEqual(result["status"], "error")
        sink.write.assert_called_once()

    def test_platform_failure_does_not_block_next(self):
        cfg = config()
        cfg["app"]["platforms"] = ["mercado", "shopee"]
        source, sink = Mock(), Mock()
        source.read_records.return_value = [record(), record(平台="shopee")]
        bad, good = Mock(), Mock()
        bad.return_value.collect.side_effect = RuntimeError("page failed")
        good.return_value.collect.return_value = {"shopee:MLB123": {"status": "ok"}}
        result = run_once(cfg, source, {"mercado": bad, "shopee": good}, sink)
        self.assertEqual(result["products"]["shopee:MLB123"]["status"], "ok")
        self.assertEqual(result["status"], "partial")

    def test_timezone_and_daily_boundary(self):
        now = datetime(2026, 9, 15, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(next_run(now, "09:00").day, 15)
        self.assertEqual(next_run(now.replace(hour=9), "09:00").day, 16)

    def test_atomic_output_and_process_lock(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            JsonSink(root).write({"run_id": "test", "status": "ok"})
            self.assertEqual(json.loads((root / "latest.json").read_text())["status"], "ok")
            with single_instance(root):
                with self.assertRaises(RuntimeError):
                    with single_instance(root):
                        pass
            with single_instance(root):
                pass


if __name__ == "__main__":
    unittest.main()
