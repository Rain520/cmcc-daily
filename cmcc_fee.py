#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动话费余额查询脚本（touch.10086.cn 充值页 H5 通道）
========================================================

查询话费并推送：realBalanceFee=话费余额、realFee=实时话费。

加密体系（2026-10-02 从充值页 JS 分析获得，密钥嵌在前端 inner.js）：
  AES-128-CBC，key = iv = "043AOQGK6ykklyZA"（qenP/penP/ivP 字节码数组拼合）
  - 手机号 → AES 加密 → base64 → base64 + 手机号第 6-7 位 → URL 路径 + payphoneno 头
  - 响应 data.outParam → base64 → base64 → AES 解密 → JSON

会话（本脚本的关键限制，半自动方案）：
  服务端要求已绑定账号的 jsessionid-cmcc 会话，该会话由 App 原生侧签发
  （H5 未登录时 showLogin 走 JS bridge 调原生登录；qwhdsso 的 actUrl 白名单
  也拒绝 touch.10086.cn，实测返回「非法活动地址」），纯 HTTP 无法自助建立。
  因此流程为：手机保持代理 → App 打开一次「充值页」→ 从 Proxyman 里
  任意一条 touch.10086.cn 请求的 Cookie 复制 jsessionid-cmcc 值 → 填入
  配置的 fee_session_cookie。会话失效（脚本报 500003）后重抓一次即可。

用法：
  python3 cmcc_fee.py --config config2.json    # 查询并推送话费余额

配置：config.json 增加 "fee_session_cookie": "<jsessionid-cmcc 的值>"
      （或环境变量 CMCC_FEE_SESSION）
      可选 "fee_prov_code"（或环境变量 CMCC_FEE_PROV）：省编码，
      默认 731（湖南），其他省份用户建议显式填写

退出码：0 = 查询成功；1 = 失败（便于 cron 判断）
"""

import argparse
import base64
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, str(Path(__file__).parent))
from cmcc_sign import Config, LegacyTLSAdapter, notify  # noqa: E402

log = logging.getLogger("cmcc_fee")

FEE_BASE = "https://touch.10086.cn"
KEY = b"043AOQGK6ykklyZA"  # qenP/penP/ivP，P 体系 IV 即 key
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/wkwebview "
      "leadeon/12.5.2/CMCCIT")


def aes_enc(text: str) -> str:
    """AES-128-CBC(key=iv=KEY) + PKCS7 → base64 → base64（页面 encryptByAES 的等价实现）。"""
    pad = 16 - len(text.encode()) % 16
    data = text.encode() + bytes([pad]) * pad
    e = Cipher(algorithms.AES(KEY), modes.CBC(KEY)).encryptor()
    b1 = base64.b64encode(e.update(data) + e.finalize()).decode()
    return base64.b64encode(b1.encode()).decode()


def aes_dec(nested_b64: str) -> dict:
    """页面 decryptByAES 的等价实现：outParam → 双层 base64 → AES 解密 → JSON。"""
    ct = base64.b64decode(base64.b64decode(nested_b64))
    d = Cipher(algorithms.AES(KEY), modes.CBC(KEY)).decryptor()
    p = d.update(ct) + d.finalize()
    return json.loads(p[:-p[-1]])


def t16() -> str:
    """页面 time 参数：YYYYM?DHHMMSSmmm（日/月不补零的变体，仅作缓存穿透）。"""
    n = datetime.now()
    return (f"{n.year}{n.month}{n.day}"
            f"{n.hour:02d}{n.minute:02d}{n.second:02d}{n.microsecond // 1000:03d}")


def query_fee(phone: str, session_cookie: str, provcode: str = "731") -> dict:
    """查询实时话费，返回解密后的 JSON。"""
    s = requests.Session()
    s.trust_env = False  # 直连，不继承本机代理设置
    s.mount("https://", LegacyTLSAdapter())
    s.cookies.set("jsessionid-cmcc", session_cookie, domain="10086.cn")

    token = aes_enc(phone)
    headers = {
        "user-agent": UA,
        "payphoneno": token + phone[5:7],  # 页面构造规则：token + 手机号第 6-7 位
        "provcode": provcode,
        "referer": f"{FEE_BASE}/i/reapp/v2.0/pages/recharge/recharge.html",
        "accept": "*/*",
    }
    r = s.get(f"{FEE_BASE}/i/v1/fee/real/{token}",
              params={"time": t16(), "channel": "11"}, headers=headers, timeout=15)
    body = r.json()
    if body.get("retCode") != "000000":
        raise RuntimeError(f"接口返回 {body.get('retCode')}: {body.get('retMsg')}"
                           + ("（会话失效，请重新抓 jsessionid-cmcc）"
                              if body.get("retCode") == "500003" else ""))
    return aes_dec(body["data"]["outParam"])


def run(config_path: str) -> int:
    cfg = Config(config_path)
    if err := cfg.validate(require_token=False):
        log.error("%s\n参考 config.example.json 填写，或设置对应环境变量", err)
        return 1
    cookie = cfg.fee_session_cookie
    if not cookie:
        log.error("配置缺少 fee_session_cookie（手机打开充值页后从代理抓包的 "
                  "jsessionid-cmcc 值，或环境变量 CMCC_FEE_SESSION）")
        return 1
    provcode = str(cfg.fee_prov_code or "731")

    fee = query_fee(cfg.phone, cookie, provcode)
    lines = [
        f"话费余额: {fee.get('realBalanceFee', '?')} 元",
        f"实时话费: {fee.get('realFee', '?')} 元",
    ]
    summary = "\n".join(lines)
    log.info("\n%s", summary)
    notify(cfg, "移动话费余额", f"{summary}\n手机尾号 {cfg.phone[-4:]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动话费余额查询（充值页 H5 通道）")
    parser.add_argument("--config", default="config.json",
                        help="配置文件路径（默认 config.json，需含 fee_session_cookie）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run(args.config)
    except Exception as e:
        log.error("查询失败: %s", e)
        try:
            cfg = Config(args.config)
            notify(cfg, "移动话费查询异常", f"{e}\n手机尾号 {cfg.phone[-4:] if cfg.phone else '????'}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
