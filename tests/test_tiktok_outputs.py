from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from competitive_tracking.config import load_config
from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.destinations import destination_config
from competitive_tracking.sinks.feishu import FeishuSink, product_values
from competitive_tracking.sinks.feishu_sheets import FeishuSheetsSink, history_row
from competitive_tracking.sinks.provision_shopee import field_definition, provision
from competitive_tracking.sinks.tiktok_fields import COLUMNS, KINDS


PID = '1737194607628027090'
SHOP_ID = '7494155028209435858'


def sample():
    return {'run_id': 'tiktok_test', 'products': {'tiktok:'+PID: {
        'platform': 'tiktok', 'product_id': PID, 'status': 'ok',
        'tracking_records': [{'name': '雨伞', 'competitor': '否', 'owners': [],
                              'recipients': [{'id': 'ou_recipient', 'name': '推送人'}]}],
        'product': {'platform': 'tiktok', 'product_id': PID, 'name': '完整商品标题',
            'prices': {'BRL': 11.52}, 'raw_product': {'currency': 'BRL', 'ori_price': 'R$ 1.070,00'},
            'commission_rate_percent': 8, 'creator_order_rate_percent': 57.44, 'rating': 4.4,
            'sales': {'yesterday': 0, '7d': 25333, '14d': 26282, '28d': 26495, 'total': 26495},
            'revenue': {p: {'amount': n, 'currency': 'BRL'} for p, n in
                        [('yesterday', 0), ('7d', 481795.05), ('14d', 507330.6), ('28d', 512658), ('total', 512658)]},
            'related_creators': 660, 'total_creators': 661, 'related_videos': 477, 'related_livestreams': 1359,
            'shop': {'id': SHOP_ID, 'name': '店铺', 'sales_total': 102640,
                     'source_url': 'https://www.fastmoss.com/zh/shop-marketing/detail/'+SHOP_ID},
            'url': 'https://shop.tiktok.com/view/product/'+PID, 'category': '雨伞',
            'captured_at': '2026-09-16T06:57:36+00:00', 'off_shelves': 0}}}}


class TikTokOutputTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.original = load_config(Path(__file__).resolve().parents[1]/'config.example.toml')
        self.original['app'].update(session_dir=Path(tmp.name)/'sessions', output_dir=Path(tmp.name)/'data', platforms=['tiktok'])
        for key in ('tiktok_feishu_output', 'tiktok_feishu_sheets'):
            self.original[key]['enabled'] = True
        self.cfg = destination_config(self.original, 'tiktok')
        self.client = Mock(spec=FeishuClient)
        self.client.table_path.side_effect = FeishuClient.table_path
        self.now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)

    def test_exact_order_units_and_text_ids(self):
        entry = sample()['products']['tiktok:'+PID]
        row, warnings = history_row(entry, '2026-09-16 16:00:00', COLUMNS)
        self.assertEqual(warnings, [])
        self.assertEqual(row, [PID, '完整商品标题', '雨伞', '2026-09-16 16:00:00', '否', '',
            11.52, 1070, '8.00%', 0, 25333, 26282, 26495, 26495, 0, 481795.05, 507330.6,
            512658, 512658, 4.4, '57.44%', 660, 661, 477, 1359, SHOP_ID, '店铺', 102640,
            'https://www.fastmoss.com/zh/shop-marketing/detail/'+SHOP_ID,
            'https://shop.tiktok.com/view/product/'+PID, '雨伞', '2026-09-16 14:57:36', '否'])

    def test_unknown_values_and_non_brl_stay_blank(self):
        entry = sample()['products']['tiktok:'+PID]
        p = entry['product']
        p.update(commission_rate_percent=None, rating=None, off_shelves=None,
                 raw_product={'currency': 'USD', 'ori_price': '$ 70.00'})
        p['revenue']['7d']['currency'] = 'USD'
        v, _ = product_values(entry)
        for key in ('commission_rate', 'rating', 'off_shelves', 'original_price_brl', 'revenue_7d_brl', 'owners'):
            self.assertIsNone(v[key])
        for flag, expected in [(0, '否'), (1, '是'), ('1', '是'), ('unknown', None), (True, None)]:
            p['off_shelves'] = flag
            self.assertEqual(product_values(entry)[0]['off_shelves'], expected)

    def test_update_existing_id_not_duplicate_and_preserve_unknown(self):
        definitions = [field_definition(k, v, KINDS) for k, v in COLUMNS]
        rows = [{'record_id': 'existing', 'fields': {'商品ID': PID, '佣金比例': .1}}]
        self.client.list_all.side_effect = lambda path, **kw: deepcopy(definitions if path.endswith('/fields') else rows)
        def write(method, path, **kw):
            self.assertTrue(path.endswith('/batch_update'))
            for r in kw['json']['records']:
                self.assertEqual(r['record_id'], 'existing')
                rows[0]['fields'].update(r['fields'])
            return {'records': deepcopy(rows)}
        self.client.request.side_effect = write
        sink = FeishuSink(self.cfg, self.client, clock=lambda: self.now)
        self.assertEqual(sink.write(sample())['verified_count'], 1)
        fields = rows[0]['fields']
        self.assertEqual(fields['商品ID'], PID)
        self.assertEqual(fields['店铺ID'], SHOP_ID)
        self.assertAlmostEqual(fields['佣金比例'], .08)
        self.assertAlmostEqual(fields['达人出单率'], .5744)
        self.assertEqual(fields['昨日销量'], 0)
        self.assertNotEqual(fields['更新日期'], fields['采集时间'])
        missing = sample(); missing['products']['tiktok:'+PID]['product']['commission_rate_percent'] = None
        self.assertEqual(sink.write(missing)['status'], 'ok')
        self.assertAlmostEqual(fields['佣金比例'], .08)

    def test_append_ag_preserves_history_and_retry_deduplicates(self):
        sheet = self.cfg['feishu_sheets']['sheet_id']
        cells = {1: [label for _, label in COLUMNS], 2: ['old-product']+[None]*32}
        writes = []
        def api(method, path, **kw):
            if path.endswith('/sheets/query'):
                return {'sheets': [{'sheet_id': sheet, 'grid_properties': {'row_count': 20, 'column_count': 33}}]}
            if method == 'GET':
                start, col, end = re.search(r'!A(\d+):([A-Z]+)(\d+)', path).groups()
                return {'valueRange': {'values': [deepcopy(cells.get(n, [None]*33))[:1 if col == 'A' else 33]
                                                  for n in range(int(start), int(end)+1)]}}
            body = kw['json']['valueRange']; writes.append(deepcopy(body))
            start = int(re.search(r'!A(\d+)', body['range'])[1])
            for offset, row in enumerate(body['values']):
                cells[start+offset] = deepcopy(row)
            return {}
        self.client.request.side_effect = api
        sink = FeishuSheetsSink(self.cfg, self.client, clock=lambda: self.now)
        self.assertEqual(sink.write(sample())['appended_count'], 1)
        self.assertEqual(writes[0]['range'], sheet+'!A3:AG3')
        self.assertEqual(sink.write(sample())['already_recorded_count'], 1)
        newer = sample(); newer['run_id'] = 'second_capture'
        self.assertEqual(sink.write(newer)['appended_count'], 1)
        self.assertEqual(writes[-1]['range'], sheet+'!A4:AG4')
        self.assertEqual(cells[2][0], 'old-product')

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_tiktok_routing_without_messages_or_other_platforms(self, bitable, sheets, messages):
        for factory in (bitable, sheets):
            factory.return_value.write.return_value = {'status': 'ok', 'report_path': 'test'}
        source = Mock()
        source.read_records.return_value = [{'record_id': 'task1', 'fields': {'平台': 'tiktok', '商品ID': PID,
                                            '监控开关': '开启', '数据推送人': [{'id': 'ou_test'}]}}]
        collector = Mock(); collector.return_value.collect.return_value = sample()['products']
        result = run_once(self.original, source=source, registry={'tiktok': collector}, sink=Mock())
        self.assertEqual(result['tiktok_feishu_write']['status'], 'ok')
        self.assertEqual(result['tiktok_sheets_write']['status'], 'ok')
        bitable.assert_called_once(); sheets.assert_called_once(); messages.assert_not_called()
        self.assertEqual(bitable.call_args.args[0]['feishu_output']['platform'], 'tiktok')
        self.assertEqual(self.original['feishu_output']['platform'], 'mercado')

    def test_provision_preview_33_fields_and_ag_header(self):
        self.client.list_all.side_effect = [[{'field_name': '文本', 'field_id': 'first', 'type': 1, 'is_primary': True}], []]
        sheet = self.cfg['feishu_sheets']['sheet_id']
        self.client.request.side_effect = [
            {'sheets': [{'sheet_id': sheet, 'grid_properties': {'column_count': 20}}]},
            {'valueRange': {'values': [[None]*33]}}]
        report = provision(self.cfg, self.client, dry_run=True)
        self.assertEqual(report['field_count'], 33)
        self.assertEqual(report['add_columns'], 13)
        self.assertEqual(report['header_range'], sheet+'!A1:AG1')
        self.assertTrue(report['rename_primary'])
        self.assertTrue(all(call.args[0] == 'GET' for call in self.client.request.call_args_list))


if __name__ == '__main__':
    unittest.main()
