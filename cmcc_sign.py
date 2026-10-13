#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动 App「签到领流量/话费」自动签到脚本
================================================

依据抓包分析的接口链路（wx.10086.cn / qwhdhub mark31 活动）：

  1. GET  /qwhdsso/login?actUrl=<活动页>        取得内嵌一次性 sid 的登录中转页
  2. POST /qwhdsso/appTokenLogin?sid=...        用 App 票据换取活动页跳转 URL
  3. GET  <活动页?token=QWHDSSOD...>            服务器 Set-Cookie: QWHD_SESSION_TOKEN (30分钟)
  4. POST /qwhdhub/api/mark/mark31/markstatus   查询签到状态（幂等保护）
  5. POST /qwhdhub/api/mark/mark31/domark       执行签到 {"date":"YYYYMMDD"}
  6. POST /qwhdhub/api/mark/mark31/taskAward/<id>  领取累计/连签奖励（默认开启，--no-claim 跳过）

配置来源（优先级：环境变量 > config.json > 默认值）：
  CMCC_APP_TOKEN     App 原生票据，形如 "JSESSIONID=...; UID=...; ticketID=NingBo"（需抓包获取）
  CMCC_PHONE         手机号（用于生成 userCheckId = 手机号十六进制）
  CMCC_PROVINCE_CODE 省编码，如 731（湖南）
  CMCC_CITY_CODE     市编码，如 0731
  CMCC_ACTIVITY_ID   活动 ID，默认 1021122301
  CMCC_CHANNEL_ID    渠道 ID，默认 P00000109876
  CMCC_SERVERCHAN_SENDKEY / CMCC_BARK_URL  通知渠道（可选）

用法：
  python3 cmcc_sign.py --config config.json            # 常规签到（自动领累计/连签奖励）
  python3 cmcc_sign.py --dry-run                       # 只查状态，同时报「可领未领」奖励
  python3 cmcc_sign.py --no-claim                      # 只签到，不领奖
  python3 cmcc_sign.py --delay 600                     # 启动前随机延迟 0~600 秒（防风控）

