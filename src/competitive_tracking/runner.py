from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from zoneinfo import ZoneInfo

from competitive_tracking.platforms.registry import COLLECTORS
from competitive_tracking.sources.feishu import FeishuSource, select_targets
from competitive_tracking.storage import JsonSink, atomic_json

log = logging.getLogger(__name__)


def setup_logging(cfg):
    for key in ("session_dir", "output_dir", "log_dir"):
        cfg["app"][key].mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        cfg["app"]["log_dir"] / "competitive_tracking.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8")]
    logging.basicConfig(level=cfg["app"]["log_level"], format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers, force=True)


def run_once(cfg, source=None, registry=None, sink=None) -> dict:
    registry = COLLECTORS if registry is None else registry
    source = source or FeishuSource(cfg["feishu"])
    sink = sink or JsonSink(cfg["app"]["output_dir"])
    now = datetime.now(ZoneInfo(cfg["schedule"]["timezone"]))
    result = {"schema_version": 1, "run_id": now.strftime("%Y%m%d_%H%M%S_%f"),
              "started_at": now.isoformat(), "status": "ok", "platforms": {}, "products": {}}
    try:
        records = source.read_records()
        for platform in cfg["app"]["platforms"]:
            targets = select_targets(records, cfg["feishu"]["fields"], platform)
            if not targets:
                result["platforms"][platform] = {"status": "skipped", "reason": "没有符合筛选条件的记录"}
                log.info("%s 没有符合条件的数据，进入下一个平台", platform)
                continue
            if platform not in registry:
                result["platforms"][platform] = {"status": "unsupported", "target_count": len(targets)}
                log.warning("%s 尚未实现，跳过 %d 个商品", platform, len(targets))
                result["status"] = "partial"
                continue
            try:
                batch = registry[platform](cfg).collect(targets)
                for target in targets:
                    value = batch.get(target.key, {"status": "error", "error": "采集器未返回该商品"})
                    value.update({"platform": platform, "product_id": target.product_id, "tracking_records": target.records})
                    result["products"][target.key] = value
                ok = sum(result["products"][t.key]["status"] == "ok" for t in targets)
                result["platforms"][platform] = {"status": "ok" if ok == len(targets) else "partial", "target_count": len(targets), "ok_count": ok}
                if ok != len(targets):
                    result["status"] = "partial"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("平台 %s 运行失败：%s", platform, error)
                result["platforms"][platform] = {"status": "error", "error": error}
                result["status"] = "partial"
                for target in targets:
                    result["products"][target.key] = {"status": "error", "platform": platform, "product_id": target.product_id,
                                                       "tracking_records": target.records, "error": error}
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["error"] = "用户中断本次运行，结果可能不完整"
        raise
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.error("读取监控源失败：%s", result["error"])
    finally:
        result["finished_at"] = datetime.now(ZoneInfo(cfg["schedule"]["timezone"])).isoformat()
        sink.write(result)
    log.info("运行完成 status=%s，输出 %s", result["status"], cfg["app"]["output_dir"] / "latest.json")
    return result


def next_run(now: datetime, daily_time: str) -> datetime:
    hour, minute = map(int, daily_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def serve(cfg):
    settings = cfg["schedule"]
    zone = ZoneInfo(settings["timezone"])
    state_path = cfg["app"]["session_dir"] / "schedule_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    def execute(day):
        # Claim this day's scheduled attempt before running. Crashes will not trigger duplicate writes later.
        state.update({"last_attempt_date": day, "status": "running"})
        atomic_json(state_path, state)
        result = run_once(cfg)
        state["status"] = result["status"]
        atomic_json(state_path, state)
    now = datetime.now(zone)
    if settings["run_on_start"] and state.get("last_attempt_date") != now.date().isoformat():
        execute(now.date().isoformat())
    due = next_run(datetime.now(zone), settings["daily_time"])
    log.info("常驻运行；下次计划 %s，Ctrl+C 退出", due.isoformat())
    while True:
        now = datetime.now(zone)
        if now >= due:
            day = now.date().isoformat()
            if state.get("last_attempt_date") != day:
                execute(day)
            due = next_run(datetime.now(zone), settings["daily_time"])
            log.info("下次计划 %s", due.isoformat())
        time.sleep(settings["poll_seconds"])
