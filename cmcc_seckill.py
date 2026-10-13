#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动「签到有礼」假期秒杀抢券脚本
====================================

复用 cmcc_sign.py 的会话链路与配置（同一个活动 1021122301），按抓包分析的接口：

  1. exchange_session()                 SSO 换 QWHD_SESSION_TOKEN（jwt 免票据续期）
  2. POST /qwhdhub/api/mark/markSeckill/sysTime          服务器时间，算本机时钟偏移
  3. POST /qwhdhub/api/mark/markSeckill/secConfig        秒杀场次（secId/prizeId/开抢时间）
  4. POST /qwhdhub/api/mark/markSeckill/todayMarkStatus  当日签到状态=参与资格（未签则先 domark）
  5. 等到开抢时刻 → 循环 POST /qwhdhub/api/mark/markSeckill/redeem {"secId","prizeId"}

redeem 响应判定（前端源码还原）：
  code=SUCCESS            抢到，去 App「我的奖品」核销
  status=PRIZE_NO_STOCK   券抢完（停手）
  status=NOT_TIME_IN      未开抢（继续，密集重试）
  status=PRIZE_LIMIT_*    限次（停手）

用法：
  python3 cmcc_seckill.py --dry-run      # 只查场次/校时/资格，不抢
  python3 cmcc_seckill.py --once         # 立即打一次 redeem（验证响应格式）
  python3 cmcc_seckill.py                # 等到下一场开抢自动抢（配合 cron 常驻）
  python3 cmcc_seckill.py --at 11:59:50  # 手动指定开抢时刻（调试用）

退出码：0 = 秒杀成功；1 = 失败/抢完/异常
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from cmcc_sign import (
    API_MARK,
    Config,
    api_headers,
    do_mark,
    exchange_session,
    notify,
)

log = logging.getLogger("cmcc_seckill")

TZ_CN = timezone(timedelta(hours=8))  # 活动时间均为北京时间

SYSTIME_URL = f"{API_MARK}/markSeckill/sysTime"
SECCONFIG_URL = f"{API_MARK}/markSeckill/secConfig"
TODAY_MARK_URL = f"{API_MARK}/markSeckill/todayMarkStatus"
REDEEM_URL = f"{API_MARK}/markSeckill/redeem"

# 触达即终态的 redeem status：不再浪费请求
STOP_STATUSES = {
    "PRIZE_NO_STOCK": "券已抢完",
    "PRIZE_LIMIT_DAY": "当日中奖次数已用完",
    "PRIZE_LIMIT_MONTH": "当月中奖次数已用完",
    "PRIZE_RESTRIC_LIMIT": "活动期间中奖次数已达上限",
    "WORK_ORDER_RESTRIC_LIMIT": "工单限流（黑名单/风控）",
}

# 会话 cookie 滑动 30 分钟，长等时的心跳间隔（也顺带刷新 cookie）
KEEPALIVE_INTERVAL = 480


def api_post(s, referer: str, url: str, payload: dict | None = None) -> dict:
    r = s.post(url, json=payload if payload is not None else {},
               headers=api_headers(referer), timeout=10)
    r.raise_for_status()
    return r.json()


def server_now_ms(s, referer: str) -> int:
    """服务器当前毫秒时间。实测响应: {"code":"SUCCESS","data":{"sysTime":1790923978566,...}}；
    解析失败时用 Date 响应头兜底（秒级精度）。"""
    try:
        resp = api_post(s, referer, SYSTIME_URL)
        ms = (resp.get("data") or {}).get("sysTime")
        if isinstance(ms, int):
            return ms
        log.warning("sysTime 响应格式变化: %s", json.dumps(resp, ensure_ascii=False)[:200])
    except Exception as e:
        log.warning("sysTime 请求失败: %s", e)
    from email.utils import parsedate_to_datetime
    try:
        r = s.post(SYSTIME_URL, json={}, headers=api_headers(referer), timeout=10)
        date = r.headers.get("Date")
        if date:
            return int(parsedate_to_datetime(date).timestamp() * 1000)
    except Exception as e:
        log.warning("Date 响应头兜底失败: %s", e)
    raise RuntimeError("无法获取服务器时间（sysTime 与 Date 响应头均不可用）")


def pick_zone(zones: list, now_ms: int) -> tuple[dict | None, bool]:
    """选出当前进行中或下一场即将开始的场次，返回 (zone, 是否已开抢)。"""
    for z in zones:
        if int(z["startTime"]) <= now_ms <= int(z["endTime"]):
            return z, True
    upcoming = sorted((z for z in zones if int(z["startTime"]) > now_ms),
                      key=lambda z: int(z["startTime"]))
    return (upcoming[0] if upcoming else None), False


