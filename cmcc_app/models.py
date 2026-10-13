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
TASKS = {"sign": "每日签到", "wed": "周三拼图", "rate": "评价有礼", "fee": "话费查询"}


def utcnow():
    return datetime.now(timezone.utc)


TASK_ORDER = ("sign", "wed", "rate", "fee")
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
                    finished_at TEXT, exit_code INTEGER, message TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_account
                    ON runs(account_id) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at DESC);
            """)
            # 1.3 及更早版本只有一条每日签到计划，旧记录按原有列还原。
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
            if "schedules" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN schedules TEXT")

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
                    (id,name,phone,config,options,enabled,schedule_time,next_run,schedules)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                    config=excluded.config, options=excluded.options, enabled=excluded.enabled,
                    schedule_time=excluded.schedule_time, next_run=excluded.next_run,
                    schedules=excluded.schedules""",
                    (account["id"], account["name"], account["config"]["phone"],
                     json.dumps(account["config"]), json.dumps(account["options"]),
                     account["enabled"], account["schedule_time"], account["next_run"],
                     json.dumps(schedules, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                raise ValueError("此手机号已经存在") from None

    def delete(self, account_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM runs WHERE account_id=? AND status IN ('queued','running')",
                            (account_id,)).fetchone():
                raise ValueError("账号有待执行或运行中的任务，请结束后再删除")
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    @staticmethod
    def insert_run(conn, account, task, dry_run, source, now):
        run_id = uuid.uuid4().hex
        try:
            conn.execute("""INSERT INTO runs
                (id,account_id,account_name,task,dry_run,source,status,created_at)
                VALUES (?,?,?,?,?,?,'queued',?)""",
                (run_id, account["id"], account["name"], task, dry_run, source, now))
        except sqlite3.IntegrityError:
            raise ValueError("该账号已有待执行或运行中的任务") from None
        return run_id

    def enqueue(self, account_id, task, dry_run=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self.decode(conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
            if not account:
                raise ValueError("账号不存在")
            if task == "fee" and not account["config"]["fee_session_cookie"]:
                raise ValueError("话费查询需要先填写话费会话 Cookie")
            return self.insert_run(conn, account, task, dry_run, "manual", utcnow().isoformat())

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

    def take_run(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",
                         (utcnow().isoformat(), row["id"]))
            return dict(row)

    def finish(self, run_id, status, exit_code, message=""):
        with self.connect() as conn:
            conn.execute("UPDATE runs SET status=?,exit_code=?,message=?,finished_at=? WHERE id=?",
                         (status, exit_code, message, utcnow().isoformat(), run_id))

    def recover(self):
        with self.connect() as conn:
            conn.execute("""UPDATE runs SET status='interrupted',finished_at=?,
                message='上次应用停止时任务未结束，未自动重试' WHERE status='running'""", (utcnow().isoformat(),))

    def runs(self, limit=100):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def pending_runs(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    def clear(self):
        """清空账号、执行记录和对应日志，用于替换导入或重置实例。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT id FROM runs").fetchall():
                (self.data_dir / "logs" / f"{row['id']}.log").unlink(missing_ok=True)
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM accounts")

    def add_history(self, rows):
        """导入历史记录；已存在的记录保留原样，返回新增条数。"""
        if not rows:
            return 0
        inserted = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                cursor = conn.execute("""INSERT OR IGNORE INTO runs
                    (id,account_id,account_name,task,dry_run,source,status,created_at,
                     started_at,finished_at,exit_code,message)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["id"], row["account_id"], row["account_name"], row["task"],
                     row["dry_run"], row["source"], row["status"], row["created_at"],
                     row["started_at"], row["finished_at"], row["exit_code"], row["message"]))
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
