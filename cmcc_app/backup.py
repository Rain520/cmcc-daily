"""加密备份与迁移。

导出内容包含账号凭证、通知配置、每日计划、JWT 缓存和执行记录，
使用备份密码派生的 AES-256-GCM 密钥加密，备份密码不会写入文件。
换服务器部署后导入同一个文件即可继续使用，不需要重新添加账号。
"""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import __version__
from .models import CONFIG_DEFAULTS, OPTION_DEFAULTS, normalize_account
from .storage import account_key, atomic_json

FORMAT_NAME = "cmcc-backup"
FORMAT_VERSION = 1
MIN_PASSPHRASE_LENGTH = 8
MAX_PASSPHRASE_LENGTH = 128
KDF_ITERATIONS = 300_000
SALT_BYTES = 16
NONCE_BYTES = 12
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ACCOUNTS = 200
MAX_RUNS = 1000
MAX_LOG_BYTES = 512 * 1024
MAX_LOG_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024
CACHE_NAME_LENGTH = 64
HISTORY_STATUSES = ("success", "partial", "failed", "interrupted")


def check_passphrase(value, field="备份密码"):
    """备份密码用于派生加密密钥，长度不足时无法保证加密强度。"""
    if not isinstance(value, str):
        raise ValueError(f"{field}必须为文本")
    if not MIN_PASSPHRASE_LENGTH <= len(value) <= MAX_PASSPHRASE_LENGTH:
        raise ValueError(f"{field}长度需为 {MIN_PASSPHRASE_LENGTH}～{MAX_PASSPHRASE_LENGTH} 位")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field}不能包含换行或控制字符")
    return value


def _key(passphrase, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def _aad(envelope):
    # 元数据参与认证：格式、版本或导出时间被改动后无法解密。
    return f"{envelope.get('format')}:{envelope.get('format_version')}:{envelope.get('created_at')}".encode("utf-8")


def _text(value, limit):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def encrypt_payload(payload, passphrase):
    check_passphrase(passphrase)
    created_at = payload.get("created_at") or datetime.now(timezone.utc).isoformat()
    payload = {**payload, "created_at": created_at}
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    envelope = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": created_at,
        "kdf": {"name": "pbkdf2-hmac-sha256", "iterations": KDF_ITERATIONS, "salt": salt.hex()},
        "cipher": {"name": "aes-256-gcm", "nonce": nonce.hex()},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    envelope["payload"] = base64.b64encode(
        AESGCM(_key(passphrase, salt, KDF_ITERATIONS)).encrypt(nonce, body, _aad(envelope))).decode("ascii")
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def decrypt_payload(raw, passphrase):
    check_passphrase(passphrase)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("备份文件为空或超过 8 MiB")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("这不是有效的备份文件") from None
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT_NAME:
        raise ValueError("这不是本应用的备份文件")
    if envelope.get("format_version") != FORMAT_VERSION:
        raise ValueError("备份文件版本不受支持，请使用相同或更新版本的镜像")
    try:
        kdf, cipher = envelope["kdf"], envelope["cipher"]
        iterations = int(kdf["iterations"])
        salt = bytes.fromhex(str(kdf["salt"]))
        nonce = bytes.fromhex(str(cipher["nonce"]))
        ciphertext = base64.b64decode(str(envelope["payload"]), validate=True)
        if kdf.get("name") != "pbkdf2-hmac-sha256" or cipher.get("name") != "aes-256-gcm":
            raise ValueError
        if not 10_000 <= iterations <= 4_000_000 or len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("备份文件已损坏或不完整") from None
    try:
        plain = AESGCM(_key(passphrase, salt, iterations)).decrypt(nonce, ciphertext, _aad(envelope))
    except InvalidTag:
        raise ValueError("备份密码不正确，或文件已损坏") from None
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("备份内容无法解析") from None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError("备份内容缺少账号数据")
    if len(data["accounts"]) > MAX_ACCOUNTS:
        raise ValueError("备份中的账号数量超出限制")
    return data


def build_payload(store, auth, settings, include_password=True, include_history=True, include_cache=True):
    """整理导出内容；统计信息用于界面提示，不包含明文密码。"""
    accounts = store.accounts()
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": __version__,
        "settings": {"timezone": settings.timezone, "task_timeout": settings.task_timeout,
                     "retention_days": settings.retention_days},
        "accounts": accounts,
        "runs": [],
        "logs": {},
        "cache": {},
    }
    stats = {"accounts": len(accounts), "runs": 0, "logs": 0, "cache": 0, "password": False}
    if include_password:
        payload["password"] = auth.export_record()
        stats["password"] = True
    if include_history:
        runs = store.runs(limit=MAX_RUNS)
        payload["runs"] = runs
        stats["runs"] = len(runs)
        total = 0
        for run in runs:
            path = settings.data_dir / "logs" / f"{run['id']}.log"
            try:
                size = path.stat().st_size
                if not path.is_file() or size > MAX_LOG_BYTES or total + size > MAX_LOG_TOTAL_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            payload["logs"][run["id"]] = text
            total += size
        stats["logs"] = len(payload["logs"])
    if include_cache:
        directory = settings.data_dir / "cache"
        if directory.is_dir():
            for path in sorted(directory.glob("jwt-*.json")):
                try:
                    if path.is_file() and path.stat().st_size <= MAX_CACHE_BYTES:
                        payload["cache"][path.name] = path.read_text(encoding="utf-8")
                except OSError:
                    continue
        stats["cache"] = len(payload["cache"])
    payload["counts"] = stats
    return payload, stats