def ensure_eligible(s, referer: str, cfg: Config) -> bool:
    """秒杀资格 = 完成当日签到。未签则先走同活动的 domark 补签。"""
    resp = api_post(s, referer, TODAY_MARK_URL)
    if (resp.get("data") or {}).get("markStatus") == "marked":
        return True
    log.info("今日未签到，先补签获取秒杀资格...")
    try:
        result = do_mark(s, referer, datetime.now(TZ_CN).strftime("%Y%m%d"))
        log.info("补签响应: code=%s status=%s msg=%s",
                 result.get("code"), result.get("status"), result.get("msg", ""))
    except Exception as e:
        log.warning("补签请求失败: %s", e)
    resp = api_post(s, referer, TODAY_MARK_URL)
    return (resp.get("data") or {}).get("markStatus") == "marked"


def wait_until(target: float, s, referer: str):
    """睡到 target（本地时钟秒）。超过一个心跳周期就定期发请求保活会话。"""
    while True:
        remain = target - time.time()
        if remain <= 0:
            return
        time.sleep(min(remain, KEEPALIVE_INTERVAL))
        if target - time.time() > 60:
            try:
                api_post(s, referer, TODAY_MARK_URL)
                log.info("会话心跳 ok，距开抢还有 %.1f 分钟", (target - time.time()) / 60)
            except Exception as e:
                log.warning("会话心跳失败: %s", e)


def fire_redeem(s, referer: str, zone: dict, interval: float,
                max_attempts: int, deadline_srv_ms: int, offset_ms: float) -> tuple[dict | None, str]:
    """开抢主循环。返回 (成功时的响应, 结束原因)。"""
    payload = {"secId": str(zone["id"]), "prizeId": str(zone["prize"]["id"])}
    log.info("开始抢购: %s (secId=%s prizeId=%s)",
             zone["prize"]["name"], payload["secId"], payload["prizeId"])

    for attempt in range(1, max_attempts + 1):
        if (time.time() + offset_ms) * 1000 > deadline_srv_ms:
            return None, "场次窗口已结束"
        try:
            resp = api_post(s, referer, REDEEM_URL, payload)
        except Exception as e:
            log.warning("第%d发请求异常: %s", attempt, e)
            time.sleep(interval)
            continue

        code, status, msg = resp.get("code"), resp.get("status"), resp.get("msg", "")
        log.info("第%d发: code=%s status=%s msg=%s", attempt, code, status, msg)
        if code == "SUCCESS":
            return resp, "SUCCESS"
        if status in STOP_STATUSES:
            return resp, status
        # NOT_TIME_IN（提前量打早了）与其他未知错误：密集重试直到开抢/窗口结束
        time.sleep(0.05 if status == "NOT_TIME_IN" else interval)

    return None, f"连续 {max_attempts} 发未中"


