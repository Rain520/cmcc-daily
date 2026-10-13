#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动「评价有礼」自动满分评价 + 评价币兑换脚本
================================================

同一 qwhdhub SSO 通道下的评价活动（入口 /qwhdhub/assess/1024071116，
活动名「评价得好礼」，2026-12-31 结束），每周可评价一次，满分 10 分
得 10 评价币；评价币可兑换流量/话费券，每月限兑 4 次，活动结束即清零。
凭证与会话完全复用 cmcc_sign.exchange_session()（jwt 自续期缓存同样生效）。

接口链路（2026-10-02 Proxyman 抓包 + 页面 JS 分析）：

  GET  /qwhdhub/assess/markStatus                       本周评价机会与上次评分
  GET  /qwhdhub/assess/assess?score=10&time=<ms>        提交评分（满分 +10 评价币）
  POST /qwhdhub/account/query                           评分币余额/账本
  GET  /qwhdhub/activity/info                           活动配置（prizes: id/名称/exchangePrice）
  GET  /qwhdhub/assess/prizeStatus                      各档位库存/资格 + 剩余兑换次数
  GET  /qwhdhub/assess/redeem?prizeId=<id>&time=<ms>    兑换（页面 JS 还原）

响应字段（抓包确认）：
  markStatus.data    = {score: 上次评分, chance: 本周是否还有机会}
  account/query.data = {balance: 评价币余额, income: 累计获得,
                        outcome: 累计消费, records: 账本}
  prizeStatus.data   = {remain: 剩余兑换次数, convertType: mon=按月计,
                        prizeStatusHashMap: {prizeId: {stockStatus, chanceStatus}}}
  activity/info.data.prizes[] = {id, name, exchangePrice(所需评价币)}
  time 参数为毫秒时间戳，仅作缓存穿透；余额/次数是否足够由服务端校验。

用法：
  python3 cmcc_rate.py --dry-run               # 只查机会/余额/档位，不评价不兑换
  python3 cmcc_rate.py                         # 本周未评则自动满分评价
  python3 cmcc_rate.py --exchange              # 默认兑「2元话费券」（20 币，不足则跳过）
  python3 cmcc_rate.py --exchange <prizeId>    # 兑换指定档位（对照表见 README）

