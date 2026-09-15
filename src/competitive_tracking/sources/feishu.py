from __future__ import annotations

import logging
from competitive_tracking.integrations.feishu import FeishuClient

from competitive_tracking.models import TrackingTarget

log = logging.getLogger(__name__)


def text_value(value) -> str:
    """Bitable text may be a string or an array of rich text fragments."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(text_value(part) for part in value).strip()
    if isinstance(value, dict):
        return text_value(value.get("text", value.get("name", "")))
    return ""


def select_targets(records: list[dict], fields: dict, platform: str) -> list[TrackingTarget]:
    grouped: dict[str, TrackingTarget] = {}
    for record in records:
        data = record.get("fields", {})
        required = ("platform", "product_id", "monitor", "recipients")
        # 飞书 API 对空字段可能省略字段名，因此不把单条缺字段视为 schema 错误。
        values = {key: data.get(fields[key]) for key in required}
        if text_value(values["platform"]).lower() != platform:
            continue
        pid = text_value(values["product_id"]).upper()
        people = values["recipients"]
        valid_people = bool(people) and (not isinstance(people, str) or bool(people.strip()))
        if isinstance(people, list):
            valid_people = any(
                isinstance(p, dict) and any(p.get(k) for k in ("id", "open_id", "user_id", "name"))
                or isinstance(p, str) and bool(p.strip()) for p in people
            )
        if not pid or text_value(values["monitor"]) != "开启" or not valid_people:
            continue
        target = grouped.setdefault(pid, TrackingTarget(platform, pid))
        target.records.append({
            "record_id": record.get("record_id"),
            "name": text_value(data.get(fields["name"])),
            "recipients": people,
            "push_enabled": text_value(data.get(fields["push"])) == "开启",
            "competitor": text_value(data.get(fields.get("competitor", "是否竞品"))) or None,
            "owners": data.get(fields.get("owner", "负责人")) or [],
        })
    return list(grouped.values())


class FeishuSource(FeishuClient):
    def read_records(self) -> list[dict]:
        missing = [key for key in ("app_id", "app_secret", "app_token", "table_id") if not self.cfg[key].strip()]
        if missing:
            raise ValueError("请在 config.toml 或环境变量中填写飞书配置：" + ", ".join(missing))
        path = self.table_path(self.cfg["app_token"], self.cfg["table_id"]) + "/records"
        params = {"page_size": self.cfg["page_size"], "user_id_type": "open_id"}
        if self.cfg["view_id"]:
            params["view_id"] = self.cfg["view_id"]
        records = self.list_all(path, **params)
        log.info("飞书读取完成：%d 条记录", len(records))
        return records
