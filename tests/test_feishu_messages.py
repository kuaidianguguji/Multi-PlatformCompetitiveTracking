from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from competitive_tracking.config import load_config
from competitive_tracking.integrations.feishu import FeishuAPIError
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.feishu_messages import FeishuMessageSink, markdown_text
from test_feishu_output import sample


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.toml')
        for key in ('session_dir', 'output_dir'):
            self.cfg['app'][key] = Path(self.tmp.name) / key
        self.cfg['feishu_messages']['enabled'] = True
        self.result = sample()
        self.source = Mock()
        self.rows = [{'record_id': 'r1', 'fields': {'平台': 'mercado', '商品ID': 'MLB123', '监控开关': '开启',
                     '推送开关': '开启', '数据推送人': [{'id': 'ou_recipient', 'name': '接收人'}], '负责人': [{'id': 'ou_owner'}]}}]
        self.source.read_records.side_effect = lambda: deepcopy(self.rows)
        self.client = Mock()
        self.client.request.return_value = {'message_id': 'om_test'}
        self.now = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
        self.sink = FeishuMessageSink(self.cfg, self.client, self.source, clock=lambda: self.now)

    def test_current_recipient_not_owner_and_markdown_metrics(self):
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'ok')
        body = self.client.request.call_args.kwargs['json']
        self.assertEqual(body['receive_id'], 'ou_recipient')
        self.assertEqual(body['msg_type'], 'interactive')
        card = json.loads(body['content'])
        text = card['elements'][0]['text']['content']
        self.assertIn('**价格：** R$ 30.9', text)
        self.assertIn('7天 0', text)
        self.assertIn('2026-09-15 11:00:00', text)
        self.assertIn('[查看商品](https://example.com/p)', text)
        self.assertNotIn('ou_owner', json.dumps(report))

    def test_custom_name_and_three_links_in_requested_order(self):
        self.rows[0]['fields']['自定义-商品名'] = [{'text': '运营名称'}]
        self.result['products']['mercado:MLB123']['tracking_records'][0]['name'] = '旧名称'
        self.cfg['feishu_messages'].update(data_url='https://example.com/base?table=one&view=two',
                                          history_url='https://example.com/history')
        report = self.sink.write(self.result, dry_run=True)
        text = json.loads(report['planned'][0]['body']['content'])['elements'][0]['text']['content']
        self.assertTrue(text.startswith('**mercado · MLB123 - 运营名称**\n商品完整标题'))
        self.assertNotIn('旧名称', text)
        self.assertIn('[查看商品](https://example.com/p) ｜ [数据链接](https://example.com/base?table=one&view=two) ｜ [历史链接](https://example.com/history)', text)

    def test_custom_names_are_scoped_to_recipient_and_enabled_records(self):
        self.rows[0]['fields']['自定义-商品名'] = '甲名'
        second = deepcopy(self.rows[0])
        second['fields'].update({'自定义-商品名': '乙名', '数据推送人': [{'id': 'ou_second'}]})
        disabled = deepcopy(self.rows[0])
        disabled['fields'].update({'自定义-商品名': '关闭名', '推送开关': '关闭'})
        self.rows.extend([second, disabled])
        report = self.sink.write(self.result, dry_run=True)
        text_by_person = {b['body']['receive_id']: json.loads(b['body']['content'])['elements'][0]['text']['content'] for b in report['planned']}
        self.assertIn('MLB123 - 甲名', text_by_person['ou_recipient'])
        self.assertNotIn('乙名', text_by_person['ou_recipient'])
        self.assertNotIn('关闭名', text_by_person['ou_recipient'])
        self.assertIn('MLB123 - 乙名', text_by_person['ou_second'])
        self.assertNotIn('message_custom_names', self.result['products']['mercado:MLB123'])

    def test_disabled_push_monitor_or_removed_target_never_sends(self):
        for field, value in [('推送开关', '关闭'), ('监控开关', '关闭'), ('推送开关', True), ('商品ID', 'OTHER')]:
            with self.subTest(field=field, value=value):
                old = self.rows[0]['fields'][field]
                self.rows[0]['fields'][field] = value
                self.assertEqual(self.sink.write(self.result)['status'], 'ok')
                self.rows[0]['fields'][field] = old
        self.client.request.assert_not_called()

    def test_multiple_people_duplicate_records_do_not_cross_route(self):
        self.rows[0]['fields']['数据推送人'] += [{'id': 'ou_second', 'name': '乙'}]
        duplicate = deepcopy(self.rows[0])
        duplicate['record_id'] = 'r2'
        self.rows.append(duplicate)
        disabled = deepcopy(duplicate)
        disabled['fields']['推送开关'] = '关闭'
        disabled['fields']['数据推送人'] = [{'id': 'ou_disabled'}]
        self.rows.append(disabled)
        report = self.sink.write(self.result)
        self.assertEqual(len(report['sent']), 2)
        self.assertEqual({c.kwargs['json']['receive_id'] for c in self.client.request.call_args_list}, {'ou_recipient', 'ou_second'})

    def test_same_run_skip_new_run_send(self):
        self.sink.write(self.result)
        self.assertEqual(self.sink.write(self.result)['skipped_count'], 1)
        self.result['run_id'] = 'next_run'
        self.sink.write(self.result)
        self.assertEqual(self.client.request.call_count, 2)

    def test_dry_run_no_send_or_state(self):
        report = self.sink.write(self.result, dry_run=True)
        self.assertEqual(report['status'], 'preview')
        self.assertEqual(len(report['planned']), 1)
        self.assertFalse(self.sink.state_path.exists())
        self.client.request.assert_not_called()

    def test_uncertain_retry_uses_same_uuid_and_content(self):
        self.client.request.side_effect = RuntimeError('timeout')
        self.sink.write(self.result)
        body = deepcopy(self.client.request.call_args.kwargs['json'])
        self.client.request.side_effect = None
        self.assertEqual(self.sink.write(self.result)['status'], 'ok')
        self.assertEqual(body, self.client.request.call_args.kwargs['json'])

    def test_expired_uncertain_send_is_not_resent(self):
        self.client.request.side_effect = RuntimeError('timeout')
        self.sink.write(self.result)
        self.now += timedelta(hours=2)
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(self.client.request.call_count, 1)

    def test_permission_rejected_attempt_can_retry_after_permission_fix(self):
        self.client.request.side_effect = FeishuAPIError('missing scope', code=99991672)
        self.sink.write(self.result)
        self.now += timedelta(days=1)
        self.client.request.side_effect = None
        self.assertEqual(self.sink.write(self.result)['status'], 'ok')
        self.assertEqual(self.client.request.call_count, 2)

    def test_pending_retry_rechecks_switch(self):
        self.client.request.side_effect = RuntimeError('timeout')
        self.sink.write(self.result)
        self.rows[0]['fields']['推送开关'] = '关闭'
        self.sink.write(self.result)
        self.assertEqual(self.client.request.call_count, 1)

    def test_missing_person_id_reports_error_without_guessing(self):
        self.rows[0]['fields']['数据推送人'] = [{'name': '某人'}]
        self.assertEqual(self.sink.write(self.result)['status'], 'partial')
        self.client.request.assert_not_called()

    def test_chunks_and_partial_but_not_failed_products(self):
        for i in range(5):
            pid = 'MLB' + str(i)
            entry = deepcopy(self.result['products']['mercado:MLB123'])
            entry['product_id'] = entry['product']['product_id'] = pid
            entry['status'] = 'partial' if i == 0 else 'ok'
            self.result['products']['mercado:' + pid] = entry
            record = deepcopy(self.rows[0])
            record['fields']['商品ID'] = pid
            self.rows.append(record)
        self.result['products']['mercado:MLB123']['status'] = 'error'
        report = self.sink.write(self.result)
        self.assertEqual([s['product_count'] for s in report['sent']], [4, 1])
        self.assertIn('本次采集不完整', report['planned'][0]['body']['content'])

    def test_external_text_cannot_inject_card_mentions(self):
        self.assertEqual(markdown_text('<at id=all>**hi**</at>'), '&lt;at id=all&gt;\\*\\*hi\\*\\*&lt;/at&gt;')

    @patch('competitive_tracking.runner.FeishuMessageSink')
    @patch('competitive_tracking.runner.FeishuSink')
    def test_output_failure_does_not_block_message_stage(self, bitable, messages):
        self.cfg['feishu_output']['enabled'] = True
        bitable.return_value.write.side_effect = RuntimeError('write failed')
        messages.return_value.write.return_value = {'status': 'ok', 'report_path': 'test'}
        source = Mock()
        source.read_records.return_value = []
        result = run_once(self.cfg, source=source, sink=Mock())
        self.assertEqual(result['messages']['status'], 'ok')
        messages.return_value.write.assert_called_once()


if __name__ == '__main__':
    unittest.main()