def _account_body(entry):
    if not isinstance(entry, dict):
        raise ValueError("备份中的账号数据格式不正确")
    supplied = entry.get("config")
    if not isinstance(supplied, dict):
        raise ValueError("备份中的账号配置格式不正确")
    config = {}
    for field in CONFIG_DEFAULTS:
        value = supplied.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError(f"备份中的 {field} 字段格式不正确")
        config[field] = value
    options = entry.get("options")
    options = options if isinstance(options, dict) else {}
    for field, value in options.items():
        if field in OPTION_DEFAULTS and not isinstance(value, bool):
            raise ValueError(f"备份中的 {field} 选项格式不正确")
    name = entry.get("name", "")
    body = {
        "name": name if isinstance(name, str) else "",
        "config": config,
        "options": {field: value for field, value in options.items() if field in OPTION_DEFAULTS},
        "enabled": bool(entry.get("enabled")),
        "schedule_time": entry.get("schedule_time", "08:23"),
    }
    if isinstance(entry.get("schedules"), dict):
        # 1.4 起每个任务有独立计划；带上它才能在换服务器后保留周计划等设置。
        body["schedules"] = entry["schedules"]
    return body


def _history_row(run, account_id, account_name):
    run_id = _text(run.get("id"), 64)
    task = _text(run.get("task"), 32)
    created_at = _text(run.get("created_at"), 64)
    if not run_id or task not in ("sign", "wed", "rate", "fee") or not created_at:
        return None
    if run.get("status") not in HISTORY_STATUSES:
        return None
    from .results import clean
    result = clean(run.get("result"))
    return {
        "result": json.dumps(result) if result else None,
        "id": run_id, "account_id": account_id,
        "account_name": _text(run.get("account_name"), 200) or account_name,
        "task": task, "dry_run": 1 if run.get("dry_run") else 0,
        "source": "import", "status": run["status"], "created_at": created_at,
        "started_at": _text(run.get("started_at"), 64),
        "finished_at": _text(run.get("finished_at"), 64),
        "exit_code": run.get("exit_code") if isinstance(run.get("exit_code"), int) else None,
        "message": _text(run.get("message"), 2000) or "",
    }


