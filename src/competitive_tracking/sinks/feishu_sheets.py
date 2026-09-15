from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.sinks.feishu import finite_number, product_values
from competitive_tracking.sources.feishu import text_value
from competitive_tracking.storage import atomic_json

log = logging.getLogger(__name__)

# Fixed A:Y order requested for the historical worksheet; independent of Bitable labels.
COLUMNS = [
    ("product_id", "商品ID"), ("name", "商品标题"), ("updated_at", "更新日期"),
    ("competitor", "竞品"), ("owners", "负责人"), ("price_brl", "价格-BRL"),
    ("price_cny", "价格-RMB"), ("sales_total", "总销量"), ("sales_7d", "7天销量"),
    ("sales_30d", "30天销量"), ("sales_60d", "60天销量"), ("sales_90d", "90天销量"),
    ("revenue_30d_brl", "近30天销售额"), ("growth_7d", "7天销量变化环比"),
    ("growth_30d", "30天销量变化环比"), ("conversion", "转换率"),
    ("review_count", "评论数"), ("rating", "评分"), ("brand", "品牌"),
    ("seller", "卖家名称"), ("shop_type", "店铺类型"), ("bsr", "BSR"),
    ("url", "商品链接"), ("image_url", "商品图片链接"), ("categories", "类目路径"),
]


def supplement_history_metadata(result, source_records, cfg):
    """Old JSON may lack owners/competitor. Fill only missing metadata, without filtering history."""
    result = deepcopy(result)
    fields = cfg["feishu"]["fields"]
    index = {}
    for record in source_records:
        data = record.get("fields", {})
        platform = text_value(data.get(fields["platform"])).lower()
        pid = text_value(data.get(fields["product_id"])).upper()
        index[(platform, pid, record.get("record_id"))] = data
    for entry in result.get("products", {}).values():
        for record in entry.get("tracking_records", []):
            data = index.get((entry.get("platform"), entry.get("product_id"), record.get("record_id")))
            if data is not None:
                record.setdefault("competitor", text_value(data.get(fields.get("competitor", "是否竞品"))) or None)
                record.setdefault("owners", data.get(fields.get("owner", "负责人")) or [])
    return result


def history_row(entry, written_at):
    values, warnings = product_values(entry)
    values["updated_at"] = written_at
    owners = {}
    for record in entry.get("tracking_records", []):
        for person in record.get("owners") or []:
            if isinstance(person, dict):
                identity = person.get("id") or person.get("open_id") or person.get("name")
                if identity:
                    owners[identity] = person.get("name") or identity
    values["owners"] = "、".join(owners.values())
    for key in ("growth_7d", "growth_30d", "conversion"):
        n = finite_number(values.get(key))
        values[key] = f"{n:.2f}%" if n is not None else ""
    row = [values.get(key) if values.get(key) is not None else "" for key, _ in COLUMNS]
    return row, warnings


def blank(value):
    # Whitespace, 0, and formulas are occupied; never overwrite them as "empty".
    return value is None or value == ""


def same_rows(actual, expected):
    if len(actual) != len(expected):
        return False
    for have, want in zip(actual, expected):
        for col, value in enumerate(want):
            old = have[col] if col < len(have) else None
            if blank(old) and blank(value):
                continue
            # Sheets turns a URL string into a rich-text hyperlink on readback.
            # Verify both its visible text and destination, not just one of them.
            if isinstance(value, str) and isinstance(old, list) and len(old) == 1:
                link = old[0]
                if isinstance(link, dict) and link.get("type") == "url" and link.get("text") == value and link.get("link") == value:
                    continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if finite_number(old) != finite_number(value):
                    return False
            elif old != value:
                return False
    return True