退出码：0 = 签到成功或今日已签；1 = 失败（便于 CI 判断）
"""

import argparse
import ipaddress
import json
import logging
import os
import random
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from cmcc_app.storage import account_key, atomic_json

# ---------------------------------------------------------------- 常量

BASE = "https://wx.10086.cn"
SSO_LOGIN = BASE + "/qwhdsso/login"
API_MARK = BASE + "/qwhdhub/api/mark"

# 与抓包完全一致的 App WebView UA（服务端校验 leadeon 标识）
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/wkwebview "
    "leadeon/12.5.2/CMCCIT"
)

# 常见内网/元数据主机名（不依赖 DNS 解析即可识别）
LOCAL_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost",
    "metadata.google.internal", "instance-data",
}

log = logging.getLogger("cmcc_sign")


class LegacyTLSAdapter(HTTPAdapter):
    """wx.10086.cn 前端网关只接受老式 TLS 套件（如 ECDHE-RSA-AES128-SHA），
    Python 默认的现代 GCM 套件会在握手阶段被拒（handshake failure）。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def assert_safe_url(url: str, allowed_hosts: set[str] | None = None):
    """动态 URL 出网前的 SSRF 边界校验：

    - 仅允许 https；
    - IP 直填必须为公网地址（阻断环回/私网/链路本地/云元数据等）；
    - 内网/元数据主机名直接拒绝；
    - 传入 allowed_hosts 时执行域名白名单。

    注：不做 DNS 解析结果的 IP 校验——本机开启 TUN 代理（fake-ip 网段
    198.18.0.0/15）时所有域名都解析为非公网地址，该检查会全量误杀；
    域名被劫持的场景由 https 证书验证兜底。
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"仅允许 https 协议: {url!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"URL 缺少主机名: {url!r}")
    if host in LOCAL_HOSTNAMES:
        raise ValueError(f"拒绝访问内网主机名: {host}")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"主机 {host!r} 不在白名单 {sorted(allowed_hosts)} 内")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名形式，交由 https 证书校验兜底
    if not ip.is_global:
        raise ValueError(f"拒绝访问非公网地址: {ip}")


# ---------------------------------------------------------------- 配置

def validate_webhook(url: str, token: str = ""):
    """Validate notification settings without including credentials in errors."""
    try:
        assert_safe_url(url)
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError()
        if parts.port == 0 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError()
    except ValueError:
        raise ValueError("Webhook 地址必须是有效的公网 HTTPS 地址，不含用户名、密码或片段") from None
    if token and not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise ValueError("Webhook Bearer Token 格式不正确，只填写令牌本身")


class Config:
    def __init__(self, path: str | None):
        file_cfg = {}
        if path and Path(path).exists():
            file_cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if not isinstance(file_cfg, dict):
                raise ValueError("账号配置必须为 JSON 对象")
        env = os.environ

        self.app_token = env.get("CMCC_APP_TOKEN", file_cfg.get("app_token", ""))
        self.phone = str(env.get("CMCC_PHONE", file_cfg.get("phone", "")))
        self.province_code = env.get("CMCC_PROVINCE_CODE", file_cfg.get("province_code", "731"))
        self.city_code = env.get("CMCC_CITY_CODE", file_cfg.get("city_code", "0731"))
        self.carrier_operator = env.get("CMCC_CARRIER_OPERATOR", file_cfg.get("carrier_operator", "002"))
        self.app_version_code = env.get("CMCC_APP_VERSION_CODE", file_cfg.get("app_version_code", "12.5.2"))
        self.activity_id = env.get("CMCC_ACTIVITY_ID", file_cfg.get("activity_id", "1021122301"))
        self.channel_id = env.get("CMCC_CHANNEL_ID", file_cfg.get("channel_id", "P00000109876"))
        self.serverchan_sendkey = env.get("CMCC_SERVERCHAN_SENDKEY", file_cfg.get("serverchan_sendkey", ""))
        self.bark_url = env.get("CMCC_BARK_URL", file_cfg.get("bark_url", ""))
        self.webhook_url = env.get("CMCC_WEBHOOK_URL", file_cfg.get("webhook_url", ""))
        self.webhook_token = env.get("CMCC_WEBHOOK_TOKEN", file_cfg.get("webhook_token", ""))
        self.fee_session_cookie = env.get("CMCC_FEE_SESSION", file_cfg.get("fee_session_cookie", ""))
        self.fee_prov_code = env.get("CMCC_FEE_PROV", file_cfg.get("fee_prov_code") or self.province_code)

    @property
    def act_url(self) -> str:
        return f"{BASE}/qwhdhub/qwhdmark/{self.activity_id}?channelId={self.channel_id}"

    def validate(self, require_token: bool = True) -> str | None:
        if require_token and not self.app_token:
            return "缺少 app_token（CMCC_APP_TOKEN 或 config.json）"
        if not self.phone or not re.fullmatch(r"1\d{10}", self.phone):
            return "缺少合法手机号（CMCC_PHONE 或 config.json）"
        return None


# ---------------------------------------------------------------- 会话建立

def api_headers(referer: str) -> dict:
    """活动 API 的固定头（抓包还原，缺一可能被拦）。"""
    return {
        "accept": "*/*",
        "content-type": "application/json;charset=UTF-8",
        "origin": BASE,
        "referer": referer,
        "login-check": "1",
        "x-requested-with": "XMLHttpRequest",
    }


def jwt_cache_file(cfg: Config) -> Path:
    """持久化目录按完整手机号散列隔离，同尾号账号不会互相覆盖。"""
    return Path(os.environ.get("CMCC_DATA_DIR", "./data")) / "cache" / f"jwt-{account_key(cfg.phone)}.json"


def load_jwt_cache(cfg: Config) -> tuple[str, float] | None:
    """返回 (jwt, 凭证链起点 first_seen)，无可用缓存时返回 None。"""
    try:
        path = jwt_cache_file(cfg)
        if not path.exists():
            # 兼容原脚本的缓存，仍校验完整手机号后才采用。
            path = Path(__file__).with_name(f".cmcc_jwt_cache_{cfg.phone[-4:]}.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        # 缓存记录签发时的手机号，与当前配置不一致则视为串号，弃用
        if data.get("phone") != cfg.phone:
            return None
        if time.time() - data.get("saved_at", 0) < 30 * 86400:  # 至少 30 天内的缓存值得尝试
            jwt = data.get("jwt")
            if jwt:
                return jwt, data.get("first_seen") or data.get("saved_at", time.time())
    except Exception:
        pass
    return None


def save_jwt_cache(cfg: Config, jwt: str | None):
    """回写最新 jwt。first_seen 记录凭证链起点（首次引导时间）并跨重签保持，
    用于在 jwt 最终失效时计算整条凭证链的实际存活时长。"""
    if not jwt:
        return
    try:
        path = jwt_cache_file(cfg)
        first_seen = time.time()
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("phone") == cfg.phone and old.get("first_seen"):
                first_seen = old["first_seen"]
        except Exception:
            pass
        atomic_json(path, {"jwt": jwt, "phone": cfg.phone,
                           "first_seen": first_seen, "saved_at": time.time()})
    except OSError:
        log.warning("JWT 缓存保存失败，请检查 CMCC_DATA_DIR 的目录权限")


def exchange_session(cfg: Config, act_url: str | None = None) -> tuple[requests.Session, str]:
    """走 SSO 换取活动会话（QWHD_SESSION_TOKEN 落在 session cookie 里）。

    act_url 缺省用主签到活动页；其他活动（如 hlwyxhdhub 的周三充值日）传入
    各自的页面地址即可——同一 jwt 跨 hub 通用，只是不同 hub 落不同令牌名。

    返回 (session, referer)。referer 是带 token 的活动页地址，后续 API 都要带。

    凭证策略（实测结论）：
    - jwt 是账号级长期凭证：appTokenLogin 请求里 jwtToken 优先于 token 字段，
      服务端校验通过 jwt 即签发会话（使用 jwt 时 token 字段可留空），
      且不受 App 内切换登录影响；
    - 因此优先用缓存 jwt 免票据续期，app_token 仅在 jwt 缺失/失效时作引导兜底；
    - 每次登录响应都会重签 jwt，总是回写缓存保持最新。
    """
    s = requests.Session()
    # 直连：不继承系统/环境变量代理，避免本机抓包工具的 MITM 证书干扰 SSL 验证
    s.trust_env = False
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({"user-agent": USER_AGENT, "accept-language": "zh-CN,zh-Hans;q=0.9"})

    # ① 登录中转页，提取一次性 sid
    r = s.get(SSO_LOGIN, params={"dlwmh": "true", "actUrl": act_url or cfg.act_url}, timeout=30)
    r.raise_for_status()
    m_app = re.search(r"loginPath\s*=\s*'([^']+)'", r.text)
    if not m_app:
        raise RuntimeError("登录页未返回 sid，SSO 入口可能已变更")
    login_url = BASE + "/qwhdsso" + m_app.group(1)

    base_body = {
        "provinceCode": cfg.province_code,
        "cityCode": cfg.city_code,
        "userCheckId": format(int(cfg.phone), "x"),  # 手机号转十六进制（前端 parseFloat().toString(16) 的等价实现）
        "carrierOperator": cfg.carrier_operator,
        "appVersionCode": cfg.app_version_code,
        "took": random.randint(120, 900),
    }

    # ② 优先用缓存 jwt 免票据续期
    resp = None
    cached = load_jwt_cache(cfg)
    if cached:
        jwt_str, jwt_first_seen = cached
        r = s.post(login_url, json={**base_body, "jwtToken": jwt_str, "token": ""}, timeout=30)
        try:
            cand = r.json()
        except ValueError:
            cand = {}
        if cand.get("code") == "SUCCESS":
            resp = cand
            log.info("jwt 续期成功（未使用 app_token，凭证链已 %.1f 天）",
                     (time.time() - jwt_first_seen) / 86400)
        else:
            log.info("jwt 续期失败(%s)，回落 appTokenLogin 引导", cand.get("msg"))

    # ③ 回落：app_token 引导登录（首次配置或 jwt 失效后）
    if resp is None:
        r = s.post(login_url, json={**base_body, "jwtToken": None, "token": cfg.app_token}, timeout=30)
        resp = r.json()
        if resp.get("code") != "SUCCESS":
            # App 票据失效是脚本唯一的"需要人工介入"场景
            raise RuntimeError(
                f"appTokenLogin 失败: {resp.get('code')} {resp.get('msg')} —— "
                "通常是 app_token 过期，请重新抓包更新凭证"
            )

    data = resp["data"]
    save_jwt_cache(cfg, data.get("jwt"))

    # ③ 访问带 token 的活动页，服务器 Set-Cookie: QWHD_SESSION_TOKEN
    # URL 来自服务端响应，按 SSRF 防护约束在 wx.10086.cn 域内，且不跟随重定向
    assert_safe_url(data["url"], allowed_hosts={"wx.10086.cn"})
    r = s.get(data["url"], timeout=30, allow_redirects=False)
    r.raise_for_status()
    # 不同 hub 落不同令牌名（qwhdhub → QWHD_SESSION_TOKEN，hlwyxhdhub → HLWHD_SESSION_TOKEN），
    # 同一 jwt 跨 hub 通用，故按后缀匹配
    if not any(n.endswith("SESSION_TOKEN") for n in s.cookies.keys()):
        raise RuntimeError("未取得活动会话令牌，会话兑换失败")
    token_name = next(n for n in s.cookies.keys() if n.endswith("SESSION_TOKEN"))
    log.info("活动会话建立成功（%s）", token_name)
    return s, data["url"]


# ---------------------------------------------------------------- 业务接口

def query_markstatus(s: requests.Session, referer: str) -> dict:
    r = s.post(f"{API_MARK}/mark31/markstatus", json={}, headers=api_headers(referer), timeout=30)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") != "SUCCESS":
        raise PermissionError(f"markstatus 失败: {resp.get('code')} {resp.get('msg')}")
    return resp["data"]


def do_mark(s: requests.Session, referer: str, date: str) -> dict:
    r = s.post(f"{API_MARK}/mark31/domark", json={"date": date}, headers=api_headers(referer), timeout=30)
    r.raise_for_status()
    return r.json()


def claim_task_awards(s: requests.Session, referer: str, status_data: dict) -> list[str]:
    """领取 taskAwardChance 里的累计/连签奖励（热门奖品库存紧张，领不到属正常）。

    服务端把"当前可领"的奖励算好放在 taskAwardChance（App 打开签到页弹窗领的就是
    同一批），领取后条目即从清单消失，重复运行天然幂等。领取成功时奖品名在响应
    data.prizeName 里（taskAwardChance 条目自身 prize=null，任务池里也未必查得到名
    字），仅在拿不到 prizeName 时才回退用任务池里的名称展示。
    """
    # 任务ID -> 奖品名（taskAwardChance 条目自身或 accumulateTaskInfo/myTaskInfo 中同名任务；
    # 各池信息详略不一，只有取到非空名称才登记，避免空值抢先占位）
    names = {}
    pools = [status_data.get("taskAwardChance") or [],
             status_data.get("accumulateTaskInfo") or [],
             status_data.get("myTaskInfo") or []]
    for pool in pools:
        for t in pool:
            tid = t.get("id")
            if not tid or names.get(tid):
                continue
            name = (t.get("prize") or {}).get("name") or t.get("lotteryText") or t.get("prizeAlertText") or ""
            if name:
                names[tid] = name

    def label(task: dict) -> str:
        tid = task.get("id") or "?"
        bits = []
        if task.get("taskType") == "accumulate" and task.get("num"):
            bits.append(f"累计{task['num']}签")
        if names.get(tid):
            bits.append(names[tid])
        return f"任务{tid}" + (f"（{'·'.join(bits)}）" if bits else "")

    results = []
    for task in status_data.get("taskAwardChance") or []:
        tid = task.get("id")
        if not tid:
            continue
        r = s.post(f"{API_MARK}/mark31/taskAward/{tid}", json={}, headers=api_headers(referer), timeout=30)
        resp = r.json()
        data = resp.get("data") or {}
        # status 为 None 时回落到 code（实测领奖成功响应 status 可能为空）；
        # 成功领取时 data 带 prizeName/prizeValue，直接展示领到了什么
        if isinstance(data, dict) and data.get("prizeName"):
            value = data.get("prizeValue")
            unit = {"FLOW": "MB", "FEE": "元"}.get(data.get("prizeCategory"), "")
            amount = f"{value}{unit}" if value else ""
            results.append(f"{label(task)} 领到: {data['prizeName']}" + (f"（{amount}）" if amount else ""))
        else:
            status_text = resp.get("status") or resp.get("code") or "?"
            results.append(f"{label(task)}: {status_text} {resp.get('msg')}")
        time.sleep(random.uniform(1, 2))
    return results


def parse_prize(mark_result: dict) -> str:
    """从 domark 响应里提取奖品描述。"""
    prize = (mark_result.get("data") or {}).get("markPrize")
    if not prize:
        return ""
    parts = [prize.get("name") or ""]
    if prize.get("prizeValue"):
        parts.append(f"{prize['prizeValue']}{'MB' if prize.get('prizeCategory') == 'FLOW' else '元'}")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- 通知

def _truncate(detail: str, limit: int = 3800) -> str:
    return detail if len(detail) <= limit else detail[:limit] + "\n…（内容过长已截断）"


def _truncate_bytes(text: str, limit: int, note: str = "…（内容过长已截断）") -> str:
    """按 UTF-8 字节数截断：机器人接口按字节限制长度，中文一个字占三字节。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    budget = max(limit - len(note.encode("utf-8")), 0)
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + note


