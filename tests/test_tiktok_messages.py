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
from competitive_tracking.sinks.feishu_messages import FeishuMessageSink, product_markdown
from test_tiktok_outputs import sample, PID


class TikTokMessageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = load_config(Path(__file__).resolve().parents[1]/'config.example.toml')
        for key in ('session_dir', 'output_dir'):
            self.cfg['app'][key] = Path(tmp.name)/key
        self.cfg['feishu_messages'].update(enabled=True, platforms=['tiktok'])
        self.result = sample()
        self.rows = [{'record_id': 'r1', 'fields': {'平台': 'tiktok', '商品ID': PID,
            '监控开关': '开启', '推送开关': '开启', '自定义-商品名': '当前名称',
            '数据推送人': [{'id': 'ou_recipient', 'name': '接收人'}], '负责人': [{'id': 'ou_owner'}]}}]
        self.source = Mock(); self.source.read_records.side_effect = lambda: deepcopy(self.rows)
        self.client = Mock(); self.client.request.return_value = {'message_id': 'om_tiktok'}
        self.sink = FeishuMessageSink(self.cfg, self.client, self.source,
                                     clock=lambda: datetime(2026, 9, 16, 9, tzinfo=timezone.utc))

    def test_card_metrics_current_name_capture_time_and_links(self):
        report = self.sink.write(self.result, dry_run=True)
        self.assertEqual(report['status'], 'preview')
        batch = report['planned'][0]
        self.assertEqual(batch['body']['receive_id'], 'ou_recipient')
        text = json.loads(batch['body']['content'])['elements'][0]['text']['content']
        self.assertTrue(text.startswith('**tiktok · '+PID+' - 当前名称**\n完整商品标题'))
        for expected in ('2026-09-16 14:57:36', '当前价格：** R$ 11.52', '原价：** R$ 1,070',
                         '佣金比例：** 8.00%', '昨日 0', '近7天 25,333', '近14天 26,282',
                         '近28天 26,495', '近7天 R$ 481,795.05', '星级：** 4.4',
                         '达人出单率：** 57.44%', '关联达人数：** 660', '总关联达人数：** 661',
                         '关联视频数：** 477', '关联直播数：** 1,359', '店铺总销量：** 102,640',
                         '当前类目：** 雨伞', '是否下架：** 否'):
            self.assertIn(expected, text)
        links = self.cfg['feishu_messages']['platform_links']['tiktok']
        self.assertIn('[数据链接]('+links['data_url']+') ｜ [历史链接]('+links['history_url']+')', text)
        self.assertLess(text.index('[查看商品]'), text.index('[数据链接]'))
        self.client.request.assert_not_called()

    def test_current_routes_duplicates_switches_and_replay(self):
        second = deepcopy(self.rows[0]); second['fields'].update({'数据推送人': [{'id': 'ou_second'}], '自定义-商品名': '乙名'})
        closed = deepcopy(second); closed['fields'].update({'推送开关': '关闭', '数据推送人': [{'id': 'ou_closed'}]})
        self.rows.extend([deepcopy(self.rows[0]), second, closed])
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['sent']), 2)
        self.assertEqual({c.kwargs['json']['receive_id'] for c in self.client.request.call_args_list}, {'ou_recipient', 'ou_second'})
        self.assertNotIn('乙名', report['planned'][0]['body']['content'])
        self.assertEqual(self.sink.write(self.result)['skipped_count'], 2)
        self.assertEqual(self.client.request.call_count, 2)

    def test_disabled_targets_platform_or_failed_product_not_sent(self):
        for field, value in [('监控开关', '关闭'), ('推送开关', '关闭'), ('数据推送人', [])]:
            old = self.rows[0]['fields'][field]; self.rows[0]['fields'][field] = value
            self.assertEqual(self.sink.write(self.result)['planned'], [])
            self.rows[0]['fields'][field] = old
        self.cfg['feishu_messages']['platforms'] = ['mercado', 'shopee']
        self.assertEqual(self.sink.write(self.result)['planned'], [])
        self.cfg['feishu_messages']['platforms'] = ['tiktok']
        self.result['products']['tiktok:'+PID]['status'] = 'error'
        self.assertEqual(self.sink.write(self.result)['planned'], [])
        self.client.request.assert_not_called()

    def test_unknowns_zero_escaping_and_platform_link_isolation(self):
        entry = self.result['products']['tiktok:'+PID]; entry['status'] = 'partial'
        entry['product'].update(commission_rate_percent=None, rating=None, off_shelves=None,
                                category='<at id=all>**类目**</at>')
        text = product_markdown(entry, ZoneInfo('Asia/Shanghai'), {'data_url': 'https://example.com/mercado'})
        for expected in ('佣金比例：** —', '星级：** —', '昨日 0', '是否下架：** —', '本次采集不完整'):
            self.assertIn(expected, text)
        self.assertNotIn('<at', text)
        self.assertNotIn('数据链接', text)

    def test_six_products_chunk_into_four_and_two(self):
        entry = self.result['products']['tiktok:'+PID]
        for n in range(5):
            pid = str(int(PID)+n+1)
            e = deepcopy(entry); e['product_id'] = e['product']['product_id'] = pid
            self.result['products']['tiktok:'+pid] = e
            row = deepcopy(self.rows[0]); row['fields']['商品ID'] = pid; self.rows.append(row)
        report = self.sink.write(self.result)
        self.assertEqual([batch['product_count'] for batch in report['sent']], [4, 2])
        for batch in report['planned']:
            self.assertLess(len(json.dumps(batch['body'], ensure_ascii=False).encode()), 28000)

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSheetsSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_automatic_send_after_both_tables(self, bitable, sheets, messages):
        self.cfg['app']['platforms'] = ['tiktok']
        for setting in ('tiktok_feishu_output', 'tiktok_feishu_sheets'):
            self.cfg[setting]['enabled'] = True
        sequence = []
        for label, factory in [('bitable', bitable), ('sheets', sheets), ('messages', messages)]:
            def write(result, name=label):
                sequence.append(name)
                return {'status': 'ok', 'report_path': name}
            factory.return_value.write.side_effect = write
        collector = Mock(); collector.return_value.collect.return_value = self.result['products']
        result = run_once(self.cfg, source=self.source, registry={'tiktok': collector}, sink=Mock())
        self.assertEqual(sequence, ['bitable', 'sheets', 'messages'])
        self.assertEqual(result['messages']['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
