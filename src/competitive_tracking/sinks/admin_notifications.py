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
            warnings = (entry.get("product") or {}).get("warnings", [])
            if entry.get("error"):
                add("商品采集", key, entry["error"])
            for warning in warnings:
                add("商品采集", key, warning)
            if not entry.get("error") and not warnings:
                add("商品采集", key, "采集状态：" + str(entry.get("status")))
        else:
            for warning in (entry.get("product") or {}).get("warnings", []):
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


class AdminNotificationSink(FeishuMessageSink):
    """Use the existing durable Feishu transport, with separate alert state.

    No task-table reads or operator push switches affect administrators.
    Inherited _send retains fixed UUIDs and the uncertain-response retry guard.
    """

    def __init__(self, config, client=None, clock=None):
        super().__init__(config, client=client, clock=clock)
        self.cfg = config["admin_notifications"]
        self.state_path = self.state_path.with_name(self.state_path.name.replace("feishu_messages_", "admin_notifications_"))

    def write(self, result, *, reports=None, logs=(), dry_run=False):
        issues = collect_problems(result, reports, logs)
        if not issues:
            return {"status": "skipped", "reason": "本次运行正常", "sent": [], "issues": []}
        now = self.clock()
        path = self.config["app"]["output_dir"] / f"admin_notifications_{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
        report = {"status": "running", "run_id": result["run_id"], "issues": issues,
                  "sent": [], "planned": [], "errors": [], "skipped_count": 0, "report_path": str(path)}
        state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"completed": {}, "pending": []}
        admins = {pid: name for name, pid in self.cfg["admins"].items()}
        # Bound each card, retaining unabridged details in the local report.
        chunks = [issues[i:i + 8] for i in range(0, len(issues), 8)]
        for pid, name in admins.items():
            for index, chunk in enumerate(chunks):
                digest = hashlib.sha256(json.dumps(chunk, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                identity = json.dumps([result["run_id"], pid, digest])
                if identity in state["completed"]:
                    report["skipped_count"] += 1
                    continue
                batch = next((b for b in state["pending"] if b["identities"] == [identity]), None)
                if batch is None:
                    lines = [f"**运行批次：** {markdown_text(str(result['run_id'])[:100])}",
                             f"**运行时间：** {markdown_text(str(result.get('started_at', '未知'))[:100])}",
                             f"**异常数量：** {len(issues)}（第 {index + 1}/{len(chunks)} 张）"]
                    for item in chunk:
                        lines += ["", f"**{markdown_text(item['stage'][:100])} · {markdown_text(item['key'][:100] or '整体')}**",
                                  markdown_text(item["message"][:600])]
                    card = {"config": {"wide_screen_mode": True},
                            "header": {"template": "red", "title": {"tag": "plain_text", "content": "CompetitiveTracking · 运行异常"}},
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


def notify_admins(config, result, *, reports=None, logs=()):
    if not config.get("admin_notifications", {}).get("enabled"):
        return None
    try:
        return AdminNotificationSink(config).write(result, reports=reports, logs=logs)
    except Exception as exc:
        # Never recurse into admin notifications when the notification itself fails.
        log.error("管理员通知执行失败：%s: %s", type(exc).__name__, exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
