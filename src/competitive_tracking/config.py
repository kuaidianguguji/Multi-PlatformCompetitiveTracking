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
    for section in ('shopee_feishu_output', 'shopee_feishu_sheets', 'tiktok_feishu_output', 'tiktok_feishu_sheets'):
        platform = section.split('_', 1)[0]
        target = cfg.setdefault(section, {'enabled': False, 'platform': platform})
        if not target.get('enabled'):
            continue
        if target.get('platform') != platform:
            raise ValueError(f'{section}.platform 必须为 {platform}')
        required = ('app_token', 'table_id') if section.endswith('output') else ('spreadsheet_token', 'sheet_id')
        if any(not target.get(key) for key in required):
            raise ValueError(f'{section} 缺少目标文档标识')
        maximum = 500 if section.endswith('output') else 1000
        if not isinstance(target.get('batch_size'), int) or not 1 <= target['batch_size'] <= maximum:
            raise ValueError(f'{section}.batch_size 必须为 1..{maximum}')
        if section.endswith('output'):
            from competitive_tracking.sinks.shopee_fields import COLUMNS as SHOPEE_COLUMNS
            from competitive_tracking.sinks.tiktok_fields import COLUMNS as TIKTOK_COLUMNS
            COLUMNS = SHOPEE_COLUMNS if platform == "shopee" else TIKTOK_COLUMNS
            if list(target.get('fields', {})) != [key for key, _ in COLUMNS]:
                raise ValueError(f'{platform} 输出必须按顺序配置全部 {len(COLUMNS)} 个字段')
            labels = list(target['fields'].values())
            if not all(isinstance(label, str) and label.strip() for label in labels) or len(set(labels)) != len(COLUMNS):
                raise ValueError(f'{platform} 输出字段名不得为空或重复')
        else:
            for key, minimum, maximum in (('data_start_row', 2, 1000000), ('scan_chunk_rows', 1, 5000), ('grow_rows', 1, 5000)):
                if not isinstance(target.get(key), int) or not minimum <= target[key] <= maximum:
                    raise ValueError(f'{section}.{key} 必须为 {minimum}..{maximum}')
            if not isinstance(target.get('validate_headers'), bool):
                raise ValueError(f'{section}.validate_headers 必须为布尔值')
    cfg.setdefault("feishu_messages", {"enabled": False, "products_per_message": 4})
    messages = cfg["feishu_messages"]
    messages.setdefault("platforms", ["mercado"])
    messages.setdefault("products_per_message", 4)
    if not isinstance(messages['platforms'], list) or any(p not in ('mercado', 'shopee', 'tiktok') for p in messages['platforms']):
        raise ValueError('feishu_messages.platforms 必须为 mercado/shopee/tiktok 平台名称列表')
    platform_links = messages.setdefault('platform_links', {})
    if not isinstance(platform_links, dict):
        raise ValueError('feishu_messages.platform_links 必须为按平台区分的链接配置')
    for platform, links in platform_links.items():
        if platform not in ('mercado', 'shopee', 'tiktok') or not isinstance(links, dict):
            raise ValueError('消息链接配置仅支持 mercado/shopee/tiktok')
        for key in ('data_url', 'history_url'):
            url = links.get(key, '')
            if not isinstance(url, str) or (url and (urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc)):
                raise ValueError(f'feishu_messages.platform_links.{platform}.{key} 必须为完整 HTTP/HTTPS 链接或空字符串')
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
        cfg['shopee'].setdefault('enrich_favorites', False)
        if not isinstance(cfg['shopee']['enrich_favorites'], bool):
            raise ValueError('shopee.enrich_favorites 必须为布尔值')
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
    if 'tiktok' in cfg:
        tiktok = cfg['tiktok']
        tiktok.setdefault('search_response_path', '/api/goods/V2/search')
        for key in ('username', 'password'):
            tiktok[key] = os.environ.get(f'FASTMOSS_{key.upper()}', tiktok.get(key, ''))
        if tiktok.get('region') != 'BR':
            raise ValueError('TikTok 当前仅实现 FastMoss 巴西站，region 必须为 BR')
        address = urlsplit(tiktok.get('search_url', ''))
        if address.scheme != 'https' or address.hostname not in ('www.fastmoss.com', 'fastmoss.com') or address.query or address.fragment:
            raise ValueError('tiktok.search_url 必须为 FastMoss HTTPS 搜索页，不带查询参数')
        for key in ('login_timeout_seconds', 'result_timeout_seconds', 'poll_seconds', 'max_pages'):
            if tiktok.get(key, 0) <= 0:
                raise ValueError(f'tiktok.{key} 必须大于 0')
        if not isinstance(tiktok['max_pages'], int):
            raise ValueError('tiktok.max_pages 必须为整数')
        for key in ('page_wait_seconds', 'login_settle_seconds', 'result_settle_seconds'):
            if tiktok.get(key, -1) < 0:
                raise ValueError(f'tiktok.{key} 不得小于 0')
    elif 'tiktok' in cfg['app']['platforms']:
        raise ValueError('启用 TikTok 需要 [tiktok] 配置')
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
