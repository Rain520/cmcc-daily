# 验证记录

## 1.6.7 发布（2026-10-15）

发布前：`python -m unittest discover -s tests -q` 共 168 项通过（4 项 Linux 专用测试在 Windows 跳过）；`docker build -t cmcc-daily:test .` 后 `python tests/container_smoke.py` 通过（含审核预览用例）；在本机 Docker 审核预览（`cmcc-preview-cmcc-1`，0.0.0.0:18081）里用浏览器逐页自检账号总览、秒杀任务页（含示例活动）、秒杀结果弹窗与一键执行弹窗，桌面与 390×844 手机宽度各看一遍。

发布：`docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false -t rain5201/cmcc-daily:1.6.7 -t rain5201/cmcc-daily:latest --push .` 推送成功，两个标签指向同一索引 `sha256:6f32710e…`（AMD64 清单 `sha256:63820cdc…`，ARM64 清单 `sha256:77732b06…`）。第一次推送时 `auth.docker.io` 直连被重置（`failed to fetch oauth token ... connection forcibly closed`），设置 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890` 后成功；这条经验已写进 `docs/PUBLISHING.md`。发布后 `docker pull rain5201/cmcc-daily:1.6.7` 拉到的镜像 ID 与该索引摘要一致，并对该镜像运行容器冒烟测试（通过）。

本机正式实例（`cmcc-local` 项目，127.0.0.1:18080，1 个账号 15 条记录）随后升级到该镜像：升级前把数据卷快照到 `test-results/backup-before-1.6.7/cmcc-data-before-1.6.7.tgz`，`.env.local` 的 `CMCC_IMAGE` 改为 `rain5201/cmcc-daily:1.6.7` 后 `docker compose -p cmcc-local -f compose.image.yaml --env-file .env --env-file .env.local up -d`（数据卷 `cmcc-local_cmcc-data` 不变）。升级后容器镜像摘要 `sha256:6f32710e…` 与发布索引一致、健康检查 200、账号与执行记录数不变、页面版本显示 1.6.7、端口仍只绑 127.0.0.1:18080。审核预览容器（`cmcc-preview` 项目）在发布后已 `down`（保留虚构数据卷，需要时 `docker compose -f compose.preview.yaml up -d` 即可再起）。

## 审核预览里发现的三处界面问题（2026-10-15）

审核 1.6.7 预览时发现并修好三处，都是「秒杀任务」接进来后暴露的：

1. **账号总览整页渲染失败**：`accountSchedules()` 只按 `taskOrder`（签到／拼图／评价／话费）补默认计划，账号卡片却按后端返回的 `summary.tasks` 渲染；只要某账号有过秒杀记录，`schedules["seckill"]` 就是 `undefined`，`schedule.enabled` 抛 "Cannot read properties of undefined (reading 'enabled')"，卡片渲染中断、右上角显示「连接异常」并弹出红色提示。现在新增 `taskKeys`（与后端 `TASK_ORDER` 一致，含 `seckill`）用于 `accountSchedules`，卡片取值再加 `|| defaultSchedule` 兜底。
2. **秒杀结果弹窗的小字说明被账号按钮压住**（用户截图反馈）：说明文字用了 `.field-hint` 的 `margin-top:-7px`，紧贴按钮最后一行，按钮底边正好压在文字首行上。现在 `.dialog-footnote` 自带 `margin:16px 0 0`，列表高度改为 `min(380px,46vh)`，窗口变矮时先收缩列表而不是顶到说明文字。实测（浏览器实测 DOM 矩形）：按钮底边 417px、说明文字顶边 433px，间隔 16px；手机宽度 390×844 下说明折成 2 行同样保持 16px 间隔，弹窗高 422px < 90vh。
3. **一键执行弹窗漏掉秒杀，且与后端判定不一致**：`planFor` 原先只遍历 `taskOrder`，而后端 `runnable_tasks` 含 `seckill`——弹窗不提秒杀，提交后却出现秒杀记录；同时 `taskMark` 对秒杀一律返回「将执行」。现在弹窗按 `taskKeys` 列出五个任务，秒杀按最近一次结果判定：最近已成功时在「只执行未完成和出错的」下显示「已执行成功，本次跳过」、切到「全部执行」显示「重复执行（最近一次已成功）」、排队或执行中显示跳过，与后端 `select_tasks` 的 pending／all 语义一致；「只执行已开启计划的任务」下秒杀没有账号级计划，与后端一样跳过。审核预览实测：批量弹窗显示「示例 · 日常账号」为 每日签到跳过、周三拼图将执行、评价有礼跳过、话费查询跳过、假期秒杀已执行成功本次跳过，汇总「将提交 11 个任务，另有 19 个会跳过」（6 账号 × 5 任务），与后端返回的提交数一致。

单元测试 `python -m unittest discover -s tests -q` 共 168 项通过（4 项 Linux 专用测试在 Windows 跳过），新增 `tests/test_history.py` 两项：前端 `taskKeys` 必须等于后端 `TASK_ORDER`（缺 seckill 会重现上面的崩溃）、账号卡片必须保留 `schedules[entry.task] || defaultSchedule` 兜底；以及结果弹窗样式必须保留 `.dialog-footnote{margin:16px 0 0` 与 `max-height:min(380px,46vh)`。`node --check cmcc_app/static/app.js` 通过。

界面验证方式：用 Codex 内置浏览器打开审核预览（`http://127.0.0.1:18081/`，虚构数据、任务为本地模拟），登录后逐页截图核对；账号总览恢复渲染（右上角显示「审核预览」、6 张账号卡片齐全），秒杀结果弹窗与一键执行弹窗在桌面与 390×844 手机宽度下均无重叠、无报错。截图：`seckill-result-dialog-desktop.png`、`seckill-result-dialog-phone.png`（本次会话的临时产物目录，未进仓库）。

## 秒杀活动配置真正生效（2026-10-15）

改动：1.6.7 的「秒杀任务」页此前只做了界面与排队，活动卡片里的抢购参数既没保存也没传给脚本，属于「看着能配、实际不生效」。本次修好三处：

1. 保存活动时的参数被丢弃。网页表单把 `lead`/`interval`/`max_attempts` 平铺在请求里（`readActivityForm`），而 `normalize_activity` 只读 `body["config"]`，于是三个值静默落回默认值；现在两种写法都接受（平铺优先、不认识的键忽略），保存后再读出来就是用户填的值。
2. 活动参数到不了抢券脚本。秒杀任务改成带着 `activity_id` 入队（`runs.activity_id` 新列 + 旧库自动 `ALTER TABLE`），执行时由 `Store.seckill_settings()` 取该活动（缺失时退到第一个已启用的活动，再退回默认值）并把参数作为 worker 的第 5 个参数交给 `cmcc_seckill`，`worker.task_args` 拼成 `--lead`/`--interval`/`--max-attempts`（非法值不拼，留给脚本用自己的默认值），日志里会打印「秒杀活动：名称（开抢时间…）」。其他任务不追加该参数。
3. 排队时机来不及。定时活动过去按 `next_run` 准点入队，子进程启动时已经晚于「开抢时间 − 提前量」；现在提前「提前量 + 60 秒」入队（`SECKILL_ENQUEUE_AHEAD`），脚本先建会话、校时、拉场次、确认当日签到资格，再在开抢时刻前提前量秒开火，下一场从这次的场次时间往后算，同一次开抢不会重复排队。活动卡片里补了一行说明。

顺带修掉一个死判断：`delete_activity` 原本拿活动 id 去比 `runs.account_id`（永远匹配不上），现在改成按 `activity_id` 检查，还有排队或执行中的秒杀任务时拒绝删除并提示「请先停止再删除」。审核预览的示例数据里加了 1 个虚构活动（示例 · 假期抢券，12:00:00，提前量 3 秒，间隔 0.5 秒，最多 40 次，全部 6 个虚构账号），并且预览里的秒杀也按账号并行模拟，方便点「立即执行」看结果弹窗。

单元测试 `python -m unittest discover -s tests -q` 共 166 项通过（4 项 Linux 专用测试在 Windows 跳过），新增/加强覆盖：`task_args` 对秒杀拼出三个开关且非法值不拼、其他任务不追加参数；`test_runner` 走真实 `Manager._execute_body` 断言第 5 个参数就是活动的 `{name,time,lead,interval,max_attempts}`；网页接口保存活动后三个参数原样读回；秒杀记录带 `activity_id`；定时活动超过「提前量 + 预热 1 分钟」不排队、进入窗口才排队且同一场次只入队一次；删除活动在有待执行秒杀任务时返回 400。

容器验证：`docker build -t cmcc-daily:test .` 后 `python tests/container_smoke.py` 通过（含审核预览用例）。镜像内直接验证：`task_args("seckill", …, {"lead":3,"interval":0.5,"max_attempts":40})` 得到 `--lead 3 --interval 0.5 --max-attempts 40`；`python cmcc_seckill.py --config /tmp/missing.json --lead 3 --interval 0.5 --max-attempts 40` 被脚本正常解析（只报缺少 app_token，未联网）；用真实 worker 入口跑一次秒杀任务，日志打印「秒杀活动：示例抢券（开抢时间 12:00:00，提前量 3 秒，重试间隔 0.5 秒，最多 40 次）」后按脚本流程失败在缺失配置上。

真实运行验证（本机 Docker 审核预览 0.0.0.0:18081，模拟执行）：`GET /api/activities` 返回 1 个示例活动、6 个账号、`config` 为 `{lead:3, interval:0.5, max_attempts:40}`；以平铺方式 `PUT` 改成 8/0.6/66 后立刻读回一致（下次执行 12:00:00）；`POST /api/activities/<id>/run` 返回 202 并为 6 个账号各排一个秒杀任务，21.4 秒后 6 条记录全部 success（并行模拟），活动卡片的「账号结果」有 6 条且成功率为 100%。全程未访问中国移动接口，正式实例（127.0.0.1:18080 的 1.6.6）不受影响。

## 审核预览改在 Docker 里跑（2026-10-15）

改动：测试版预览从「本机 Python 脚本」改为「容器内预览模式」。新增 `cmcc_app/preview.py`：首次启动向空的独立数据目录写入 6 个虚构账号（覆盖正常／凭证失效／部分异常／正在执行／排队中／尚未验证，其中 2 条排队记录固定保留）并留下 `synthetic-preview.txt` 标记；替换调度器的 `recover`、`schedule_due`、`schedule_activities_due` 与 `Manager.execute`，让定时计划与秒杀活动到点不排队、任务改为本地模拟（约 20 秒）；页面顶部显示预览横幅、`/api/accounts` 返回 `preview: true`（前端据此把状态显示成「审核预览」）、通知测试与备份导入返回 400。数据目录里已有账号或执行记录时拒绝启动，避免把正式实例切成模拟。新增 `compose.preview.yaml`（项目名 `cmcc-preview`、端口 18081、数据卷隔离、默认密码 123456，且不读取正式实例的 `.env` 变量）；删除 `tools/preview_server.py`，`tools/preview_seed.py` 改为调用 `cmcc_app.preview.seed` 的薄封装（手动给空目录生成示例数据仍可用）。`CMCC_PREVIEW=1` 必须与调度器启动顺序配合：预览先在 `install()` 里替换 `recover` 再启动调度线程，否则示例里的「正在执行」会被当成中断。

单元测试 `python -m unittest discover -s tests -q` 共 164 项通过（4 项 Linux 专用测试在 Windows 跳过，1.6.7 秒杀相关既有用例不变），新增 `tests/test_preview.py` 12 项：`CMCC_PREVIEW` 取值解析、空目录一次性写入示例数据（6 个账号、12 条记录、标记文件）、已有真实记录时拒绝写入、页面横幅与 `preview` 标记、定时计划与活动不排队、示例「正在执行／排队中」不被取走而新任务照常执行、签到模拟结果的子步骤（claim=empty／tasks=skipped／games=ok）、仅查询与秒杀（并行）模拟、停止后记为 interrupted、通知测试与备份导入被拒、导出备份只含虚构数据、以及调度线程在 `install()` 之后启动才保留示例状态。

容器验证：`docker build -t cmcc-daily:test .` 后运行 `python tests/container_smoke.py` 通过，冒烟测试新增审核预览用例——6 个虚构账号与 `preview: true`、页面横幅、`synthetic-preview.txt`、通知测试与备份导入返回 400、手动签到约 20 秒后成功且提示「审核预览模拟」、以及把真实账号写进数据目录后 `CMCC_PREVIEW=1` 启动失败并提示「只能用于空的独立预览目录」。原来的权限修复、登录、改密、CRUD、重启持久化、备份导入导出、运行用户与能力、只读文件系统与健康检查用例仍全部通过。

真实运行验证：`docker compose -f compose.preview.yaml up -d --build` 起本机预览（0.0.0.0:18081，容器 `cmcc-preview-cmcc-1`，健康检查通过），页面显示预览横幅，`/api/accounts` 返回 6 个虚构账号且状态覆盖 6 种，手动执行签到 21.4 秒后为 success，结构化结果为 main=ok／claim=empty／tasks=skipped／games=ok，执行记录 12 条示例 + 1 条手动。全程未访问中国移动接口、未发送通知；正式实例（`rain5201/cmcc-daily:1.6.6`，127.0.0.1:18080）不受影响。当时尚未推送镜像与代码，后续按“发布”流程一并完成。

顺带修复：`tests/test_account_status.py::test_summary_independent_of_history_page_and_auth_required` 依赖真实时钟（用例固定 2026-10-14 12:00，接口按当天算计划窗口），跨零点后必然失败；改为在该请求里把 `cmcc_app.account_status.utcnow` 固定成用例时间，只改用例不改功能。

## 1.6.7：秒杀任务、Docker 审核预览与界面修复（2026-10-14 起）

改动：上游 `cmcc_seckill.py` 接入网页应用，成为第五种任务；新增侧边栏「秒杀任务」页，以活动为单位配置开抢时间（精确到秒）、提前量、重试间隔、最大尝试次数与活动日；全部账号自动参与，到点由调度器按账号排队 `seckill`，同一场次只入队一次；支持活动卡片手动「立即执行」与结果弹窗（成功率、各状态数量、复制成功/失败名单）；活动 CRUD 接口 `GET/POST/PUT/DELETE /api/activities` 与 `POST /api/activities/<id>/run`。秒杀的 dry-run 语义与查询分离：网页执行秒杀始终为正式模式（到点抢券），排队与执行仍受全局串行队列约束。

单元测试 `python -m unittest discover -s tests -q` 共 152 项通过（4 项 Linux 专用测试在 Windows 跳过），新增覆盖：活动 CRUD（同名拒绝、未知 kind 拒绝、空账号列表表示全部账号）、手动执行为每个账号排队并在重复时跳过、到点调度入队且同场次不重复、秒杀任务并行取走（不同账号可同时 running）、结果接口返回每账号最近一次状态、`worker` 对秒杀任务的参数拼装（正式无 --dry-run）、模板与脚本对侧边栏入口、活动卡片、结果弹窗（成功率/复制）与函数的校验。

真实运行验证（本地审核预览 0.0.0.0:18081，模拟数据）：创建活动（空账号列表=全部 6 个账号）返回 201；手动执行返回 202 并为 6 个账号各排一个 `seckill` 任务；results 覆盖全部账号；编辑弹窗已无账号勾选，结果弹窗含成功率高亮与「复制结果」按钮；侧边栏出现「秒杀任务」入口。未真实调用移动接口。

## 1.6.6：AI 豆跳过与一键执行范围选项（2026-10-14）

改动：AI 豆「需要真实操作而跳过」不再让整次执行变成部分成功（`cmcc_extra` 记为 success/skipped，`results.finish` 不再把 skipped 计入异常），子步骤结果带上原因代码，界面据此把这类标签渲染成中性的「部分跳过」；一键执行新增 `pending` / `all`（批量另有 `enabled`）三种范围，服务端按账号状态挑选任务，前端按任务逐条标注状态与跳过原因。

单元测试 `python -m unittest discover -s tests -q` 共 147 项通过（4 项 Linux 专用测试在 Windows 跳过），新增覆盖：跳过的步骤不再产生 partial、原因代码进入账号卡片、`run-all` 的 pending/all 两种范围（pending 跳过已成功的任务）、`run-batch` 的 enabled/pending/all（pending 只补未完成与出错的、all 包含已成功的）、`mode` 取值非法返回 400、调度器未运行时返回 503，以及模板与脚本中新增的选项、`batch-tasks`、`taskMark`、`planFor`、`renderBatchPlan` 等选择器与函数。

真实运行验证（本地审核预览 0.0.0.0:18081，模拟执行不联网）：账号卡片对同一账号分别用 `mode=pending` 与 `mode=all` 提交，分别得到「只提交等待执行的任务」与「重复提交已成功的任务并跳过已在队列中的任务」；模拟执行完成后签到任务状态为 success，子步骤为 claim=empty、tasks=skipped、games=ok，界面据此显示「AI 豆 · 部分跳过」而整次仍为已完成。

前端：`node --check cmcc_app/static/app.js` 通过。

发布：`docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false -t rain5201/cmcc-daily:1.6.6 -t rain5201/cmcc-daily:latest --push .` 推送成功，两个标签指向同一索引 `sha256:9b3d32fb…`（AMD64 `sha256:1b1b0dd8…`，ARM64 `sha256:6a818831…`）；从 Docker Hub 拉取该标签后运行容器冒烟测试（通过），本地实例重建后镜像 ID 与该索引摘要一致。

## 1.6.5：界面确认弹窗与发布验证（2026-10-14）

改动：把浏览器原生确认框全部换成页面内弹窗——账号卡片「一键执行」（与「一键执行全部」同款，逐条列出任务状态、显示「将提交 N 个任务，另有 M 个会跳过」、无可提交任务时确认按钮置灰）、手动执行里「已执行成功仍要正式重跑」的二次确认（新增 `rerun-dialog`，说明最近成功时间并提示可改勾仅查询）、替换导入备份、删除所选记录与删除筛选结果（统一走 `askConfirm`，危险操作按钮为红色）。

单元测试 `python -m unittest discover -s tests -q` 共 144 项通过（4 项 Linux 专用测试在 Windows 跳过），其中模板与脚本联动测试新增校验 `run-all-dialog`、`run-all-form`、`run-all-account`、`run-all-tasks`、`run-all-hint`、`run-all-error`、`run-all-submit`、`rerun-dialog`、`rerun-message`、`rerun-warning`、`rerun-submit`、`confirm-dialog`、`confirm-message`、`confirm-submit` 以及 `openRunAll`、`renderRunAllTasks`、`runAllTaskState`、`openRerun`、`runTask`、`askConfirm`，并断言脚本里已不存在 `window.confirm` 与 `window.alert`。

前端：`node --check cmcc_app/static/app.js` 通过。后端接口与 1.6.4 相同，未改动。

发布：`docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false -t rain5201/cmcc-daily:1.6.5 -t rain5201/cmcc-daily:latest --push .` 推送成功，两个标签指向同一索引 `sha256:ec037d20…`（AMD64 `sha256:5c9177ca…`，ARM64 `sha256:54e80f94…`）；删除本地构建的临时标签后从 Docker Hub 重新拉取并运行容器冒烟测试（通过），本地实例重建后镜像 ID 与该索引摘要一致。

## 1.6.4：一键执行与发布验证（2026-10-14）

新增：账号卡片「一键执行」（该账号全部可执行任务）与页面标题「一键执行全部」（所有账号已开启计划的任务／仅重试失败的任务）。

单元测试 `python -m unittest discover -s tests -q` 共 143 项通过（4 项 Linux 专用测试在 Windows 跳过），其中新增覆盖：一键执行只提交可执行任务（未配置话费会话时不提交话费查询）、同一任务已在执行或排队时跳过并返回原因、批量「已开启计划」按各账号计划提交、「仅失败任务」按最近一次正式执行结果选取（仅查询失败与重试成功都不算失败）、调度器未运行时返回 503、账号不存在时返回 404。

前端：`node --check cmcc_app/static/app.js` 通过；模板中新增 `batch-run`、`batch-dialog`、`batch-form`、`batch-hint`、`batch-error`、`batch-submit`，测试校验选择器与账号总览视图的归属。

真实运行验证（本地审核预览 0.0.0.0:18081，模拟执行不联网）：登录后 6 个虚构账号；「仅失败任务」提交 3 项（更新凭证的签到与评价有礼、部分异常的签到），无跳过；「已开启计划」提交 13 项并跳过 5 项正在执行或排队中的同一任务；对单个账号连续点击两次「一键执行」，第一次提交 3 项，第二次全部跳过并返回「…已在排队或执行中，请等它结束后再提交」。

发布：`docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false -t rain5201/cmcc-daily:1.6.4 -t rain5201/cmcc-daily:latest --push .` 推送成功，两个标签指向同一索引 `sha256:9b8ef649…`（AMD64 `sha256:901644a7…`，ARM64 `sha256:678bf277…`）；对该镜像再次运行容器冒烟测试并通过。

## 1.6.3：账号状态、按任务排队与发布验证（2026-10-14）

新增内容：账号健康标记与状态筛选、逐任务结果与附属步骤结果、按任务排队的手动执行（同一任务重复提交才拒绝）、已完成任务的重复执行提示（默认仅查询）、结构化结果记录（进程间白名单传递）、凭证变更后重新验证。

单元测试 `python -m unittest discover -s tests -q` 共 138 项通过（4 项 Linux 专用测试在 Windows 跳过），覆盖：任务周期（每日 / 每周一次 / 每周多天）、查询与执行分离、凭证失效与 App Token 兜底、话费 Cookie 独立、更换凭证后重新验证、同一账号不同任务排队与同任务去重、定时任务排队后推进时间、部分成功备份导入、结构化结果不进日志明文、模板与脚本的选择器联动。

前端：`node --check cmcc_app/static/app.js` 通过。

真实运行验证（本地审核预览 0.0.0.0:18081，模拟执行不联网）：登录后 6 个虚构账号状态正确；固定示例保持「正在执行 / 排队中」；对同一账号连续提交 周三拼图 → 202、周三拼图 → 400「周三拼图已在排队或执行中，请等它结束后再提交」、评价有礼 → 202；`pending_tasks` 同时列出两条排队任务，账号健康状态显示为排队中；等待约 40 秒后两个任务依次模拟执行成功，账号回到正常。

容器验证：`docker build -t cmcc-daily:test` 构建通过，`python tests/container_smoke.py` 通过（绑定挂载、UID/GID=10001:10001、UMASK=077、数据目录权限修复、登录、改密、账号增删改查、重启持久化、加密备份导入导出、运行时身份与能力、只读根文件系统、健康检查）。

发布：`docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false -t rain5201/cmcc-daily:1.6.3 -t rain5201/cmcc-daily:latest --push .` 推送成功，两次标签指向同一索引 `sha256:0dae2534…`（AMD64 `sha256:a9340b52…`，ARM64 `sha256:72e98ed4…`）；随后对该镜像再次运行容器冒烟测试，通过。

GitHub Actions：1.6.3 发布提交在 `Test and build` 工作流中通过（Linux 单元测试、镜像构建、容器内 bootstrap 测试与容器冒烟测试）。

已知限制：添加 Windows 防火墙入站规则的提权请求被系统拒绝，手机能否直接访问预览取决于本机防火墙是否已放行 18081（本机自测 `192.168.0.104:18081` 返回 200，未用其他设备实测）。

## 预览实例与示例数据（2026-10-13）

- README「界面预览」的四张截图由项目维护者提供，直接放入 `docs/images/`，未做任何像素修改（已用 SHA-256 与原图逐一比对）。
- 预览实例与截图统一使用完全虚构的数据：示例号码为 138/137/139 开头的 `13800000011`、`13700000022`、`13900000033`，凭证写成 `preview-token`，不使用任何真实手机号、真实 Token 或真实通知地址。
- 种子脚本固化为 `tools/preview_seed.py`（默认 3 个示例账号、25 天约 160 条记录，含日志正文，所有计划关闭，不联网），文档中也写明了这条约定；`python tools/package_feiniu.py` 负责生成带截图的飞牛部署包。
- 新增 `tests/test_docs.py`：校验 README 与各文档引用的本地图片、链接真实存在，四张预览截图确实被 README 引用。测试总数 105 → 108。

## 1.6.1 → 1.6.2：手机端底部导航（2026-10-13）

- 现象：桌面浏览器有侧边栏，手机浏览器打开没有侧边栏，也无法切换到执行记录。原因是原有样式在 `@media(max-width:420px)` 下直接隐藏侧栏（`.sidebar{display:none}`），却没有提供替代导航，而多数手机竖屏宽度正好落在这个区间。
- 变更：新增底部导航 `#mobile-nav`（账号总览 / 执行记录），基础规则隐藏，仅在同一个 420px 断点内以固定底栏显示；同断点内同时隐藏冗余的面包屑、把顶栏按钮右对齐、给 `main` 增加 `padding-bottom:92px` 避免被底栏遮挡；脚本改为统一绑定 `[data-view]`，侧栏与底栏共用同一套切换与高亮逻辑（421–760px 仍是图标侧栏）。
- 新增测试：底栏存在于应用视图内、包含两个导航项、脚本使用 `[data-view]` 绑定、CSS 基础规则为 `display:none`、且唯一的 420 断点内同时包含 `.sidebar{display:none}`、`.mobile-nav{display:flex` 与 `main{padding-bottom:`；测试总数 103 → 104，本地与 Linux 容器内均通过，容器冒烟测试通过。
- 1.6.1 发布后手机上仍看不到导航：基础规则 `.mobile-nav{display:none}` 被放在媒体查询之后，同优先级下覆盖了媒体查询里的 `display:flex`。已把基础规则移到断点之前，并在测试里加入按视口宽度计算最终样式的解析器（400/420px 必须为 `flex`、600/1280px 必须为 `none`），反向验证过：改回旧顺序测试即失败。
- 按评审意见改为在 1.6.0 基础上重做：撤销 1.6.1 中隐藏面包屑、顶栏右对齐等附带调整，只保留底部导航本身（导航标记、`[data-view]` 绑定、底栏样式与 `main` 底部留白），顶栏与侧栏断点行为与 1.6.0 一致。1.6.1 已作废。
- 测试总数 104 → 105，本地与 Linux 容器内均通过；容器冒烟测试通过；已发布 `rain5201/cmcc-daily:1.6.2` 与 `latest`，摘要 `sha256:0c9bf155…`，手机浏览器实测确认正常。

## 1.6.0：执行记录分页筛选删除与界面优化（2026-10-13，已发布）

- 执行记录新增分页（每页 10/20/50/100 条，返回总数、总页数、当前页，越界自动收敛），筛选支持账号、任务、状态、触发方式、日期区间（按设置时区换算，结束日期含当天，非法日期忽略）。
- 关键字筛选同时匹配账号名、说明与日志正文；日志正文检索最多扫描最近 1000 条记录、每条读取开头 256 KiB，响应里返回 `scanned` 与 `truncated`，界面据此提示，避免大日志拖慢查询。
- 新增删除能力：勾选后批量删除（接口限制单次 500 条）、按当前筛选条件删除、日志弹窗内删除单条；只删除已结束的记录与对应日志文件，排队或运行中的记录保留并在返回里计数，避免删掉正在执行的任务。
- 界面优化：筛选栏与批量操作工具栏、全选本页（含半选状态）、选中行高亮、行悬停反馈、运行中行着色、分页控件、记录统计条（共 N 条 · 完成/失败/已停止/运行中/排队中）、账号下拉仅在账号变化时重建、卡片与按钮过渡动画、窄屏下筛选栏两列布局。
- 新增 `tests/test_history.py` 8 项测试：分页与越界、各维度筛选与非法参数、状态统计、关键字匹配记录与日志、单条与批量删除（含日志文件清理）、保留进行中记录、按条件清理、鉴权与参数校验；总数从 89 项增至 97 项，本地全部通过。
- 按评审意见把执行记录改为独立界面：账号总览与执行记录各自成页，侧边栏切换并用 URL 哈希（`#accounts` / `#history`）记忆当前位置，执行记录有独立标题栏（计数、统计摘要、刷新），页面切换后自动回到顶部；隐藏视图不再重复渲染，手动执行后自动跳到执行记录并打开日志。
- 视图拆分曾出现缺陷：拼装模板时按字符下标截取，前一处插入导致偏移，`#accounts-view` 未正确闭合、执行记录卡片被挤到视图外，表现为首页两个区块堆叠、记录页空白。已从上一提交恢复模板并改用锚点插入，同时补上基于 `HTMLParser` 的结构测试（标签闭合、两个视图同级、账号元素只在账号视图、筛选/分页/删除元素都在记录视图），避免再次静默破坏结构。
- 顺带修正前端状态初始化：`pendingAccounts`、`runsLoaded` 在 state 中显式声明；账号总览首次加载会取一次记录用于「最近执行」统计，之后只在记录视图刷新列表。
- 测试总数 99 → 102，本地与 Linux 容器内均通过；容器冒烟测试通过；用新镜像重建预览实例（端口 18081，172 条示例记录）并对实时页面做结构校验通过。
- 账号总览新增搜索：本地即时过滤（不请求服务端），匹配名称、完整手机号、掩码形式与号码片段，显示匹配数量与「清除」，无匹配时给出独立提示；新增账号后自动清空搜索以便直接看到新账号。新增结构测试覆盖输入框、清除按钮与无匹配提示所在视图，测试总数 102 → 103。
- 评审通过后已发布 `rain5201/cmcc-daily:1.6.0` 与 `latest`，多架构索引摘要 `sha256:1ffe627ca54436201c068a7ef47937cd7064a1233b1249915c220a48457363cb`（AMD64 `019c77ee…`、ARM64 `344576ee…`）；提交已推送到仓库，本地实例与源码同步。

## 1.5.0：去掉重复的周三拼图选项（2026-10-13）

- 现象：账号弹窗里「任务计划」已有「周三拼图」任务，签到附加选项里又有「周三充值日拼图」，同时开启会跑两次。
- 变更：签到附加选项只保留领取累计/连签奖励、AI 豆任务、拓展活动；`options.wed` 在读取与保存时统一按 `False` 处理（旧账号无需手工修改，worker 不再传 `--wed`），周三拼图只由独立任务触发。界面同步给三项附加步骤加上说明文字。
- 新增测试：`normalize_account` 忽略 `wed=True`、`worker.task_args` 不产生 `--wed`、旧数据库行读取后同样视为关闭；共 89 项测试在本地通过。
- 已发布 `rain5201/cmcc-daily:1.5.0` 与 `latest`，多架构索引摘要 `sha256:14e01fbfa0ffebeb58ed12ac95b336051c473678cd045959a5a8d494167a4664`；本地与 Linux 容器内 89 项测试、容器冒烟测试（AMD64）均通过。

## 1.4.1：页面版本号与静态资源版本（2026-10-13）

- 现象：升级镜像后浏览器仍显示旧的账号弹窗。复核确认服务端已返回新页面与脚本，原因是页面在升级前加载，弹窗结构留在浏览器内存中，属于使用「刷新」即可解决的问题。
- 变更：页面底部显示当前镜像版本（`独立自托管 · v1.4.1`），`styles.css` 与 `app.js` 地址带 `?v=<版本>`，升级后必然重新加载；新增测试校验页面带版本参数、含运行时版本号且不再硬编码旧版本。
- 容器冒烟测试的 HTTP 超时从 5 秒放宽到 20 秒：高负载下备份导入偶发超过 5 秒（服务端日志已记录完成），属测试阈值，不是功能缺陷；复跑通过。
- 已发布 `rain5201/cmcc-daily:1.4.1` 与 `latest`，多架构索引摘要 `sha256:494bbb13086e67e92e62c5f89da384267687a31703592c5d699b3f64e4333064`（AMD64 `23bd43c7…`、ARM64 `9d3545d6…`）；本机实例已升级到该镜像并确认服务端返回新界面。

## 任务独立计划与通知标题 1.4.0（2026-10-13）

- 每个任务（每日签到、周三拼图、评价有礼、话费查询）拥有独立的开关、时间与频率（每天，或每周指定星期），解析与校验在 `normalize_schedule`，账号表新增 `schedules` 列；旧库启动时自动 `ALTER TABLE` 补列，旧记录按原来的 `enabled`/`schedule_time`/`next_run` 还原为每日签到计划，无需手工迁移。
- 调度器改为按任务判断：到点才排队，账号忙时保留原定时间在下一个检查周期补跑，不会丢任务；`enabled`/`schedule_time`/`next_run` 保留为兼容镜像字段，始终由任务计划推导。
- 通知标题统一为「手机号（中间四位 `*`）+ 任务名」，例如 `138****9090 移动评价有礼`、`184****9527 移动签到通知`；正文不再重复尾号行，测试通知使用「手机号 + 通知测试」。
- 备份导入导出带上每个任务的计划，换服务器后周计划等设置原样保留（`_account_body` 携带 `schedules`）。
- 测试从 79 项增加到 86 项，新增：任务计划校验（频率、星期、时间）、每任务独立到点排队、每周只在选中星期触发、排队失败后补跑、旧库单计划还原、通知标题掩码与正文、备份保留任务计划、界面控件与脚本选择器一致、容器冒烟中的计划字段读写。本地 Windows 与 Linux 容器内 86 项均通过。
- 容器冒烟测试在 AMD64 与 ARM64（模拟）通过，覆盖任务计划存取、备份往返、权限初始化、非 root、只读根文件系统与健康检查。
- 已发布 `rain5201/cmcc-daily:1.4.0` 与 `latest`，多架构索引摘要 `sha256:a2c40fc2313a55bf49e7e60790f83217ea6f4d0d125670bb193e18f718413601`，含 `linux/amd64`（`a0631fef…`）与 `linux/arm64`（`d9755ed4…`）。本机测试实例已升级到该镜像，账号与执行记录保持。
- 未做浏览器点击式验证：界面接线由测试保证，页面、脚本与样式通过实际服务响应校验。

## 加密备份与迁移 1.3.0（2026-10-13）

- 新增 `cmcc_app/backup.py`：备份文件为 JSON 信封 + AES-256-GCM 密文，密钥由备份密码经 PBKDF2-HMAC-SHA256（300000 次）派生，格式、版本与导出时间参与认证；备份内容含账号凭证、通知配置、计划、JWT 缓存、执行记录与日志。
- 新增网页接口 `POST /api/backup/export`（需登录、CSRF 与当前管理密码，失败次数沿用登录限速）与 `POST /api/backup/import`（multipart 上传，按合并/替换导入，可恢复管理密码与执行记录）；`MAX_CONTENT_LENGTH` 相应放宽到 8 MiB，其余接口不变。
- 导入前先整体校验再写入：手机号、Token、选项等格式不合法时不会留下部分账号；排队或运行中的任务不允许替换导入；排队与未知状态的记录不会导入，避免在新实例上意外触发任务。
- 新增 `tests/test_backup.py` 13 项测试：加解密往返、错误密码、元数据篡改、非本应用文件、备份密码强度、导出鉴权与限速、导出文件不含明文凭证、合并与替换导入、跨实例迁移（账号/凭证/计划/JWT 缓存/执行记录/日志、原密码生效与新实例密码失效）、未恢复密码时会话保持、导入内容不泄露凭证。
- 本地 Windows 与 Linux 容器内均通过完整 77 项测试；容器冒烟测试新增「导出加密备份 → 删除账号 → 替换导入 → 账号与通知凭证恢复」流程，在 AMD64 与 ARM64（模拟）绑定挂载下均通过，同时保持权限初始化、非 root、只读根文件系统与健康检查验证。
- 已发布 `rain5201/cmcc-daily:1.3.0` 与 `latest`，多架构索引摘要 `sha256:4f785cd260febaf3c0a633e02d8025e408da10ae0ea05099807c29d4b5705902`，含 `linux/amd64`（`b663911b…`）与 `linux/arm64`（`dac862fb…`）。本机测试实例已升级到该镜像，升级后账号与执行记录正常。
- 按摘要从 Docker Hub 拉取已发布的 AMD64 镜像后再次运行容器冒烟测试通过；推送触发 GitHub Actions `Test and build`（提交 `8931031`）在 ubuntu-latest 上通过 Python 测试、镜像构建与容器冒烟测试。
- 前端新增界面接线测试：备份相关控件与脚本选择器一致，且 `app.js` 中所有 `$("#id")` 选择器都能在页面中找到对应元素。
- 未完成：ARM64 模拟环境下 `test_backup.py` 的整套单元测试因 PBKDF2 在 QEMU 下耗时过长（13 项超过 20 分钟）被中止，改以 ARM64 容器冒烟测试覆盖真实加解密往返；未做浏览器点击式验证，页面结构、脚本与样式通过实际服务响应校验。

## 自动数据权限初始化 1.2.0（2026-10-13）

- 新增 root 启动初始化，自动修复绑定目录和已有文件的所有者及所有者读写权限，再切换到 `PUID:PGID` 运行；支持 `UMASK`，账号数据库保持 `0600`。
- AMD64 容器内 64 项单元测试全部通过；Windows 64 项测试通过（4 项 Linux/root 权限测试跳过）。ARM64 模拟容器内 6 项新增权限测试通过，覆盖路径限制、符号链接不跟随、硬链接拒绝、已有文件权限修复和配置校验。
- 容器冒烟测试使用 Docker 管理的临时 Linux 目录作为真实 bind mount，预先将目录、`app.lock`、密码文件及缓存设为 root 所有且模式 `000`。验证自动修复、原密码保留、账号增删、修改密码、重启持久化、缓存内容保留、实际网页进程 UID/GID/UMASK、只读根文件系统、健康检查，以及默认网页进程 capabilities 清零。
- AMD64 上默认 `10001:10001/077`、自定义 `12345:12346/022`、root `0:0/000` 三种配置通过；root 场景使用命名卷，其余使用绑定挂载。ARM64 默认配置的绑定挂载冒烟测试通过。
- ARM64 首次冒烟测试在密码修改请求触发原 5 秒超时，启动及权限修复已成功；将 ARM64 模拟测试的 HTTP 超时设为 30 秒后复测通过。未调整应用密码散列算法或服务端超时。
- 另验证旧版缺少 capabilities 的配置、非法 PUID、非法 UMASK、将 CMCC_DATA_DIR 指向镜像系统目录均明确失败；三份 Compose 配置均通过 Docker Compose 解析。
- 已推送 `rain5201/cmcc-daily:1.2.0` 与 `latest`，多架构索引摘要 `sha256:3c8f61f2e8cd45e476d92d6b48612bfdd6a0968d7bb4c4d6ec7c30036908dd3c`。匿名令牌查询确认两标签摘要一致且均含 AMD64/ARM64；从仓库按摘要拉取后的 AMD64 绑定挂载冒烟测试通过。未连接用户 NAS；现有 Windows 本地实例未重建。

## 飞牛绑定挂载配置（2026-10-13）

- 按要求将飞牛镜像标签改为 `latest`，并设置 `pull_policy: always`。联网查询 Docker Hub 的 `latest` 多架构摘要为 `sha256:ec954c71a822d83aaffb70b140230c9699b67848d2cf80f117879768a016e308`，与此前发布的 `1.1.1` 摘要一致；仅查询远端镜像，未操作 NAS。
- `docker compose --env-file .env.example -f compose.fn.yaml config --format json` 校验通过：`/data` 使用 `bind`，宿主机路径为 `/vol1/1000/docker/cmcc-daily/data`（部署时替换），`create_host_path` 为 `false`，已移除顶层命名卷声明。
- 新增 `docs/FEINIU.md`，说明停止旧容器后复制完整 `/data`、设置 `10001:10001` 权限、在原项目切换挂载以及回退步骤；README 同步更新。
- 本次仅修改部署配置和说明，未更改镜像或应用代码。未连接飞牛 NAS，未在 NAS 上执行数据迁移；现有 Windows 本地实例未重建。

## 已完成

- `python -m unittest discover -s tests -v`：40 项测试，覆盖账号配置、敏感字段保留与隐藏、登录鉴权、CSRF、登录限速、每日计划、重复排队、进程超时与取消、重启恢复、JWT 持久化和上游兼容修改。
- 任务执行器测试会实际创建本地 Python 子进程，验证成功、失败、超时和终止，子进程只运行模拟代码。
- 实际启动 Waitress 服务，经 HTTP 验证登录、添加账号、查询账号、删除账号和健康检查。
- `pip check`：依赖关系正常。
- Python 编译检查、前端 JavaScript 语法检查通过；Compose YAML 可解析。
- 5 个独立上游脚本的 `--help` 均可正常运行。
- 浏览器已打开实际登录页面，并读取到页面内容。
- AMD64 和 ARM64 镜像构建成功；两种架构均通过容器冒烟测试，包括登录、账号管理、重启持久化、非 root、只读文件系统与健康检查。ARM64 使用 Docker Desktop 模拟执行。
- AMD64 和 ARM64 容器中的 40 项测试均通过。ARM64 首次完整复测出现一次子进程状态失败，随后该组及完整套件复测通过，未能复现；测试现已在再次失败时输出详细结果和模拟任务日志。
- Docker Hub 已发布 `rain5201/cmcc-daily:1.0.0` 和 `latest`；匿名访问已验证，远端镜像已成功拉取。两标签摘要均为 `sha256:45f06b44196d46ed63aa6b551fe3f74d304e0fdc67357b14a521dd11ea933836`。（2026-10-13 更新：`latest` 已改为指向 `1.1.1`。）

## 尚未完成

- 未在真实 ARM64 NAS 硬件上运行；GitHub Actions 工作流尚未提交到远程执行。本次发布使用本机 Docker Desktop。
- 浏览器端的自动点击、响应式布局检查与截图未纳入自动验证，网页交互与视觉效果以人工检查为准。
- 没有使用真实中国移动凭证，未实测在线签到、奖励领取或通知发送。上游接口是否仍可用需使用者自行配置账号后确认。

部署后建议先查看容器健康状态，然后添加本人账号并选择「仅查询状态」，确认登录和活动状态正常后再开启计划。
- 本地镜像 `cmcc-daily:1.1.0` 已构建并验证：52 项单元测试在 Windows 与容器内通过；容器冒烟测试覆盖登录、修改管理密码、旧密码失效、重启后密码与账号数据保持、Webhook 配置、非 root、只读文件系统与健康检查；另用独立临时容器确认未设置 `CMCC_WEB_PASSWORD` 时默认密码 `123456` 可登录。
- 2026-10-13 源码托管到 GitHub 仓库 [Rain520/cmcc-daily](https://github.com/Rain520/cmcc-daily)（分支 `main`，44 个文件，提交信息使用 GitHub noreply 邮箱，`.env`、数据卷与 `dist` 未纳入版本库）；推送后 `Test and build #1`、`#2` 均在 GitHub Actions 上通过；随后按要求把仓库可见性改为私有。
- 2026-10-13 发布 `rain5201/cmcc-daily:1.1.1` 与 `latest`：多架构索引摘要 `sha256:ec954c71a822d83aaffb70b140230c9699b67848d2cf80f117879768a016e308`，匿名 token 校验两个标签均返回 `linux/amd64`、`linux/arm64`；拉取远端镜像后 AMD64 冒烟测试与 ARM64 模拟测试通过；本地实例已改为运行该发布镜像。
