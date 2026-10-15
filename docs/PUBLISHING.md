# 发布容器镜像

## 1.6.10（2026-10-15）

已通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.10` 与 `latest`，支持 `linux/amd64`、`linux/arm64`。仅秒杀新增从账号卡片移除的显示设置，主签到成功后附加步骤不再改变整体状态或触发补跑；同时包含 1.6.9 的一键执行排除秒杀、AI 豆外部合作区过滤和错误日志修复。升级自动创建卡片显示状态表，并修正有明确主签到成功证据的旧结果。

两个远端标签均已核对，索引摘要为 `sha256:93745ba6c51b1af7b850a5160fcfbc834ac92a140c9f6792882665245b76c721`：AMD64 清单 `sha256:35a13ab0d900909bdddeaf4decb67142ab93e6264f3240aa2dea65f4f09e2f47`，ARM64 清单 `sha256:fe23e756b0a10b522b779737850ad316054c13fa13252d6e5b31449e9a499fdb`。

发布前 202 项单元测试（Windows 跳过 4 项 Linux 专用测试）、JavaScript 语法检查、容器冒烟及虚构预览页面验证完成；发布时另在 Linux 容器内运行 6 项启动与权限测试，全部通过。结论见 [验证记录](VALIDATION.md)。本地正式实例已使用同一份源码的本地标签 `cmcc-daily:local-1.6.10`，继续保留原数据卷、配置与 18080 端口。

发布后按上述索引摘要从 Docker Hub 拉取 AMD64 镜像，再运行 `tests/container_smoke.py`，权限修复、登录改密、重启持久化、加密备份、只读根文件系统和隔离预览全部通过；预览还验证了主签到成功保留 AI 豆部分成功子结果、仅秒杀可移除及日志保留。ARM64 已完成构建与远端清单核对。本次沿用源码直接推送 `main` 的流程，未另外创建 GitHub Release。`python tools/package_feiniu.py` 已生成 `dist/cmcc-feiniu-1.6.10.zip` 与 `dist/cmcc-feiniu-latest.zip`，包完整性和图片引用校验通过。

本次多架构发布命令：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.10 -t rain5201/cmcc-daily:latest --push .
```

## 1.6.9 本地审核记录（2026-10-15，合入 1.6.10 发布）

修复所有一键执行模式误提交秒杀；AI 豆跳过整个外部合作区（包括待领奖），保留正常跳过原因，补全领奖、补领和任务接口失败的日志与计数。四处版本声明已同步为 `1.6.9`。

本地使用 `docker compose -f compose.preview.yaml up -d --build` 构建 `cmcc-daily:preview` 并完成隔离预览。随后按用户授权，将同一已测试镜像以 `cmcc-daily:local-1.6.9` 更新到本机正式实例，继续使用原数据卷和 18080 端口，当时在 `.env.local` 固定该本地标签。1.6.9 没有单独发布远端标签，相关改动已包含在 1.6.10 中；本地实例随后也升级为 1.6.10。验证详情见 [验证记录](VALIDATION.md)。

## 1.6.8（2026-10-15）

