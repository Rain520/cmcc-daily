# 中国移动 App 自动签到与活动自动化

一套基于抓包分析的中国移动 App 活动自动化脚本，覆盖签到领流量/话费、
周三充值日拼图、评价有礼、限时秒杀、话费余额查询等日常活动。
只需一次抓包获取凭证，之后长期自动运行；支持多账号、消息推送、定时任务。

## 功能一览

| 脚本 | 功能 | 频率建议 |
|------|------|----------|
| `cmcc_sign.py` | 每日签到、自动领取累计/连签奖励、AI豆任务、拓展活动、周三充值日 | 每天 1 次 |
| `cmcc_wed.py` | 周三充值日：每日任务 + 抽拼图 + 集齐自动领券 + 场次秒杀 | 每天 1 次（秒杀仅周三） |
| `cmcc_rate.py` | 评价有礼：每周满分评价得评价币，攒够自动兑换话费/流量券 | 每周 1 次 |
| `cmcc_seckill.py` | 假期限时秒杀（如 5 元话费加赠券） | 仅活动期 |
| `cmcc_fee.py` | 话费余额查询并推送 | 随意 |

所有脚本共用一份配置和凭证，互不影响；每步结果都可通过 Server酱/Bark 推送到手机。

## 快速开始

```bash
pip3 install -r requirements.txt        # 最低安装：pip3 install requests
cp config.example.json config.json      # 填入凭证（见下一节）
python3 cmcc_sign.py                    # 首次运行
python3 cmcc_sign.py --dry-run          # 或先只查状态，不做任何操作
```

运行后按手机尾号生成凭证缓存文件 `.cmcc_jwt_cache_<尾号>.json`，
此后长期有效，**无需反复抓包**；多账号复制多份 config 并用 `--config` 区分。

## 获取凭证（只需一次）

1. 手机装抓包工具（Proxyman / Charles / Stream 等），对 App 开启 HTTPS 抓包；
2. 打开中国移动 App → 我的 → 签到领流量，进入签到页；
3. 在抓包记录中找到 `wx.10086.cn/qwhdsso/appTokenLogin` 这条 POST 请求；
4. 复制请求体里 `token` 字段的完整值填入 `config.json` 的 `app_token`
   （形如 `JSESSIONID=xxx; UID=xxx; ...; ticketID=NingBo`，尾部属性标记可留可删）；
5. 同一请求体里的 `provinceCode`、`cityCode` 一并照抄。

![appToken 抓包位置示例](images/app-token-capture.png)

脚本会自动用 `app_token` 登录并续签一个长期凭证（jwt），之后每次运行优先
用 jwt 续期，**App 内切换登录也不影响**。只有当脚本同时推送「jwt 续期失败」
和「登录失败」时，才需要重新抓包更新 `app_token`。凭证属于账号敏感信息，
只在本地使用，切勿外传。

## 配置说明

`config.json` 各字段（均可被同名环境变量覆盖，适合 CI）：

| 字段 | 说明 | 默认 |
|------|------|------|
| `app_token` | App 票据，抓包获取（环境变量 `CMCC_APP_TOKEN`） | 必填 |
| `phone` | 手机号（`CMCC_PHONE`） | 必填 |
| `province_code` / `city_code` | 省/市编码（`CMCC_PROVINCE_CODE` / `CMCC_CITY_CODE`） | 731 / 0731 |
| `fee_session_cookie` | 话费查询用会话，见 `cmcc_fee.py` 一节（`CMCC_FEE_SESSION`） | 选填 |
| `serverchan_sendkey` | Server酱推送（`CMCC_SERVERCHAN_SENDKEY`） | 选填 |
| `bark_url` | Bark 推送，形如 `https://api.day.app/你的key`（`CMCC_BARK_URL`） | 选填 |
| `activity_id` / `channel_id` / `carrier_operator` / `app_version_code` | 活动参数，照抄模板即可 | 已填好 |

多账号：复制 `config.json` 为 `config2.json` 填入另一号码，运行时加
`--config config2.json`。凭证缓存按尾号自动隔离，不会串号。

## 每日主脚本 `cmcc_sign.py`

默认运行做两件事：**每日签到** 和 **自动领取累计/连签奖励**。
AI豆任务、拓展活动、周三充值日通过参数选择，可任意组合：