退出码：0 = 评价/兑换成功或已评过；1 = 失败（便于 cron 判断）
"""

import argparse
import logging
import sys
import time

import requests

from cmcc_sign import BASE, Config, api_headers, exchange_session, notify, notify_task

log = logging.getLogger("cmcc_rate")

QWHD_BASE = BASE + "/qwhdhub"

# 活动页 URL 为公开分享链接（非个人凭证）；不同省份/渠道入口不同时替换 query 参数
ENTRY = (f"{BASE}/qwhdhub/assess/1024071116"
         "?A_C_CODE=mbQ0p2z44K&channelId=P00000054182&yx=1182211444")


class ActConfig:
    """复用 Config 的凭证字段，仅把 act_url 换成评价活动页（同 cmcc_extra 的做法：
    jwt 是账号级凭证，换票不消耗新的 app_token）。"""

    def __init__(self, base_cfg, entry: str):
        self.__dict__.update(vars(base_cfg))
        self._entry = entry

    @property
    def act_url(self) -> str:
        return self._entry


def _request(s: requests.Session, referer: str, method: str, path: str,
             params: dict | None = None) -> dict:
    """qwhdhub 接口统一调用；网关偶发返回 HTML（限流/会话过期）按失败结构返回。"""
    try:
        if method == "GET":
            r = s.get(QWHD_BASE + path, params=params,
                      headers=api_headers(referer), timeout=30)
        else:
            r = s.post(QWHD_BASE + path, json=params or {},
                       headers=api_headers(referer), timeout=30)
        return r.json()
    except ValueError:
        return {"code": f"HTTP_{getattr(r, 'status_code', 0)}", "msg": "响应非 JSON"}
    except requests.RequestException as e:
        return {"code": "NETWORK_ERROR", "msg": str(e)}


def run(cfg: Config, dry_run: bool, exchange_id: str | None) -> int:
    s, referer = exchange_session(ActConfig(cfg, ENTRY))
    lines = []
    failed = False

    def query(method, path):
        response = _request(s, referer, method, path)
        if response.get("code") != "SUCCESS":
            raise RuntimeError(f"{path} 查询失败: {response.get('code')} {response.get('msg')}")
        return response.get("data") or {}

    # ① 本周评价机会
    st = query("GET", "/assess/markStatus")
    if st.get("chance"):
        if dry_run:
            lines.append("[dry-run] 本周可评价，将评 10 分得 10 评价币")
        else:
            r = _request(s, referer, "GET", "/assess/assess",
                         {"score": 10, "time": int(time.time() * 1000)})
            if r.get("code") == "SUCCESS":
                lines.append("满分评价成功，+10 评价币")
            else:
                lines.append(f"评价失败: {r.get('code')} {r.get('msg')}")
                failed = True
    else:
        lines.append(f"本周已评过（上次 {st.get('score', '?')} 分），无需操作")

    # ② 评价币余额：account/query 的 balance 是真实余额
    #    （prizeStatus.remain 是"月剩余兑换次数"）
    acc = query("POST", "/account/query")
    lines.append(f"评价币余额 {acc.get('balance', '?')}"
                 f"（累计获得 {acc.get('income', '?')}，累计消费 {acc.get('outcome', '?')}）")

    # ③ 档位表：activity/info 给名称与所需评价币，prizeStatus 给库存/资格与剩余次数
    info = query("GET", "/activity/info")
    prize_meta = {str(p.get("id")): p for p in (info.get("prizes") or [])}
    pz = query("GET", "/assess/prizeStatus")
    status_map = pz.get("prizeStatusHashMap") or {}
    status_keys = {str(k) for k in status_map}
    remain_label = "月剩余兑换次数" if pz.get("convertType") == "mon" else "剩余兑换次数"

    def tier_text(pid, v):
        meta = prize_meta.get(str(pid)) or {}
        return (f"{meta.get('name', pid)}({meta.get('exchangePrice', '?')}币,"
                f"{'有货' if v.get('stockStatus') else '无货'}/"
                f"{'有资格' if v.get('chanceStatus') else '无资格'})")

    tiers = "、".join(
        tier_text(pid, v)
        for pid, v in sorted(status_map.items(),
                             key=lambda kv: (prize_meta.get(str(kv[0])) or {}).get("exchangePrice") or 0))
    lines.append(f"{remain_label}: {pz.get('remain', '?')} 次；档位: {tiers or '无'}")

    # ④ 兑换：--exchange 不带参数默认兑话费券（按名称匹配，档位配置变了也不怕），
    #    带 prizeId 兑指定档位；余额不足本地先跳过（对定时攒币场景友好），
    #    其余错误以服务端校验为准，不会误扣
    if exchange_id:
        meta = None
        if exchange_id == "auto":
            # 优先全名匹配「2元话费券」，避免未来新增其他话费类档位时误选
            candidates = [p for p in prize_meta.values()
                          if str(p.get("id")) in status_keys]
            meta = next((p for p in candidates
                         if str(p.get("name", "")).strip() == "2元话费券"), None) \
                or next((p for p in candidates
                         if "话费" in str(p.get("name", ""))), None)
            if meta is None:
                lines.append("兑换: 档位列表中未找到话费券，跳过（可 --exchange <prizeId> 指定）")
                failed = True
        else:
            meta = prize_meta.get(exchange_id)
            if meta is None or exchange_id not in status_keys:
                lines.append(f"兑换 {exchange_id}: 不在当前档位列表中，已取消")
                failed = True
                meta = None
        if meta:
            name = str(meta.get("name") or exchange_id)
            price = meta.get("exchangePrice") or 0
            balance = acc.get("balance")
            if isinstance(balance, (int, float)) and price and balance < price:
                lines.append(f"兑换 {name}: 余额 {balance} 币不足（需 {price}），先攒币，本次跳过")
            elif dry_run:
                lines.append(f"[dry-run] 将兑换 {name}（{price} 币，prizeId={meta.get('id')}）")
            else:
                r = _request(s, referer, "GET", "/assess/redeem",
                             {"prizeId": meta.get("id"), "time": int(time.time() * 1000)})
                if r.get("code") == "SUCCESS":
                    lines.append(f"兑换成功: {name}，券 48 小时内到账，请到 App「我的奖品」查看")
                else:
                    lines.append(f"兑换失败: {r.get('code')} {r.get('msg')}")
                    failed = True

    summary = "\n".join(lines)
    log.info("\n%s", summary)
    notify_task(cfg, "移动评价有礼", summary)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动「评价有礼」自动评价脚本")
    parser.add_argument("--config", default="config.json",
                        help="配置文件路径（默认 config.json，与 cmcc_sign.py 共用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只查评价机会/余额/档位，不评价不兑换")
    parser.add_argument("--exchange", nargs="?", const="auto", metavar="PRIZE_ID",
                        help="兑换：不带参数默认兑「2元话费券」，带 prizeId 兑指定档位")
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
        return run(cfg, dry_run=args.dry_run, exchange_id=args.exchange)
    except Exception as e:
        log.exception("执行失败: %s", e)
        notify_task(cfg, "移动评价有礼异常", f"{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
