import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SECRET_FIELDS = ("app_token", "fee_session_cookie", "serverchan_sendkey", "bark_url", "webhook_url", "webhook_token")
CONFIG_DEFAULTS = {
    "app_token": "", "phone": "", "province_code": "731", "city_code": "0731",
    "carrier_operator": "002", "app_version_code": "12.5.2",
    "activity_id": "1021122301", "channel_id": "P00000109876",
    "fee_session_cookie": "", "fee_prov_code": "",
    "serverchan_sendkey": "", "bark_url": "", "webhook_url": "", "webhook_token": "",
}
OPTION_DEFAULTS = {"claim": True, "tasks": False, "games": False, "wed": False}
TASKS = {"sign": "每日签到", "wed": "周三拼图", "rate": "评价有礼", "fee": "话费查询", "seckill": "假期秒杀"}


def utcnow():
    return datetime.now(timezone.utc)


TASK_ORDER = ("sign", "wed", "rate", "fee", "seckill")
BATCH_TASKS = ("sign", "wed", "rate", "fee")
RUN_STATUSES = ("queued", "running", "success", "partial", "failed", "interrupted")
RUN_SOURCES = ("manual", "schedule", "import")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SCHEDULE_FREQUENCIES = ("daily", "weekly")
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DEFAULT_SCHEDULE_TIME = "08:23"
TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def empty_schedule():
    """任务默认关闭；开启后按每天固定时间执行。"""
    return {"enabled": False, "time": DEFAULT_SCHEDULE_TIME, "frequency": "daily",
            "weekdays": [], "next_run": None}


def next_run(schedule_time, timezone_name="Asia/Shanghai", now=None, frequency="daily", weekdays=()):
    """计算下一次执行时间；每周计划只在选中的星期触发。"""
    now = now or utcnow()
    local = now.astimezone(ZoneInfo(timezone_name))
    hour, minute = map(int, schedule_time.split(":"))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "weekly":
        days = sorted({int(day) % 7 for day in weekdays}) or [target.weekday()]
        for offset in range(8):
            candidate = target + timedelta(days=offset)
            if candidate.weekday() in days and candidate > local:
                target = candidate
                break
    elif target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


def normalize_schedule(task, raw, old=None, timezone_name="Asia/Shanghai", now=None):
    """校验单个任务的执行计划。"""
    if not isinstance(raw, dict):
        raise ValueError(f"{TASKS[task]}的执行计划格式不正确")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"{TASKS[task]}的开关必须为布尔值")
    time_value = raw.get("time", DEFAULT_SCHEDULE_TIME)
    if not isinstance(time_value, str) or not TIME_PATTERN.fullmatch(time_value):
        raise ValueError("执行时间格式应为 HH:MM")
    frequency = raw.get("frequency", "daily")
    if frequency not in SCHEDULE_FREQUENCIES:
        raise ValueError("执行频率只能是每天或每周")
    weekdays = raw.get("weekdays") or []
    if (not isinstance(weekdays, list) or len(weekdays) > 7
            or any(not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6 for day in weekdays)):
        raise ValueError("每周执行的星期取值无效")
    weekdays = sorted(set(weekdays))
    if frequency == "weekly" and not weekdays:
        raise ValueError(f"{TASKS[task]}按每周执行时至少要选择一天")
    entry = {"enabled": enabled, "time": time_value, "frequency": frequency,
             "weekdays": weekdays, "next_run": None}
    previous = old if isinstance(old, dict) else {}
    if (enabled and previous.get("enabled") and previous.get("time") == time_value
            and previous.get("frequency") == frequency
            and list(previous.get("weekdays") or []) == weekdays):
        # 计划定义没变时保留调度器已经推进的时间，编辑账号不会重复排队。
        entry["next_run"] = previous.get("next_run")
    if enabled and not entry["next_run"]:
        entry["next_run"] = next_run(time_value, timezone_name, now, frequency, weekdays)
    return entry


def normalize_schedules(body, old=None, timezone_name="Asia/Shanghai", now=None):
    """每个任务一套独立计划；兼容 1.3 及更早版本的单条每日签到计划。"""
    previous = (old or {}).get("schedules") or {}
    supplied = body.get("schedules")
    if supplied is not None and not isinstance(supplied, dict):
        raise ValueError("执行计划格式不正确")
    legacy = supplied is None and ("enabled" in body or "schedule_time" in body)
    schedules = {}
    for task in TASK_ORDER:
        if supplied is not None:
            raw = supplied.get(task) or {"enabled": False}
        elif legacy and task == "sign":
            old_sign = previous.get("sign") or empty_schedule()
            raw = {"enabled": body.get("enabled", (old or {}).get("enabled", False)),
                   "time": body.get("schedule_time", old_sign["time"]),
                   "frequency": old_sign["frequency"], "weekdays": old_sign["weekdays"]}
        else:
            raw = previous.get(task) or {"enabled": False}
        schedules[task] = normalize_schedule(task, raw, previous.get(task), timezone_name, now)
    return schedules


