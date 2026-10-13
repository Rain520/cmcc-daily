"""预览 / 截图用的示例数据。

约定：预览实例与截图里**只能用完全虚构的数据**，不得使用真实手机号、真实 App Token
或真实通知地址；这里统一使用 138/137/139 开头的示例号码，凭证写成 preview-token。
所有任务计划默认关闭，脚本不联网、不会触发任何真实任务。

用法（容器内执行，数据目录为 /data）：

    Get-Content tools/preview_seed.py -Raw | docker exec -i -u 10001:10001 <容器名> python -
"""

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cmcc_app.models import Store, normalize_account

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


def main(days=25, seed=20261013, data_dir="/data", status_demo=False):
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
    random.seed(seed)
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
    """Only use with preview_server.py: schedules are visible, no real task ever starts."""
    import json
    from zoneinfo import ZoneInfo
    frozen = []
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    fixtures = (
        ("13800000011", "示例 · 日常账号", "normal"),
        ("13700000022", "示例 · 更新凭证", "invalid"),
        ("13900000033", "示例 · 部分异常", "warning"),
        ("13600000044", "示例 · 正在签到", "running"),
        ("13500000055", "示例 · 等待队列", "queued"),
        ("13400000066", "示例 · 新增账号", "unknown"),
    )
    for index, (phone, name, health) in enumerate(fixtures):
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
    # 这两条记录固定展示「正在执行 / 排队中」，预览服务不会真的取走它们。
    (root / "preview-fixtures.json").write_text(
        json.dumps({"runs": frozen}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已生成 6 个虚构账号，覆盖全部账号状态；其中 2 条排队记录会固定保留。")


if __name__ == "__main__":
    main()
