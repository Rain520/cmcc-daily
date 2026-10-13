import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .auth import DEFAULT_PASSWORD, MIN_PASSWORD_LENGTH


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password: str
    timezone: str = "Asia/Shanghai"
    task_timeout: int = 3600
    retention_days: int = 30
    secure_cookie: bool = False

    @classmethod
    def from_env(cls):
        password = os.environ.get("CMCC_WEB_PASSWORD", "")
        if password_file := os.environ.get("CMCC_WEB_PASSWORD_FILE"):
            password = Path(password_file).read_text(encoding="utf-8").strip()
        # 不设置时使用默认密码；首次启动写入数据卷，之后可在网页里修改。
        password = password or DEFAULT_PASSWORD
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"CMCC_WEB_PASSWORD 至少 {MIN_PASSWORD_LENGTH} 位，或留空使用默认密码 {DEFAULT_PASSWORD}")
        timezone = os.environ.get("TZ", "Asia/Shanghai")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TZ 必须为有效的 IANA 时区，例如 Asia/Shanghai") from None
        timeout = int(os.environ.get("CMCC_TASK_TIMEOUT", "3600"))
        retention = int(os.environ.get("CMCC_RETENTION_DAYS", "30"))
        if not 30 <= timeout <= 86400:
            raise ValueError("CMCC_TASK_TIMEOUT 必须为 30～86400 秒")
        if not 1 <= retention <= 365:
            raise ValueError("CMCC_RETENTION_DAYS 必须为 1～365 天")
        return cls(
            Path(os.environ.get("CMCC_DATA_DIR", "./data")).resolve(),
            password, timezone, timeout, retention,
            os.environ.get("CMCC_SECURE_COOKIE", "false").lower() == "true",
        )
