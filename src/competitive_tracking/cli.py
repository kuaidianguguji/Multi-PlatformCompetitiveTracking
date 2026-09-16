from __future__ import annotations

import argparse
import json
from pathlib import Path

from competitive_tracking.config import load_config
from competitive_tracking.platforms.mercado.parser import parse_html
from competitive_tracking.platforms.shopee.parser import parse_html as parse_shopee_html
from competitive_tracking.platforms.tiktok.parser import parse_html as parse_tiktok_html
from competitive_tracking.runner import run_once, serve, setup_logging
from competitive_tracking.sources.feishu import FeishuSource
from competitive_tracking.sinks.feishu import FeishuSink, enrich_from_source
from competitive_tracking.sinks.feishu_sheets import FeishuSheetsSink, supplement_history_metadata
from competitive_tracking.sinks.feishu_messages import FeishuMessageSink
from competitive_tracking.storage import atomic_json, single_instance
from competitive_tracking.sinks.destinations import destination_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="CompetitiveTracking 商品监控")
    parser.add_argument("--config", default="config.toml", help="配置文件路径，默认当前目录 config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="从飞书读取并采集一次")
    once.add_argument("--platform", action="append", help="仅运行指定平台，可重复，例如 --platform shopee")
    commands.add_parser("serve", help="常驻每日运行")
    commands.add_parser("check-config", help="检查配置，不打开浏览器、不访问飞书")
    publish = commands.add_parser("write-feishu", help="将已有采集 JSON 同步到飞书结果表，不打开浏览器")
    publish.add_argument("json_file", type=Path, help="run_*.json 文件路径")
    publish.add_argument("--platform", choices=["mercado", "shopee", "tiktok"], default="mercado", help="目标平台，默认 mercado")
    publish.add_argument("--dry-run", action="store_true", help="读取字段和记录并生成本地写入预览，不修改飞书")
    history = commands.add_parser("write-sheets", help="将已有采集 JSON 追加到二维历史表，不覆盖旧数据")
    history.add_argument("json_file", type=Path, help="run_*.json 文件路径")
    history.add_argument("--platform", choices=["mercado", "shopee", "tiktok"], default="mercado", help="目标平台，默认 mercado")
    history.add_argument("--dry-run", action="store_true", help="只生成追加区域和数据预览，不修改二维表")
    initialize = commands.add_parser('init-shopee-tables', help='按配置创建 Shopee 28 个字段和二维表表头；不写商品数据')
    initialize.add_argument('--dry-run', action='store_true', help='检查结构并列出所需变更，不修改飞书')
    initialize_tiktok = commands.add_parser('init-tiktok-tables', help='创建 TikTok 33 个字段和二维表表头')
    initialize_tiktok.add_argument('--dry-run', action='store_true', help='只检查结构，不修改飞书')
    messages = commands.add_parser("send-feishu", help="按当前任务表接收人和推送开关发送已有采集数据")
    messages.add_argument("json_file", type=Path, help="run_*.json 文件路径")
    messages.add_argument('--platform', action='append', choices=['mercado', 'shopee'], help='仅推送指定且在配置中启用的平台，可重复；不填写则使用配置的平台列表')
    messages.add_argument("--dry-run", action="store_true", help="只读任务表并生成消息预览，不发送")
    offline = commands.add_parser("parse-html", help="离线解析导出 HTML；Canvas 和未导出的虚拟行不可恢复")
    offline.add_argument("html", type=Path)
    offline.add_argument("--output", type=Path, default=Path("data/offline.json"))
    offline.add_argument("--product-id", action="append", help="仅输出给定商品ID，可重复")
    offline.add_argument("--platform", choices=["mercado", "shopee", "tiktok"], default="mercado", help="导出页面所属平台")
    args = parser.parse_args(argv)
    try:
        if args.command == "parse-html":
            parser_fn = {'shopee': parse_shopee_html, 'tiktok': parse_tiktok_html, 'mercado': parse_html}[args.platform]
            products = parser_fn(args.html.read_text(encoding="utf-8-sig"))
            if args.product_id:
                wanted = {x.upper().strip() for x in args.product_id}
                products = {k: v for k, v in products.items() if k in wanted}
            atomic_json(args.output, {"mode": "offline", "warning": "导出文件不包含 Canvas 数值及未导出的虚拟行/后续页", "products": products})
            print(json.dumps({"parsed_products": len(products), "output": str(args.output.resolve())}, ensure_ascii=False))
            return 0
        cfg = load_config(args.config)
        if args.command in ('write-feishu', 'write-sheets'):
            cfg = destination_config(cfg, args.platform)
        if args.command == 'send-feishu' and args.platform:
            cfg['feishu_messages']['platforms'] = [p for p in cfg['feishu_messages']['platforms'] if p in args.platform]
        if args.command == "once" and args.platform:
            cfg["app"]["platforms"] = list(dict.fromkeys(args.platform))
        setup_logging(cfg)
        if args.command == "check-config":
            missing = [k for k in ("app_id", "app_secret", "app_token", "table_id") if not cfg["feishu"][k]]
            print("配置结构正确。" + ("尚需填写飞书字段：" + ", ".join(missing) if missing else "飞书必要字段已填写。"))
            return 2 if missing else 0
        with single_instance(cfg["app"]["session_dir"]):
            if args.command in ('init-shopee-tables', 'init-tiktok-tables'):
                from competitive_tracking.sinks.provision_shopee import provision
                print(json.dumps(provision(destination_config(cfg, args.command.split('-')[1]), dry_run=args.dry_run), ensure_ascii=False))
                return 0
            elif args.command == "send-feishu":
                if not cfg["feishu_messages"].get("enabled"):
                    raise ValueError("请先配置并启用 [feishu_messages]")
                result = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
                if result.get("schema_version") != 1 or not isinstance(result.get("products"), dict):
                    raise ValueError("输入文件不是 schema_version=1 的采集结果")
                report = FeishuMessageSink(cfg).write(result, dry_run=args.dry_run)
                print(json.dumps({"status": report["status"], "sent_cards": len(report["sent"]), "skipped_count": report["skipped_count"],
                                  "errors": report["errors"], "report_path": report["report_path"]}, ensure_ascii=False))
                return 0 if report["status"] in ("ok", "preview") else 1
            elif args.command == "write-sheets":
                if not cfg["feishu_sheets"].get("enabled"):
                    raise ValueError("请先配置并启用 [feishu_sheets]")
                result = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
                if result.get("schema_version") != 1 or not isinstance(result.get("products"), dict):
                    raise ValueError("输入文件不是 schema_version=1 的采集结果")
                needs_metadata = any("owners" not in r or "competitor" not in r
                                     for p in result["products"].values() for r in p.get("tracking_records", []))
                if needs_metadata:
                    result = supplement_history_metadata(result, FeishuSource(cfg["feishu"]).read_records(), cfg)
                report = FeishuSheetsSink(cfg).write(result, dry_run=args.dry_run)
                print(json.dumps({k: report.get(k) for k in ("status", "planned_count", "appended_count", "recovered_count", "already_recorded_count", "report_path")}, ensure_ascii=False))
                return 0 if report["status"] in ("ok", "preview") else 1
            elif args.command == "write-feishu":
                if not cfg["feishu_output"].get("enabled"):
                    raise ValueError("请先配置并启用 [feishu_output]")
                result = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
                if result.get("schema_version") != 1 or not isinstance(result.get("products"), dict):
                    raise ValueError("输入文件不是 schema_version=1 的采集结果")
                result = enrich_from_source(result, FeishuSource(cfg["feishu"]).read_records(), cfg)
                report = FeishuSink(cfg).write(result, dry_run=args.dry_run)
                print(json.dumps({"status": report["status"], "report_path": report["report_path"],
                                  "counts": {k: len(report["plan"][k]) for k in ("created", "updated", "unchanged", "skipped", "errors")}}, ensure_ascii=False))
                return 0 if report["status"] in ("ok", "preview") and not report["plan"]["errors"] else 1
            elif args.command == "serve":
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
