from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from competitive_tracking.config import load_config
from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.runner import run_once
from competitive_tracking.sinks.feishu import FeishuSink, KINDS, encode_value, enrich_from_source
from competitive_tracking.sources.feishu import select_targets

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return {"schema_version": 1, "run_id": "test_run", "status": "ok", "products": {"mercado:MLB123": {
        "status": "ok", "platform": "mercado", "product_id": "MLB123",
        "tracking_records": [{"record_id": "source1", "competitor": "是", "owners": [{"id": "ou_owner"}], "push_enabled": False}],
        "product": {"platform": "mercado", "product_id": "MLB123", "name": "商品完整标题",
                    "captured_at": "2026-09-15T03:00:00+00:00", "prices": {"BRL": 30.9, "CNY": 40.17},
                    "sales": {"7d": 0, "30d": 219, "total": 354}, "revenue": {"近30天销售额($BRL)": {"amount": 6767.1, "currency": "BRL"}},
                    "sales_growth_percent": {"7d": -69.5, "30d": 1890.91}, "conversion_rate_percent": 8.1,
                    "review_count": 0, "rating": 0, "bsr": "--", "categories": ["父类", "子类"],
                    "brand_and_seller": ["品牌", "卖家 有空格", "本土店"], "url": "https://example.com/p", "images": ["https://example.com/p.jpg"]}
    }}}


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg["app"]["session_dir"] = Path(self.temp.name) / "session"
        self.cfg["app"]["output_dir"] = Path(self.temp.name) / "data"
        self.cfg["feishu_output"].update(enabled=True, app_token="app_test", table_id="tbl_test")
        self.rows = []
        types = {"text": 1, "number": 2, "percent": 1, "select": 3, "people": 11, "url": 15, "datetime": 5}
        self.schema = [{"field_name": name, "type": types[KINDS[key]], "property": {}}
                       for key, name in self.cfg["feishu_output"]["fields"].items()]
        for field in self.schema:
            if field["type"] == 3:
                field["property"] = {"options": [{"name": "是"}, {"name": "否"}]}
        self.client = Mock(spec=FeishuClient)
        self.client.table_path.side_effect = FeishuClient.table_path
        self.client.list_all.side_effect = lambda path, **_: deepcopy(self.schema if path.endswith("/fields") else self.rows)
        def write(method, endpoint, **kwargs):
            result = []
            for record in kwargs["json"]["records"]:
                if endpoint.endswith("batch_create"):
                    row = {"record_id": f"rec_{len(self.rows)}", "fields": deepcopy(record["fields"])}
                    self.rows.append(row)
                else:
                    row = next(r for r in self.rows if r["record_id"] == record["record_id"])
                    row["fields"].update(deepcopy(record["fields"]))
                result.append(deepcopy(row))
            return {"records": result}
        self.client.request.side_effect = write
        self.now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
        self.sink = FeishuSink(self.cfg, self.client, clock=lambda: self.now)

    def test_create_exact_schema_and_repeat_no_duplicates(self):
        report = self.sink.write(sample())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["verified_count"], 1)
        fields = self.rows[0]["fields"]
        self.assertEqual(fields["价格-RMB"], 40.17)
        self.assertEqual(fields["转换率"], "8.10%")
        self.assertEqual(fields["销量变化-7天环比"], "-69.50%")
        self.assertEqual(fields["负责人"], [{"id": "ou_owner"}])
        self.assertEqual(fields["竞品"], "是")
        self.assertEqual(fields["销量-7天"], 0)
        self.assertNotIn("bsr", fields)
        self.assertEqual(fields["类目路径"], "父类 > 子类")
        self.assertEqual(fields["卖家名称"], "卖家 有空格")
        self.assertEqual(fields["商品链接"]["link"], "https://example.com/p")
        self.assertIn("client_token", self.client.request.call_args.kwargs["params"])
        original_id = self.rows[0]["record_id"]
        first_date = self.rows[0]["fields"]["更新日期"]
        self.assertEqual(first_date, int(self.now.timestamp() * 1000))
        self.now += timedelta(minutes=5)
        again = self.sink.write(sample())
        self.assertEqual(len(again["plan"]["updated"]), 1)
        self.assertEqual(self.client.request.call_count, 2)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.rows[0]["record_id"], original_id)
        self.assertGreater(self.rows[0]["fields"]["更新日期"], first_date)
        self.assertEqual(self.client.request.call_args.kwargs["json"]["records"], [
            {"record_id": original_id, "fields": {"更新日期": int(self.now.timestamp() * 1000)}}])

    def test_patch_changed_values_only_preserve_unknowns_and_manual_columns(self):
        self.sink.write(sample())
        self.rows[0]["fields"].update({"bsr": 12, "人工备注": "保留"})
        newer = sample()
        newer["products"]["mercado:MLB123"]["product"].update(captured_at="2026-09-16T03:00:00+00:00", prices={"BRL": 31.9})
        report = self.sink.write(newer)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(self.client.request.call_args.kwargs["json"]["records"][0]["fields"], {"价格-BRL": 31.9, "更新日期": int(self.now.timestamp() * 1000)})
        self.assertEqual(self.rows[0]["fields"]["价格-RMB"], 40.17)
        self.assertEqual(self.rows[0]["fields"]["bsr"], 12)
        self.assertEqual(self.rows[0]["fields"]["人工备注"], "保留")

    def test_readback_string_numbers_are_compared_numerically(self):
        real_write = self.client.request.side_effect
        def write_as_strings(*args, **kwargs):
            result = real_write(*args, **kwargs)
            for row in self.rows:
                for key, value in row["fields"].items():
                    if isinstance(value, (int, float)):
                        row["fields"][key] = str(value)
            return result
        self.client.request.side_effect = write_as_strings
        self.assertEqual(self.sink.write(sample())["status"], "ok")
        again = self.sink.write(sample())
        self.assertEqual(again["verified_count"], 1)
        self.assertEqual(self.client.request.call_count, 2)

    def test_duplicate_target_ids_are_not_updated(self):
        self.rows.extend([{"record_id": "a", "fields": {"商品ID": "MLB123"}}, {"record_id": "b", "fields": {"商品ID": "MLB123"}}])
        report = self.sink.write(sample())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(report["plan"]["errors"]), 1)
        self.client.request.assert_not_called()

    def test_blank_id_row_is_preserved_even_if_title_matches(self):
        self.rows.append({"record_id": "blank", "fields": {"商品标题": "商品完整标题"}})
        report = self.sink.write(sample())
        self.assertEqual(report["plan"]["blank_id_rows_preserved"], 1)
        self.assertNotIn("商品ID", self.rows[0]["fields"])
        self.assertEqual(len(self.rows), 2)

    def test_old_snapshot_cannot_overwrite_newer_synced_snapshot(self):
        self.sink.write(sample())
        old = sample()
        old["products"]["mercado:MLB123"]["product"]["captured_at"] = "2026-09-14T03:00:00+00:00"
        report = self.sink.write(old)
        self.assertEqual(len(report["plan"]["skipped"]), 1)
        self.assertEqual(self.client.request.call_count, 1)

    def test_dry_run_makes_no_external_or_ledger_changes(self):
        report = self.sink.write(sample(), dry_run=True)
        self.assertEqual(report["status"], "preview")
        self.client.request.assert_not_called()
        self.assertFalse(self.sink.state_path.exists())
        self.assertTrue(Path(report["report_path"]).exists())

    def test_schema_mismatch_stops_before_writes(self):
        self.schema[0]["type"] = 2
        with self.assertRaisesRegex(ValueError, "类型不匹配"):
            self.sink.write(sample())
        self.client.request.assert_not_called()

    def test_uncertain_create_reuses_saved_client_token(self):
        self.client.request.side_effect = RuntimeError("timeout")
        self.assertEqual(self.sink.write(sample())["status"], "error")
        first = self.client.request.call_args.kwargs["params"]["client_token"]
        first_body = deepcopy(self.client.request.call_args.kwargs["json"])
        self.now += timedelta(minutes=5)
        self.sink.write(sample())
        self.assertEqual(self.client.request.call_args.kwargs["params"]["client_token"], first)
        self.assertEqual(self.client.request.call_args.kwargs["json"], first_body)

    def test_optional_update_date_can_be_disabled(self):
        self.cfg["feishu_output"]["fields"]["updated_at"] = ""
        self.sink.write(sample())
        self.now += timedelta(minutes=5)
        report = self.sink.write(sample())
        self.assertEqual(len(report["plan"]["unchanged"]), 1)
        self.assertNotIn("更新日期", self.rows[0]["fields"])
        self.assertEqual(self.client.request.call_count, 1)

    def test_numeric_percent_uses_fraction_only_for_percent_format(self):
        self.assertEqual(encode_value("percent", 8.1, {"type": 2, "property": {"formatter": "0.00%"}}), 0.081)
        self.assertEqual(encode_value("percent", 8.1, {"type": 2, "property": {"formatter": "0.00"}}), 8.1)

    def test_conflicting_single_owner_is_not_arbitrarily_selected(self):
        result = sample()
        result["products"]["mercado:MLB123"]["tracking_records"][0]["owners"].append({"id": "ou_other"})
        report = self.sink.write(result)
        self.assertEqual(report["status"], "partial")
        self.assertNotIn("负责人", self.rows[0]["fields"])

    def test_error_and_other_platform_are_not_written(self):
        result = sample()
        result["products"]["mercado:MLB123"]["status"] = "error"
        result["products"]["shopee:MLB123"] = {"platform": "shopee", "status": "ok"}
        report = self.sink.write(result)
        self.assertEqual(len(report["plan"]["skipped"]), 1)
        self.client.request.assert_not_called()

    def test_write_readback_mismatch_reported(self):
        real_write = self.client.request.side_effect
        def bad_write(*args, **kwargs):
            result = real_write(*args, **kwargs)
            self.rows[0]["fields"].pop("转换率")
            return result
        self.client.request.side_effect = bad_write
        self.assertEqual(self.sink.write(sample())["status"], "error")

    def test_source_metadata_refresh_uses_id_and_current_filter(self):
        source = [{"record_id": "source1", "fields": {"平台": "mercado", "商品ID": "MLB123", "监控开关": "开启", "数据推送人": [{"id": "ou_recipient"}], "负责人": [{"id": "ou_new"}], "是否竞品": "否"}}]
        result = enrich_from_source(sample(), source, self.cfg)
        meta = result["products"]["mercado:MLB123"]["tracking_records"][0]
        self.assertEqual(meta["owners"], [{"id": "ou_new"}])
        self.assertEqual(meta["competitor"], "否")
        source[0]["fields"]["监控开关"] = "关闭"
        self.assertEqual(enrich_from_source(sample(), source, self.cfg)["products"]["mercado:MLB123"]["status"], "skipped")

    @patch("competitive_tracking.runner.FeishuSink")
    def test_runner_keeps_local_result_when_destination_fails(self, writer):
        source, local = Mock(), Mock()
        source.read_records.return_value = []
        writer.return_value.write.side_effect = RuntimeError("permission denied")
        result = run_once(self.cfg, source=source, sink=local)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["feishu_write"]["status"], "error")
        self.assertEqual(local.write.call_count, 2)


if __name__ == "__main__":
    unittest.main()