```bash
python3 cmcc_sign.py                          # 签到 + 领取累计/连签奖励
python3 cmcc_sign.py --tasks --games --wed    # 再加上全部顺带活动

# 按需单独叠加
python3 cmcc_sign.py --tasks                  # 加做 AI豆任务
python3 cmcc_sign.py --games                  # 加做拓展活动
python3 cmcc_sign.py --wed                    # 加做周三充值日

# 其他选项
python3 cmcc_sign.py --dry-run                # 只查状态，零操作
python3 cmcc_sign.py --no-claim               # 只签到，不领奖励
python3 cmcc_sign.py --delay 1800             # 启动前随机延迟 0~1800 秒（防风控，定时任务建议加）
```

各部分说明：

- **签到与累计奖励**：完成当日签到；累计签到达到门槛的奖励（话费券/流量券）
  自动领取，热门奖励被领光时会在推送中如实说明；
- **AI豆任务**（`--tasks`）：自动完成任务列表中可线上完成的部分
  （功能体验/福利活动/业务办理等区），外部合作区任务需要真实进入
  合作方 App，由合作方记账，脚本不做；
- **拓展活动**（`--games`）：周六游戏中心、追剧领福利等页面的打卡、
  任务与抽奖。内置窗口闸门：未到开放窗口只查余额不消耗；
- **周三充值日**（`--wed`）：详见下一节。

## 周三充值日 `cmcc_wed.py`

活动玩法：做任务攒抽奖次数 → 抽拼图碎片 → 集齐解锁话费券
（95折 4片 → 9折 6片 → 8折 9片，依次解锁）。

```bash
python3 cmcc_wed.py                     # 进页 + 自动任务 + 抽光当日次数 + 集齐自动领券
python3 cmcc_wed.py --dry-run           # 只查次数/拼图进度/任务状态
python3 cmcc_sign.py --wed              # 或挂在主签到后一起跑
```

自动化的内容：

- **进页**：「每日登录」任务自动 +1 次；
- **任务**：游戏日/视频日、一豆有好礼、看精彩视频、查话费余额、查账单、
  瓜分话费等可线上完成的任务全部自动完成（各 +1 次），服务端新增任务
  也会自动覆盖；「参与签到并浏览5秒」需在 App 内真实浏览签到页、
  邀请助力需真实受邀者、充值任务涉及真实消费，这三类不代做；
- **抽奖与领取**：抽光当日全部次数；某档拼图集齐后自动进位下一档并领取
  该档话费券（奖励月底清零，已领过的不会重复领）；
- **周三 8 点秒杀**（`--seckill`）：自动校准服务器时钟，到点前 0.4 秒开始
  连发抢券，抢中即停并推送，无库存自动止盈。

```bash
python3 cmcc_wed.py --seckill --dry-run      # 查时钟偏差与场次状态
python3 cmcc_wed.py --seckill                # 周三等 08:00 自动开抢
python3 cmcc_wed.py --seckill --at 12:00:00  # 指定其他场次
```

## 评价有礼 `cmcc_rate.py`

每周做一次 App 满意度评价（满分 10 分得 10 评价币），评价币可兑换
流量/话费券（如 2 元话费券 20 币，每月限兑 4 次，活动结束清零）。

```bash
python3 cmcc_rate.py --dry-run              # 查评价机会/币余额/档位（只读）
python3 cmcc_rate.py                        # 本周未评则自动满分评价
python3 cmcc_rate.py --exchange             # 攒够 20 币自动兑「2元话费券」（不足跳过）
python3 cmcc_rate.py --exchange <prizeId>   # 兑指定档位
```

每日运行亦可（按周幂等，重复运行不会重复评价）。档位与所需评价币
以 `--dry-run` 实时输出为准。

## 假期限时秒杀 `cmcc_seckill.py`

「签到有礼」页的限时秒杀（如 5 元话费加赠券）：完成当日签到即获资格，
每日 12:00 开抢、数量有限。会话与配置和主脚本完全共用。

```bash
python3 cmcc_seckill.py --dry-run    # 查场次/校时/资格（未签会自动补签获资格）
python3 cmcc_seckill.py --once       # 立即试发一发，验证响应
python3 cmcc_seckill.py              # 常驻等到下一场自动开抢
python3 cmcc_seckill.py --at 11:59:50 --interval 0.2   # 调参
```

