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
from competitive_tracking.sinks.admin_notifications import AdminNotificationSink, RunProblems, collect_problems

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
        report = self.sink.write(result, reports={'messages': {'status': 'ok', 'skipped_count': 3}})
        self.assertEqual(report['status'], 'skipped')
        self.client.request.assert_not_called()
        self.assertFalse(self.sink.state_path.exists())

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
