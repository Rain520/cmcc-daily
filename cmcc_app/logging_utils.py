import logging
import re
from urllib.parse import quote, quote_plus

from .models import SECRET_FIELDS


def redact(text, config=None):
    values = [str((config or {}).get(key, "")) for key in (*SECRET_FIELDS, "phone")]
    for value in sorted((v for v in values if v), key=len, reverse=True):
        for variant in {value, quote(value, safe=""), quote_plus(value)}:
            text = text.replace(variant, "[已隐藏]")
    text = re.sub(r"(?<![0-9])1[0-9]{10}(?![0-9])", "[手机号已隐藏]", text)
    text = re.sub(r"(?i)([?&](?:token|jwtToken|jtToken|cToken|sid)=)[^&\s'\"<>]+", r"\1[已隐藏]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT已隐藏]", text)
    text = re.sub(r"(?i)((?:JSESSIONID|UID|QWHD_SESSION_TOKEN|HLWHD_SESSION_TOKEN)=)[^;\s]+", r"\1[已隐藏]", text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, config):
        super().__init__("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        self.config = config

    def format(self, record):
        return redact(super().format(record), self.config)
