from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import logging
import re
import uuid
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from competitive_tracking.integrations.feishu import FeishuAPIError, FeishuClient
from competitive_tracking.sinks.feishu import finite_number, product_values
from competitive_tracking.sources.feishu import FeishuSource, select_targets
from competitive_tracking.storage import atomic_json

log = logging.getLogger(__name__)


def markdown_text(value):
    # External titles cannot inject mentions, links or card markup.
    text = html.escape(str(value), quote=False).replace("\n", " ").replace("\r", " ")[:600]
    return re.sub(r"([\\`*_\[\]~])", r"\\\1", text)


def number(value, percent=False):
    n = finite_number(value)
    if n is None:
        return "—"
    return f"{n:,.2f}%" if percent else f"{n:,.2f}".rstrip("0").rstrip(".")


def mercado_metrics(values, product):
    return [
        f"**价格：** R$ {number(values.get('price_brl'))} ｜ ¥ {number(values.get('price_cny'))}",
        "**销量：** 总 " + number(values.get("sales_total")) + " ｜ " + " ｜ ".join(
            f"{d}天 {number(values.get(f'sales_{d}d'))}" for d in (7, 30, 60, 90)),
        f"**近30天销售额：** R$ {number(values.get('revenue_30d_brl'))}",
        f"**销量环比：** 7天 {number(values.get('growth_7d'), True)} ｜ 30天 {number(values.get('growth_30d'), True)}",
        f"**转换率：** {number(values.get('conversion'), True)} ｜ **评论：** {number(values.get('review_count'))} ｜ **评分：** {number(values.get('rating'))}",
        f"**品牌 / 卖家：** {markdown_text(values.get('brand') or '—')} / {markdown_text(values.get('seller') or '—')}",
    ]


def shopee_metrics(values, product):
    lines = [
        f"**价格：** R$ {number(values.get('price_brl'))}",
        f"**销量：** 日 {number(values.get('sales_daily'))} ｜ 月 {number(values.get('sales_monthly'))}",
        f"**销售额：** 日 R$ {number(values.get('revenue_daily_brl'))} ｜ 月 R$ {number(values.get('revenue_monthly_brl'))}",
        f"**评分数：** {number(values.get('review_count'))} ｜ **留评率：** {number(values.get('review_rate'), True)} ｜ **星级：** {number(values.get('rating'))}",
        f"**月新增评分：** {number(values.get('monthly_new_reviews'))}",
        f"**点赞数：** {number(values.get('like_count'))} ｜ **月新增点赞：** {number(values.get('monthly_new_likes'))}",
        f"**类目排名：** {number(values.get('category_rank'))} ｜ **近1天变化：** {number(values.get('rank_daily_change'))} ｜ **近7天变化：** {number(values.get('rank_weekly_change'))}",
        f"**品牌 / 卖家：** {markdown_text(values.get('brand') or '—')} / {markdown_text(values.get('seller') or '—')}",
        f"**变体数：** {number(values.get('variant_count'))} ｜ **类目路径：** {markdown_text(values.get('categories') or '—')}",
    ]
    if product.get('query_period'):
        lines.insert(0, f"**统计期间：** {markdown_text(product['query_period'])}")
    return lines


METRIC_RENDERERS = {'mercado': mercado_metrics, 'shopee': shopee_metrics}