def webhook_request(url: str, title: str, detail: str) -> tuple[str, dict]:
    """按接收端类型生成请求体。

    企业微信、钉钉、飞书机器人只接受各自的消息结构，收到别的格式时同样返回
    HTTP 200，但正文里带非 0 的 errcode，因此这里必须先按类型组装。
    返回 (类型, requests 关键字参数)。
    """
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path or ""
    content = f"{title}\n\n{detail}".strip() if title else detail
    if host == "qyapi.weixin.qq.com" and path.startswith("/cgi-bin/webhook/send"):
        return "wecom", {"json": {"msgtype": "text", "text": {"content": _truncate_bytes(content, 2000)}}}
    if host == "oapi.dingtalk.com" and path.startswith("/robot/send"):
        return "dingtalk", {"json": {"msgtype": "text", "text": {"content": _truncate_bytes(content, 2000)}}}
    if host.endswith("feishu.cn") and "/bot/" in path:
        return "feishu", {"json": {"msg_type": "text", "content": {"text": _truncate_bytes(content, 2000)}}}
    return "generic", {"json": {"title": title, "body": detail}}


WEBHOOK_LABELS = {"wecom": "企业微信 Webhook", "dingtalk": "钉钉 Webhook",
                  "feishu": "飞书 Webhook", "generic": "Webhook"}


