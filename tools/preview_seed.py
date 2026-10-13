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


def main(days=25, seed=20261013):
    store = Store(Path("/data"))
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
                (Path("/data/logs") / f"{run_id}.log").write_text(
                    LOGS[task] + f"\n手机尾号 {account['config']['phone'][-4:]}\n", encoding="utf-8")
                created += 1
    print(f"示例账号 {len(accounts)} 个，示例记录 {created} 条")


if __name__ == "__main__":
    main()
