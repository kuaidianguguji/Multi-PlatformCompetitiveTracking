from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    with path.open("rb") as file:
        cfg = tomllib.load(file)
    for section in ("app", "schedule", "feishu", "browser", "mercado"):
        if section not in cfg:
            raise ValueError(f"配置缺少 [{section}]，请参考 config.example.toml")
    cfg["root"] = path.parent
    for key in ("app_id", "app_secret", "app_token", "table_id"):
        cfg["feishu"][key] = os.environ.get(f"FEISHU_{key.upper()}", cfg["feishu"][key])
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
    for key in ("page_wait_seconds", "result_settle_seconds", "scroll_wait_seconds", "favorite_dialog_wait_seconds"):
        if cfg["mercado"][key] < 0:
            raise ValueError(f"mercado.{key} 不得为负数")
    if not 0 < cfg["mercado"]["scroll_fraction"] < 1:
        raise ValueError("mercado.scroll_fraction 必须在 0 和 1 之间")
    if not 1 <= cfg["feishu"]["page_size"] <= 500 or cfg["feishu"]["retries"] < 0:
        raise ValueError("飞书 page_size 必须为 1..500，retries 不得为负数")
    if not 1024 <= cfg["browser"]["port"] <= 65535:
        raise ValueError("browser.port 必须为 1024..65535")
    return cfg
