from __future__ import annotations

import argparse
import json
from pathlib import Path

from competitive_tracking.config import load_config
from competitive_tracking.platforms.mercado.parser import parse_html
from competitive_tracking.runner import run_once, serve, setup_logging
from competitive_tracking.storage import atomic_json, single_instance


def main(argv=None):
    parser = argparse.ArgumentParser(description="CompetitiveTracking 商品监控")
    parser.add_argument("--config", default="config.toml", help="配置文件路径，默认当前目录 config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("once", help="从飞书读取并采集一次")
    commands.add_parser("serve", help="常驻每日运行")
    commands.add_parser("check-config", help="检查配置，不打开浏览器、不访问飞书")
    offline = commands.add_parser("parse-html", help="离线解析导出 HTML；Canvas 和未导出的虚拟行不可恢复")
    offline.add_argument("html", type=Path)
    offline.add_argument("--output", type=Path, default=Path("data/offline.json"))
    offline.add_argument("--product-id", action="append", help="仅输出给定商品ID，可重复")
    args = parser.parse_args(argv)
    try:
        if args.command == "parse-html":
            products = parse_html(args.html.read_text(encoding="utf-8-sig"))
            if args.product_id:
                wanted = {x.upper().strip() for x in args.product_id}
                products = {k: v for k, v in products.items() if k in wanted}
            atomic_json(args.output, {"mode": "offline", "warning": "导出文件不包含 Canvas 数值及未导出的虚拟行/后续页", "products": products})
            print(json.dumps({"parsed_products": len(products), "output": str(args.output.resolve())}, ensure_ascii=False))
            return 0
        cfg = load_config(args.config)
        setup_logging(cfg)
        if args.command == "check-config":
            missing = [k for k in ("app_id", "app_secret", "app_token", "table_id") if not cfg["feishu"][k]]
            print("配置结构正确。" + ("尚需填写飞书字段：" + ", ".join(missing) if missing else "飞书必要字段已填写。"))
            return 2 if missing else 0
        with single_instance(cfg["app"]["session_dir"]):
            if args.command == "serve":
                serve(cfg)
            else:
                return 0 if run_once(cfg)["status"] == "ok" else 1
    except KeyboardInterrupt:
        print("已停止运行。")
        return 130
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
