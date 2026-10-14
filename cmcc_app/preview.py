"""本地审核预览：只用虚构数据，任务改为本地模拟，不联系中国移动。

容器里通过 `CMCC_PREVIEW=1` 开启（见 compose.preview.yaml）：

- 首次启动写入 6 个虚构示例账号，覆盖「正常／凭证失效／部分异常／正在执行／排队中／
  尚未验证」全部状态，并在数据目录留下 synthetic-preview.txt 标记；
- 定时计划与秒杀活动到点不再排队，手动执行的任务交给 simulate() 本地模拟约 20 秒；
- 页面顶部显示预览横幅，通知测试与备份导入被拒绝。

安全约束：只在「空目录」或「带预览标记的目录」上启用，数据目录里已有账号或执行记录时
直接拒绝启动，避免把正式实例误切成预览。示例数据同样必须遵守 AGENTS.md 的虚构约定。
"""

import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import threading

from flask import jsonify, request

from . import __version__
from .models import Store, normalize_account, normalize_activity, utcnow

ENV_FLAG = "CMCC_PREVIEW"
MARKER_NAME = "synthetic-preview.txt"
MARKER_TEXT = "Fictional data only"
FIXTURE_NAME = "preview-fixtures.json"
BANNER = f"{__version__} 审核预览 · 全部为虚构数据 · 任务为本地模拟，不联网"
WORK_SECONDS = 20.0

ACCOUNTS = (("13800000011", "示例账号甲", "0731"),
            ("13700000022", "示例账号乙", "0734"),
            ("13900000033", "示例账号丙", "0755"))
LOGS = {
    "sign": "登录成功\n[签到] 签到成功，获得 10 分\n[领奖] 已领取累计奖励 5 分",
    "rate": "评价页加载完成\n本周已评过（上次 10 分），无需操作\n评价币余额 0（累计获得 20，累计消费 20）",
    "fee": "话费查询完成\n话费余额 12.34 元\n本月已用流量 3.2GB",
    "wed": "周三充值日拼图\n[任务] 拼图代做成功（+1 次）\n[抽奖] 未中奖，明日再来",
}
OUTCOMES = ([("success", 0, "执行完成，请查看活动详情")] * 3
            + [("failed", 1, "执行失败，请查看日志"), ("interrupted", 130, "任务已停止，未自动重试")])
# 状态演示用的 6 个虚构账号：手机号连续、名称不含任何真实信息。
STATUS_FIXTURES = (
    ("13800000011", "示例 · 日常账号", "normal"),
    ("13700000022", "示例 · 更新凭证", "invalid"),
    ("13900000033", "示例 · 部分异常", "warning"),
    ("13600000044", "示例 · 正在签到", "running"),
    ("13500000055", "示例 · 等待队列", "queued"),
    ("13400000066", "示例 · 新增账号", "unknown"),
)


def enabled():
    """CMCC_PREVIEW 只接受明确的开启值，默认关闭。"""
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def marker(data_dir):
    return Path(data_dir) / MARKER_NAME


