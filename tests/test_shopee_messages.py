from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from competitive_tracking.config import load_config
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.feishu_messages import FeishuMessageSink, build_card, product_markdown
from test_shopee_outputs import sample
from test_feishu_output import sample as mercado_sample


class ShopeeMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.toml')
        for key in ('session_dir', 'output_dir'):
            self.cfg['app'][key] = Path(self.tmp.name) / key
        self.cfg['feishu_messages'].update(enabled=True, platforms=['mercado', 'shopee'],
                                          data_url='https://example.com/mercado', history_url='https://example.com/mercado-history')
        self.result = sample()
        self.rows = [{'record_id': 'r1', 'fields': {
            '平台': 'shopee', '商品ID': '12345678901', '监控开关': '开启', '推送开关': '开启',
            '数据推送人': [{'id': 'ou_recipient', 'name': '接收人甲'}], '负责人': [{'id': 'ou_owner'}],
            '自定义-商品名': '当前自定义名称'}}]
        self.source = Mock()
        self.source.read_records.side_effect = lambda: deepcopy(self.rows)
        self.client = Mock()
        self.client.request.return_value = {'message_id': 'om_shopee_test'}
        self.sink = FeishuMessageSink(self.cfg, self.client, self.source, clock=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc))

    def text(self, batch):
        return json.loads(batch['body']['content'])['elements'][0]['text']['content']

    def test_shopee_metrics_current_custom_name_and_platform_links(self):
        product = self.result['products']['shopee:12345678901']['product']
        product['query_period'] = '最近30天'
        report = self.sink.write(self.result, dry_run=True)
        self.assertEqual(report['status'], 'preview')
        text = self.text(report['planned'][0])
        self.assertTrue(text.startswith('**shopee · 12345678901 - 当前自定义名称**\n平台标题'))
        for expected in ('采集时间：** 2026-09-15 11:00:00', '统计期间：** 最近30天', '价格：** R$ 23.66',
                         '销量：** 日 0 ｜ 月 300,000', '销售额：** 日 R$ 0 ｜ 月 R$ 7,098,000',
                         '留评率：** 4.10%', '星级：** 5', '月新增评分：** 0', '点赞数：** 71',
                         '类目排名：** 1', '近1天变化：** -2', '近7天变化：** 0', '父类-子类'):
            self.assertIn(expected, text)
        for wrong in ('转换率', '近30天销售额', '￥', '¥', '2366000', 'example.com/mercado'):
            self.assertNotIn(wrong, text)
        urls = self.cfg['feishu_messages']['platform_links']['shopee']
        self.assertIn(f"[查看商品]({product['url']}) ｜ [数据链接]({urls['data_url']}) ｜ [历史链接]({urls['history_url']})", text)
        self.client.request.assert_not_called()

    def test_current_recipients_switches_and_idempotent_replay(self):
        second = deepcopy(self.rows[0])
        second['fields'].update({'数据推送人': [{'id': 'ou_second'}], '自定义-商品名': '乙名称'})
        closed = deepcopy(self.rows[0]); closed['fields']['推送开关'] = '关闭'
        closed['fields']['数据推送人'] = [{'id': 'ou_closed'}]
        self.rows.extend([deepcopy(self.rows[0]), second, closed])
        self.result['products']['shopee:12345678901']['tracking_records'][0]['recipients'] = [{'id': 'ou_old'}]
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['sent']), 2)
        self.assertEqual({c.kwargs['json']['receive_id'] for c in self.client.request.call_args_list}, {'ou_recipient', 'ou_second'})
        self.assertNotIn('乙名称', self.text(report['planned'][0]))
        self.assertEqual(self.sink.write(self.result)['skipped_count'], 2)
        self.assertEqual(self.client.request.call_count, 2)

    def test_disabled_or_failed_shopee_never_sent(self):
        for field, value in [('推送开关', '关闭'), ('监控开关', '关闭'), ('数据推送人', [])]:
            old = self.rows[0]['fields'][field]
            self.rows[0]['fields'][field] = value
            self.assertEqual(self.sink.write(self.result)['planned'], [])
            self.rows[0]['fields'][field] = old
        self.result['products']['shopee:12345678901']['status'] = 'error'
        self.assertEqual(self.sink.write(self.result)['planned'], [])
        self.client.request.assert_not_called()

    def test_partial_missing_values_keep_zero_and_do_not_inherit_mercado_links(self):
        entry = self.result['products']['shopee:12345678901']
        entry['status'] = 'partial'
        entry['product']['category_rank']['weekly_change'] = None
        text = product_markdown(entry, ZoneInfo('Asia/Shanghai'), {'data_url': 'https://example.com/mercado'})
        self.assertIn('近7天变化：** —', text)
        self.assertIn('销量：** 日 0', text)
        self.assertIn('本次采集不完整', text)
        self.assertNotIn('数据链接', text)

    def test_same_numeric_id_across_platforms_keeps_routes_and_links_separate(self):
        mercado = mercado_sample()['products']['mercado:MLB123']
        mercado['product_id'] = mercado['product']['product_id'] = '12345678901'
        self.result['products']['mercado:12345678901'] = mercado
        task = deepcopy(self.rows[0]); task['fields']['平台'] = 'mercado'
        self.rows.append(task)
        report = self.sink.write(self.result, dry_run=True)
        identities = [identity for b in report['planned'] for identity in b['identities']]
        self.assertEqual(len(identities), 2)
        card = build_card([mercado, self.result['products']['shopee:12345678901']], self.cfg)
        self.assertIn('[数据链接](https://example.com/mercado)', card['elements'][0]['text']['content'])
        self.assertNotIn('example.com/mercado', card['elements'][2]['text']['content'])

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_once_sends_after_both_shopee_tables(self, bitable, sheets, messages):
        self.cfg['app']['platforms'] = ['shopee']
        for setting in ('shopee_feishu_output', 'shopee_feishu_sheets'):
            self.cfg[setting]['enabled'] = True
        sequence = []
        for label, factory in [('bitable', bitable), ('sheets', sheets), ('messages', messages)]:
            def write(result, name=label):
                sequence.append(name)
                return {'status': 'ok', 'report_path': name}
            factory.return_value.write.side_effect = write
        collector = Mock(); collector.return_value.collect.return_value = self.result['products']
        result = run_once(self.cfg, source=self.source, registry={'shopee': collector}, sink=Mock())
        self.assertEqual(sequence, ['bitable', 'sheets', 'messages'])
        self.assertEqual(result['messages']['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