def apply_payload(store, auth, payload, mode="merge", restore_password=True,
                  import_history=True, timezone_name="Asia/Shanghai"):
    """把备份内容写入当前实例；先整体校验再写入，避免导入到一半失败。"""
    if mode not in ("merge", "replace"):
        raise ValueError("导入方式不正确")
    if mode == "replace" and store.pending_runs():
        raise ValueError("有任务正在排队或执行，请等任务结束后再使用替换导入")
    plan, skipped, seen = [], [], set()
    existing = {account["config"]["phone"]: account for account in store.accounts()}
    reserved = set()
    for entry in payload.get("accounts", []):
        body = _account_body(entry)
        phone = body["config"].get("phone", "")
        old_id = _text(entry.get("id"), 64)
        current = existing.get(phone)
        if current is not None and mode == "merge":
            skipped.append(body["name"] or phone or "未命名账号")
            plan.append((old_id, current, False))
            continue
        if phone in seen:
            skipped.append(body["name"] or phone or "未命名账号")
            continue
        candidate = old_id or uuid.uuid4().hex
        if candidate in reserved or store.account(candidate):
            candidate = uuid.uuid4().hex
        # 备份中的账号按原样校验；id 沿用备份，执行记录的关联关系因此保持不变。
        account = normalize_account(body, None, timezone_name)
        account["id"] = candidate
        for field in ("status_reset_at", "fee_reset_at"):
            raw = entry.get(field)
            try:
                stamp = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
            except ValueError:
                stamp = None
            if stamp and stamp.tzinfo:
                account[field] = stamp.isoformat()
        reserved.add(account["id"])
        seen.add(phone)
        plan.append((old_id, account, True))
    if mode == "replace":
        store.clear()
        existing = {}
    added, id_map = 0, {}
    for old_id, account, created in plan:
        store.save(account)
        added += 1 if created else 0
        existing[account["config"]["phone"]] = account
        id_map[old_id or account["config"]["phone"]] = account["id"]
    by_id = {value["id"]: value for value in existing.values()}
    runs_imported = logs_imported = 0
    if import_history:
        rows = []
        for run in payload.get("runs") or []:
            if not isinstance(run, dict):
                continue
            account_id = id_map.get(_text(run.get("account_id"), 64))
            if not account_id:
                continue
            account = by_id.get(account_id)
            row = _history_row(run, account_id, account["name"] if account else "")
            if row:
                rows.append(row)
        runs_imported = store.add_history(rows)
        kept = {row["id"] for row in rows}
        directory = store.data_dir / "logs"
        for run_id, text in (payload.get("logs") or {}).items():
            if run_id not in kept or not isinstance(text, str) or len(text.encode("utf-8")) > MAX_LOG_BYTES:
                continue
            try:
                (directory / f"{run_id}.log").write_text(text, encoding="utf-8")
            except OSError:
                continue
            logs_imported += 1
    cache_imported = 0
    allowed = {f"jwt-{account_key(phone)}.json" for phone in existing}
    for name, text in (payload.get("cache") or {}).items():
        if not isinstance(name, str) or Path(name).name != name or name not in allowed:
            continue
        if len(name) > CACHE_NAME_LENGTH or not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CACHE_BYTES:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if not isinstance(record, dict) or not record.get("jwt"):
            continue
        try:
            atomic_json(store.data_dir / "cache" / name, record)
        except OSError:
            continue
        cache_imported += 1
    password_restored = False
    if restore_password and isinstance(payload.get("password"), dict):
        auth.restore(payload["password"])
        password_restored = True
    return {
        "mode": mode, "added": added, "skipped": skipped[:20], "skipped_total": len(skipped),
        "runs": runs_imported, "logs": logs_imported, "cache": cache_imported,
        "password_restored": password_restored,
        "source_version": payload.get("app_version", ""), "source_created_at": payload.get("created_at", ""),
    }