def product_markdown(entry, zone, settings=None):
    settings = settings or {}
    platform = entry['platform']
    if platform not in METRIC_RENDERERS:
        raise ValueError(f'尚未实现 {platform} 的消息卡片')
    # Legacy top-level URLs belong to Mercado. Other platforms must not inherit them.
    links_settings = dict(settings) if platform == 'mercado' else {}
    links_settings.update(settings.get('platform_links', {}).get(platform, {}))
    values, _ = product_values(entry)
    p = entry["product"]
    captured = p.get("captured_at")
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        captured = parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        captured = "未知"
    heading = f"{markdown_text(entry['platform'])} · {markdown_text(entry['product_id'])}"
    custom_names = entry.get("message_custom_names") or []
    if custom_names:
        heading += " - " + " / ".join(markdown_text(name) for name in custom_names)
    lines = [f"**{heading}**",
             markdown_text(values.get("name") or "未获取标题"),
             f"**采集时间：** {captured}"]
    lines.extend(METRIC_RENDERERS[platform](values, p))
    links = []
    for label, url in (("查看商品", values.get("url")), ("数据链接", links_settings.get("data_url")),
                       ("历史链接", links_settings.get("history_url"))):
        if url and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
            links.append(f"[{label}]({quote(url, safe=':/?=&%#@+,-._~')})")
    if links:
        lines.append(" ｜ ".join(links))
    if entry.get("status") == "partial":
        lines.append("*本次采集不完整或收藏操作异常；缺失指标显示为 —。*")
    return "\n".join(lines)


def build_card(entries, cfg):
    zone = ZoneInfo(cfg["schedule"]["timezone"])
    elements = []
    for entry in entries:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": product_markdown(entry, zone, cfg.get("feishu_messages"))}})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"商品监控 · {len(entries)} 个商品"}},
            "elements": elements}