def fixtures(data_dir):
    """读取固定保留的示例记录编号；缺少或损坏时按「没有示例记录」处理。"""
    try:
        data = json.loads((Path(data_dir) / FIXTURE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {item["id"]: item for item in data.get("runs", [])
            if isinstance(item, dict) and item.get("id")}


def seed(data_dir, days=25, random_seed=20261013, status_demo=False):
    """向空的独立预览目录写入虚构数据；已有账号或记录时拒绝写入。"""
    root = Path(data_dir)
    store = Store(root)
    if store.accounts() or store.runs(1):
        raise ValueError("仅允许向空的独立预览目录写入虚构数据")
    (root / "logs").mkdir(parents=True, exist_ok=True)
    if status_demo:
        return seed_status_demo(store, root)
    accounts = []
    for phone, name, city in ACCOUNTS:
        account = normalize_account({
            "name": name,
            "config": {"phone": phone, "app_token": "preview-token", "fee_session_cookie": "preview-cookie",
                       "city_code": city},
            "schedules": {"sign": {"enabled": False, "time": "08:23", "frequency": "daily", "weekdays": []}},
        })
        store.save(account)
        accounts.append(account)
    random.seed(random_seed)
    now = datetime.now(timezone.utc)
    created = 0
    for days_ago in range(days):
        for account in accounts:
            for task in ("sign", "rate", "fee", "wed"):
                if random.random() > 0.6:
                    continue
                status, code, message = random.choice(OUTCOMES)
                run_id = store.enqueue(account["id"], task, dry_run=random.random() < 0.15)
                store.take_run()
                store.finish(run_id, status, code, message)
                moment = (now - timedelta(days=days_ago, hours=random.randint(0, 20),
                                          minutes=random.randint(0, 59))).isoformat()
                source = "schedule" if random.random() < 0.7 else "manual"
                with store.connect() as conn:
                    conn.execute("UPDATE runs SET created_at=?, source=? WHERE id=?", (moment, source, run_id))
                (root / "logs" / f"{run_id}.log").write_text(
                    LOGS[task] + f"\n手机尾号 {account['config']['phone'][-4:]}\n", encoding="utf-8")
                created += 1
    print(f"示例账号 {len(accounts)} 个，示例记录 {created} 条")


def seed_status_demo(store, root):
    """写入覆盖全部账号状态的 6 个虚构账号；计划可见，但不会真的触发任务。"""
    frozen = []
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    for index, (phone, name, health) in enumerate(STATUS_FIXTURES):
        account = normalize_account({"name": name, "config": {"phone": phone, "app_token": "preview-token"},
            "options": {"claim": True, "tasks": True, "games": True}, "schedules": {
                "sign": {"enabled": True, "time": "10:00"},
                "wed": {"enabled": True, "time": "14:00", "frequency": "weekly", "weekdays": [2]},
                "rate": {"enabled": True, "time": "09:00", "frequency": "weekly", "weekdays": [0]}}})
        store.save(account)
        if health == "unknown":
            continue
        for task in ("rate", "sign"):
            status = "success"
            reason, auth = "ok", "valid"
            if health == "invalid":
                status, reason, auth = "failed", "auth", "invalid"
            elif health == "warning" and task == "sign":
                status, reason = "partial", "partial"
            parts = {"main": {"state": "failed" if status == "failed" else "success", "reason": reason}}
            if task == "sign":
                parts.update({key: {"state": "unexecuted" if status == "failed" else "success", "reason": "unknown" if status == "failed" else "ok"}
                              for key in ("claim", "tasks", "games")})
                if health == "warning":
                    parts["games"] = {"state": "failed", "reason": "network"}
            result = {"version": 1, "state": status, "reason": reason, "auth": auth, "parts": parts}
            run_id = store.enqueue(account["id"], task)
            store.finish(run_id, status, 1 if status == "failed" else 0, "虚构预览记录", result)
            at = now - timedelta(minutes=10 + index)
            if task == "rate":
                at = local.replace(hour=9, minute=1, second=0, microsecond=0) - timedelta(days=local.weekday())
            at = at.astimezone(timezone.utc).isoformat()
            with store.connect() as conn:
                conn.execute("UPDATE runs SET created_at=?,started_at=?,finished_at=?,source='schedule' WHERE id=?", (at, at, at, run_id))
            (root / "logs" / f"{run_id}.log").write_text(
                "[审核预览] 完全虚构的数据，未调用移动接口。\n" + LOGS[task] + "\n模拟结果：" + status,
                encoding="utf-8")
        if health in ("running", "queued"):
            run_id = store.enqueue(account["id"], "sign")
            started = now.isoformat() if health == "running" else None
            with store.connect() as conn:
                conn.execute("UPDATE runs SET status=?,started_at=? WHERE id=?", (health, started, run_id))
            (root / "logs" / f"{run_id}.log").write_text("[审核预览] 模拟执行状态，不会发送真实请求。", encoding="utf-8")
            frozen.append({"id": run_id, "status": health, "started_at": started})
    # 一个示例秒杀活动：预览不跑定时计划，点「立即执行」即可看到并行抢购的模拟结果。
    activity = normalize_activity({"name": "示例 · 假期抢券", "kind": "seckill", "time": "12:00:00",
                                   "lead": 3, "interval": 0.5, "max_attempts": 40,
                                   "weekdays": list(range(7)), "accounts": [], "enabled": True},
                                  accounts=store.accounts(), timezone_name="Asia/Shanghai")
    store.save_activity(activity, "Asia/Shanghai")
    # 这两条记录固定展示「正在执行 / 排队中」，预览运行时不会真的取走它们。
    (root / FIXTURE_NAME).write_text(
        json.dumps({"runs": frozen}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已生成 6 个虚构账号与 1 个示例秒杀活动，覆盖全部账号状态；其中 2 条排队记录会固定保留。")


def prepare(data_dir):
    """首次启动时生成虚构示例数据；已有真实数据时拒绝启动。"""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    if marker(path).exists():
        return
    store = Store(path)
    if store.accounts() or store.runs(1):
        raise ValueError(f"{ENV_FLAG}=1 只能用于空的独立预览目录：{path} 已有账号或执行记录，"
                         "请换一个数据目录，或清空后重试")
    seed(path, status_demo=True)
    marker(path).write_text(MARKER_TEXT, encoding="utf-8")


def keep_fixture_status(store, frozen):
    """示例里的「正在执行 / 排队中」固定保留：取任务时跳过它们，其余照常执行。"""
    ids = json.dumps(sorted(frozen))

    def take(seckill):
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE status='queued' AND id NOT IN (SELECT value FROM json_each(?))"
                + (" AND task='seckill'" if seckill else "") + " ORDER BY created_at LIMIT 1",
                (ids,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",
                         (utcnow().isoformat(), row["id"]))
            return dict(row)

    store.take_run = lambda: take(False)
    store.take_run_parallel = lambda: take(True)


def simulate(manager, run, parallel=False):
    """模拟执行：只写日志和结构化结果，不访问任何外部接口。"""
    run_id, task = run["id"], run["task"]
    log_path = manager.data / "logs" / f"{run_id}.log"
    log_path.write_text("[审核预览] 模拟执行，未联网、未调用中国移动接口。\n"
                        f"任务：{task}\n模式：{'仅查询' if run['dry_run'] else '正式执行'}\n"
                        + ("方式：秒杀任务按账号并行模拟\n" if parallel else ""), encoding="utf-8")
    deadline = time.monotonic() + WORK_SECONDS
    while time.monotonic() < deadline:
        manager.heartbeat = time.monotonic()
        if manager.stop_event.is_set() or manager.is_cancelled(run_id):
            manager.store.finish(run_id, "interrupted", 130, "任务已停止（审核预览）",
                                 {"version": 1, "state": "interrupted", "reason": "stopped",
                                  "auth": "unknown", "parts": {}})
            return
        manager.stop_event.wait(0.5)
    if run["dry_run"]:
        result = {"version": 1, "state": "success", "reason": "query", "auth": "valid",
                  "parts": {"main": {"state": "query", "reason": "query"}}}
    else:
        parts = {"main": {"state": "success", "reason": "ok"}}
        if task == "sign":
            # 模拟真实情况：累计奖励无可领取、拓展活动正常，AI 豆里有几个任务只能在手机 App 完成。
            parts.update({"claim": {"state": "success", "reason": "empty"},
                          "tasks": {"state": "success", "reason": "skipped"},
                          "games": {"state": "success", "reason": "ok"}})
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("[AI豆任务] 3 个待办，完成 1 个，跳过 2 个（需要真实操作，属正常）\n")
        result = {"version": 1, "state": "success", "reason": "ok", "auth": "valid", "parts": parts}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("模拟结果：执行完成（未访问移动接口）\n")
    manager.store.finish(run_id, "success", 0, "执行完成（审核预览模拟，未访问移动接口）", result)


def install(app, data_dir):
    """把已创建的应用切成预览行为：关闭定时计划、执行改为本地模拟、页面显示横幅。"""
    store, manager = app.extensions["cmcc_store"], app.extensions["cmcc_manager"]
    if frozen := fixtures(data_dir):
        keep_fixture_status(store, frozen)
    store.recover = lambda: None                          # 保留示例里的「正在执行」记录
    store.schedule_due = lambda *_, **__: None            # 预览不跑定时计划
    store.schedule_activities_due = lambda *_, **__: []   # 秒杀活动到点也不排队

    def execute(run, parallel=False):
        # 秒杀按账号并行：和正式实例一样各起一个线程，预览里才能看到 6 个账号同时开抢。
        if not parallel:
            simulate(manager, run)
            return
        threading.Thread(target=simulate, args=(manager, run), kwargs={"parallel": True},
                         name=f"preview-seckill-{run['id'][:8]}", daemon=True).start()

    manager.execute = execute

    @app.before_request
    def guard():
        if request.method == "POST" and (request.path.endswith("/notify-test")
                                         or request.path == "/api/backup/import"):
            return jsonify(error="审核预览不会发送通知或导入备份，请在正式实例中操作。"), 400
        return None

    @app.after_request
    def label(response):
        if request.path == "/" and response.mimetype == "text/html":
            text = response.get_data(as_text=True)
            text = text.replace('<div id="accounts-view">',
                                f'<div class="password-warning">{BANNER}</div><div id="accounts-view">')
            response.set_data(text)
        elif request.path == "/api/accounts" and response.is_json and response.status_code == 200:
            value = response.get_json()
            value["preview"] = True
            response.set_data(app.json.dumps(value))
        return response
