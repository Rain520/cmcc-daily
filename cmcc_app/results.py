"""Structured worker results. Only fixed codes cross the worker/UI boundary."""
from contextvars import ContextVar

REASONS = {
    "ok": "执行完成", "already": "已完成，无需重复操作", "empty": "当前没有可领取的奖励",
    "auth": "登录凭证失效，请更新 App Token", "fee_auth": "话费会话失效，请更新 Cookie",
    "timeout": "任务超过执行时限，已停止",
    "login": "登录失败，请查看日志", "network": "网络请求失败，请稍后重试",
    "business": "活动接口返回异常，请查看日志", "partial": "部分步骤未完成，请查看日志",
    "skipped": "部分步骤需要真实操作，已跳过", "missing": "附属模块不可用",
    "query": "仅查询，未执行任务", "unknown": "详情未记录", "stopped": "任务已停止",
}
PARTS = ("main", "claim", "tasks", "games")
STATES = ("success", "partial", "failed", "skipped", "unexecuted", "query", "interrupted")
_current = ContextVar("task_result", default=None)


def start(task, options, dry_run=False):
    parts = {"main": {"state": "unexecuted", "reason": "unknown"}}
    if task == "sign":
        parts.update({key: {"state": "unexecuted", "reason": "unknown"}
                      for key in PARTS[1:] if options.get(key)})
    _current.set({"version": 1, "state": "failed", "reason": "unknown", "auth": "unknown",
                  "parts": parts, "active": "main", "dry_run": dry_run})


def begin(part):
    if value := _current.get():
        value["active"] = part


def mark(state="success", reason="ok"):
    if value := _current.get():
        part = value["active"]
        previous = value["parts"].get(part, {})
        # A later successful step must not hide an earlier failure in the same group.
        if previous.get("state") in ("failed", "partial", "skipped") and state == "success":
            return
        value["parts"][part] = {"state": state, "reason": reason}


def auth(valid, fee=False):
    if value := _current.get():
        value["auth"] = "valid" if valid else "invalid"
        if not valid:
            mark("failed", "fee_auth" if fee else "auth")


def error(exc):
    import requests
    value = _current.get()
    if value and (value["auth"] == "invalid" or value["parts"].get(value["active"], {}).get("reason") == "login"):
        return
    mark("failed", "network" if isinstance(exc, requests.RequestException) else "business")


def finish(exit_code):
    value = _current.get()
    if not value:
        return None
    main = value["parts"]["main"]
    if exit_code == 130:
        state, reason = "interrupted", "stopped"
    elif exit_code:
        state, reason = "failed", value["parts"].get(value["active"], main)["reason"]
    else:
        if main["state"] == "unexecuted":
            value["parts"]["main"] = {"state": "success", "reason": "ok"}
        # 「skipped」是已知做不了的任务（如需要真实操作的 AI 豆任务），不算异常。
        bad = [p for p in value["parts"].values() if p["state"] in ("failed", "partial")]
        state, reason = ("failed", main["reason"]) if main["state"] == "failed" else ("partial", "partial") if bad else ("success", main["reason"] if main["state"] == "success" else "ok")
    if value["auth"] == "invalid":
        reason = "fee_auth" if any(p["reason"] == "fee_auth" for p in value["parts"].values()) else "auth"
    if value["dry_run"] and state == "success":
        reason = "query"
    for part in value["parts"].values():
        if value["dry_run"] and part["state"] == "success":
            part.update(state="query", reason="query")
    return clean({**value, "state": state, "reason": reason})


def clean(value):
    """Whitelist backup/worker data; never copy free-form errors or secrets into cards."""
    import json
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("state") not in STATES:
        return None
    parts = value.get("parts") if isinstance(value.get("parts"), dict) else {}
    return {"version": 1, "state": value["state"],
            "reason": value.get("reason") if isinstance(value.get("reason"), str) and value["reason"] in REASONS else "unknown",
            "auth": value.get("auth") if value.get("auth") in ("valid", "invalid") else "unknown",
            "parts": {key: {"state": p["state"], "reason": p.get("reason") if isinstance(p.get("reason"), str) and p["reason"] in REASONS else "unknown"}
                      for key, p in parts.items() if key in PARTS and isinstance(p, dict) and p.get("state") in STATES}}
