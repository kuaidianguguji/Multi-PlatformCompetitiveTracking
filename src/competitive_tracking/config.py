from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    with path.open("rb") as file:
        cfg = tomllib.load(file)
    for section in ("app", "schedule", "feishu", "browser", "mercado"):
        if section not in cfg:
            raise ValueError(f"配置缺少 [{section}]，请参考 config.example.toml")
    cfg["root"] = path.parent
    # 兼容尚未添加此配置项的旧 config.toml。
    cfg["mercado"].setdefault("login_settle_seconds", 5)
    cfg.setdefault("feishu_output", {"enabled": False})
    cfg.setdefault("feishu_sheets", {"enabled": False})
    cfg.setdefault("feishu_messages", {"enabled": False, "products_per_message": 4})
    messages = cfg["feishu_messages"]
    messages.setdefault("platforms", ["mercado"])
    messages.setdefault("products_per_message", 4)
    for key in ("data_url", "history_url"):
        messages.setdefault(key, "")
        url = messages[key]
        if not isinstance(url, str) or (url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc)):
            raise ValueError(f"feishu_messages.{key} 必须为完整 HTTP/HTTPS 链接或空字符串")
    if not isinstance(messages["products_per_message"], int) or not 1 <= messages["products_per_message"] <= 4:
        raise ValueError("feishu_messages.products_per_message 必须为 1..4 的整数")
    sheets = cfg["feishu_sheets"]
    if sheets.get("enabled"):
        for key in ("spreadsheet_token", "sheet_id", "platform"):
            if not sheets.get(key):
                raise ValueError(f"启用二维表写入时必须填写 feishu_sheets.{key}")
        for key, minimum, maximum in (("data_start_row", 2, 1000000), ("batch_size", 1, 1000),
                                      ("scan_chunk_rows", 1, 5000), ("grow_rows", 1, 5000)):
            if not isinstance(sheets.get(key), int) or not minimum <= sheets[key] <= maximum:
                raise ValueError(f"feishu_sheets.{key} 必须为 {minimum}..{maximum} 的整数")
    output = cfg["feishu_output"]
    if output.get("enabled"):
        for key in ("app_token", "table_id", "platform"):
            if not output.get(key):
                raise ValueError(f"启用飞书写入时必须填写 feishu_output.{key}")
        if not 1 <= output.get("batch_size", 100) <= 500:
            raise ValueError("feishu_output.batch_size 必须为 1..500")
        labels = [name for name in output.get("fields", {}).values() if name]
        if len(set(labels)) != len(labels):
            raise ValueError("飞书输出字段不能映射到重复的目标列")
    for key in ("app_id", "app_secret", "app_token", "table_id"):
        cfg["feishu"][key] = os.environ.get(f"FEISHU_{key.upper()}", cfg["feishu"][key])
    if "shopee" in cfg:
        for key in ("username", "password"):
            cfg["shopee"][key] = os.environ.get(f"SHOPDORA_{key.upper()}", cfg["shopee"].get(key, ""))
        if cfg["shopee"].get("site") != "br":
            raise ValueError("Shopee 当前仅实现巴西站，shopee.site 必须为 br")
        for key in ("login_timeout_seconds", "result_timeout_seconds", "poll_seconds", "max_pages"):
            if cfg["shopee"].get(key, 0) <= 0:
                raise ValueError(f"shopee.{key} 必须大于 0")
        for key in ("page_wait_seconds", "login_settle_seconds", "result_settle_seconds"):
            if cfg["shopee"].get(key, -1) < 0:
                raise ValueError(f"shopee.{key} 不得为负数")
    elif "shopee" in cfg["app"]["platforms"]:
        raise ValueError("启用 Shopee 需要 [shopee] 配置，请参考 config.example.toml")
    for key in ("session_dir", "output_dir", "log_dir"):
        cfg["app"][key] = (path.parent / cfg["app"][key]).resolve()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cfg["schedule"]["daily_time"]):
        raise ValueError("schedule.daily_time 必须为 HH:MM")
    ZoneInfo(cfg["schedule"]["timezone"])
    for section, keys in {
        "schedule": ["poll_seconds"],
        "feishu": ["timeout_seconds", "retry_seconds"],
        "browser": ["element_timeout_seconds", "page_load_timeout_seconds"],
        "mercado": ["manual_login_timeout_seconds", "poll_seconds", "max_pages", "max_scroll_steps"],
    }.items():
        for key in keys:
            if cfg[section][key] <= 0:
                raise ValueError(f"{section}.{key} 必须大于 0")
    for key in ("page_wait_seconds", "login_settle_seconds", "result_settle_seconds", "scroll_wait_seconds", "favorite_dialog_wait_seconds"):
        if cfg["mercado"][key] < 0:
            raise ValueError(f"mercado.{key} 不得为负数")
    if not 0 < cfg["mercado"]["scroll_fraction"] < 1:
        raise ValueError("mercado.scroll_fraction 必须在 0 和 1 之间")
    if not 1 <= cfg["feishu"]["page_size"] <= 500 or cfg["feishu"]["retries"] < 0:
        raise ValueError("飞书 page_size 必须为 1..500，retries 不得为负数")
    if not 1024 <= cfg["browser"]["port"] <= 65535:
        raise ValueError("browser.port 必须为 1024..65535")
    return cfg