class FeishuMessageSink:
    def __init__(self, config, client=None, source=None, clock=None):
        self.config = config
        self.cfg = config["feishu_messages"]
        self.client = client or FeishuClient(config["feishu"])
        self.source = source or FeishuSource(config["feishu"])
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        identity = ":".join(config["feishu"][k] for k in ("app_id", "app_token", "table_id"))
        key = hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.state_path = config["app"]["session_dir"] / f"feishu_messages_{key}.json"

    def _routes(self, result, report):
        # Re-read immediately before sending, including when replaying an old JSON.
        records = self.source.read_records()
        routes = {}
        for platform in sorted({e.get("platform") for e in result["products"].values() if e.get("platform")}):
            if platform not in self.cfg.get("platforms", ["mercado"]):
                continue
            targets = select_targets(records, self.config["feishu"]["fields"], platform)
            for target in targets:
                entry = result["products"].get(target.key)
                if not entry or entry.get("status") not in ("ok", "partial") or not entry.get("product"):
                    continue
                p = entry["product"]
                if p.get("product_id") != target.product_id or p.get("platform") != platform or entry.get("product_id") != target.product_id:
                    raise ValueError(f"消息商品标识不一致：{target.key}")
                for record in target.records:
                    if record["push_enabled"] is not True:
                        continue
                    people = record["recipients"]
                    for person in people if isinstance(people, list) else [people]:
                        pid = (person.get("open_id") or person.get("id")) if isinstance(person, dict) else None
                        if not isinstance(pid, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]+", pid):
                            report["errors"].append({"key": target.key, "error": "数据推送人缺少有效 open_id，不能按姓名猜测接收者"})
                            continue
                        identity = json.dumps([result["run_id"], pid, target.key], ensure_ascii=False)
                        # Custom names belong to this recipient's enabled task records. Do not
                        # leak another recipient's label or overwrite the platform product title.
                        item = routes.setdefault(identity, {"recipient": pid, "name": person.get("name") or pid,
                                                           "key": target.key, "entry": {**entry, "message_custom_names": []}})
                        custom_name = record.get("name")
                        names = item["entry"]["message_custom_names"]
                        if custom_name and custom_name not in names:
                            names.append(custom_name)
        return routes

    def _send(self, batch, state, report):
        now = self.clock()
        if batch.get("first_attempt_at"):
            age = (now - datetime.fromisoformat(batch["first_attempt_at"])).total_seconds()
            # Server UUID deduplication is time limited. Do not silently resend an uncertain old attempt.
            if age > 45 * 60 or age < 0:
                raise RuntimeError("未确认消息已超过安全重试窗口，请先核对接收方消息及本地状态，禁止自动重发")
        else:
            batch["first_attempt_at"] = now.isoformat()
            atomic_json(self.state_path, state)
        try:
            response = self.client.request("POST", "/im/v1/messages", params={"receive_id_type": "open_id"}, json=batch["body"])
        except FeishuAPIError as exc:
            if exc.code == 99991672:
                # Definitive permission rejection: no message was accepted. It is safe to retry
                # after permissions are granted even beyond the uncertain-response window.
                batch["first_attempt_at"] = None
                batch["rejected_code"] = exc.code
                atomic_json(self.state_path, state)
            raise
        message_id = response.get("message_id")
        if not message_id:
            raise RuntimeError("消息接口未返回 message_id，保留原 UUID 等待核对")
        for identity in batch["identities"]:
            state["completed"][identity] = {"message_id": message_id, "sent_at": now.isoformat()}
        state["pending"].remove(batch)
        atomic_json(self.state_path, state)
        report["sent"].append({"recipient_name": batch["recipient_name"], "message_id": message_id, "product_count": len(batch["identities"])})
        log.info("飞书消息发送成功：%s，%d 个商品，message_id=%s", batch["recipient_name"], len(batch["identities"]), message_id)

    def write(self, result, *, dry_run=False):
        if not result.get("run_id"):
            raise ValueError("发送消息需要采集 run_id")
        now = self.clock()
        path = self.config["app"]["output_dir"] / f"messages_{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
        report = {"status": "preview" if dry_run else "running", "run_id": result["run_id"],
                  "sent": [], "skipped_count": 0, "planned": [], "errors": [], "report_path": str(path)}
        state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"completed": {}, "pending": []}
        try:
            routes = self._routes(result, report)
            reserved = {identity for b in state["pending"] for identity in b["identities"]}
            for batch in list(state["pending"]):
                if batch["run_id"] != result["run_id"]:
                    continue
                if not all(identity in routes for identity in batch["identities"]):
                    report["errors"].append({"uuid": batch["body"]["uuid"], "error": "待重试消息的当前推送条件或接收人已改变，未发送"})
                    continue
                report["planned"].append(batch)
                if not dry_run:
                    try:
                        self._send(batch, state, report)
                    except Exception as exc:
                        report["errors"].append({"uuid": batch["body"]["uuid"], "error": str(exc)})
            grouped = {}
            for identity, item in routes.items():
                if identity in reserved:
                    continue
                if identity in state["completed"]:
                    report["skipped_count"] += 1
                    continue
                grouped.setdefault(item["recipient"], []).append((identity, item))
            for recipient, items in grouped.items():
                size = self.cfg["products_per_message"]
                for start in range(0, len(items), size):
                    chunk = items[start:start + size]
                    card = build_card([item["entry"] for _, item in chunk], self.config)
                    body = {"receive_id": recipient, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False), "uuid": str(uuid.uuid4())}
                    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 28000:
                        raise ValueError("消息卡片超过大小限制，请调小 products_per_message")
                    batch = {"run_id": result["run_id"], "identities": [i for i, _ in chunk], "recipient_name": chunk[0][1]["name"], "body": body}
                    report["planned"].append(batch)
                    if not dry_run:
                        state["pending"].append(batch)
                        atomic_json(self.state_path, state)
                        try:
                            self._send(batch, state, report)
                        except Exception as exc:
                            report["errors"].append({"uuid": body["uuid"], "error": str(exc)})
            report["status"] = "partial" if report["errors"] else ("preview" if dry_run else "ok")
        except Exception as exc:
            report["status"] = "error"
            report["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
        atomic_json(path, report)
        log.info("飞书消息 %s：成功 %d 张卡片，跳过已发 %d 项，错误 %d；报告 %s", report["status"], len(report["sent"]), report["skipped_count"], len(report["errors"]), path)
        return report