def _check_response(response, status: int):
    """HTTP 2xx 之外还要看业务错误码：机器人接口失败时也返回 200。"""
    try:
        payload = response.json()
    except ValueError:
        return True, f"HTTP {status}"
    if not isinstance(payload, dict):
        return True, f"HTTP {status}"
    code = payload.get("errcode", payload.get("code"))
    if code is None:
        return True, f"HTTP {status}"
    if str(code) == "0":
        return True, f"HTTP {status}（errcode 0）"
    message = str(payload.get("errmsg") or payload.get("msg") or "").strip().replace("\n", " ")[:100]
    return False, f"HTTP {status}，业务错误 errcode {code}：{message}" if message else f"HTTP {status}，业务错误 errcode {code}"


def _deliver(url: str, timeout: int, **kwargs):
    """单独发送一个渠道，返回 (是否成功, 描述)；描述里不含 URL、响应正文或密钥。"""
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(url, timeout=timeout, allow_redirects=False, **kwargs)
        if 200 <= response.status_code < 300:
            return _check_response(response, response.status_code)
        return False, f"服务端返回 HTTP {response.status_code}"
    except requests.Timeout:
        return False, f"请求超时（{timeout} 秒）"
    except requests.RequestException as exc:
        # 异常消息可能包含推送密钥，只保留类型名。
        return False, f"请求失败（{type(exc).__name__}）"
    except Exception as exc:
        return False, f"发送异常（{type(exc).__name__}）"
    finally:
        session.close()


