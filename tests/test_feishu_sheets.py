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
from competitive_tracking.sinks.feishu_sheets import COLUMNS, FeishuSheetsSink, same_rows, supplement_history_metadata
from test_feishu_output import sample

ROOT = Path(__file__).resolve().parents[1]


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg["app"]["session_dir"] = Path(self.temp.name) / "session"
        self.cfg["app"]["output_dir"] = Path(self.temp.name) / "data"
        self.cfg["feishu_sheets"].update(enabled=True, spreadsheet_token="test", sheet_id="sheet")
        self.meta = {"sheet_id": "sheet", "title": "Test", "grid_properties": {"column_count": 25, "row_count": 200}, "merges": [
            {"start_row_index": 0, "end_row_index": 0, "start_column_index": a, "end_column_index": b} for a, b in [(5, 6), (7, 11), (13, 14)]]}
        self.cells = {
            1: ["商品ID", "商品标题", "更新日期", "竞品", "负责人", "价格", None, "销量", None, None, None, None,
                "近30天销售额", "销量变化环比", None, "转换率", "评论数", "评分", "品牌", "卖家名称", "店铺类型", "BSR", "商品链接", "商品图片链接", "类目路径"],
            2: [None] * 5 + ["BRL", "RMB", "总", "7天", "30天", "60天", "90天", None, "7天", "30天"] + [None] * 10,
            3: [333] + [None] * 24,
        }
        self.client = Mock(spec=FeishuClient)
        self.writes = []
        self.client.request.side_effect = self.api
        self.sink = FeishuSheetsSink(self.cfg, self.client, clock=lambda: datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc))
        self.result = sample()
        self.result["products"]["mercado:MLB123"]["tracking_records"][0]["owners"][0]["name"] = "测试负责人"

    def api(self, method, path, **kwargs):
        if path.endswith("/sheets/query"):
            return {"sheets": [deepcopy(self.meta)]}
        if method == "GET":
            start, end_col, end = re.search(r"!A(\d+):([AY])(\d+)", path).groups()
            values = [deepcopy(self.cells.get(row, [None] * 25))[:1 if end_col == 'A' else 25]
                      for row in range(int(start), int(end) + 1)]
            return {"valueRange": {"values": values}}
        if path.endswith("/dimension_range"):
            self.meta["grid_properties"]["row_count"] += kwargs["json"]["dimension"]["length"]
            return {"addCount": kwargs["json"]["dimension"]["length"]}
        if method == "PUT":
            body = deepcopy(kwargs["json"]["valueRange"])
            self.writes.append(body)
            start = int(re.search(r"!A(\d+)", body["range"]).group(1))
            for i, row in enumerate(body["values"]):
                self.cells[start + i] = row
            return {"updatedRange": body["range"]}
        raise AssertionError((method, path))

    def test_append_25_columns_preserves_merged_headers_and_existing_value(self):
        before = deepcopy(self.cells)
        report = self.sink.write(self.result)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["appended_count"], 1)
        self.assertEqual(self.writes[0]["range"], "sheet!A4:Y4")
        self.assertEqual([self.cells[i] for i in (1, 2, 3)], [before[i] for i in (1, 2, 3)])
        row = self.cells[4]
        self.assertEqual(len(row), 25)
        self.assertEqual(row[:8], ["MLB123", "商品完整标题", "2026-09-15 16:00:00", "是", "测试负责人", 30.9, 40.17, 354])
        self.assertEqual(row[8:18], [0, 219, "", "", 6767.1, "-69.50%", "1890.91%", "8.10%", 0, 0])
        self.assertEqual(row[18:], ["品牌", "卖家 有空格", "本土店", "", "https://example.com/p", "https://example.com/p.jpg", "父类 > 子类"])

    def test_new_capture_run_appends_even_when_product_is_identical(self):
        self.sink.write(self.result)
        before = deepcopy(self.cells[4])
        newer = deepcopy(self.result)
        newer["run_id"] = "another_capture"
        report = self.sink.write(newer)
        self.assertEqual(report["appended_count"], 1)
        self.assertEqual(self.writes[-1]["range"], "sheet!A5:Y5")
        self.assertEqual(self.cells[4], before)
        self.assertEqual(self.cells[5], before)

    def test_same_capture_replay_does_not_duplicate(self):
        self.sink.write(self.result)
        report = self.sink.write(self.result)
        self.assertEqual(report["appended_count"], 0)
        self.assertEqual(report["already_recorded_count"], 1)
        self.assertEqual(len(self.writes), 1)

    def test_hyperlink_readback_recovers_without_duplicate_write(self):
        def rich_read(method, path, **kwargs):
            response = self.api(method, path, **kwargs)
            for row in response.get("valueRange", {}).get("values", []):
                for col in (22, 23):
                    if len(row) > col and isinstance(row[col], str) and row[col].startswith("https://"):
                        row[col] = [{"type": "url", "text": row[col], "link": row[col], "cellPosition": None}]
            return response
        self.client.request.side_effect = rich_read
        self.assertEqual(self.sink.write(self.result)["status"], "ok")
        self.assertEqual(self.sink.write(self.result)["already_recorded_count"], 1)
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(same_rows([[ [{"type": "url", "text": "https://example.com", "link": "https://changed.example"}] ]], [["https://example.com"]]))

    def test_missing_values_response_is_not_treated_as_empty(self):
        self.client.request.return_value = {}
        self.client.request.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "缺少有效 values"):
            self.sink._read("sheet!A4:Y4")
        self.assertEqual(self.writes, [])

    def test_last_used_a_row_not_middle_empty_hole(self):
        self.cells[8] = ["old_product"] + [None] * 24
        self.sink.write(self.result)
        self.assertEqual(self.writes[0]["range"], "sheet!A9:Y9")

    def test_orphan_other_column_is_not_overwritten(self):
        self.cells[4] = [None, "manual note"] + [None] * 23
        report = self.sink.write(self.result)
        self.assertEqual(report["status"], "error")
        self.assertEqual(self.cells[4][1], "manual note")
        self.assertEqual(self.writes, [])

    def test_header_order_mismatch_fails_before_mutation(self):
        self.cells[1][0], self.cells[1][1] = self.cells[1][1], self.cells[1][0]
        with self.assertRaisesRegex(ValueError, "表头不匹配"):
            self.sink.write(self.result)
        self.assertEqual(self.writes, [])

    def test_dry_run_does_not_write_or_change_ledger(self):
        report = self.sink.write(self.result, dry_run=True)
        self.assertEqual(report["status"], "preview")
        self.assertEqual(report["batches"][0]["range"], "sheet!A4:Y4")
        self.assertFalse(self.sink.state_path.exists())
        self.assertEqual(self.writes, [])

    def test_sheet_grows_at_end_when_full(self):
        self.meta["grid_properties"]["row_count"] = 3
        report = self.sink.write(self.result)
        self.assertEqual(report["status"], "ok")
        self.assertGreaterEqual(self.meta["grid_properties"]["row_count"], 4)
        self.assertEqual(self.cells[3][0], 333)

    def test_timeout_after_server_write_is_recovered_without_second_append(self):
        def flaky(method, path, **kwargs):
            response = self.api(method, path, **kwargs)
            if method == "PUT":
                raise RuntimeError("response lost")
            return response
        self.client.request.side_effect = flaky
        self.assertEqual(self.sink.write(self.result)["status"], "error")
        self.client.request.side_effect = self.api
        report = self.sink.write(self.result)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["recovered_count"], 1)
        self.assertEqual(len(self.writes), 1)

    def test_pending_range_changed_by_human_stops_without_overwriting(self):
        def failed(method, path, **kwargs):
            if method == "PUT":
                raise RuntimeError("timeout")
            return self.api(method, path, **kwargs)
        self.client.request.side_effect = failed
        self.sink.write(self.result)
        self.cells[4] = ["manual"] + [None] * 24
        self.client.request.side_effect = self.api
        self.assertEqual(self.sink.write(self.result)["status"], "error")
        self.assertEqual(self.cells[4][0], "manual")
        self.assertEqual(self.writes, [])

    def test_pending_batch_moved_by_row_deletion_is_recovered_read_only(self):
        def lost_response(method, path, **kwargs):
            response = self.api(method, path, **kwargs)
            if method == "PUT":
                raise RuntimeError("response lost")
            return response
        self.client.request.side_effect = lost_response
        self.sink.write(self.result)
        self.cells[3] = self.cells.pop(4)
        self.client.request.side_effect = self.api
        report = self.sink.write(self.result)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["recovered_count"], 1)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(next(iter(self.sink._state()["completed"].values()))["range"], "sheet!A3:Y3")

    def test_partial_is_recorded_and_missing_product_is_skipped(self):
        self.result["products"]["mercado:MLB123"]["status"] = "partial"
        self.result["products"]["mercado:MLB456"] = {"platform": "mercado", "status": "error"}
        report = self.sink.write(self.result)
        self.assertEqual(report["appended_count"], 1)
        self.assertEqual(len(report["skipped"]), 1)

    def test_history_metadata_does_not_remove_disabled_products(self):
        entry = self.result["products"]["mercado:MLB123"]
        entry["tracking_records"] = [{"record_id": "old_record"}]
        source = [{"record_id": "old_record", "fields": {"平台": "mercado", "商品ID": "MLB123", "监控开关": "关闭", "是否竞品": "否", "负责人": [{"id": "ou_new", "name": "甲"}]}}]
        result = supplement_history_metadata(self.result, source, self.cfg)
        self.assertEqual(result["products"]["mercado:MLB123"]["status"], "ok")
        self.assertEqual(result["products"]["mercado:MLB123"]["tracking_records"][0]["competitor"], "否")

    @patch("competitive_tracking.runner.FeishuSheetsSink")
    @patch("competitive_tracking.runner.FeishuSink")
    def test_bitable_failure_does_not_block_history(self, bitable, sheets):
        self.cfg["feishu_output"]["enabled"] = True
        bitable.return_value.write.side_effect = RuntimeError("bitable offline")
        sheets.return_value.write.return_value = {"status": "ok", "report_path": "test"}
        source, local = Mock(), Mock()
        source.read_records.return_value = []
        result = run_once(self.cfg, source=source, sink=local)
        sheets.return_value.write.assert_called_once()
        self.assertEqual(result["sheets_write"]["status"], "ok")
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
