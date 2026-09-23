from __future__ import annotations

import time
import re
from urllib.parse import quote

import requests

BASE = "https://open.feishu.cn/open-apis"


class FeishuAPIError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class FeishuClient:
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
                        try:
                            error = response.json()
                        except ValueError:
                            error = {}
                        if not isinstance(error, dict):
                            error = {}
                        code = error.get("code")
                        # Only expose scope names and error code, never raw response/request credentials.
                        scopes = sorted(set(re.findall(r"(?:sheets|drive|bitable|im):[a-zA-Z0-9_:]+", str(error))))
                        detail = f" code={code}" if code is not None else ""
                        if scopes:
                            detail += "，所需权限：" + ", ".join(scopes)
                        if code == 230013:
                            detail += "；机器人对该用户没有可用性，请在应用版本管理的可用范围添加该用户并发布"
                        raise FeishuAPIError(f"飞书 HTTP {response.status_code}{detail}，请检查应用权限和表配置", code=code)
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
        if not self.cfg.get("app_id") or not self.cfg.get("app_secret"):
            raise ValueError("请填写飞书 app_id/app_secret")
        data = self._request("POST", "/auth/v3/tenant_access_token/internal", json={
            "app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"],
        })
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"飞书鉴权失败 code={data.get('code')}，检查 app_id/app_secret")
        self.token = data["tenant_access_token"]
        self.expires_at = time.monotonic() + max(0, data.get("expire", 0) - 60)
        return self.token

    def request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(2):
            result = self._request(method, path, headers={"Authorization": f"Bearer {self._access_token()}"}, **kwargs)
            if attempt == 0 and result.get("code") in (99991663, 99991668):
                self.token = ""
                continue
            if result.get("code") != 0:
                raise FeishuAPIError(f"飞书 API 失败 code={result.get('code')}，接口 {path.rsplit('/', 1)[-1]}；请检查字段类型与应用权限", code=result.get('code'))
            return result.get("data", {})
        raise AssertionError("unreachable")

    @staticmethod
    def table_path(app_token: str, table_id: str) -> str:
        return f"/bitable/v1/apps/{quote(app_token, safe='')}/tables/{quote(table_id, safe='')}"

    def list_all(self, path: str, **params) -> list[dict]:
        items, seen = [], set()
        while True:
            page = self.request("GET", path, params=dict(params))
            items.extend(page.get("items", []))
            if not page.get("has_more"):
                return items
            cursor = page.get("page_token")
            if not cursor or cursor in seen:
                raise RuntimeError("飞书分页游标缺失或重复，拒绝返回不完整记录")
            seen.add(cursor)
            params["page_token"] = cursor
