"""管理密码存储。

密码以 PBKDF2 加盐散列保存在数据卷的 auth.json，明文不落盘。
首次启动使用环境变量（或默认密码）初始化，之后可以在网页里修改，
重新启动容器不会覆盖网页中保存的密码。
"""

import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_json

DEFAULT_PASSWORD = "123456"
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128
ITERATIONS = 240000


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def check_password(password):
    if not isinstance(password, str):
        raise ValueError("管理密码必须为文本")
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"管理密码长度需为 {MIN_PASSWORD_LENGTH}～{MAX_PASSWORD_LENGTH} 位")
    if any(ord(character) < 32 for character in password):
        raise ValueError("管理密码不能包含换行或控制字符")
    return password


class AuthStore:
    """读取、校验并更新管理密码；文件损坏时明确报错，不静默重置。"""

    def __init__(self, data_dir, seed_password, reset=False):
        self.path = Path(data_dir) / "auth.json"
        self.lock = threading.Lock()
        self.record = self._load()
        if self.record is None or reset:
            self.set_password(seed_password)

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            salt, digest = str(raw["salt"]), str(raw["hash"])
            iterations = int(raw["iterations"])
            bytes.fromhex(salt)
            bytes.fromhex(digest)
            if not 10000 <= iterations <= 5000000 or len(digest) != 64:
                raise ValueError
            return {"version": 1, "salt": salt, "hash": digest, "iterations": iterations}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(
                "数据卷中的 auth.json 已损坏：请恢复备份，或设置 CMCC_PASSWORD_RESET=true 后重启以重置管理密码"
            ) from None

    @staticmethod
    def _digest(password, salt, iterations):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)

    def verify(self, password):
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
            return False
        with self.lock:
            record = dict(self.record)
        digest = self._digest(password, record["salt"], record["iterations"])
        return hmac.compare_digest(digest.hex(), record["hash"])

    def set_password(self, password):
        check_password(password)
        record = {"version": 1, "salt": secrets.token_hex(16), "iterations": ITERATIONS,
                  "changed_at": utcnow()}
        record["hash"] = self._digest(password, record["salt"], record["iterations"]).hex()
        with self.lock:
            atomic_json(self.path, record)
            self.record = record

    def export_record(self):
        """导出散列记录供备份使用；不含明文密码。"""
        with self.lock:
            return dict(self.record)

    def restore(self, record):
        """按备份中的散列恢复管理密码，导入后可继续使用原密码登录。"""
        if not isinstance(record, dict):
            raise ValueError("备份中的管理密码记录格式不正确")
        try:
            salt, digest = str(record["salt"]), str(record["hash"])
            iterations = int(record["iterations"])
            bytes.fromhex(salt)
            bytes.fromhex(digest)
            if not 10000 <= iterations <= 5000000 or len(digest) != 64 or len(salt) < 16:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("备份中的管理密码记录已损坏，无法恢复") from None
        restored = {"version": 1, "salt": salt, "hash": digest, "iterations": iterations,
                    "changed_at": record.get("changed_at") or utcnow()}
        with self.lock:
            atomic_json(self.path, restored)
            self.record = {"version": 1, "salt": salt, "hash": digest, "iterations": iterations}

    @property
    def fingerprint(self):
        """用于派生会话签名材料；密码变化后取值随之变化。"""
        with self.lock:
            return f"{self.record['salt']}:{self.record['hash']}"

    @property
    def is_default(self):
        return self.verify(DEFAULT_PASSWORD)