def run(cfg: Config, dry_run: bool, once: bool, at: str | None,
        interval: float, lead: float, max_attempts: int) -> int:
    # 会话建立兜底重试一次
    for attempt in (1, 2):
        try:
            s, referer = exchange_session(cfg)
            break
        except Exception:
            if attempt == 2:
                raise
            log.warning("会话建立失败，重试一次")

    srv_ms = server_now_ms(s, referer)
    offset_ms = srv_ms - time.time() * 1000
    log.info("服务器时间 %s，本机时钟偏移 %+.0f ms",
             datetime.fromtimestamp(srv_ms / 1000, TZ_CN).strftime("%m-%d %H:%M:%S.%f")[:-3],
             offset_ms)

    cfg_resp = api_post(s, referer, SECCONFIG_URL)
    zones = ((cfg_resp.get("data") or {}).get("secKillData") or {}).get("secKillZones") or []
    if not zones:
        raise RuntimeError("secConfig 未返回任何场次（活动未配置或已结束）")
    log.info("共 %d 个场次:", len(zones))
    for z in zones:
        st = datetime.fromtimestamp(int(z["startTime"]) / 1000, TZ_CN)
        et = datetime.fromtimestamp(int(z["endTime"]) / 1000, TZ_CN)
        log.info("  场次%s %s ~ %s  %s (prizeId=%s)",
                 z["id"], st.strftime("%m-%d %H:%M:%S"), et.strftime("%H:%M:%S"),
                 z["prize"]["name"], z["prize"]["id"])

    if dry_run:
        status = api_post(s, referer, TODAY_MARK_URL)
        eligible = (status.get("data") or {}).get("markStatus") == "marked"
    else:
        eligible = ensure_eligible(s, referer, cfg)
    log.info("秒杀资格（当日签到）: %s", "已具备" if eligible else "未获取！")

    zone, active = pick_zone(zones, srv_ms)
    if zone is None:
        raise RuntimeError("没有可参与的场次（全部已结束）")
    start_srv_ms, end_srv_ms = int(zone["startTime"]), int(zone["endTime"])
    st = datetime.fromtimestamp(start_srv_ms / 1000, TZ_CN)
    log.info("选定场次%s：%s 开抢（%s）", zone["id"], st.strftime("%m-%d %H:%M:%S"),
             "进行中" if active else f"距开始 {(start_srv_ms - srv_ms) / 1000:.0f} 秒")

    if dry_run:
        log.info("[dry-run] 到此为止，不执行抢购")
        return 0 if eligible else 1

    if at:  # 手动覆盖开抢时刻（zone 仍取选定场次，用于验证计时逻辑）
        hh, mm, *ss = at.split(":")
        target = datetime.now(TZ_CN).replace(
            hour=int(hh), minute=int(mm), second=int(ss[0]) if ss else 0, microsecond=0)
        start_srv_ms = int(target.timestamp() * 1000)
        active = False  # --at 语义是等到该时刻再出手，即便场次已在进行中
        log.info("开抢时刻被 --at 覆盖为 %s", target.strftime("%H:%M:%S"))

    if once:
        resp = api_post(s, referer, REDEEM_URL,
                        {"secId": str(zone["id"]), "prizeId": str(zone["prize"]["id"])})
        log.info("单发响应: %s", json.dumps(resp, ensure_ascii=False))
        ok = resp.get("code") == "SUCCESS"
        if ok:
            notify(cfg, "移动秒杀成功", f"{zone['prize']['name']}\n手机尾号 {cfg.phone[-4:]}")
        return 0 if ok else 1

    # 常规流程：等到开抢（提前 lead 秒出手，抵消 RTT），然后循环抢
    if not eligible:
        notify(cfg, "移动秒杀资格缺失",
               f"当日未签到且补签失败，无法参与秒杀\n手机尾号 {cfg.phone[-4:]}")
        return 1
    if not active:
        wait_until(start_srv_ms / 1000 - offset_ms / 1000 - lead, s, referer)

    resp, reason = fire_redeem(s, referer, zone, interval, max_attempts, end_srv_ms, offset_ms)

    if reason == "SUCCESS":
        detail = f"{zone['prize']['name']}\n场次 {st.strftime('%m-%d %H:%M')}，请去 App「我的奖品」核销\n手机尾号 {cfg.phone[-4:]}"
        log.info("秒杀成功！%s", zone["prize"]["name"])
        notify(cfg, "移动秒杀成功 🎉", detail)
        return 0

    detail = f"{reason}\n场次 {st.strftime('%m-%d %H:%M')} {zone['prize']['name']}\n手机尾号 {cfg.phone[-4:]}"
    log.warning("秒杀未成功: %s", reason)
    notify(cfg, "移动秒杀未成功", detail)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动「签到有礼」假期秒杀抢券脚本")
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json，与 cmcc_sign.py 共用）")
    parser.add_argument("--dry-run", action="store_true", help="只查场次/校时/资格，不抢")
    parser.add_argument("--once", action="store_true", help="立即打一次 redeem 后退出（验证响应格式）")
    parser.add_argument("--at", metavar="HH:MM[:SS]", help="手动指定开抢时刻（北京时间，调试用）")
    parser.add_argument("--interval", type=float, default=0.35, metavar="SEC", help="重试间隔秒（默认 0.35）")
    parser.add_argument("--lead", type=float, default=0.4, metavar="SEC", help="提前开火秒数，抵消网络延迟（默认 0.4）")
    parser.add_argument("--max-attempts", type=int, default=120, metavar="N", help="最大尝试次数（默认 120）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config(args.config)
    if err := cfg.validate():
        log.error("%s\n参考 config.example.json 填写，或设置对应环境变量", err)
        return 1

    try:
        return run(cfg, dry_run=args.dry_run, once=args.once, at=args.at,
                   interval=args.interval, lead=args.lead, max_attempts=args.max_attempts)
    except KeyboardInterrupt:
        log.info("手动中断")
        return 1
    except Exception as e:
        log.exception("执行失败: %s", e)
        notify(cfg, "移动秒杀异常", f"{e}\n手机尾号 {cfg.phone[-4:] if cfg.phone else '????'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
