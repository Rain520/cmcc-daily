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


def next_run(schedule_time, timezone_name, now=None):
    now = now or utcnow()
    local = now.astimezone(ZoneInfo(timezone_name))
    hour, minute = map(int, schedule_time.split(":"))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


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
    enabled = body.get("enabled", old.get("enabled", False))
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须为布尔值")
    schedule = body.get("schedule_time", old.get("schedule_time", "08:23"))
    if not isinstance(schedule, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule):
        raise ValueError("执行时间格式应为 HH:MM")
    supplied_options = body.get("options", {})
    if not isinstance(supplied_options, dict) or set(supplied_options) - OPTION_DEFAULTS.keys():
        raise ValueError("不支持该任务选项")
    options = {**OPTION_DEFAULTS, **old.get("options", {}), **supplied_options}
    if any(not isinstance(v, bool) for v in options.values()):
        raise ValueError("任务选项必须为布尔值")
    scheduled = old.get("next_run")
    if not enabled:
        scheduled = None
    elif not old.get("enabled") or schedule != old.get("schedule_time") or not scheduled:
        scheduled = next_run(schedule, timezone_name)
    return {"id": old.get("id", uuid.uuid4().hex), "name": name.strip(),
            "config": config, "options": options, "enabled": enabled,
            "schedule_time": schedule, "next_run": scheduled}


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
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
                    config TEXT NOT NULL, options TEXT NOT NULL, enabled INTEGER NOT NULL,
                    schedule_time TEXT NOT NULL, next_run TEXT
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
        value = dict(row)
        value.pop("phone", None)
        # 合并默认值：升级前保存的账号可能缺少后加入的字段（如 webhook_url）。
        value["config"] = {**CONFIG_DEFAULTS, **json.loads(value["config"])}
        value["options"] = {**OPTION_DEFAULTS, **json.loads(value["options"])}
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
            current = conn.execute("SELECT enabled,schedule_time,next_run FROM accounts WHERE id=?",
                                   (account["id"],)).fetchone()
            if (current and current["enabled"] and account["enabled"]
                    and current["schedule_time"] == account["schedule_time"]):
                # 编辑配置时保留调度器在此期间已经推进的时间。
                account["next_run"] = current["next_run"]
            try:
                conn.execute("""INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                    config=excluded.config, options=excluded.options, enabled=excluded.enabled,
                    schedule_time=excluded.schedule_time, next_run=excluded.next_run""",
                    (account["id"], account["name"], account["config"]["phone"],
                     json.dumps(account["config"]), json.dumps(account["options"]),
                     account["enabled"], account["schedule_time"], account["next_run"]))
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
        now = now or utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM accounts WHERE enabled=1 AND next_run<=?",
                                (now.isoformat(),)).fetchall()
            for row in rows:
                account = self.decode(row)
                try:
                    self.insert_run(conn, account, "sign", False, "schedule", now.isoformat())
                except ValueError:
                    pass  # 当前任务覆盖本次调度，不重复排队。
                conn.execute("UPDATE accounts SET next_run=? WHERE id=?",
                             (next_run(account["schedule_time"], timezone_name, now), account["id"]))

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

    def runs(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100")]

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
