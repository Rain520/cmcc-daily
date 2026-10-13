"""Account cards are independent of history filters, pagination and log text."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import TASK_ORDER, utcnow
from .results import clean, REASONS

HEALTH_LABELS = {"normal": "最近运行正常", "running": "正在执行", "queued": "排队中",
                 "warning": "部分任务异常", "invalid": "凭证失效", "unknown": "尚未验证"}


def moment(text):
    try:
        value = datetime.fromisoformat(text)
        return value if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def window(entry, now):
    """The current calendar occurrence; multi-day weekly schedules have separate days."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if entry["frequency"] == "weekly":
        days = entry.get("weekdays") or [now.weekday()]
        if len(days) == 1:
            start = day - timedelta(days=now.weekday())
            due = start + timedelta(days=days[0])
        else:
            due = next(day - timedelta(days=i) for i in range(7)
                       if (day - timedelta(days=i)).weekday() in days)
            start = due
    else:
        start = due = day
    hour, minute = map(int, entry["time"].split(":"))
    return start, due.replace(hour=hour, minute=minute)


def summaries(store, accounts, timezone_name, now=None):
    now = (now or utcnow()).astimezone(ZoneInfo(timezone_name))
    with store.connect() as conn:
        rows = conn.execute("""SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY account_id,task,dry_run,
                CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END
                ORDER BY created_at DESC,rowid DESC) AS rank
            FROM runs) WHERE rank=1""").fetchall()
        auth_rows = conn.execute("""SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY account_id,CASE WHEN task='fee' THEN 'fee' ELSE 'app' END
                ORDER BY COALESCE(finished_at,created_at) DESC,rowid DESC) AS rank
            FROM runs WHERE status NOT IN ('queued','running') AND json_valid(result)
              AND json_extract(result,'$.auth') IN ('valid','invalid')) WHERE rank=1""").fetchall()
    auth_evidence = {}
    for row in auth_rows:
        run = dict(row)
        run["outcome"] = clean(run.get("result"))
        auth_evidence.setdefault(run["account_id"], []).append(run)
    grouped = {}
    for row in rows:
        run = dict(row)
        run["outcome"] = clean(run.get("result"))
        grouped.setdefault(run["account_id"], []).append(run)
    return {account["id"]: summarize(account, grouped.get(account["id"], []), now, auth_evidence.get(account["id"], [])) for account in accounts}


def summarize(account, runs, now, auth_evidence=()):
    valid = []
    for run in runs:
        reset = moment(account.get("fee_reset_at" if run["task"] == "fee" else "status_reset_at"))
        created = moment(run.get("created_at"))
        if created and (not reset or created >= reset):
            valid.append(run)
    completed = sorted((r for r in valid if r["status"] not in ("queued", "running")),
                       key=lambda r: moment(r.get("finished_at")) or moment(r["created_at"]), reverse=True)
    pending = next((r for r in runs if r["status"] in ("queued", "running")), None)
    # App Token and the separate fee Cookie never validate each other.
    auth_states = {}
    evidence = []
    for run in auth_evidence:
        reset = moment(account.get("fee_reset_at" if run["task"] == "fee" else "status_reset_at"))
        created = moment(run.get("created_at"))
        if created and (not reset or created >= reset):
            evidence.append(run)
    for run in sorted(completed + evidence, key=lambda r: moment(r.get("finished_at")) or moment(r["created_at"]), reverse=True):
        scope = "fee" if run["task"] == "fee" else "app"
        outcome = run.get("outcome")
        if outcome and outcome["auth"] != "unknown":
            auth_states.setdefault(scope, outcome["auth"])
    latest_tasks = {}
    for run in completed:
        if not run["dry_run"] or run["task"] == "fee":
            latest_tasks.setdefault(run["task"], run)
    warnings = []
    for run in latest_tasks.values():
        outcome = run.get("outcome")
        if run["status"] in ("failed", "partial", "interrupted"):
            scope = "fee" if run["task"] == "fee" else "app"
            if outcome and outcome["reason"] in ("auth", "fee_auth") and auth_states.get(scope) == "valid":
                continue
            warnings.append(run)
    # Failed diagnostic queries matter too, but a later successful run recovers them.
    for task in TASK_ORDER:
        latest = next((r for r in completed if r["task"] == task), None)
        if latest and latest["dry_run"] and latest["status"] in ("failed", "partial"):
            outcome = latest.get("outcome")
            scope = "fee" if task == "fee" else "app"
            if not (outcome and outcome["reason"] in ("auth", "fee_auth") and auth_states.get(scope) == "valid"):
                warnings.append(latest)
    reset_pending = any(account.get(column) and (not fee or account["config"].get("fee_session_cookie")) and not any(
        (r["task"] == "fee") == fee for r in completed)
        for column, fee in (("status_reset_at", False), ("fee_reset_at", True)))
    health = "invalid" if "invalid" in auth_states.values() else "warning" if warnings else "normal" if completed and not reset_pending else "unknown"
    issue = health if health in ("invalid", "warning") else None
    if pending:
        health = pending["status"]
    tasks = []
    for task in TASK_ORDER:
        entry = account["schedules"][task]
        latest = latest_tasks.get(task)
        active = pending if pending and pending["task"] == task else None
        # Sign is always available; other manual tasks appear after use or configuration.
        query = next((r for r in completed if r["task"] == task and r["dry_run"] and task != "fee"), None)
        if task != "sign" and not entry["enabled"] and not latest and not active and not query and not (
                task == "fee" and account["config"].get("fee_session_cookie")):
            continue
        start, due = window(entry, now)
        current = latest and (not entry["enabled"] or moment(latest["created_at"]) >= start)
        state = active["status"] if active else latest["status"] if current else "waiting" if entry["enabled"] else "unexecuted"
        if state == "waiting" and now >= due:
            # A future next_run without history may be a newly enabled plan.
            next_at = moment(entry.get("next_run"))
            if latest or (next_at and next_at <= now):
                state = "missed"
        outcome = latest.get("outcome") if latest else None
        reason = REASONS[outcome["reason"]] if outcome else "详情未记录" if latest else "尚无执行记录"
        if active:
            reason = "仅查询" if active["dry_run"] and task != "fee" else "任务正在执行" if state == "running" else "等待前面的任务完成"
        parts = []
        if task == "sign":
            for key in ("claim", "tasks", "games"):
                if account["options"].get(key):
                    part = (outcome or {}).get("parts", {}).get(key)
                    parts.append({"key": key, "state": part["state"] if current and part and not active else "unknown" if current and latest and not active else "unexecuted",
                                  "detail": REASONS[part["reason"]] if current and part and not active else "详情未记录" if current and latest and not active else "待本次签到执行"})
        query = next((r for r in completed if r["task"] == task and r["dry_run"] and task != "fee"), None)
        tasks.append({"task": task, "state": state, "run_id": active["id"] if active else latest["id"] if latest else None,
                      "current": bool(current), "last_state": latest["status"] if latest else None, "last_at": latest.get("finished_at") or latest["created_at"] if latest else None,
                      "detail": reason, "parts": parts, "query_at": query.get("finished_at") if query else None,
                      "query_id": query["id"] if query else None})
    last = completed[0] if completed else None
    return {"state": health, "label": HEALTH_LABELS[health], "issue": issue,
            "issue_detail": "话费会话失效，请更新 Cookie" if auth_states.get("fee") == "invalid" else "登录凭证失效，请更新 App Token" if auth_states.get("app") == "invalid" else None,
            "checked_at": last.get("finished_at") or last["created_at"] if last else None,
            "tasks": tasks}