默认提前 0.4 秒出手、每 0.35 秒一发、最多 120 发；抢完或达到限次即停。
退出码 `0`=抢到、`1`=未中，便于外层脚本判断。非活动日运行会因无场次推送异常。

## 话费余额查询 `cmcc_fee.py`

查询实时话费余额并推送（走充值页通道，需额外安装 `cryptography`，
requirements.txt 已包含）。会话为半自动：服务端要求 App 签发的
`jsessionid-cmcc`，获取步骤：

1. 手机保持代理，在 App 内打开一次「充值页」；
2. 抓包工具里找任意 `touch.10086.cn` 请求，复制 Cookie 中
   `jsessionid-cmcc=` 的值；
3. 填入配置的 `fee_session_cookie`。

![话费会话抓包示例](images/fee-session-capture.png)

```bash
python3 cmcc_fee.py --config config.json    # 查询并推送话费余额
```

会话失效后脚本报 `500003` 并提示，重新抓一次即可；查询本身只读。

## 定时任务

macOS crontab 示例（按需取舍，路径替换为实际目录）：

```bash
# 每天 8:23 主脚本（签到+领奖+AI豆+拓展+周三充值日），随机延迟半小时内
23 8 * * * cd /path/to/cmcc-auto-checkin && /usr/bin/python3 cmcc_sign.py --tasks --games --wed --delay 1800 >> sign.log 2>&1

# 周三 7:55 充值日秒杀（仅活动期需要挂着）
55 7 * * 3 cd /path/to/cmcc-auto-checkin && /usr/bin/python3 cmcc_wed.py --seckill >> wed_seckill.log 2>&1

# 每周一 9:17 评价有礼
17 9 * * 1 cd /path/to/cmcc-auto-checkin && /usr/bin/python3 cmcc_rate.py --exchange >> rate.log 2>&1
```

分钟刻意避开整点，配合 `--delay` 随机延迟降低风控风险。
GitHub Actions 亦可（secrets 里配 `CMCC_APP_TOKEN`、`CMCC_PHONE` 等
环境变量即可，无需 config 文件）；注意在 App 内切换账号登录会使
`app_token` 失效，云上定时请留意推送的失败告警。

## 常见问题

- **什么时候需要重新抓包？** 只有同时推送「jwt 续期失败」和「登录失败」时。
  平时 jwt 自动续期，无需任何维护；
- **推送里"领不到/已发完"是错误吗？** 不是。累计奖励和秒杀券数量有限，
  被领光属正常，脚本会如实标注；
- **"今日已签到（服务端幂等）"？** 同一天重复运行脚本安全，不会重复签到
  或重复领奖；
- **AI豆任务为什么有一些不完成？** 外部合作区（去快手/淘宝/支付宝等）
  由合作方记账，需真实进入对方 App，脚本整区跳过；充值、公众号、
  签到页浏览类需真实行为，失败后快速跳过并如实标注；
- **多账号怎么跑？** 每账号一份 config，`--config` 指定，cron 里各加一行；
- **运行有什么风险？** 请控制频率（脚本已内置随机延迟）、仅用于本人号码。
  详见下方免责声明。

## 免责声明

本项目基于抓包分析的活动接口实现，仅供自动化技术学习与个人效率研究，
与中国移动官方无任何关联。使用本脚本自动完成签到、领奖、限时秒杀、
满意度评价（尤其是非本人真实意愿的满分评价）等操作，可能违反中国移动
App 用户协议及相关活动规则，涉及奖励领取的真实性与公平性问题，并可能
导致账号被风控、奖励清零、限制或封禁；活动接口随 App 版本与运营策略
调整可能随时变更或失效。项目涉及的 `app_token`、jwt 缓存与会话 Cookie
均为账号敏感凭证，请妥善保管、切勿外传，因凭证泄露造成的账号损失由
使用者自行承担。是否使用、如何使用由使用者自行决定，作者与贡献者不对
由此产生的任何账号、财产、法律或其他后果负责。本脚本仅供个人号码自动化
使用，请勿高频调用、批量多开或用于商业用途。请遵守平台规则与当地法律法规。
