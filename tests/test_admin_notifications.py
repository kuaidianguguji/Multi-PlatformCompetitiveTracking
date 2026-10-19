from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from competitive_tracking.config import load_config
from competitive_tracking.cli import notify_command_problem
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.admin_notifications import AdminNotificationSink, RunProblems, build_run_summary, collect_problems, notify_admins

ROOT = Path(__file__).resolve().parents[1]


class AdminTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = load_config(ROOT / 'config.example.toml')
        self.cfg['app']['session_dir'] = Path(tmp.name) / 'session'
        self.cfg['app']['output_dir'] = Path(tmp.name) / 'output'
        self.cfg['admin_notifications'] = {'enabled': True, 'admins': {'甲': 'ou_a', '乙': 'ou_b'}}
        self.client = Mock()
        self.client.request.return_value = {'message_id': 'om_test'}
        self.now = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
        self.sink = AdminNotificationSink(self.cfg, client=self.client, clock=lambda: self.now)
        self.result = {'run_id': 'run1', 'status': 'partial', 'started_at': self.now.isoformat(), 'platforms': {},
                       'products': {'mercado:MLBU4813895273': {'status': 'error', 'error': '搜索 MLBU4813895273 结果未完成更新'}}}

    def test_shared_search_error_notifies_all_admins_even_with_operator_push_disabled(self):
        self.assertFalse(self.cfg['feishu_messages']['enabled'])
        self.result['products']['mercado:MLBU4813895273']['tracking_records'] = [{'push_enabled': False}]
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(len(report['sent']), 2)
        self.assertEqual({c.kwargs['json']['receive_id'] for c in self.client.request.call_args_list}, {'ou_a', 'ou_b'})
        card = json.loads(self.client.request.call_args.kwargs['json']['content'])
        self.assertIn('mercado:MLBU4813895273', card['elements'][0]['text']['content'])
        self.assertIn('结果未完成更新', card['elements'][0]['text']['content'])

    def test_all_platforms_empty_results_and_partial_collection_warnings(self):
        self.result['products'] = {p + ':123': {'status': 'not_found', 'error': '商品搜索结果为空'} for p in ('mercado', 'shopee', 'tiktok')}
        self.result['products']['shopee:456'] = {'status': 'partial', 'product': {'warnings': ['加入收藏失败']}}
        issues = collect_problems(self.result)
        self.assertEqual({p['key'] for p in issues}, {'mercado:123', 'shopee:123', 'tiktok:123', 'shopee:456'})

    def test_writer_errors_plan_warnings_messages_and_recovered_log_warning(self):
        reports = {'feishu_write': {'status': 'partial', 'plan': {'errors': [{'key': 'mercado:123', 'error': '字段类型不匹配'}], 'warnings': ['评分缺失']}},
                   'sheets_write': {'status': 'error', 'error': '表头不匹配'},
                   'messages': {'status': 'partial', 'errors': [{'error': '飞书 HTTP 403'}]}}
        issues = collect_problems(self.result, reports, [{'stage': 'browser', 'key': '', 'message': '无法保存 sessionStorage'}])
        self.assertTrue({'字段类型不匹配', '评分缺失', '表头不匹配', '飞书 HTTP 403', '无法保存 sessionStorage'} <= {p['message'] for p in issues})

    def test_normal_empty_tasks_and_dedup_have_no_alert(self):
        result = {'run_id': 'normal', 'status': 'ok', 'products': {}, 'platforms': {'shopee': {'status': 'skipped'}}}
        report = self.sink.write(result, reports={'messages': {'status': 'ok', 'skipped_count': 3}}, summary=False)
        self.assertEqual(report['status'], 'skipped')
        self.client.request.assert_not_called()
        self.assertFalse(self.sink.state_path.exists())

    def test_successful_daily_run_sends_green_summary_to_all_admins(self):
        result = {'run_id': 'normal', 'status': 'ok', 'products': {}, 'platforms': {}}
        reports = {'feishu_write': {'status': 'ok', 'batches': [{'action': 'updated', 'count': 2}],
                                   'verified_count': 3, 'plan': {'created': [{'key': 'planned-only'}],
                                   'updated': [1, 2], 'unchanged': [1], 'skipped': []}},
                   'sheets_write': {'status': 'ok', 'appended_count': 2, 'recovered_count': 1,
                                    'already_recorded_count': 4, 'skipped': []},
                   'messages': {'status': 'ok', 'sent': [{'recipient_id': 'ou_a', 'recipient_name': '甲',
                                    'product_count': 2, 'data_product_count': 2, 'notice_count': 0}], 'errors': []}}
        report = self.sink.write(result, reports=reports)
        self.assertEqual(len(report['sent']), 2)
        card = json.loads(self.client.request.call_args.kwargs['json']['content'])
        self.assertEqual(card['header']['template'], 'green')
        self.assertIn('每日运行报告', card['header']['title']['content'])
        self.assertIn('新增 0，更新 2，无变化 1，跳过 0，写后核对 3', card['elements'][0]['text']['content'])
        self.assertIn('新增历史 2，恢复 1，同批次已记录 4', card['elements'][0]['text']['content'])
        self.assertEqual(report['summary']['messages']['recipient_count'], 1)
        self.assertEqual(report['summary']['messages']['product_delivery_count'], 2)
        self.assertEqual(json.loads(Path(report['report_path']).read_text(encoding='utf-8'))['summary'], report['summary'])

    def test_daily_summary_distinguishes_disabled_and_unexecuted_outputs(self):
        self.cfg['feishu_output']['enabled'] = True
        self.cfg['feishu_messages']['enabled'] = True
        result = {'run_id': 'normal', 'status': 'ok', 'products': {}, 'platforms': {}}
        summary = build_run_summary(self.cfg, result)
        self.assertEqual(summary['bitable']['mercado']['status'], 'not_run')
        self.assertEqual(summary['bitable']['shopee']['status'], 'disabled')
        self.assertEqual(summary['messages']['status'], 'not_run')
        self.assertNotIn('created_count', summary['bitable']['mercado'])
        self.sink.write(result)
        text = json.loads(self.client.request.call_args.kwargs['json']['content'])['elements'][0]['text']['content']
        self.assertIn('未执行', text)
        self.assertIn('未开启', text)
        self.assertNotIn('全部成功', text)

    def test_three_platform_summary_uses_completed_batches_and_shows_data_skips(self):
        result = deepcopy(self.result)
        result['platforms'] = {'mercado': {'status': 'partial', 'target_count': 3},
                               'shopee': {'status': 'ok', 'target_count': 4},
                               'tiktok': {'status': 'partial', 'target_count': 2}}
        reports = {}
        for platform, created, updated, skipped in [('mercado', 0, 0, 3), ('shopee', 2, 2, 0), ('tiktok', 0, 1, 1)]:
            prefix = '' if platform == 'mercado' else platform + '_'
            reports[prefix + 'feishu_write'] = {'status': 'ok', 'batches': [{'action': 'created', 'count': created},
                                               {'action': 'updated', 'count': updated}], 'verified_count': created + updated,
                                               'plan': {'unchanged': [], 'skipped': [{'key': platform + ':' + str(i)} for i in range(skipped)]}}
            reports[prefix + 'sheets_write'] = {'status': 'ok', 'appended_count': created + updated, 'recovered_count': 0,
                                               'already_recorded_count': 0, 'skipped': [1] * skipped}
        report = self.sink.write(result, reports=reports)
        summary = report['summary']
        self.assertEqual(summary['bitable']['mercado']['skipped_count'], 3)
        self.assertEqual(summary['sheets']['mercado']['appended_count'], 0)
        self.assertEqual(summary['bitable']['shopee']['created_count'], 2)
        self.assertEqual(summary['bitable']['shopee']['updated_count'], 2)
        self.assertEqual(summary['sheets']['shopee']['appended_count'], 4)
        self.assertEqual(summary['bitable']['tiktok']['updated_count'], 1)
        self.assertEqual(summary['sheets']['tiktok']['skipped_count'], 1)
        card = json.loads(self.client.request.call_args.kwargs['json']['content'])
        self.assertEqual(card['header']['template'], 'red')
        self.assertIn('mercado：完成；新增 0，更新 0，无变化 0，跳过 3', card['elements'][0]['text']['content'])

    def test_delivered_empty_result_notice_is_not_reported_as_unsent(self):
        identity = json.dumps(['run1', 'ou_a', 'mercado:MLBU4813895273'], ensure_ascii=False)
        reports = {'messages': {'status': 'partial', 'sent': [{'recipient_id': 'ou_a', 'recipient_name': '甲',
                                    'product_count': 1, 'data_product_count': 0, 'notice_count': 1,
                                    'product_keys': ['mercado:MLBU4813895273']}],
                                'planned': [{'recipient_name': '甲', 'body': {'receive_id': 'ou_a'}, 'identities': [identity]}],
                                'unavailable_products': [{'key': 'mercado:MLBU4813895273', 'reason': '无搜索结果',
                                                         'recipients': [{'id': 'ou_a', 'name': '甲'}]}], 'errors': []}}
        row = build_run_summary(self.cfg, self.result, reports)['messages']
        self.assertEqual(row['notice_delivery_count'], 1)
        self.assertEqual(row['data_product_delivery_count'], 0)
        self.assertEqual(row['affected_recipients'], ['甲'])
        self.assertEqual(row['unsent_recipients'], [])

    def test_summary_reports_failed_recipients_without_mislabeling_dedup(self):
        key = 'mercado:MLBU4813895273'
        identities = {pid: json.dumps(['run1', pid, key], ensure_ascii=False) for pid in ('ou_a', 'ou_b', 'ou_c')}
        report = {'status': 'partial', 'skipped_count': 1, 'completed_identities': [identities['ou_c']],
                  'planned': [{'recipient_name': name, 'body': {'receive_id': pid}, 'identities': [identities[pid]]}
                              for name, pid in [('甲', 'ou_a'), ('乙', 'ou_b'), ('丙', 'ou_c')]],
                  'sent': [{'recipient_id': 'ou_a', 'recipient_name': '甲', 'product_count': 1, 'product_keys': [key]}],
                  'errors': [{'error': '发送给乙 HTTP 403'}]}
        row = build_run_summary(self.cfg, self.result, {'messages': report})['messages']
        self.assertEqual(row['unsent_recipients'], ['乙'])
        self.assertEqual(row['error_count'], 1)
        self.assertEqual(row['already_sent_count'], 1)

    def test_daily_summary_toggle_preserves_error_alerts_and_manual_summary_false(self):
        self.cfg['admin_notifications']['send_daily_summary'] = False
        normal = {'run_id': 'normal', 'status': 'ok', 'products': {}, 'platforms': {}}
        self.assertEqual(self.sink.write(normal)['status'], 'skipped')
        report = self.sink.write(self.result)
        self.assertIsNone(report['summary'])
        card = json.loads(self.client.request.call_args.kwargs['json']['content'])
        self.assertIn('运行异常', card['header']['title']['content'])
        self.cfg['admin_notifications']['send_daily_summary'] = True
        self.assertEqual(self.sink.write(normal, summary=False)['status'], 'skipped')

    def test_summary_update_does_not_reuse_old_issue_only_completion(self):
        first = self.sink.write(self.result, summary=False)
        self.assertEqual(len(first['sent']), 2)
        report = self.sink.write(self.result)
        self.assertEqual(len(report['sent']), 2)
        self.assertEqual(self.sink.write(self.result)['skipped_count'], 2)
        reports = {'sheets_write': {'status': 'ok', 'appended_count': 1, 'recovered_count': 0,
                                    'already_recorded_count': 0, 'skipped': []}}
        self.assertEqual(len(self.sink.write(self.result, reports=reports)['sent']), 2)

    @patch('competitive_tracking.sinks.admin_notifications.AdminNotificationSink')
    def test_notify_admins_forwards_summary_preference(self, sink):
        notify_admins(self.cfg, self.result, summary=False)
        self.assertFalse(sink.return_value.write.call_args.kwargs['summary'])

    def test_admin_dedup_and_same_run_replay(self):
        self.cfg['admin_notifications']['admins']['同一人'] = 'ou_a'
        self.sink.write(self.result)
        self.assertEqual(self.sink.write(self.result)['skipped_count'], 2)
        self.assertEqual(self.client.request.call_count, 2)
        self.result['run_id'] = 'run2'
        self.sink.write(self.result)
        self.assertEqual(self.client.request.call_count, 4)

    def test_one_failed_admin_does_not_block_others_and_retry_preserves_uuid(self):
        self.client.request.side_effect = [RuntimeError('网络超时'), {'message_id': 'om_b'}]
        report = self.sink.write(self.result)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(len(report['sent']), 1)
        failed_body = deepcopy(self.client.request.call_args_list[0].kwargs['json'])
        self.client.request.side_effect = None
        self.assertEqual(self.sink.write(self.result)['status'], 'ok')
        self.assertEqual(failed_body, self.client.request.call_args.kwargs['json'])

    def test_uncertain_old_admin_send_not_repeated(self):
        self.client.request.side_effect = RuntimeError('timeout')
        self.sink.write(self.result)
        count = self.client.request.call_count
        self.now += timedelta(hours=2)
        self.assertEqual(self.sink.write(self.result)['status'], 'partial')
        self.assertEqual(count, self.client.request.call_count)

    def test_many_long_problems_are_split_without_losing_details(self):
        self.result['products'] = {'tiktok:' + str(i): {'status': 'error', 'error': '错' * 3000} for i in range(20)}
        report = self.sink.write(self.result, dry_run=True)
        self.assertEqual(len(report['planned']), 6)
        self.assertEqual(len(report['issues']), 20)
        for batch in report['planned']:
            self.assertLess(len(json.dumps(batch['body'], ensure_ascii=False).encode()), 28000)
        self.client.request.assert_not_called()

    def test_log_capture_excludes_normal_login_and_other_libraries(self):
        handler = RunProblems()
        for name, message in [('competitive_tracking.browser', '无法保存 sessionStorage'),
                              ('competitive_tracking.platforms.mercado.collector', '请在项目浏览器中手动登录蓝鲸，最长等待 300 秒'),
                              ('other_library', 'warning')]:
            handler.handle(logging.LogRecord(name, logging.WARNING, '', 1, message, (), None))
        self.assertEqual(len(handler.problems), 1)

    @patch('competitive_tracking.runner.notify_admins')
    def test_runner_passes_source_and_storage_failure_without_losing_error(self, notify):
        notify.return_value = {'status': 'ok', 'report_path': 'admin.json'}
        source, sink = Mock(), Mock()
        source.read_records.side_effect = RuntimeError('读取任务表失败')
        result = run_once(self.cfg, source=source, registry={}, sink=sink)
        self.assertEqual(result['status'], 'error')
        self.assertIn('读取任务表失败', notify.call_args.args[1]['error'])
        sink.write.side_effect = OSError('磁盘已满')
        result = run_once(self.cfg, source=source, registry={}, sink=sink)
        self.assertIn('磁盘已满', result['error'])
        self.assertIn('磁盘已满', notify.call_args.args[1]['error'])

    @patch('competitive_tracking.runner.FeishuSink')
    @patch('competitive_tracking.runner.notify_admins')
    def test_runner_passes_full_output_report(self, notify, bitable):
        self.cfg['feishu_output']['enabled'] = True
        notify.return_value = {'status': 'ok', 'report_path': 'admin.json'}
        output = {'status': 'partial', 'report_path': 'writer.json', 'plan': {'errors': [{'error': '字段异常'}]}}
        bitable.return_value.write.return_value = output
        source = Mock()
        source.read_records.return_value = []
        result = run_once(self.cfg, source=source, registry={}, sink=Mock())
        self.assertEqual(notify.call_args.kwargs['reports']['feishu_write'], output)
        self.assertEqual(result['status'], 'partial')

    def test_config_default_disabled_and_invalid_admin_rejected(self):
        original = (ROOT / 'config.example.toml').read_text(encoding='utf-8')
        self.assertFalse(load_config(ROOT / 'config.example.toml')['admin_notifications']['enabled'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            for invalid in ('admin1 = "wrong_id"', 'admin1 = 123'):
                path.write_text(original + '\n' + invalid, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'open_id'):
                    load_config(path)

    @patch('competitive_tracking.cli.notify_admins')
    def test_manual_sync_and_send_failures_notify_but_dry_run_does_not(self, notify):
        for command in ('write-feishu', 'write-sheets', 'send-feishu'):
            report = {'status': 'error', 'error': 'HTTP 403'}
            notify_command_problem(self.cfg, command, report=report)
            self.assertEqual(notify.call_args.kwargs['reports'][command], report)
        self.assertEqual(notify.call_count, 3)
        for command in ('check-config', 'parse-html'):
            notify_command_problem(self.cfg, command, error='校验失败')
        notify_command_problem(self.cfg, 'write-sheets', error='表头不匹配', dry_run=True)
        self.assertEqual(notify.call_count, 3)

    @patch('competitive_tracking.runner.notify_admins')
    def test_runner_notifications_failure_does_not_recurse(self, notify):
        notify.return_value = {'status': 'error', 'error': '机器人不可用'}
        source = Mock()
        source.read_records.return_value = []
        result = run_once(self.cfg, source=source, registry={}, sink=Mock())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['admin_notifications']['status'], 'error')
        notify.assert_called_once()

    def test_log_and_structured_error_are_not_duplicated(self):
        error = '搜索 MLBU4813895273 结果未完成更新'
        issues = collect_problems(self.result, logs=[{'stage': 'collector', 'key': '', 'message': '采集失败：' + error}])
        self.assertEqual(len(issues), 1)


if __name__ == '__main__':
    unittest.main()