已发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.8` 与 `latest`：秒杀活动清单改由服务端维护并固定让全部账号参与，日志弹窗新增「复制日志」；AI 豆等链路中需要真实操作、库存不足、已达上限、未到活动窗口等良性结果改为成功/跳过，真正的接口与网络错误仍告警，已完成结果不再被 pending 范围重跑。支持 `linux/amd64`、`linux/arm64`。

两标签索引摘要：`sha256:f8fda4288d554de2eff1fad3c90d7fc8922b35b713d29b4dcc240f7808d38134`（AMD64 清单 `sha256:3927eba2daa0da3506e2eb1d685fdcad8acc4056f321246faabe9c148d2559f9`，ARM64 清单 `sha256:04673d2f97184f851302a75423ebf183ab804999664d109dc56fb6801a83d4cb`）。发布前 184 项单测通过（4 项 Linux 专用测试在 Windows 跳过），`node --check`、容器冒烟及 Docker 审核预览检查通过；发布后从 Docker Hub 拉取 1.6.8 并再次运行容器冒烟测试通过。

## 1.6.7

已于 2026-10-15 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.7` 与 `latest`：新增独立的「秒杀任务」页（活动卡片：开抢时间精确到秒、提前量、重试间隔、最大尝试次数、活动日；全部账号自动参与，到点并行开抢、支持手动执行与结果摘要/复制），秒杀作为第五种任务接入队列、记录、筛选与一键执行；活动卡片的提前量／重试间隔／最大尝试次数会传给抢券脚本，并在开抢前约 1 分钟自动排队（修复了此前这些配置不生效、保存时被丢弃的问题）；账号总览在账号有秒杀记录时不再整页渲染失败，秒杀结果弹窗的说明文字不再被账号按钮压住，一键执行弹窗补齐秒杀并改为按最近结果判定（与后端一致）。支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:6f32710e55551074aa7d8b484b0a3e8d8ca950f169524b815a802b6551b3617e`（AMD64 清单 `sha256:63820cdcb9ac432b620d43491fdb2cda66d47e334ca38008b6ea4538e412e7c6`，ARM64 清单 `sha256:77732b06a2bc3ff15b929f0f31def6321bae34891d065752504adb7dc68ce44c`）。发布前在本机 Docker 审核预览里自检（账号总览、秒杀任务页、秒杀结果弹窗、一键执行弹窗，桌面与手机宽度各看一遍），发布后从 Docker Hub 拉取该标签并运行容器冒烟测试（通过）。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`；本次 Docker Hub 鉴权域名直连被重置，设置 `http://127.0.0.1:7890` 代理后推送成功）：

```bash
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.7 -t rain5201/cmcc-daily:latest --push .
```

自 1.6.7 起，测试版一律先在本机 Docker 里起审核预览（`docker compose -f compose.preview.yaml up -d --build`，容器内 `CMCC_PREVIEW=1`，只用虚构数据、任务本地模拟、不联网），确认后再执行上面的多架构发布；在得到确认前不推送镜像。

## 1.6.6

已于 2026-10-14 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.6` 与 `latest`：AI 豆里「需要真实操作而跳过」的任务不再把整次执行标成部分成功（整次保持「已完成」，子标签改为中性的「部分跳过」，真正失败与部分成功仍保留告警）；两个一键执行弹窗新增执行范围选项（只执行未完成和出错的、全部执行、仅已开启计划），并按任务逐条标注「尚未执行过／上次执行失败／上次部分成功／本次计划未执行／重复执行（最近一次已成功）／执行中或排队中跳过／未配置话费会话跳过」，提示行给出提交数与重复执行数。批量接口的 `failed` 由 `pending` 取代（涵盖从未执行过的任务）。支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:9b3d32fb814dfa35d3b9074d99e1c6c92edef10346bbd725da387a483bccc08f`（AMD64 清单 `sha256:1b1b0dd8d293cfd710b7d05febe01ddcf3d0d01c24e1bbf50f3e28ba2a694318`，ARM64 清单 `sha256:6a8188313c8871ca9e7a0d492539aeb4afca927b3330680a39f380e715530930`）。发布后从 Docker Hub 拉取该标签并运行容器冒烟测试（通过），本地实例当前运行的镜像 ID 与该索引摘要一致。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.6 -t rain5201/cmcc-daily:latest --push .
```

## 1.6.5

已于 2026-10-14 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.5` 与 `latest`：把浏览器原生确认框全部换成页面内弹窗——账号卡片「一键执行」改为与「一键执行全部」同款面板（逐条列出可提交任务与跳过原因、无可提交任务时确认按钮置灰）、手动执行里「已执行成功仍要正式重跑」新增二次确认弹窗、替换导入备份与删除记录统一走同一个确认弹窗（危险操作按钮为红色）。前端不再出现 `window.confirm`／`window.alert`，后端接口与 1.6.4 相同。支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:ec037d20f3fc45e6e1e50f244a10c32b5c86321436829e07c364642c876c1be3`（AMD64 清单 `sha256:5c9177cad59c674fdb5967134692d9bb76e300d0fe7f61028f0bab517b3bf247`，ARM64 清单 `sha256:54e80f94b301ac48a4d36a959d0c3229166e6059a1911378a6ac26cbf90920bd`）。发布后从 Docker Hub 重新拉取该标签并运行容器冒烟测试（通过），本地实例当前运行的镜像 ID 与该索引摘要一致。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.5 -t rain5201/cmcc-daily:latest --push .
```