def schedule_mirrors(schedules):
    """旧版单计划字段（enabled/schedule_time/next_run）与任务计划保持一致。"""
    upcoming = [datetime.fromisoformat(entry["next_run"]) for entry in schedules.values()
                if entry["enabled"] and entry["next_run"]]
    sign = schedules.get("sign") or empty_schedule()
    return {"enabled": any(entry["enabled"] for entry in schedules.values()),
            "schedule_time": sign["time"],
            "next_run": min(upcoming).isoformat() if upcoming else None}


def decode_schedules(raw, row=None):
    """读取任务计划；没有任务计划列的旧记录按原来的每日签到计划还原。"""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    schedules = {}
    for task in TASK_ORDER:
        entry = data.get(task)
        if isinstance(entry, dict):
            stored = {**empty_schedule(), **{key: entry[key] for key in empty_schedule() if key in entry}}
            stored["enabled"] = bool(stored["enabled"])
            stored["weekdays"] = sorted({day for day in stored.get("weekdays") or []
                                         if isinstance(day, int) and not isinstance(day, bool)})
            schedules[task] = stored
        else:
            fallback = empty_schedule()
            if task == "sign" and row is not None:
                fallback["enabled"] = bool(row.get("enabled"))
                fallback["time"] = row.get("schedule_time") or DEFAULT_SCHEDULE_TIME
                fallback["next_run"] = row.get("next_run")
            schedules[task] = fallback
    return schedules


def normalize_account(body, old=None, timezone_name="Asia/Shanghai"):
    if not isinstance(body, dict):
        raise ValueError("账号内容必须是 JSON 对象")
    old = old or {}
    name = body.get("name", old.get("name", ""))
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        raise ValueError("账号名称应为 1～40 个字符")
    supplied = body.get("config", {})
    if not isinstance(supplied, dict) or set(supplied) - CONFIG_DEFAULTS.keys():
        raise ValueError("账号配置包含不支持的字段")
    config = {**CONFIG_DEFAULTS, **old.get("config", {})}
    for field, value in supplied.items():
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError(f"{field} 必须为文本且不超过 16384 字符")
        if field not in SECRET_FIELDS or value.strip():
            config[field] = value.strip()
    clear = body.get("clear_secrets", [])
    if not isinstance(clear, list) or any(x not in SECRET_FIELDS for x in clear):
        raise ValueError("不支持清除该字段")
    for field in clear:
        config[field] = ""
    if not re.fullmatch(r"1[0-9]{10}", config["phone"]):
        raise ValueError("请输入 11 位手机号")
    if not config["app_token"]:
        raise ValueError("首次添加需要填写 App Token")
    if old and config["phone"] != old["config"]["phone"]:
        raise ValueError("手机号不可修改；请为另一个号码新增账号")
    for field in ("province_code", "city_code"):
        if not re.fullmatch(r"[0-9]{1,8}", config[field]):
            raise ValueError(f"{field} 应为数字编码")
    if config["serverchan_sendkey"] and not re.fullmatch(r"[0-9A-Za-z]+", config["serverchan_sendkey"]):
        raise ValueError("Server酱 SendKey 格式不正确")
    if config["bark_url"]:
        from cmcc_sign import assert_safe_url
        try:
            assert_safe_url(config["bark_url"])
        except ValueError:
            raise ValueError("Bark 地址必须是有效的公网 HTTPS 地址") from None
    if config["webhook_url"]:
        from cmcc_sign import validate_webhook
        validate_webhook(config["webhook_url"], config["webhook_token"])
    elif config["webhook_token"]:
        raise ValueError("填写 Webhook Token 时需要同时配置 Webhook 地址；移除渠道请一并清除 Token")
    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise ValueError("enabled 必须为布尔值")
    if "schedule_time" in body and (not isinstance(body["schedule_time"], str)
                                    or not TIME_PATTERN.fullmatch(body["schedule_time"])):
        raise ValueError("执行时间格式应为 HH:MM")
    supplied_options = body.get("options", {})
    if not isinstance(supplied_options, dict) or set(supplied_options) - OPTION_DEFAULTS.keys():
        raise ValueError("不支持该任务选项")
    options = {**OPTION_DEFAULTS, **old.get("options", {}), **supplied_options}
    if any(not isinstance(v, bool) for v in options.values()):
        raise ValueError("任务选项必须为布尔值")
    # 周三拼图已是独立任务，签到脚本不再顺带执行，避免同一天跑两次。
    options["wed"] = False
    schedules = normalize_schedules(body, old, timezone_name)
    account = {"id": old.get("id", uuid.uuid4().hex), "name": name.strip(),
               "config": config, "options": options, "schedules": schedules}
    account.update(schedule_mirrors(schedules))
    return account


def runnable_tasks(account):
    """一键执行只包含日常任务；秒杀通过独立活动配置和运行。"""
    return [task for task in BATCH_TASKS
            if task != "fee" or account["config"].get("fee_session_cookie")]