class FeishuSheetsSink:
    def __init__(self, config, client=None, clock=None):
        self.config = config
        self.cfg = config["feishu_sheets"]
        self.client = client or FeishuClient(config["feishu"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        token = quote(self.cfg["spreadsheet_token"], safe="")
        self.base = f"/sheets/v2/spreadsheets/{token}"
        self.meta_path = f"/sheets/v3/spreadsheets/{token}/sheets/query"
        self.sheet_id = self.cfg["sheet_id"]
        target = hashlib.sha256(f"{token}:{self.sheet_id}".encode()).hexdigest()[:16]
        self.state_path = config["app"]["session_dir"] / f"sheets_history_{target}.json"

    def _state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"completed": {}, "pending": []}

    def _metadata(self):
        sheets = self.client.request("GET", self.meta_path).get("sheets", [])
        sheet = next((s for s in sheets if s.get("sheet_id") == self.sheet_id), None)
        if sheet is None:
            raise ValueError("配置的二维工作表 sheet_id 不存在")
        if sheet.get("grid_properties", {}).get("column_count", 0) < len(COLUMNS):
            raise ValueError("二维表不足 25 列，无法按 A:Y 写入")
        return sheet

    def _range(self, start, end, last="Y"):
        return f"{self.sheet_id}!A{start}:{last}{end}"

    def _read(self, range_, *, formulas=False):
        data = self.client.request("GET", self.base + "/values/" + quote(range_, safe="!:"),
                                   params={"valueRenderOption": "Formula" if formulas else "UnformattedValue",
                                           "dateTimeRenderOption": "FormattedString"})
        values = data.get("valueRange", {}).get("values")
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise RuntimeError("二维表读取响应缺少有效 values，不能将未知区域当作空白")
        return values

    def _check_headers(self, sheet):
        if not self.cfg["validate_headers"]:
            return
        count = self.cfg["data_start_row"] - 1
        rows = self._read(self._range(1, count))
        rows = [list(row) + [None] * (25 - len(row)) for row in rows]
        rows += [[None] * 25 for _ in range(count - len(rows))]
        for merge in sheet.get("merges", []):
            top, left = merge["start_row_index"], merge["start_column_index"]
            bottom, right = merge["end_row_index"], merge["end_column_index"]
            if bottom >= count:
                raise ValueError("表头合并单元格进入数据区域，请检查 data_start_row")
            if top < count and left < 25:
                for col in range(left, min(right + 1, 25)):
                    rows[top][col] = rows[top][left]
        normalize = lambda value: re.sub(r"[\s\-]", "", str(value)).lower()
        for col, (key, label) in enumerate(COLUMNS):
            actual = normalize("".join(str(row[col]) for row in rows if not blank(row[col])))
            allowed = {normalize(label)}
            if key.startswith("sales_"):
                period = key.removeprefix("sales_")
                allowed.add("销量总" if period == "total" else "销量" + period.replace("d", "天"))
            if key.startswith("growth_"):
                allowed.add("销量变化环比" + key.removeprefix("growth_").replace("d", "天"))
            if actual not in allowed:
                raise ValueError(f"二维表第 {col + 1} 列表头不匹配：期望 {label}，实际 {actual}")

    def _next_row(self, sheet):
        first = self.cfg["data_start_row"]
        last = first - 1
        count = sheet["grid_properties"]["row_count"]
        step = self.cfg["scan_chunk_rows"]
        for start in range(first, count + 1, step):
            rows = self._read(self._range(start, min(count, start + step - 1), "A"), formulas=True)
            for offset, row in enumerate(rows):
                if row and not blank(row[0]):
                    last = start + offset
        return last + 1

    def _ensure_capacity(self, last_row):
        while True:
            count = self._metadata()["grid_properties"]["row_count"]
            if count >= last_row:
                return
            # Only add blank rows at the end; retries cannot move existing history.
            length = min(5000, max(self.cfg["grow_rows"], last_row - count))
            self.client.request("POST", self.base + "/dimension_range", json={
                "dimension": {"sheetId": self.sheet_id, "majorDimension": "ROWS", "length": length}})
            if self._metadata()["grid_properties"]["row_count"] <= count:
                raise RuntimeError("二维表扩容后行数未增加")

    def _locate_batch(self, batch):
        """Recover a whole batch moved by a sheet row insertion/deletion, read-only."""
        count = self._metadata()["grid_properties"]["row_count"]
        step = self.cfg["scan_chunk_rows"]
        matches = []
        for start in range(self.cfg["data_start_row"], count + 1, step):
            rows = self._read(self._range(start, min(count, start + step - 1), "A"))
            for offset, row in enumerate(rows):
                if not row or row[0] != batch["values"][0][0]:
                    continue
                end = start + offset + len(batch["values"]) - 1
                if end <= count:
                    candidate = self._range(start + offset, end)
                    if same_rows(self._read(candidate), batch["values"]):
                        matches.append(candidate)
        if len(matches) > 1:
            raise RuntimeError("发现多处完全相同的待恢复批次，无法确定历史位置")
        return matches[0] if matches else None

    def _complete_batch(self, batch, state, *, recovering=False):
        actual = self._read(batch["range"])
        if not same_rows(actual, batch["values"]):
            relocated = self._locate_batch(batch) if recovering else None
            if relocated:
                log.info("已核对待恢复批次位置：%s → %s", batch["range"], relocated)
                batch["range"] = relocated
            else:
                occupied = self._read(batch["range"], formulas=True)
                if any(not blank(cell) for row in occupied for cell in row):
                    raise RuntimeError(f"待追加区域 {batch['range']} 已有其他内容，停止以免覆盖历史")
                self.client.request("PUT", self.base + "/values", json={
                    "valueRange": {"range": batch["range"], "values": batch["values"]}})
                if not same_rows(self._read(batch["range"]), batch["values"]):
                    raise RuntimeError(f"二维表写后核对失败：{batch['range']}，保留待恢复批次")
        for key in batch["identities"]:
            state["completed"][key] = {"range": batch["range"], "written_at": batch["written_at"]}
        state["pending"].remove(batch)
        atomic_json(self.state_path, state)

    def write(self, result, *, dry_run=False):
        run_id = result.get("run_id")
        if not run_id:
            raise ValueError("历史追加需要 run_id 来区分采集批次")
        sheet = self._metadata()
        self._check_headers(sheet)
        state = self._state()
        now = self.clock().astimezone(ZoneInfo(self.config["schedule"]["timezone"]))
        report_path = self.config["app"]["output_dir"] / f"sheets_write_{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
        report = {"status": "preview" if dry_run else "running", "run_id": run_id, "columns": [v for _, v in COLUMNS],
                  "appended_count": 0, "recovered_count": 0, "already_recorded_count": 0, "skipped": [],
                  "batches": [], "warnings": [], "report_path": str(report_path)}
        atomic_json(report_path, report)
        try:
            if state["pending"] and dry_run:
                raise RuntimeError("存在未确认的历史写入批次；先正式恢复该批次再预览新追加")
            for batch in list(state["pending"]):
                self._complete_batch(batch, state, recovering=True)
                report["recovered_count"] += len(batch["values"])
            items = []
            for key, entry in result.get("products", {}).items():
                if entry.get("platform") != self.cfg["platform"]:
                    continue
                p = entry.get("product")
                if entry.get("status") not in ("ok", "partial") or not p:
                    report["skipped"].append({"key": key, "reason": "无已采集的商品数据"})
                    continue
                if p.get("platform") != self.cfg["platform"] or p.get("product_id") != entry.get("product_id") or key != f"{self.cfg['platform']}:{p.get('product_id')}":
                    raise ValueError("历史数据的平台或商品 ID 不一致")
                identity = json.dumps([run_id, key], ensure_ascii=False)
                if identity in state["completed"]:
                    report["already_recorded_count"] += 1
                    continue
                row, warnings = history_row(entry, now.strftime("%Y-%m-%d %H:%M:%S"))
                report["warnings"].extend(f"{key}: {w}" for w in warnings)
                items.append((identity, row))
            next_row = self._next_row(self._metadata()) if items else None
            report["planned_count"] = len(items)
            for start in range(0, len(items), self.cfg["batch_size"]):
                group = items[start:start + self.cfg["batch_size"]]
                end = next_row + len(group) - 1
                range_ = self._range(next_row, end)
                current_count = self._metadata()["grid_properties"]["row_count"]
                if next_row <= current_count:
                    occupied = self._read(self._range(next_row, min(end, current_count)), formulas=True)
                    if any(not blank(cell) for row in occupied for cell in row):
                        raise RuntimeError(f"A 列末尾后区域 {range_} 其他列已有内容，停止以免覆盖")
                batch = {"range": range_, "values": [row for _, row in group],
                         "identities": [key for key, _ in group], "written_at": now.isoformat()}
                report["batches"].append(batch)
                atomic_json(report_path, report)
                if not dry_run:
                    self._ensure_capacity(end)
                    state["pending"].append(batch)
                    atomic_json(self.state_path, state)
                    self._complete_batch(batch, state)
                    report["appended_count"] += len(group)
                next_row = end + 1
            report["status"] = "preview" if dry_run else ("partial" if report["warnings"] else "ok")
        except Exception as exc:
            report["status"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"
            log.error("二维表追加失败：%s", report["error"])
        finally:
            atomic_json(report_path, report)
        log.info("二维表追加 %s：新增历史 %d，恢复 %d，同批次已记录 %d；详情 %s", report["status"],
                 report["appended_count"], report["recovered_count"], report["already_recorded_count"], report_path)
        return report
