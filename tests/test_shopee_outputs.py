from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from competitive_tracking.config import load_config
from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.platforms.shopee.collector import ShopeeCollector
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.destinations import destination_config
from competitive_tracking.sinks.feishu import FeishuSink, product_values
from competitive_tracking.sinks.feishu_sheets import FeishuSheetsSink, history_row
from competitive_tracking.sinks.provision_shopee import field_definition
from competitive_tracking.sinks.shopee_fields import COLUMNS


def sample():
    return {'run_id': 'shopee_test', 'products': {'shopee:12345678901': {
        'platform': 'shopee', 'product_id': '12345678901', 'status': 'ok',
        'tracking_records': [{'name': '自定义', 'competitor': '是', 'owners': [{'id': 'ou_test', 'name': '负责人甲'}]}],
        'product': {'platform': 'shopee', 'product_id': '12345678901', 'name': '平台标题',
                    'prices': {'BRL': 23.66}, 'sales': {'daily': 0, 'monthly': 300000},
                    'revenue': {'daily': {'currency': 'BRL', 'amount': 0}, 'monthly': {'currency': 'BRL', 'amount': 7098000}},
                    'review_count': 27454, 'review_rate_percent': 4.10, 'rating': 5,
                    'monthly_new_reviews': 0, 'like_count': 71, 'monthly_new_likes': 0,
                    'category_rank': {'rank': 1, 'daily_change': -2, 'weekly_change': 0},
                    'seller': {'name': '店铺甲', 'url': 'https://shopee.com.br/shop/123'},
                    'brand': '无', 'variant_count': 5, 'category_path': '父类-子类',
                    'url': 'https://shopee.com.br/product/123/12345678901', 'images': ['https://example.com/image.jpg'],
                    'captured_at': '2026-09-15T03:00:00+00:00', 'warnings': [], 'origin': 'favorite',
                    'raw_product': {'price': 2366000, 'salesAmountM': 709800000000, 'ratingRateTotal': 410}}}}}


class ShopeeOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = load_config(Path(__file__).resolve().parents[1] / 'config.example.toml')
        self.original['app'].update(session_dir=Path(self.tmp.name)/'sessions', output_dir=Path(self.tmp.name)/'data', platforms=['shopee'])
        for key in ('shopee_feishu_output', 'shopee_feishu_sheets'):
            self.original[key]['enabled'] = True
        self.cfg = destination_config(self.original, 'shopee')
        self.client = Mock(spec=FeishuClient)
        self.client.table_path.side_effect = FeishuClient.table_path
        self.now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)

    def test_exact_28_values_percent_dates_and_raw_units(self):
        entry = sample()['products']['shopee:12345678901']
        row, warnings = history_row(entry, '2026-09-16 16:00:00', COLUMNS)
        self.assertEqual(warnings, [])
        self.assertEqual(row, ['12345678901', '平台标题', '自定义', '2026-09-16 16:00:00', '是', '负责人甲',
                               23.66, 0, 300000, 0, 7098000, 27454, '4.10%', 5, 0, 71, 0, 1, -2, 0,
                               '店铺甲', 'https://shopee.com.br/shop/123', '无', 5, '父类-子类',
                               'https://shopee.com.br/product/123/12345678901', 'https://example.com/image.jpg', '2026-09-15 11:00:00'])

    def test_favorite_missing_metrics_are_blank_not_zero(self):
        entry = sample()['products']['shopee:12345678901']
        entry['product'].update(revenue={}, category_rank={}, sales={'daily': None, 'monthly': 10})
        row, _ = history_row(entry, 'now', COLUMNS)
        self.assertEqual([row[i] for i in (7, 9, 10, 17, 18, 19)], ['']*6)
        entry['product']['revenue'] = {'daily': {'currency': 'USD', 'amount': 40}}
        self.assertIsNone(product_values(entry)[0]['revenue_daily_brl'])

    def test_bitable_updates_existing_id_and_preserves_unknown(self):
        definitions = [field_definition(k, v) for k, v in COLUMNS]
        rows = [{'record_id': 'existing', 'fields': {'商品ID': '12345678901', '日销量': 9}}]
        self.client.list_all.side_effect = lambda path, **kw: deepcopy(definitions if path.endswith('/fields') else rows)
        def write(method, path, **kwargs):
            self.assertTrue(path.endswith('/batch_update'))
            for record in kwargs['json']['records']:
                self.assertEqual(record['record_id'], 'existing')
                rows[0]['fields'].update(record['fields'])
            return {'records': deepcopy(rows)}
        self.client.request.side_effect = write
        sink = FeishuSink(self.cfg, self.client, clock=lambda: self.now)
        report = sink.write(sample())
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['verified_count'], 1)
        fields = rows[0]['fields']
        self.assertAlmostEqual(fields['留评率'], .041)
        self.assertEqual(fields['日销量'], 0)
        self.assertEqual(fields['采集时间'], int(datetime(2026, 9, 15, 3, tzinfo=timezone.utc).timestamp()*1000))
        self.assertNotEqual(fields['更新时间'], fields['采集时间'])
        missing = sample()
        missing['products']['shopee:12345678901']['product']['sales']['daily'] = None
        report = sink.write(missing)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(fields['日销量'], 0)
        self.assertEqual(len(rows), 1)

    def test_sheets_appends_ab_and_replay_is_idempotent(self):
        sheet = self.cfg['feishu_sheets']['sheet_id']
        cells = {1: [label for _, label in COLUMNS], 2: ['old-product'] + [None]*27}
        writes = []
        def api(method, path, **kw):
            if path.endswith('/sheets/query'):
                return {'sheets': [{'sheet_id': sheet, 'grid_properties': {'row_count': 20, 'column_count': 28}}]}
            if method == 'GET':
                first, lastcol, last = re.search(r'!A(\d+):([A-Z]+)(\d+)', path).groups()
                return {'valueRange': {'values': [deepcopy(cells.get(n, [None]*28))[:1 if lastcol == 'A' else 28] for n in range(int(first), int(last)+1)]}}
            body = kw['json']['valueRange']
            writes.append(deepcopy(body))
            first = int(re.search(r'!A(\d+)', body['range'])[1])
            for offset, row in enumerate(body['values']):
                cells[first+offset] = deepcopy(row)
            return {}
        self.client.request.side_effect = api
        sink = FeishuSheetsSink(self.cfg, self.client, clock=lambda: self.now)
        self.assertEqual(sink.write(sample())['status'], 'ok')
        self.assertEqual(writes[0]['range'], sheet+'!A3:AB3')
        self.assertEqual(len(writes[0]['values'][0]), 28)
        self.assertEqual(sink.write(sample())['already_recorded_count'], 1)
        newer = sample(); newer['run_id'] = 'second_capture'
        self.assertEqual(sink.write(newer)['appended_count'], 1)
        self.assertEqual(writes[-1]['range'], sheet+'!A4:AB4')
        self.assertEqual(cells[2][0], 'old-product')

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_automatic_shopee_routing_isolated_from_mercado_and_messages(self, bitable, sheets, messages):
        for factory in (bitable, sheets):
            factory.return_value.write.return_value = {'status': 'ok', 'report_path': 'test'}
        source = Mock()
        source.read_records.return_value = [{'record_id': 'task1', 'fields': {'平台': 'shopee', '商品ID': '12345678901',
                                             '监控开关': '开启', '数据推送人': [{'id': 'ou_test'}]}}]
        collector = Mock()
        collector.return_value.collect.return_value = sample()['products']
        result = run_once(self.original, source=source, registry={'shopee': collector}, sink=Mock())
        self.assertEqual(result['shopee_feishu_write']['status'], 'ok')
        self.assertEqual(result['shopee_sheets_write']['status'], 'ok')
        bitable.assert_called_once(); sheets.assert_called_once(); messages.assert_not_called()
        self.assertEqual(bitable.call_args.args[0]['feishu_output']['platform'], 'shopee')
        self.assertEqual(self.original['feishu_output']['platform'], 'mercado')

    def test_enrichment_uses_coherent_snapshot_and_retains_favorite_on_failure(self):
        c = ShopeeCollector(self.cfg)
        before = sample()['products']
        original = deepcopy(before)
        fresh = deepcopy(before['shopee:12345678901']['product'])
        fresh['name'] = '新标题'
        c._search = Mock(return_value=fresh)
        c._enrich_favorites(before)
        p = before['shopee:12345678901']['product']
        self.assertEqual(p['name'], '新标题')
        self.assertEqual(p['favorite_snapshot']['name'], '平台标题')
        self.assertEqual(p['origin'], 'search')
        self.assertTrue(p['enriched_from_search'])
        c._search.side_effect = RuntimeError('query failure')
        c._enrich_favorites(original)
        self.assertEqual(original['shopee:12345678901']['status'], 'partial')
        self.assertEqual(original['shopee:12345678901']['product']['origin'], 'favorite')


if __name__ == '__main__':
    unittest.main()
