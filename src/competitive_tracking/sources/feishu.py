from __future__ import annotations

import logging
import time
from urllib.parse import quote

import requests

from competitive_tracking.models import TrackingTarget

log = logging.getLogger(__name__)
BASE = "https://open.feishu.cn/open-apis"


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
        })
    return list(grouped.values())


class FeishuSource:
    def __init__(self, config: dict, session=None):
        self.cfg = config
        self.session = session or requests.Session()
        self.token = ""
        self.expires_at = 0.0

    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(self.cfg["retries"] + 1):
            try:
                response = self.session.request(method, BASE + path, timeout=self.cfg["timeout_seconds"], **kwargs)
            except requests.RequestException:
                if attempt == self.cfg["retries"]:
                    raise RuntimeError("飞书网络请求失败，已达到重试上限") from None
            else:
                if response.status_code != 429 and response.status_code < 500:
                    if not response.ok:
                        raise RuntimeError(f"飞书 HTTP {response.status_code}，请检查应用权限和表配置")
                    try:
                        return response.json()
                    except ValueError:
                        raise RuntimeError("飞书返回非 JSON 数据") from None
                if attempt == self.cfg["retries"]:
                    raise RuntimeError(f"飞书 HTTP {response.status_code}，已达到重试上限")
            time.sleep(self.cfg["retry_seconds"] * 2 ** attempt)
        raise AssertionError("unreachable")

    def _access_token(self) -> str:
        if self.token and time.monotonic() < self.expires_at:
            return self.token
        data = self._request("POST", "/auth/v3/tenant_access_token/internal", json={
            "app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"],
        })
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"飞书鉴权失败 code={data.get('code')}，检查 app_id/app_secret")
        self.token = data["tenant_access_token"]
        self.expires_at = time.monotonic() + max(0, data.get("expire", 0) - 60)
        return self.token

    def read_records(self) -> list[dict]:
        missing = [key for key in ("app_id", "app_secret", "app_token", "table_id") if not self.cfg[key].strip()]
        if missing:
            raise ValueError("请在 config.toml 或环境变量中填写飞书配置：" + ", ".join(missing))
        path = "/bitable/v1/apps/{}/tables/{}/records".format(
            quote(self.cfg["app_token"], safe=""), quote(self.cfg["table_id"], safe=""))
        params = {"page_size": self.cfg["page_size"], "user_id_type": "open_id"}
        if self.cfg["view_id"]:
            params["view_id"] = self.cfg["view_id"]
        records, seen = [], set()
        while True:
            data = self._request("GET", path, params=dict(params), headers={"Authorization": f"Bearer {self._access_token()}"})
            if data.get("code") in (99991663, 99991668):
                self.token = ""
                data = self._request("GET", path, params=dict(params), headers={"Authorization": f"Bearer {self._access_token()}"})
            if data.get("code") != 0:
                raise RuntimeError(f"飞书读取失败 code={data.get('code')}，检查权限、app_token 和 table_id")
            page = data.get("data", {})
            records.extend(page.get("items", []))
            if not page.get("has_more"):
                break
            token = page.get("page_token")
            if not token or token in seen:
                raise RuntimeError("飞书分页游标缺失或重复，拒绝返回不完整记录")
            seen.add(token)
            params["page_token"] = token
        log.info("飞书读取完成：%d 条记录", len(records))
        return records