## 1.6.4

已于 2026-10-14 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.4` 与 `latest`：新增账号卡片「一键执行」（一次提交该账号下全部可执行、且未在执行或排队的任务）与页面标题「一键执行全部」（所有账号已开启计划的任务，或仅重试失败的任务）。批量提交与单个任务共用同一队列、按发起顺序串行执行，跳过的任务会在提示与执行记录中说明原因；话费查询需要先配置话费会话。支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:9b8ef649dc5757f8013effab0cfe86af03b3ed2622f0c1d634c543f6f8d31dd1`（AMD64 清单 `sha256:901644a7bbcc56e98cc496d8ba5dc1232e4ac6fdb7189cb1c3239f5c1fe08678`，ARM64 清单 `sha256:678bf2775ae2e1303e4dfb3a50741e0d784d2e3afb944f2d5f406ec6a3753cfc`）。发布后已用该镜像运行容器冒烟测试，并在本机审核预览中验证批量提交行为。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.4 -t rain5201/cmcc-daily:latest --push .
```


## 1.6.3

已于 2026-10-14 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.3` 与 `latest`：新增账号健康状态标记（正常、执行中、排队中、任务异常、凭证失效、尚未验证）与状态筛选、逐任务执行结果与日志入口、附属步骤结果（累计奖励／AI 豆／拓展活动），手动执行改为按任务排队（只拒绝同一任务重复提交，其他任务入队串行执行），已完成的任务再次执行会提示并默认「仅查询」，并新增结构化任务结果记录与凭证变更后重新验证。支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:0dae25342291a57b1a13ef32f0bc777fd71187d977d7f770ddcf9e2e122ed175`（AMD64 清单 `sha256:a9340b522faa1d4b7e62ec814c990bef26cab2bcfecddad6ccff6bc87bb7afd4`，ARM64 清单 `sha256:72e98ed43d8f3a5ebe7512bca8156ffe0e2367c224f389ea1c0841730a80953e`）。发布后已用该镜像运行容器冒烟测试（绑定挂载、UID/GID、权限修复、登录、改密、增删改查、重启持久化、加密备份导入导出、只读根文件系统、健康检查）。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.3 -t rain5201/cmcc-daily:latest --push .
```