def notification_channels(cfg: Config):
    """返回已配置的通知渠道：[(名称, 发送函数(title, detail, timeout) -> (bool, str))]。"""
    channels = []
    sendkey = cfg.serverchan_sendkey
    if sendkey:
        if not re.fullmatch(r"[0-9A-Za-z]+", sendkey):
            channels.append(("Server酱", lambda title, detail, timeout: (False, "SendKey 含非法字符，未发送")))
        else:
            channels.append(("Server酱", lambda title, detail, timeout, sendkey=sendkey: _deliver(
                f"https://sctapi.ftqq.com/{sendkey}.send", timeout,
                data={"title": title, "desp": detail})))
    bark_url = cfg.bark_url
    if bark_url:
        try:
            assert_safe_url(bark_url)
        except ValueError:
            channels.append(("Bark", lambda title, detail, timeout: (False, "地址校验失败，仅允许公网 HTTPS")))
        else:
            # 用 POST JSON 发送：长内容走 GET 会全拼在 URL 上，触发 431 并静默失败。
            channels.append(("Bark", lambda title, detail, timeout, url=bark_url: _deliver(
                url, timeout, json={"title": title, "body": _truncate(detail)})))
    hook_url = cfg.webhook_url
    if hook_url:
        try:
            validate_webhook(hook_url, cfg.webhook_token)
        except ValueError as exc:
            message = str(exc)
            channels.append(("Webhook", lambda title, detail, timeout: (False, message)))
        else:
            hook_headers = {"Authorization": f"Bearer {cfg.webhook_token}"} if cfg.webhook_token else {}
            label = WEBHOOK_LABELS[webhook_request(hook_url, "", "")[0]]
            channels.append((label, lambda title, detail, timeout, url=hook_url, headers=hook_headers: _deliver(
                url, timeout, headers=headers, **webhook_request(url, title, detail)[1])))
    return channels


