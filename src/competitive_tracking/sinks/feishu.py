from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import uuid

from competitive_tracking.integrations.feishu import FeishuClient
from competitive_tracking.sources.feishu import select_targets, text_value
from competitive_tracking.storage import atomic_json
from competitive_tracking.sinks.shopee_fields import KINDS as SHOPEE_KINDS, values as shopee_values

log = logging.getLogger(__name__)

# Value type is independent of destination labels, which live in config.toml.
KINDS = {
    "product_id": "text", "name": "text", "competitor": "select", "owners": "people",
    "price_brl": "number", "price_cny": "number", "sales_total": "number",
    "sales_7d": "number", "sales_30d": "number", "sales_60d": "number", "sales_90d": "number",
    "revenue_30d_brl": "number", "growth_7d": "percent", "growth_30d": "percent",
    "conversion": "percent", "review_count": "number", "rating": "number", "bsr": "number",
    "brand": "text", "seller": "text", "shop_type": "text", "url": "url", "image_url": "url", "categories": "text",
    "updated_at": "datetime",
}
KINDS.update(SHOPEE_KINDS)


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def instant(value):
    if not value:
        raise ValueError("商品缺少 captured_at，无法判断数据新旧")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("captured_at 必须带时区")
    return parsed


def enrich_from_source(result: dict, source_records: list[dict], cfg: dict) -> dict:
    """For replay: refresh metadata by platform+ID, never match the display title."""
    result = deepcopy(result)
    platform = cfg["feishu_output"]["platform"]
    current = {t.key: t for t in select_targets(source_records, cfg["feishu"]["fields"], platform)}
    for key, entry in result.get("products", {}).items():
        if entry.get("platform") != platform:
            continue
        if key not in current:
            entry["status"] = "skipped"
            entry["error"] = "当前监控表已无符合筛选条件的对应商品，跳过历史补写"
        else:
            entry["tracking_records"] = current[key].records
    return result


def product_values(entry: dict) -> tuple[dict, list[str]]:
    p = entry["product"]
    warnings = []
    values = {k: p.get(k) for k in ("product_id", "name", "review_count", "rating", "url")}
    values.update(price_brl=p.get("prices", {}).get("BRL"), price_cny=p.get("prices", {}).get("CNY"),
                  conversion=p.get("conversion_rate_percent"), bsr=finite_number(p.get("bsr")),
                  categories=" > ".join(p.get("categories") or []) or None,
                  image_url=next(iter(p.get("images") or []), None))
    for period in ("7d", "30d", "60d", "90d", "total"):
        values["sales_" + period] = p.get("sales", {}).get(period)
    for period in ("7d", "30d"):
        values["growth_" + period] = p.get("sales_growth_percent", {}).get(period)
    amounts = [v.get("amount") for header, v in p.get("revenue", {}).items() if v.get("currency") == "BRL" and "30" in header]
    values["revenue_30d_brl"] = amounts[0] if len(amounts) == 1 else None
    brand = p.get("brand_and_seller") or []
    if len(brand) == 3:
        values.update(zip(("brand", "seller", "shop_type"), brand))
    elif brand:
        warnings.append("品牌/卖家结构不是三项，保留目标表已有值")
    if p.get("platform") == "shopee":
        values = shopee_values(p)
        values["captured_at"] = int(instant(p.get("captured_at")).timestamp() * 1000)
    names = list(dict.fromkeys(r['name'] for r in entry.get('tracking_records', []) if r.get('name')))
    values['custom_name'] = '、'.join(names) or None
    competitors = {r.get("competitor") for r in entry.get("tracking_records", []) if r.get("competitor")}
    if len(competitors) == 1:
        values["competitor"] = next(iter(competitors))
    elif competitors:
        warnings.append("同一商品的竞品标记冲突，保留目标表已有值")
    owners = set()
    for record in entry.get("tracking_records", []):
        for person in record.get("owners") or []:
            if isinstance(person, dict) and (pid := person.get("id") or person.get("open_id")):
                owners.add(pid)
    values["owners"] = [{"id": pid} for pid in sorted(owners)] or None
    return values, warnings


def encode_value(kind: str, value, schema: dict):
    """Use field metadata to distinguish text percentages from numeric percentages."""
    if value is None or value == "" or value == []:
        return None
    t = schema["type"]
    if kind == "datetime":
        # Bitable DateTime fields accept Unix timestamps in milliseconds.
        return int(value)
    if kind == "percent":
        number = finite_number(value)
        if number is None:
            return None
        if t == 1:
            return f"{number:.2f}%"
        formatter = (schema.get("property") or {}).get("formatter", "")
        if schema.get("ui_type") == "Progress" or "%" in formatter:
            return number / 100
        return number
    if kind == "number":
        return finite_number(value)
    if kind == "url":
        if not str(value).startswith(("https://", "http://")):
            return None
        return {"link": value, "text": value}
    if kind == "people":
        if len(value) > 1 and not (schema.get("property") or {}).get("multiple", False):
            raise ValueError("多个负责人不能写入单人字段")
        return value
    if kind == "select":
        options = {o["name"] for o in (schema.get("property") or {}).get("options", [])}
        if value not in options:
            raise ValueError(f"单选字段 {schema['field_name']} 没有选项 {value}")
    return str(value)