SECKILL_DEFAULTS = {"lead": 5, "interval": 0.35, "max_attempts": 120, "time": "11:59:55"}
# 秒杀必须提前排队：先建立会话、校时、拉取场次、确认当日签到资格，才能按提前量准时开火。
SECKILL_ENQUEUE_AHEAD = 60
ACTIVITY_KINDS = ("seckill",)
SECKILL_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?")


def activity_config(body, old=None):
    """校验活动的抢购参数：lead／interval／max_attempts，平铺与 {config:{...}} 两种写法都接受。"""
    old_config = (old or {}).get("config", {})
    nested = body.get("config", {})
    if not isinstance(nested, dict):
        raise ValueError("活动配置必须是对象")
    # 不认识的键会被忽略，避免旧客户端多传字段就保存失败。
    supplied = {key: value for key, value in nested.items() if key in SECKILL_DEFAULTS}
    for field in ("lead", "interval", "max_attempts"):
        if field in body:
            supplied[field] = body[field]
    config = {**SECKILL_DEFAULTS, **old_config}
    for field in ("lead", "interval", "max_attempts"):
        if field in supplied:
            try:
                value = float(supplied[field]) if field == "interval" else int(supplied[field])
            except (TypeError, ValueError):
                raise ValueError(f"活动配置 {field} 应为数字") from None
            if field == "lead" and not 0 <= value <= 30:
                raise ValueError("提前量应为 0～30 秒")
            if field == "interval" and not 0.05 <= value <= 5:
                raise ValueError("重试间隔应为 0.05～5 秒")
            if field == "max_attempts" and not 1 <= value <= 600:
                raise ValueError("最大尝试次数应为 1～600")
            config[field] = value
    return config


def activity_time(body, old=None):
    """校验开抢时间（HH:MM 或 HH:MM:SS）。"""
    value = body.get("time") or (old or {}).get("time") or SECKILL_DEFAULTS["time"]
    if not isinstance(value, str) or not SECKILL_TIME_PATTERN.fullmatch(value):
        raise ValueError("开抢时间格式应为 HH:MM 或 HH:MM:SS")
    return value


def activity_weekdays(body, old=None):
    """校验活动日：周一到周日中的若干天，缺省表示每天。"""
    weekdays = body.get("weekdays", (old or {}).get("weekdays"))
    if weekdays is None:
        return list(range(7))
    if (not isinstance(weekdays, list) or not weekdays
            or any(not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6 for day in weekdays)):
        raise ValueError("活动日应为周一到周日中的若干天")
    return sorted(set(weekdays))


def normalize_activity(body, old=None, accounts=None, timezone_name="Asia/Shanghai"):
    """校验一条内置活动（后端清单用）：kind、名称、执行时间与要执行的账号。"""
    old = old or {}
    kind = body.get("kind", old.get("kind"))
    if kind not in ACTIVITY_KINDS:
        raise ValueError("活动类型不支持")
    name = body.get("name", old.get("name", ""))
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
        raise ValueError("活动名称应为 1～40 个字符")
    supplied_accounts = body.get("accounts")
    if supplied_accounts is not None and not isinstance(supplied_accounts, list):
        raise ValueError("执行账号格式不正确")
    if supplied_accounts is None and old.get("accounts"):
        phone_list = old["accounts"]
    else:
        # 全部账号参与：不选或留空都表示所有账号。
        phone_list = list(dict.fromkeys(item["id"] for item in accounts)) if accounts is not None else (supplied_accounts or [])
    return {"id": old.get("id", uuid.uuid4().hex), "name": name.strip(), "kind": kind,
            "config": activity_config(body, old), "time": activity_time(body, old),
            "weekdays": activity_weekdays(body, old), "accounts": phone_list,
            "enabled": bool(body.get("enabled", old.get("enabled", False))), "next_run": None}


def activity_settings(body, activity, timezone_name="Asia/Shanghai"):
    """网页上能改的只有执行参数：是否启用、开抢时间、抢购参数与活动日。

    活动清单（有哪些活动、叫什么、用哪个脚本、给哪些账号）由 `cmcc_app/activities.py` 决定，
    提交里的 name／kind／accounts 一律忽略，改不动。
    """
    if not isinstance(body, dict):
        raise ValueError("请求必须为 JSON 对象")
    return {**activity, "config": activity_config(body, activity),
            "time": activity_time(body, activity), "weekdays": activity_weekdays(body, activity),
            "enabled": bool(body.get("enabled", activity.get("enabled", False))), "next_run": None}


def activity_next_run(activity, timezone_name="Asia/Shanghai", now=None):
    """活动的下一次开抢时间（HH:MM:SS 支持秒级）。"""
    now = (now or utcnow()).astimezone(ZoneInfo(timezone_name))
    hh, mm, *ss = activity["time"].split(":")
    target = now.replace(hour=int(hh), minute=int(mm),
                         second=int(ss[0]) if ss else 0, microsecond=0)
    days = activity["weekdays"]
    for offset in range(8):
        candidate = target + timedelta(days=offset)
        if candidate.weekday() in days and candidate > now:
            return candidate.astimezone(timezone.utc).isoformat()
    return None