def notify_channels(cfg: Config, title: str, detail: str, timeout: int = 15):
    """并发发送所有渠道，返回 [{"channel", "ok", "message"}]；单个渠道失败不影响其他渠道。"""
    channels = notification_channels(cfg)
    if not channels:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=len(channels)) as pool:
        pending = [(name, pool.submit(send, title, detail, timeout)) for name, send in channels]
        for name, future in pending:
            try:
                ok, message = future.result()
            except Exception as exc:
                ok, message = False, f"发送异常（{type(exc).__name__}）"
            results.append({"channel": name, "ok": ok, "message": message})
    return results


def notify(cfg: Config, title: str, detail: str):
    for result in notify_channels(cfg, title, detail):
        if result["ok"]:
            log.info("[通知] %s 发送成功 (%s)", result["channel"], result["message"])
        else:
            log.warning("[通知] %s 发送失败：%s", result["channel"], result["message"])


# ---------------------------------------------------------------- 主流程

def run(cfg: Config, dry_run: bool, claim: bool = True, tasks: bool = False,
        games: bool = False, wed: bool = False) -> int:
    today = datetime.now().strftime("%Y%m%d")

    # 重建会话的兜底：第一次失败若是会话问题，重建后再试一次
    for attempt in (1, 2):
        try:
            s, referer = exchange_session(cfg)
            status_data = query_markstatus(s, referer)
            break
        except PermissionError as e:
            if attempt == 2:
                raise
            log.warning("会话异常(%s)，重建后重试", e)

    userinfo = status_data.get("userinfo") or {}
    acc = userinfo.get("accumulateTimes", "?")
    signed_today = any(
        d.get("date") == today and d.get("status") == "1"
        for d in status_data.get("markstatus") or []
    )
    log.info("当前累计签到 %s 天，今日%s", acc, "已签" if signed_today else "未签")

    lines = []
    if signed_today:
        lines.append(f"今日已签到（累计 {acc} 天），无需操作")
    elif dry_run:
        lines.append(f"[dry-run] 今日未签，累计 {acc} 天")
    else:
        result = do_mark(s, referer, today)
        code, msg, status = result.get("code"), result.get("msg", ""), result.get("status", "")
        log.info("domark 响应: code=%s status=%s msg=%s", code, status, msg)
        # HAVE_MARKED 是服务端幂等保护（实测重复签到返回该码），视为已签成功
        if code == "SUCCESS" or "已签" in msg or status == "HAVE_MARKED":
            prize = parse_prize(result)
            # 重新查询拿最新累计天数
            try:
                new_data = query_markstatus(s, referer)
                acc = (new_data.get("userinfo") or {}).get("accumulateTimes", acc)
            except Exception:
                pass
            if status == "HAVE_MARKED":
                lines.append(f"今日已签到过（服务端幂等），累计 {acc} 天")
            else:
                lines.append(f"签到成功！累计 {acc} 天")
                if prize:
                    lines.append(f"获得奖品: {prize}")
                elif status == "PRIZE_NO_CONFIG":
                    lines.append("今日无单日奖品（累计/连签奖励见下方领奖结果）")
        else:
            detail = f"签到失败: {code} / {status} / {msg}"
            notify(cfg, "移动签到失败", f"{detail}\n手机尾号 {cfg.phone[-4:]}")
            log.error(detail)
            return 1

    # 领奖：门槛达标的累计/连签奖励不会自动发放（App 是打开签到页弹窗时领），
    # 这里默认代领。dry-run 下只报「可领未领」，不做任何写操作。
    if claim:
        try:
            latest = query_markstatus(s, referer)
            chances = latest.get("taskAwardChance") or []
            if dry_run:
                for t in chances:
                    lines.append(f"[领奖] [dry-run] 发现可领未领奖励: 任务{t.get('id')}"
                                 f"（累计{t.get('num')}签，正式运行将自动领取）")
                if not chances:
                    lines.append("[领奖] 当前无可领取的累计/连签奖励")
            elif not chances:
                log.info("[领奖] 当前无可领取的累计/连签奖励")
            else:
                for line in claim_task_awards(s, referer, latest):
                    log.info("[领奖] %s", line)
                    lines.append(f"[领奖] {line}")
        except Exception as e:
            lines.append(f"[领奖] 尝试失败: {e}")

    # 拓展活动（可选模块 cmcc_extra.py，缺失时静默跳过）
    if tasks or games:
        try:
            import cmcc_extra
        except ImportError:
            lines.append("[拓展] 未找到 cmcc_extra.py，已跳过")
        else:
            if tasks:
                try:
                    line = cmcc_extra.run_mark_tasks(s, referer, cfg, dry_run)
                    log.info("[AI豆任务] %s", line)
                    lines.append(f"[AI豆任务] {line}")
                except Exception as e:
                    lines.append(f"[AI豆任务] 失败: {e}")
            if games:
                try:
                    for line in cmcc_extra.run_games(cfg, dry_run):
                        log.info("[拓展活动] %s", line)
                        lines.append(f"[拓展] {line}")
                except Exception as e:
                    lines.append(f"[拓展活动] 失败: {e}")

    # 周三充值日拼图抽奖（可选模块 cmcc_wed.py，缺失时静默跳过）
    if wed:
        try:
            import cmcc_wed
        except ImportError:
            lines.append("[拼图] 未找到 cmcc_wed.py，已跳过")
        else:
            try:
                for line in cmcc_wed.run(cfg, dry_run):
                    log.info("[周三拼图] %s", line)
                    lines.append(f"[拼图] {line}")
            except Exception as e:
                lines.append(f"[拼图] 失败: {e}")

    summary = "\n".join(lines)
    log.info("\n%s", summary)
    notify(cfg, "移动签到通知", f"{summary}\n手机尾号 {cfg.phone[-4:]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动 App 签到自动脚本")
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    parser.add_argument("--dry-run", action="store_true", help="只查询状态，不执行签到/领奖")
    parser.add_argument("--claim", dest="claim", action="store_true", default=True,
                        help="签到后自动领取累计/连签奖励（默认开启）")
    parser.add_argument("--no-claim", dest="claim", action="store_false", help="跳过领奖，只签到")
    parser.add_argument("--tasks", action="store_true",
                        help="顺带完成签到页 AI豆任务（需 cmcc_extra.py）")
    parser.add_argument("--games", action="store_true",
                        help="跑拓展活动：周六游戏中心/追剧/抽话费页的打卡+任务+抽奖（需 cmcc_extra.py）")
    parser.add_argument("--wed", action="store_true",
                        help="顺带跑周三充值日拼图抽奖（需 cmcc_wed.py）")
    parser.add_argument("--delay", type=int, default=0, metavar="N", help="启动前随机延迟 0~N 秒")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.delay > 0:
        wait = random.uniform(0, args.delay)
        log.info("随机延迟 %.0f 秒后开始...", wait)
        time.sleep(wait)

    cfg = Config(args.config)
    if err := cfg.validate():
        log.error("%s\n参考 config.example.json 填写，或设置对应环境变量", err)
        return 1

    try:
        return run(cfg, dry_run=args.dry_run, claim=args.claim,
                   tasks=args.tasks, games=args.games, wed=args.wed)
    except Exception as e:
        log.exception("执行失败: %s", e)
        notify(cfg, "移动签到异常", f"{e}\n手机尾号 {cfg.phone[-4:] if cfg.phone else '????'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