def same_value(old, new, kind):
    if kind == "datetime":
        return finite_number(old) == finite_number(new)
    if kind in ("text", "select") or kind == "percent" and isinstance(new, str):
        return text_value(old) == new
    if kind == "url":
        return isinstance(old, dict) and old.get("link") == new["link"] and old.get("text", old.get("link")) == new["text"]
    if kind == "people":
        return {p.get("id") for p in old or []} == {p["id"] for p in new}
    # Feishu list-records can return numeric/currency cells as strings.
    old_number, new_number = finite_number(old), finite_number(new)
    return old_number is not None and new_number is not None and math.isclose(old_number, new_number, rel_tol=1e-12, abs_tol=1e-12)


class FeishuSink:
    def __init__(self, config: dict, client=None, clock=None):
        self.config = config
        self.cfg = config["feishu_output"]
        self.client = client or FeishuClient(config["feishu"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not self.cfg.get("app_token") or not self.cfg.get("table_id"):
            raise ValueError("请填写 feishu_output.app_token/table_id")
        self.base = self.client.table_path(self.cfg["app_token"], self.cfg["table_id"])
        target = hashlib.sha256(self.base.encode()).hexdigest()[:16]
        self.state_path = config["app"]["session_dir"] / f"feishu_output_{target}.json"

    def _state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"products": {}, "requests": {}}

    def _schema(self):
        schema = {f["field_name"]: f for f in self.client.list_all(self.base + "/fields", page_size=100)}
        allowed = {"text": {1}, "select": {3}, "people": {11}, "number": {2}, "percent": {1, 2}, "url": {15}, "datetime": {5}}
        for key, name in self.cfg["fields"].items():
            if not name:
                continue
            if key not in KINDS:
                raise ValueError(f"未知输出字段 {key}")
            if name not in schema or schema[name]["type"] not in allowed[KINDS[key]]:
                raise ValueError(f"目标字段缺失或类型不匹配：{name}（配置项 {key}）")
        if not self.cfg["fields"].get("product_id"):
            raise ValueError("必须配置输出 product_id 字段")
        return schema

    def plan(self, result: dict) -> dict:
        schema = self._schema()
        # Always inspect the complete table; a view filter must not hide an existing ID.
        rows = self.client.list_all(self.base + "/records", page_size=500, user_id_type="open_id")
        id_name = self.cfg["fields"]["product_id"]
        index, blanks = {}, 0
        for row in rows:
            pid = text_value(row.get("fields", {}).get(id_name)).upper()
            if not pid:
                blanks += 1
                continue
            index.setdefault(pid, []).append(row)
        state = self._state()
        write_time = int(self.clock().timestamp() * 1000)
        date_field = self.cfg["fields"].get("updated_at")
        plan = {"run_id": result.get("run_id"), "platform": self.cfg["platform"],
                "created": [], "updated": [], "unchanged": [], "skipped": [], "errors": [],
                "blank_id_rows_preserved": blanks, "warnings": []}
        for key, entry in result.get("products", {}).items():
            if entry.get("platform") != self.cfg["platform"]:
                continue
            if entry.get("status") not in ("ok", "partial") or not entry.get("product"):
                plan["skipped"].append({"key": key, "reason": "商品无可写入的采集结果"})
                continue
            try:
                p = entry["product"]
                pid = p["product_id"]
                if p.get("platform") != self.cfg["platform"] or entry.get("product_id") != pid or key != f"{self.cfg['platform']}:{pid}":
                    raise ValueError("平台或商品 ID 在结果中不一致")
                captured = instant(p.get("captured_at"))
                previous = state["products"].get(key, {})
                if previous.get("captured_at") and captured < instant(previous["captured_at"]):
                    plan["skipped"].append({"key": key, "reason": "旧采集数据，避免覆盖已同步的新结果"})
                    continue
                matches = index.get(pid.upper(), [])
                if len(matches) > 1:
                    raise ValueError("目标表有多个相同商品 ID，无法唯一匹配")
                values, warnings = product_values(entry)
                # This is synchronization time, independent of the original capture time.
                values["updated_at"] = write_time
                payload = {}
                for field, name in self.cfg["fields"].items():
                    if not name:
                        continue
                    try:
                        value = encode_value(KINDS[field], values.get(field), schema[name])
                    except ValueError as exc:
                        warnings.append(str(exc))
                        continue
                    if value is not None:
                        payload[name] = value
                plan["warnings"].extend(f"{key}: {warning}" for warning in warnings)
                item = {"key": key, "fields": payload, "captured_at": p["captured_at"]}
                if not matches:
                    plan["created"].append(item)
                else:
                    row = matches[0]
                    item["record_id"] = row["record_id"]
                    old = row.get("fields", {})
                    reverse = {name: KINDS[field] for field, name in self.cfg["fields"].items() if name}
                    # PATCH only changed, known values. Null/-- never erases existing data.
                    changes = {name: value for name, value in payload.items() if not same_value(old.get(name), value, reverse[name])}
                    if date_field:
                        # Successful no-change syncs also refresh the existing row's timestamp.
                        changes[date_field] = payload[date_field]
                    item["fields"] = changes
                    plan["updated" if changes else "unchanged"].append(item)
            except (ValueError, KeyError, TypeError) as exc:
                plan["errors"].append({"key": key, "error": str(exc)})
        return plan

    def write(self, result: dict, *, dry_run=False) -> dict:
        plan = self.plan(result)
        # Store the exact request plan locally before any external mutation.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        report_path = self.config["app"]["output_dir"] / f"feishu_write_{stamp}.json"
        report = {"status": "preview" if dry_run else "running", "plan": plan, "batches": [],
                  "verified_count": len(plan["unchanged"]), "report_path": str(report_path)}
        atomic_json(report_path, report)
        if dry_run:
            return report
        state = self._state()
        try:
            for action in ("created", "updated"):
                items = plan[action]
                for start in range(0, len(items), self.cfg["batch_size"]):
                    batch = items[start:start + self.cfg["batch_size"]]
                    records = [{k: item[k] for k in ("fields", "record_id") if k in item} for item in batch]
                    body = {"records": records}
                    endpoint = self.base + "/records/" + ("batch_create" if action == "created" else "batch_update")
                    params = {"user_id_type": "open_id"}
                    identity_body = deepcopy(body)
                    date_field = self.cfg["fields"].get("updated_at")
                    if action == "created" and date_field:
                        # A new wall-clock value must not change an uncertain create's identity.
                        for record in identity_body["records"]:
                            record["fields"].pop(date_field, None)
                    digest = hashlib.sha256(json.dumps([endpoint, identity_body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    # Persist UUID before sending. Retrying an uncertain create reuses the same token.
                    if digest not in state["requests"] or state["requests"][digest]["status"] == "complete":
                        state["requests"][digest] = {"client_token": str(uuid.uuid4()), "status": "pending", "body": deepcopy(body)}
                    request = state["requests"][digest]
                    if action == "created":
                        params["client_token"] = request["client_token"]
                        # Reuse the exact original body (including date) with the saved UUID.
                        body = deepcopy(request.get("body", body))
                        for item, record in zip(batch, body["records"]):
                            item["fields"] = deepcopy(record["fields"])
                        atomic_json(report_path, report)
                    atomic_json(self.state_path, state)
                    data = self.client.request("POST", endpoint, params=params, json=body)
                    returned = data.get("records", [])
                    if len(returned) != len(batch) or any(not r.get("record_id") for r in returned):
                        raise RuntimeError("飞书响应记录数或 ID 不完整；请求状态已保存，请核对后重试")
                    returned_ids = {r["record_id"] for r in returned}
                    if action == "updated" and returned_ids != {item["record_id"] for item in batch}:
                        raise RuntimeError("飞书更新响应 ID 与请求不一致")
                    for item in batch:
                        state["products"][item["key"]] = {"captured_at": item["captured_at"], "run_id": result.get("run_id")}
                    request["status"] = "complete"
                    atomic_json(self.state_path, state)
                    report["batches"].append({"action": action, "count": len(batch), "record_ids": sorted(returned_ids)})
                    atomic_json(report_path, report)
            for item in plan["unchanged"]:
                state["products"][item["key"]] = {"captured_at": item["captured_at"], "run_id": result.get("run_id")}
            atomic_json(self.state_path, state)
            if plan["created"] or plan["updated"]:
                rows = self.client.list_all(self.base + "/records", page_size=500, user_id_type="open_id")
                index = {}
                for row in rows:
                    pid = text_value(row.get("fields", {}).get(self.cfg["fields"]["product_id"])).upper()
                    index.setdefault(pid, []).append(row)
                reverse = {name: KINDS[field] for field, name in self.cfg["fields"].items() if name}
                for item in plan["created"] + plan["updated"]:
                    pid = item["key"].split(":", 1)[1].upper()
                    matches = index.get(pid, [])
                    if len(matches) != 1:
                        raise RuntimeError(f"写后核对失败：{pid} 未唯一匹配")
                    fields = matches[0].get("fields", {})
                    if any(not same_value(fields.get(k), v, reverse[k]) for k, v in item["fields"].items()):
                        raise RuntimeError(f"写后核对失败：{pid} 字段值与请求不一致")
                report["verified_count"] += len(plan["created"]) + len(plan["updated"])
            report["status"] = "partial" if plan["errors"] or plan["warnings"] else "ok"
        except Exception as exc:
            report["status"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"
            log.error("飞书写入失败：%s", report["error"])
        finally:
            atomic_json(report_path, report)
        log.info("飞书写入 %s：新增 %d，更新 %d，无变化 %d，跳过 %d；详情 %s", report["status"],
                 sum(b["count"] for b in report["batches"] if b["action"] == "created"),
                 sum(b["count"] for b in report["batches"] if b["action"] == "updated"),
                 len(plan["unchanged"]), len(plan["skipped"]), report_path)
        return report
