from __future__ import annotations

import hashlib
import json
import logging
import uuid

from competitive_tracking.sinks.feishu_messages import FeishuMessageSink, markdown_text
from competitive_tracking.storage import atomic_json

log = logging.getLogger(__name__)


class RunProblems(logging.Handler):
    """Capture this run's application warnings, including recovered failures."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.addFilter(logging.Filter("competitive_tracking"))
        self.problems = []

    def emit(self, record):
        message = record.getMessage()
        # These are instructions in the normal manual login flow, not faults.
        if message.startswith(("请在项目浏览器中手动登录蓝鲸", "FastMoss 未配置账号密码，请在最大化项目浏览器中手动登录")):
            return
        self.problems.append({"stage": record.name, "key": "", "message": message})


def collect_problems(result, reports=None, logs=()):
    problems, seen = [], set()

    def add(stage, key, message):
        message = str(message)
        identity = (stage, key, message)
        if identity not in seen:
            seen.add(identity)
            problems.append({"stage": stage, "key": key, "message": message})

    if result.get("error"):
        add("运行", "", result["error"])
    for key, entry in result.get("products", {}).items():
        if entry.get("status") not in ("ok", "skipped"):
            warnings = [*entry.get("warnings", []), *(entry.get("product") or {}).get("warnings", [])]
            if entry.get("error"):
                add("商品采集", key, entry["error"])
            for warning in warnings:
                add("商品采集", key, warning)
            if not entry.get("error") and not warnings:
                add("商品采集", key, "采集状态：" + str(entry.get("status")))
        else:
            for warning in [*entry.get("warnings", []), *(entry.get("product") or {}).get("warnings", [])]:
                add("商品采集", key, warning)
    for platform, entry in result.get("platforms", {}).items():
        if entry.get("status") in ("error", "unsupported"):
            add("平台运行", platform, entry.get("error") or "平台尚未实现")
        elif entry.get("status") == "partial" and not any(p["key"].startswith(platform + ":") for p in problems):
            add("平台运行", platform, "平台运行不完整")
    for stage, report in (reports or {}).items():
        before = len(problems)
        for section in (report, report.get("plan", {})):
            if section.get("error"):
                add(stage, "", section["error"])
            for error in section.get("errors", []):
                add(stage, error.get("key", "") if isinstance(error, dict) else "",
                    error.get("error", error) if isinstance(error, dict) else error)
            for warning in section.get("warnings", []):
                add(stage, "", warning)
        for item in report.get("unavailable_products", []):
            # The collection error already identifies this product. Preserve
            # routing diagnostics in the message report without duplicate alerts.
            if not any(p["key"] == item["key"] for p in problems):
                add(stage, item["key"], item["reason"])
        if report.get("status") in ("partial", "error") and len(problems) == before:
            if not report.get("unavailable_products"):
                add(stage, "", "输出状态：" + report["status"])
    for item in logs:
        # Avoid repeating an error that was logged and saved to the run JSON.
        if not any(p["message"] in item["message"] for p in problems):
            add(item["stage"], item.get("key", ""), item["message"])
    if not problems and result.get("status") in ("partial", "error"):
        add("运行", "", "运行状态：" + result["status"])
    return problems


def build_run_summary(config, result, reports=None):
    """Summarize confirmed sink reports, never treating a plan as a completed write."""
    reports = reports or {}
    platforms = list(dict.fromkeys(config["app"]["platforms"]))
    summary = {"status": result.get("status", "未知"), "collection": {}, "bitable": {}, "sheets": {}}
    for platform in platforms:
        entries = [entry for key, entry in result.get("products", {}).items()
                   if entry.get("platform", key.split(":", 1)[0]) == platform]
        info = result.get("platforms", {}).get(platform, {})
        summary["collection"][platform] = {
            "status": info.get("status", "not_run"), "target_count": info.get("target_count", len(entries)),
            "ok_count": sum(entry.get("status") == "ok" for entry in entries),
            "partial_count": sum(entry.get("status") == "partial" for entry in entries),
            "unavailable_count": sum(entry.get("status") not in ("ok", "partial", "skipped") for entry in entries),
            "reason": info.get("reason") or info.get("error"),
        }
        for section, suffix, kind in (("feishu_output", "feishu_write", "bitable"),
                                     ("feishu_sheets", "sheets_write", "sheets")):
            base = config.get(section, {})
            selected = base if base.get("platform") == platform else config.get(platform + "_" + section, {})
            report_key = suffix if base.get("platform") == platform else platform + "_" + suffix
            report = reports.get(report_key)
            row = {"enabled": bool(selected.get("enabled")), "reported": report is not None,
                   "status": report.get("status", "unknown") if report is not None else ("not_run" if selected.get("enabled") else "disabled")}
            if report is not None and kind == "bitable":
                plan = report.get("plan", {})
                batches = report.get("batches")
                row.update({
                    "created_count": sum(batch["count"] for batch in batches if batch["action"] == "created") if batches is not None else None,
                    "updated_count": sum(batch["count"] for batch in batches if batch["action"] == "updated") if batches is not None else None,
                    "unchanged_count": len(plan["unchanged"]) if "unchanged" in plan else None,
                    "skipped_count": len(plan["skipped"]) if "skipped" in plan else None,
                    "verified_count": report.get("verified_count"),
                    "error_count": len(plan.get("errors", [])) + bool(report.get("error")),
                    "skipped": plan.get("skipped", []),
                })
            elif report is not None:
                row.update({key: report.get(key) for key in ("appended_count", "recovered_count", "already_recorded_count")})
                row.update({"skipped_count": len(report["skipped"]) if "skipped" in report else None,
                            "skipped": report.get("skipped", []), "error_count": bool(report.get("error"))})
            summary[kind][platform] = row
    message_report = reports.get("messages")
    message_enabled = bool(config.get("feishu_messages", {}).get("enabled"))
    messages = {"enabled": message_enabled, "reported": message_report is not None,
                "status": message_report.get("status", "unknown") if message_report is not None else ("not_run" if message_enabled else "disabled")}
    if message_report is not None:
        sent = message_report.get("sent", [])
        unavailable = message_report.get("unavailable_products", [])
        affected = {}
        for item in unavailable:
            for person in item.get("recipients", []):
                affected[person.get("id") or person.get("name")] = person.get("name") or person.get("id")
        completed = set(message_report.get("completed_identities", []))
        successful_names, successful_people = set(), set()
        for item in sent:
            # Failure notices have a distinct fourth identity component so a
            # later successful scrape can still deliver this run's product.
            completed.update(item.get("identities", []))
            pid = item.get("recipient_id")
            if pid:
                successful_people.add(pid)
                if not item.get("identities"):
                    for key in item.get("product_keys", []):
                        completed.add(json.dumps([result["run_id"], pid, key], ensure_ascii=False))
            else:
                successful_names.add(item.get("recipient_name"))
        unsent = {}
        for batch in message_report.get("planned", []):
            pid = batch.get("body", {}).get("receive_id")
            # New reports provide recipient IDs and product keys. Older reports
            # only have names, so avoid claiming a known successful send failed.
            if (batch.get("identities") and not all(identity in completed for identity in batch["identities"])
                    and batch.get("recipient_name") not in successful_names):
                unsent[pid or batch.get("recipient_name")] = batch.get("recipient_name") or pid
        messages.update({"card_count": len(sent), "recipient_count": len(successful_people) + len(successful_names),
                         "product_delivery_count": sum(item.get("product_count", 0) for item in sent),
                         "data_product_delivery_count": sum(item.get("data_product_count", item.get("product_count", 0)) for item in sent),
                         "notice_delivery_count": sum(item.get("notice_count", 0) for item in sent),
                         "already_sent_count": message_report.get("skipped_count", 0),
                         "unavailable_product_count": len(unavailable), "affected_recipients": list(affected.values()),
                         "unsent_recipients": list(unsent.values()), "error_count": len(message_report.get("errors", []))})
    summary["messages"] = messages
    return summary


def summary_lines(summary):
    statuses = {"ok": "完成", "partial": "部分完成", "error": "失败", "preview": "预览",
                "disabled": "未开启", "not_run": "未执行", "skipped": "跳过", "unsupported": "未支持"}

    def count(value):
        return "未知" if value is None else str(value)

    def names(values):
        # Full names remain in the local summary; keep the Feishu card bounded.
        text = "、".join(str(name) for name in values) or "无"
        return markdown_text(text[:500] + (f"…（共 {len(values)} 人，完整名单见本地报告）" if len(text) > 500 else ""))

    lines = ["", "**采集结果**"]
    for platform, row in summary["collection"].items():
        text = (f"{platform}：{statuses.get(row['status'], row['status'])}；目标 {row['target_count']}，"
                f"成功 {row['ok_count']}，部分数据 {row['partial_count']}，无数据 {row['unavailable_count']}")
        if row.get("reason"):
            text += "；" + str(row["reason"])[:200]
        lines.append(markdown_text(text))
    for kind, title in (("bitable", "飞书多维表"), ("sheets", "飞书二维表历史")):
        lines.extend(["", "**" + title + "**"])
        for platform, row in summary[kind].items():
            text = f"{platform}：{statuses.get(row['status'], row['status'])}"
            if row["reported"] and kind == "bitable":
                text += (f"；新增 {count(row['created_count'])}，更新 {count(row['updated_count'])}，"
                         f"无变化 {count(row['unchanged_count'])}，跳过 {count(row['skipped_count'])}，"
                         f"写后核对 {count(row['verified_count'])}")
            elif row["reported"]:
                text += (f"；新增历史 {count(row['appended_count'])}，恢复 {count(row['recovered_count'])}，"
                         f"同批次已记录 {count(row['already_recorded_count'])}，无数据等跳过 {count(row['skipped_count'])}")
            lines.append(markdown_text(text))
    row = summary["messages"]
    lines.extend(["", "**运营消息发送**", statuses.get(row["status"], row["status"])])
    if row["reported"]:
        lines.append(f"成功 {row['card_count']} 张卡片，接收 {row['recipient_count']} 人，商品投递 {row['product_delivery_count']} 项"
                     f"（数据 {row['data_product_delivery_count']}，查询状态通知 {row['notice_delivery_count']}）；"
                     f"已发去重 {row['already_sent_count']} 项，发送错误 {row['error_count']} 项")
        lines.append(f"没有采集数据 {row['unavailable_product_count']} 个商品；影响人员：" +
                     names(row['affected_recipients']))
        lines.append("未发送人员：" + names(row['unsent_recipients']))
    return lines


class AdminNotificationSink(FeishuMessageSink):
    """Use the existing durable Feishu transport, with separate alert state.

    No task-table reads or operator push switches affect administrators.
    Inherited _send retains fixed UUIDs and the uncertain-response retry guard.
    """

    def __init__(self, config, client=None, clock=None):
        super().__init__(config, client=client, clock=clock)
        self.cfg = config["admin_notifications"]
        self.state_path = self.state_path.with_name(self.state_path.name.replace("feishu_messages_", "admin_notifications_"))

    def write(self, result, *, reports=None, logs=(), dry_run=False, summary=True):
        issues = collect_problems(result, reports, logs)
        send_summary = summary and self.cfg.get("send_daily_summary", True)
        if not issues and not send_summary:
            return {"status": "skipped", "reason": "本次运行正常", "sent": [], "issues": []}
        run_summary = build_run_summary(self.config, result, reports) if send_summary else None
        now = self.clock()
        path = self.config["app"]["output_dir"] / f"admin_notifications_{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
        report = {"status": "running", "run_id": result["run_id"], "issues": issues, "summary": run_summary,
                  "sent": [], "planned": [], "errors": [], "skipped_count": 0, "report_path": str(path)}
        state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"completed": {}, "pending": []}
        admins = {pid: name for name, pid in self.cfg["admins"].items()}
        # Bound each card, retaining unabridged details in the local report.
        chunks = [issues[i:i + 8] for i in range(0, len(issues), 8)] or [[]]
        for pid, name in admins.items():
            for index, chunk in enumerate(chunks):
                payload = {"version": 2, "summary": run_summary, "issues": chunk, "part": index, "parts": len(chunks)}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                identity = json.dumps([result["run_id"], pid, digest])
                if identity in state["completed"]:
                    report["skipped_count"] += 1
                    continue
                batch = next((b for b in state["pending"] if b["identities"] == [identity]), None)
                if batch is None:
                    lines = [f"**运行批次：** {markdown_text(str(result['run_id'])[:100])}",
                             f"**运行时间：** {markdown_text(str(result.get('started_at', '未知'))[:100])}",
                             f"**结束时间：** {markdown_text(str(result.get('finished_at', '未知'))[:100])}",
                             f"**运行结果：** {markdown_text(str(result.get('status', '未知')))}",
                             f"**异常数量：** {len(issues)}（第 {index + 1}/{len(chunks)} 张）"]
                    if run_summary is not None and index == 0:
                        lines.extend(summary_lines(run_summary))
                    for item in chunk:
                        lines += ["", f"**{markdown_text(item['stage'][:100])} · {markdown_text(item['key'][:100] or '整体')}**",
                                  markdown_text(item["message"][:600])]
                    card = {"config": {"wide_screen_mode": True},
                            "header": {"template": "red" if issues else "green", "title": {"tag": "plain_text", "content": "CompetitiveTracking · 每日运行报告" if send_summary else "CompetitiveTracking · 运行异常"}},
                            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]}
                    batch = {"run_id": result["run_id"], "identities": [identity], "recipient_name": name,
                             "body": {"receive_id": pid, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False), "uuid": str(uuid.uuid4())}}
                    if not dry_run:
                        state["pending"].append(batch)
                        atomic_json(self.state_path, state)
                report["planned"].append(batch)
                if not dry_run:
                    try:
                        self._send(batch, state, report)
                    except Exception as exc:
                        report["errors"].append({"admin": name, "error": f"{type(exc).__name__}: {exc}"})
                        log.error("管理员通知失败：%s，%s", name, exc)
        report["status"] = "partial" if report["errors"] else ("preview" if dry_run else "ok")
        atomic_json(path, report)
        log.info("管理员通知 %s：异常 %d 项，成功 %d 张卡片，发送错误 %d；报告 %s",
                 report["status"], len(issues), len(report["sent"]), len(report["errors"]), path)
        return report


def notify_admins(config, result, *, reports=None, logs=(), summary=True):
    if not config.get("admin_notifications", {}).get("enabled"):
        return None
    try:
        return AdminNotificationSink(config).write(result, reports=reports, logs=logs, summary=summary)
    except Exception as exc:
        # Never recurse into admin notifications when the notification itself fails.
        log.error("管理员通知执行失败：%s: %s", type(exc).__name__, exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