def seckill_enqueue_ahead(activity):
    """这场秒杀要提前多少秒排队：抢购脚本的提前量 + 预热时间。"""
    return float(activity["config"].get("lead", SECKILL_DEFAULTS["lead"])) + SECKILL_ENQUEUE_AHEAD


def public_activity(activity):
    return {**activity, "account_count": len(activity["accounts"]), "all_accounts": True}


def public_account(account):
    result = {**account, "config": dict(account["config"])}
    for field in SECRET_FIELDS:
        result["config"].pop(field, None)
        result[f"has_{field}"] = bool(account["config"].get(field))
    return result


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = data_dir / "cmcc.sqlite3"
        # Account tokens stay private even when the user chooses UMASK=000.
        self.db.touch(mode=0o600, exist_ok=True)
        self.db.chmod(0o600)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
                    config TEXT NOT NULL, options TEXT NOT NULL, enabled INTEGER NOT NULL,
                    schedule_time TEXT NOT NULL, next_run TEXT, schedules TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_name TEXT NOT NULL,
                    task TEXT NOT NULL, dry_run INTEGER NOT NULL, source TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, exit_code INTEGER, message TEXT NOT NULL DEFAULT '',
                    activity_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_account_task
                    ON runs(account_id, task) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS dismissed_task_cards (
                    account_id TEXT NOT NULL, task TEXT NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY (account_id, task)
                );
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    config TEXT NOT NULL, accounts TEXT NOT NULL,
                    enabled INTEGER NOT NULL, next_run TEXT
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
            _ = columns  # 下方沿用同一个集合做账号表迁移
            # 1.6.3 起同一账号的不同任务可以并存于队列，只限制同一任务重复提交。
            conn.execute("DROP INDEX IF EXISTS one_pending_per_account")
            # 1.3 及更早版本只有一条每日签到计划，旧记录按原有列还原。
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
            if "schedules" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN schedules TEXT")

            for name in ("status_reset_at", "fee_reset_at"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            if "result" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN result TEXT")
            # 1.6.7 起秒杀记录记住自己来自哪个活动，执行时据此取活动的抢购参数。
            if "activity_id" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN activity_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS runs_account_task ON runs(account_id,task,dry_run,created_at DESC)")
            # 有明确主签到成功证据的旧记录按新规则更新；保留子结果、日志和退出码。
            for row in conn.execute("SELECT * FROM runs WHERE task='sign' AND dry_run=0 "
                                    "AND status IN ('success','partial','failed') AND result IS NOT NULL").fetchall():
                updated = self.normalize_run_result(dict(row))
                if any(updated[key] != row[key] for key in ("status", "message", "result")):
                    conn.execute("UPDATE runs SET status=?,message=?,result=? WHERE id=?",
                                 (updated["status"], updated["message"], updated["result"], row["id"]))

    @staticmethod
    def normalize_run_result(row):
        from .results import clean, for_task, REASONS
        original = clean(row.get("result"))
        if row["status"] not in ("success", "partial", "failed"):
            return row
        result = for_task(row["task"], original, row["dry_run"])
        if result and (result != original or (row["task"] == "sign" and not row["dry_run"]
                                             and result["state"] == "success" and row["status"] != "success"
                                             and result["parts"].get("main", {}).get("state") == "success")):
            return {**row, "status": result["state"], "message": REASONS[result["reason"]],
                    "result": json.dumps(result)}
        return row

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        raw = dict(row)
        value = dict(raw)
        value.pop("phone", None)
        # 合并默认值：升级前保存的账号可能缺少后加入的字段（如 webhook_url）。
        value["config"] = {**CONFIG_DEFAULTS, **json.loads(value["config"])}
        value["options"] = {**OPTION_DEFAULTS, **json.loads(value["options"])}
        # 旧账号可能勾选过「签到时一并执行周三拼图」，读取时按独立任务处理。
        value["options"]["wed"] = False
        value["schedules"] = decode_schedules(value.pop("schedules", None), raw)
        # enabled/schedule_time/next_run 是旧版字段，始终按任务计划重新计算。
        value.update(schedule_mirrors(value["schedules"]))
        value["enabled"] = bool(value["enabled"])
        return value

    def accounts(self):
        with self.connect() as conn:
            return [self.decode(row) for row in conn.execute("SELECT * FROM accounts ORDER BY rowid")]

    def account(self, account_id):
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())

    def save(self, account):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
            schedules = {task: dict(account.get("schedules", {}).get(task) or empty_schedule())
                         for task in TASK_ORDER}
            if current is not None:
                old_config = json.loads(current["config"])
                for field, column in (("app_token", "status_reset_at"), ("fee_session_cookie", "fee_reset_at")):
                    if old_config.get(field) != account["config"].get(field):
                        conn.execute(f"UPDATE accounts SET {column}=? WHERE id=?", (utcnow().isoformat(), account["id"]))
                previous = decode_schedules(dict(current).get("schedules"), dict(current))
                for task, entry in schedules.items():
                    old = previous.get(task) or {}
                    if (entry["enabled"] and old.get("enabled") and entry["time"] == old.get("time")
                            and entry["frequency"] == old.get("frequency")
                            and list(entry.get("weekdays") or []) == list(old.get("weekdays") or [])):
                        # 计划没改时保留调度器在此期间已经推进的时间。
                        entry["next_run"] = old.get("next_run")
            account["schedules"] = schedules
            account.update(schedule_mirrors(schedules))
            try:
                conn.execute("""INSERT INTO accounts
                    (id,name,phone,config,options,enabled,schedule_time,next_run,schedules,status_reset_at,fee_reset_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                    config=excluded.config, options=excluded.options, enabled=excluded.enabled,
                    schedule_time=excluded.schedule_time, next_run=excluded.next_run,
                    schedules=excluded.schedules""",
                    (account["id"], account["name"], account["config"]["phone"],
                     json.dumps(account["config"]), json.dumps(account["options"]),
                     account["enabled"], account["schedule_time"], account["next_run"],
                     json.dumps(schedules, ensure_ascii=False),
                     account.get("status_reset_at", ""), account.get("fee_reset_at", "")))
            except sqlite3.IntegrityError:
                raise ValueError("此手机号已经存在") from None

    def delete(self, account_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM runs WHERE account_id=? AND status IN ('queued','running')",
                            (account_id,)).fetchone():
                raise ValueError("账号有待执行或运行中的任务，请结束后再删除")
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            conn.execute("DELETE FROM dismissed_task_cards WHERE account_id=?", (account_id,))

    def dismiss_task_card(self, account_id, task, run_id):
        """只隐藏已结束秒杀的卡片信息；并发提交/新结果出现时拒绝旧页面移除。"""
        if task != "seckill":
            raise ValueError("只有秒杀任务可以从账号卡片移除")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ValueError("账号不存在")
            if conn.execute("SELECT 1 FROM runs WHERE account_id=? AND task=? "
                            "AND status IN ('queued','running')", (account_id, task)).fetchone():
                raise ValueError("任务正在执行或排队，结束后才能从卡片移除")
            latest = conn.execute("SELECT id FROM runs WHERE account_id=? AND task=? "
                                  "ORDER BY created_at DESC,rowid DESC LIMIT 1", (account_id, task)).fetchone()
            if not latest or latest["id"] != run_id:
                raise ValueError("任务结果已更新，请刷新后再移除")
            conn.execute("INSERT INTO dismissed_task_cards (account_id,task,run_id) VALUES (?,?,?) "
                         "ON CONFLICT(account_id,task) DO UPDATE SET run_id=excluded.run_id",
                         (account_id, task, run_id))

    @staticmethod
    def insert_run(conn, account, task, dry_run, source, now, activity_id=None):
        run_id = uuid.uuid4().hex
        try:
            conn.execute("""INSERT INTO runs
                (id,account_id,account_name,task,dry_run,source,status,created_at,activity_id)
                VALUES (?,?,?,?,?,?,'queued',?,?)""",
                (run_id, account["id"], account["name"], task, dry_run, source, now, activity_id))
        except sqlite3.IntegrityError:
            raise ValueError(f"{TASKS[task]}已在排队或执行中，请等它结束后再提交") from None
        conn.execute("DELETE FROM dismissed_task_cards WHERE account_id=? AND task=?", (account["id"], task))
        return run_id

    def enqueue(self, account_id, task, dry_run=False, activity_id=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self.decode(conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
            if not account:
                raise ValueError("账号不存在")
            if task == "fee" and not account["config"]["fee_session_cookie"]:
                raise ValueError("话费查询需要先填写话费会话 Cookie")
            if task == "seckill" and dry_run:
                dry_run = False
            return self.insert_run(conn, account, task, dry_run, "manual", utcnow().isoformat(),
                                   activity_id if task == "seckill" else None)

    def schedule_due(self, timezone_name, now=None):
        """按每个任务自己的计划排队；同一账号同一时刻只保留一个待执行任务。"""
        now = now or utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT * FROM accounts").fetchall():
                account = self.decode(row)
                schedules = account["schedules"]
                changed = False
                for task in TASK_ORDER:
                    entry = schedules[task]
                    if not entry["enabled"]:
                        continue
                    scheduled = entry.get("next_run")
                    if scheduled and datetime.fromisoformat(scheduled) > now:
                        continue
                    try:
                        self.insert_run(conn, account, task, False, "schedule", now.isoformat())
                    except ValueError:
                        # 该账号还有别的任务在排队或运行：保留原定时间，下个周期补跑。
                        continue
                    entry["next_run"] = next_run(entry["time"], timezone_name, now,
                                                 entry["frequency"], entry["weekdays"])
                    changed = True
                if changed:
                    mirrors = schedule_mirrors(schedules)
                    conn.execute("""UPDATE accounts SET schedules=?, enabled=?, schedule_time=?, next_run=?
                                    WHERE id=?""",
                                 (json.dumps(schedules, ensure_ascii=False), mirrors["enabled"],
                                  mirrors["schedule_time"], mirrors["next_run"], account["id"]))

    def take_run_parallel(self):
        """取一个待执行的秒杀任务（不同账号可同时开抢）。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE status='queued' AND task='seckill' "
                               "ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",
                         (utcnow().isoformat(), row["id"]))
            return dict(row)

    def take_run(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",
                         (utcnow().isoformat(), row["id"]))
            return dict(row)

    def finish(self, run_id, status, exit_code, message="", result=None):
        with self.connect() as conn:
            from .results import clean
            result = clean(result)
            row = conn.execute("SELECT task,dry_run FROM runs WHERE id=?", (run_id,)).fetchone()
            if row:
                normalized = self.normalize_run_result({**dict(row), "status": status, "message": message,
                                                        "result": json.dumps(result) if result else None})
                status, message = normalized["status"], normalized["message"]
                result = clean(normalized["result"])
            conn.execute("UPDATE runs SET status=?,exit_code=?,message=?,finished_at=?,result=? WHERE id=?",
                         (status, exit_code, message, utcnow().isoformat(),
                          json.dumps(result) if result else None, run_id))

    def recover(self):
        with self.connect() as conn:
            conn.execute("""UPDATE runs SET status='interrupted',finished_at=?,
                message='上次应用停止时任务未结束，未自动重试' WHERE status='running'""", (utcnow().isoformat(),))

    def runs(self, limit=100):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    @staticmethod
    def run_where(account_id=None, task=None, status=None, source=None, date_from=None,
                  date_to=None, keyword=None, finished_only=False):
        """拼装执行记录的筛选条件；日期边界为 UTC ISO 文本，结束时间不包含在内。"""
        clauses, values = [], []
        for column, value in (("account_id", account_id), ("task", task), ("status", status),
                              ("source", source)):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if date_from:
            clauses.append("created_at>=?")
            values.append(date_from)
        if date_to:
            clauses.append("created_at<?")
            values.append(date_to)
        if keyword:
            clauses.append("(message LIKE ? OR account_name LIKE ?)")
            values.extend([f"%{keyword}%"] * 2)
        if finished_only:
            clauses.append("status NOT IN ('queued','running')")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def runs_page(self, page=1, page_size=20, **filters):
        """分页读取执行记录，返回 (rows, 总条数)。"""
        where, values = self.run_where(**filters)
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM runs{where}", values).fetchone()[0]
            rows = conn.execute(f"""SELECT * FROM runs{where}
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?""",
                [*values, page_size, (page - 1) * page_size]).fetchall()
        return [dict(row) for row in rows], int(total)

    def run_status_counts(self, **filters):
        """按状态汇总当前筛选条件（忽略状态本身）下的记录数。"""
        filters.pop("status", None)
        where, values = self.run_where(**filters)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT status, COUNT(*) AS total FROM runs{where} GROUP BY status",
                                values).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def search_run_logs(self, keyword, runs, max_files=1000, max_bytes=256 * 1024):
        """在候选记录的日志正文里查找关键字；只读每个日志的开头，避免大文件拖慢查询。"""
        needle = keyword.casefold()
        matched = set()
        for run in runs[:max_files]:
            path = self.data_dir / "logs" / f"{run['id']}.log"
            try:
                if not path.is_file():
                    continue
                with path.open("rb") as stream:
                    text = stream.read(max_bytes).decode("utf-8", errors="replace")
            except OSError:
                continue
            if needle in text.casefold():
                matched.add(run["id"])
        return matched

    def delete_runs(self, ids):
        """删除已结束的记录与日志文件，返回 (删除条数, 仍在执行的记录 id)。"""
        unique = list(dict.fromkeys(str(value) for value in ids if str(value)))[:500]
        if not unique:
            return 0, []
        marks = ",".join("?" * len(unique))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(f"SELECT id,status FROM runs WHERE id IN ({marks})", unique).fetchall()
            pending = [row["id"] for row in rows if row["status"] in ("queued", "running")]
            deletable = [row["id"] for row in rows if row["status"] not in ("queued", "running")]
            if deletable:
                conn.execute(f"DELETE FROM runs WHERE id IN ({','.join('?' * len(deletable))})", deletable)
        for run_id in deletable:
            (self.data_dir / "logs" / f"{run_id}.log").unlink(missing_ok=True)
        return len(deletable), pending

    def clear_runs(self, **filters):
        """按筛选条件删除已结束的记录与日志；排队或运行中的记录保留。"""
        where, values = self.run_where(**filters, finished_only=True)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = [row["id"] for row in conn.execute(f"SELECT id FROM runs{where}", values)]
            if ids:
                conn.execute(f"DELETE FROM runs WHERE id IN ({','.join('?' * len(ids))})", ids)
        for run_id in ids:
            (self.data_dir / "logs" / f"{run_id}.log").unlink(missing_ok=True)
        return len(ids)

    def pending_runs(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    def pending_account_ids(self):
        """有排队或运行中任务的账号，供界面显示状态。"""
        with self.connect() as conn:
            return [row["account_id"] for row in conn.execute(
                "SELECT DISTINCT account_id FROM runs WHERE status IN ('queued','running')")]

    def latest_task_runs(self, statuses):
        """每个账号每个任务最近一次正式执行的结果，用于批量重试失败的任务。

        这里不看凭证更新时间，方便更新 App Token 后一次性重跑此前失败的任务。
        """
        marks = ",".join("?" * len(statuses))
        with self.connect() as conn:
            rows = conn.execute(f"""SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY account_id,task ORDER BY created_at DESC,rowid DESC) AS rank
                FROM runs WHERE dry_run=0) WHERE rank=1 AND status IN ({marks})""",
                tuple(statuses)).fetchall()
        return [dict(row) for row in rows]

    def latest_seckill_results(self, account_ids):
        """每个账号最近一次秒杀：状态、原因与完成时间。"""
        marks = ",".join("?" * len(account_ids))
        with self.connect() as conn:
            rows = conn.execute(f"""SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY created_at DESC,rowid DESC) AS rank
                FROM runs WHERE task='seckill') WHERE rank=1""").fetchall()
        results = {}
        from .results import clean
        for row in rows:
            run = dict(row)
            if run["account_id"] not in account_ids:
                continue
            outcome = clean(run.get("result"))
            pending = run["status"] in ("queued", "running")
            results[run["account_id"]] = {
                "status": run["status"],
                "message": run["message"],
                "finished_at": run.get("finished_at") or run.get("created_at"),
                "run_id": run["id"],
                "success": (not pending and run["status"] == "success"
                            and (outcome or {}).get("reason") not in ("business", "auth", "fee_auth", "network", "timeout")),
                "pending": pending,
            }
        return results

    def activities(self):
        with self.connect() as conn:
            return [self.decode_activity(row) for row in conn.execute("SELECT * FROM activities ORDER BY rowid")]

    def activity(self, activity_id):
        with self.connect() as conn:
            return self.decode_activity(conn.execute("SELECT * FROM activities WHERE id=?",
                                                     (activity_id,)).fetchone())

    @staticmethod
    def decode_activity(row):
        if row is None:
            return None
        value = dict(row)
        config = json.loads(value["config"])
        value["config"] = {key: config[key] for key in SECKILL_DEFAULTS if key in config}
        value["time"] = config.get("time") or SECKILL_DEFAULTS["time"]
        value["weekdays"] = config.get("weekdays") or list(range(7))
        value["accounts"] = json.loads(value["accounts"])
        value["enabled"] = bool(value["enabled"])
        return value

    def sync_activities(self, definitions, timezone_name="Asia/Shanghai"):
        """按后端清单同步活动：补上新增的、刷新名称与参与账号、保留用户改过的执行参数、下线已移除的。

        秒杀接口各不相同，清单只在代码里维护（见 cmcc_app/activities.py），网页上不能增删。
        """
        stored = {item["id"]: item for item in self.activities()}
        accounts = self.accounts()  # 全部账号参与：每次同步都按当前账号刷新
        added, updated, removed, kept = [], [], [], []
        for item in definitions:
            old = stored.pop(item["id"], None)
            defaults = {**SECKILL_DEFAULTS, **item.get("defaults", {})}
            activity = normalize_activity(
                {"kind": item["kind"], "name": item["name"],
                 "config": {**defaults, **(old["config"] if old else {})},
                 "time": (old or {}).get("time") or defaults["time"],
                 "weekdays": (old or {}).get("weekdays"),
                 "enabled": bool(old["enabled"]) if old else False},
                {"id": item["id"]}, accounts=accounts, timezone_name=timezone_name)
            self.save_activity(activity, timezone_name)
            (updated if old else added).append(item["id"])
        for activity_id in list(stored):
            try:
                self.delete_activity(activity_id)
            except ValueError:
                kept.append(activity_id)  # 还有排队中的秒杀任务，先留着，任务结束再下线
            else:
                removed.append(activity_id)
        return {"added": added, "updated": updated, "removed": removed, "kept": kept}

    def save_activity(self, activity, timezone_name="Asia/Shanghai"):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            activity["next_run"] = activity_next_run(activity, timezone_name)
            # 秒杀活动固定「全部账号参与」：账号列表始终跟随当前的账号列表。
            activity["accounts"] = self.activity_account_ids()
            stored = {**activity["config"], "time": activity["time"], "weekdays": activity["weekdays"]}
            try:
                conn.execute("""INSERT INTO activities (id,name,kind,config,accounts,enabled,next_run)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, config=excluded.config,
                    accounts=excluded.accounts, enabled=excluded.enabled, next_run=excluded.next_run""",
                             (activity["id"], activity["name"], activity["kind"],
                              json.dumps(stored), json.dumps(activity["accounts"]),
                              activity["enabled"], activity["next_run"]))
            except sqlite3.IntegrityError:
                raise ValueError("已有同名活动") from None
        return activity

    def delete_activity(self, activity_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute("SELECT COUNT(*) FROM runs WHERE activity_id=? AND status IN "
                                   "('queued','running')", (activity_id,)).fetchone()[0]
            if pending:
                raise ValueError(f"活动还有 {pending} 个排队或执行中的秒杀任务，请先停止再删除")
            conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))

    def schedule_activities_due(self, timezone_name, now=None):
        """活动到点前排队秒杀：提前「提前量 + 预热时间」入队，让脚本赶在开抢前就位。"""
        now = now or utcnow()
        queued = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT * FROM activities").fetchall():
                activity = self.decode_activity(row)
                if not activity["enabled"]:
                    continue
                due = activity.get("next_run")
                if not due:
                    continue
                fire = datetime.fromisoformat(due)
                if now < fire - timedelta(seconds=seckill_enqueue_ahead(activity)):
                    continue
                # 全部账号参与：每次到点都按当前账号列表排队，新增账号不需要改配置。
                account_rows = conn.execute("SELECT * FROM accounts").fetchall()
                for account_row in account_rows:
                    account = self.decode(account_row)
                    account_id = account["id"]
                    try:
                        run_id = self.insert_run(conn, account, "seckill", False, "schedule",
                                                 now.isoformat(), activity["id"])
                    except ValueError:
                        continue  # 该账号已有同任务在队列，跳过
                    queued.append({"account_id": account_id, "task": "seckill", "run_id": run_id})
                # 下一场从这次的场次时间往后算，避免同一次开抢被重复排队。
                activity["next_run"] = activity_next_run(activity, timezone_name, fire)
                conn.execute("UPDATE activities SET next_run=? WHERE id=?",
                             (activity["next_run"], activity["id"]))
        return queued

    def seckill_settings(self, activity_id=None):
        """这条秒杀记录要用的抢购参数：优先它所属的活动，其次第一个已启用的活动，最后用默认值。"""
        activities = self.activities()
        activity = (next((item for item in activities if item["id"] == activity_id), None)
                    or next((item for item in activities if item["enabled"]), None))
        config = activity["config"] if activity else SECKILL_DEFAULTS
        return {"name": activity["name"] if activity else "默认参数",
                "time": activity["time"] if activity else SECKILL_DEFAULTS["time"],
                "lead": config.get("lead", SECKILL_DEFAULTS["lead"]),
                "interval": config.get("interval", SECKILL_DEFAULTS["interval"]),
                "max_attempts": config.get("max_attempts", SECKILL_DEFAULTS["max_attempts"])}

    def activity_account_ids(self):
        """秒杀活动固定「全部账号参与」：按当前账号列表算，新增账号不用改配置。"""
        with self.connect() as conn:
            return [row["id"] for row in conn.execute("SELECT id FROM accounts ORDER BY rowid").fetchall()]

    def pending_runs_for_activity_accounts(self, activity):
        """活动卡片显示：哪些账号的秒杀还在排队或执行中。"""
        account_ids = self.activity_account_ids()
        if not account_ids:
            return {}
        marks = ",".join("?" * len(account_ids))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT account_id, status FROM runs WHERE task='seckill' "
                                f"AND status IN ('queued','running') AND account_id IN ({marks})",
                                tuple(account_ids)).fetchall()
        return {row["account_id"]: row["status"] for row in rows}

    def pending_tasks(self):
        """每个账号各自待执行/运行中的任务；界面据此只禁用对应那一项。"""
        with self.connect() as conn:
            return [{"account_id": row["account_id"], "task": row["task"], "status": row["status"]}
                    for row in conn.execute(
                        "SELECT account_id, task, status FROM runs "
                        "WHERE status IN ('queued','running') ORDER BY created_at")]

    def clear(self):
        """清空账号、执行记录和对应日志，用于替换导入或重置实例。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT id FROM runs").fetchall():
                (self.data_dir / "logs" / f"{row['id']}.log").unlink(missing_ok=True)
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM dismissed_task_cards")

    def add_history(self, rows):
        """导入历史记录；已存在的记录保留原样，返回新增条数。"""
        if not rows:
            return 0
        inserted = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                row = self.normalize_run_result(row)
                cursor = conn.execute("""INSERT OR IGNORE INTO runs
                    (id,account_id,account_name,task,dry_run,source,status,created_at,
                     started_at,finished_at,exit_code,message,result)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["id"], row["account_id"], row["account_name"], row["task"],
                     row["dry_run"], row["source"], row["status"], row["created_at"],
                     row["started_at"], row["finished_at"], row["exit_code"], row["message"], row.get("result")))
                inserted += cursor.rowcount
        return inserted

    def run(self, run_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def prune(self, retention_days):
        cutoff = (utcnow() - timedelta(days=retention_days)).isoformat()
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM runs WHERE finished_at<?", (cutoff,)).fetchall()
            for row in rows:
                (self.data_dir / "logs" / f"{row['id']}.log").unlink(missing_ok=True)
            conn.execute("DELETE FROM runs WHERE finished_at<?", (cutoff,))