已于 2026-10-13 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.2` 与 `latest`：在 1.6.0 基础上仅新增手机底部导航（≤420px 原本隐藏侧栏后没有任何导航），不再改动顶栏与面包屑，并包含 1.6.0 的执行记录分页/筛选/关键字搜索/批量删除与独立记录界面、账号列表搜索，以及 1.5.0 去掉重复周三拼图选项、1.4.1 的页面显示当前镜像版本、静态资源带版本参数（升级后不会继续显示旧界面），并包含 1.4.0 的新增每个任务的独立计划（每天或每周指定星期、单独开关与时间）和「手机号（中间四位隐藏）+ 任务名」的通知标题，保留加密备份导出与导入、自动数据目录权限初始化、`PUID/PGID/UMASK`、Webhook 推送、网页修改管理密码与通知测试，支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:0c9bf15514ae88accb41c41761d3b7191660249ba620c140cd4d67c9284e1522`（AMD64 清单 `sha256:cd813fad991f98d1c4305f4309d449d54304fbc7fdb30034e907512a3af23ee2`，ARM64 清单 `sha256:d4cbad59774a127e726ad6f4556224a552960ff3eea9fdb9861684c636853282`）。`1.6.1` 因底栏样式被自身基础规则覆盖（手机上仍看不到导航）已作废，请勿使用；1.6.0 摘要 `sha256:1ffe627ca54436201c068a7ef47937cd7064a1233b1249915c220a48457363cb`（AMD64 清单 `sha256:019c77ee462c3a9aad1336f294faf1e137d66c5ab568c49fc124f29e7a5294f0`，ARM64 清单 `sha256:344576ee1083de2004be09e69b6536998b431b0c7e821fc27dfde64f18bd4d73`）。更早标签仍可拉取：`1.5.0`（`sha256:14e01fbfa0ffebeb58ed12ac95b336051c473678cd045959a5a8d494167a4664`）、`1.4.1`（`sha256:494bbb13086e67e92e62c5f89da384267687a31703592c5d699b3f64e4333064`）。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.3 -t rain5201/cmcc-daily:latest --push .
```

飞牛可直接使用完整 `compose.fn.yaml`，确认绑定目录后部署；版本标签保持 `latest`，升级只替换 Compose 与镜像即可。`1.2.0` 初始化需要配置中的 `cap_add`，不要仅给旧 YAML 更换镜像标签。以下 GitHub Actions 流程保留用于后续自动发布，不是使用现有镜像的前置条件。

## 使用 GitHub Actions 构建

本地电脑不需要安装 Docker。将本项目源码放到自己有管理权限的 GitHub 仓库中，包含 `.github/workflows/publish.yml`。不要上传 `.env`、`data`、真实账号配置或 JWT 缓存。

在该仓库的 Actions 中选择 **Publish container image → Run workflow**，填写：

| 输入 | 说明 |
| --- | --- |
| `registry` | `ghcr.io` 或 `docker.io` |
| `image_name` | 小写的 `账号或组织/cmcc-daily` |
| `tag` | 版本标签，例如 `1.6.6` |
| `publish_latest` | 是否同时更新 `latest` 标签 |

工作流先执行 Python 测试、Linux 镜像构建和容器冒烟测试，再发布 AMD64 与 ARM64 镜像。推送后还会拉取发布的摘要并再次验证 AMD64 容器。ARM64 镜像经过交叉构建，工作流不会运行 ARM64 容器测试。

### GHCR

`image_name` 的账号或组织必须与 GitHub 仓库所有者一致。工作流使用 GitHub 自动生成的 `GITHUB_TOKEN`，无需把个人令牌写进源码。

GHCR 新包通常默认为私有，源码仓库公开并不意味着镜像公开。要让 NAS 无需登录即可拉取，需要在 GitHub 的该 Package 设置中将可见性改为 Public；保留私有时，NAS 需要配置具有拉取权限的仓库凭证。

### Docker Hub

在源码仓库的 **Settings → Secrets and variables → Actions** 配置：

- `DOCKERHUB_USERNAME`：具有目标镜像仓库推送权限的 Docker Hub 用户名；使用本人账号发布时填写 `rain5201`。
- `DOCKERHUB_TOKEN`：对应的访问令牌，需要写入权限。

目标镜像仓库及其公开/私有属性在 Docker Hub 中管理。不要将访问令牌发送到聊天、写进 YAML 或提交到 Git。

## 生成飞牛部署包

```bash
python tools/package_feiniu.py            # 当前版本号 + latest 各打一个包
python tools/package_feiniu.py 1.6.2      # 指定标签
```

产物为 `dist/cmcc-feiniu-<标签>.zip`，内含 `compose.yaml`、飞牛部署说明、完整 README 和 `docs/images/` 界面截图。

## 镜像发布后在飞牛部署

将 `compose.image.yaml` 上传到 NAS，保存为 `compose.yaml`，并在同目录建立 `.env`：

```dotenv
# 发布成功后使用；设置地址本身不会创建镜像。
CMCC_IMAGE=rain5201/cmcc-daily:1.6.6
CMCC_WEB_PASSWORD='替换成自己的初始密码（至少6位）'
CMCC_BIND_ADDRESS=0.0.0.0
CMCC_WEB_PORT=18080
TZ=Asia/Shanghai
```

在飞牛 Compose 中创建项目，或执行：

```bash
docker compose pull
docker compose up -d
```

该方式不需要上传源码、不需要 `Dockerfile`，也不在 NAS 本地构建。已有部署请保持相同的 Compose 项目名称，才能继续使用原来的命名数据卷。
