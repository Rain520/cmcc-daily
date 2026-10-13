# 发布容器镜像

已于 2026-10-13 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.6.1` 与 `latest`，手机窄屏改为底部导航（原本 ≤420px 隐藏侧栏后没有任何导航），并包含 1.6.0 的执行记录分页/筛选/关键字搜索/批量删除与独立记录界面、账号列表搜索，以及 1.5.0 去掉重复周三拼图选项、1.4.1 的页面显示当前镜像版本、静态资源带版本参数（升级后不会继续显示旧界面），并包含 1.4.0 的新增每个任务的独立计划（每天或每周指定星期、单独开关与时间）和「手机号（中间四位隐藏）+ 任务名」的通知标题，保留加密备份导出与导入、自动数据目录权限初始化、`PUID/PGID/UMASK`、Webhook 推送、网页修改管理密码与通知测试，支持 `linux/amd64`、`linux/arm64`。

两标签摘要：`sha256:50d1d4bd4c365768d919174f82b2881a3eafd0ce2391b12a52915ac396b16c7a`（AMD64 清单 `sha256:deddc1c7ba03e96de9207a08b6e5ae535d6bde72be3e4fa1b0a2f3a3b2867996`，ARM64 清单 `sha256:27d01777942ed1ec8ccda0acb5e8fbb95c20bf849542012db505f0220894ffa1`）。1.6.0 摘要 `sha256:1ffe627ca54436201c068a7ef47937cd7064a1233b1249915c220a48457363cb`（AMD64 清单 `sha256:019c77ee462c3a9aad1336f294faf1e137d66c5ab568c49fc124f29e7a5294f0`，ARM64 清单 `sha256:344576ee1083de2004be09e69b6536998b431b0c7e821fc27dfde64f18bd4d73`）。更早标签仍可拉取：`1.5.0`（`sha256:14e01fbfa0ffebeb58ed12ac95b336051c473678cd045959a5a8d494167a4664`）、`1.4.1`（`sha256:494bbb13086e67e92e62c5f89da384267687a31703592c5d699b3f64e4333064`）。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.6.1 -t rain5201/cmcc-daily:latest --push .
```

飞牛可直接使用完整 `compose.fn.yaml`，确认绑定目录后部署；版本标签保持 `latest`，升级只替换 Compose 与镜像即可。`1.2.0` 初始化需要配置中的 `cap_add`，不要仅给旧 YAML 更换镜像标签。以下 GitHub Actions 流程保留用于后续自动发布，不是使用现有镜像的前置条件。

## 使用 GitHub Actions 构建

本地电脑不需要安装 Docker。将本项目源码放到自己有管理权限的 GitHub 仓库中，包含 `.github/workflows/publish.yml`。不要上传 `.env`、`data`、真实账号配置或 JWT 缓存。

在该仓库的 Actions 中选择 **Publish container image → Run workflow**，填写：

| 输入 | 说明 |
| --- | --- |
| `registry` | `ghcr.io` 或 `docker.io` |
| `image_name` | 小写的 `账号或组织/cmcc-daily` |
| `tag` | 版本标签，例如 `1.6.1` |
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

## 镜像发布后在飞牛部署

将 `compose.image.yaml` 上传到 NAS，保存为 `compose.yaml`，并在同目录建立 `.env`：

```dotenv
# 发布成功后使用；设置地址本身不会创建镜像。
CMCC_IMAGE=rain5201/cmcc-daily:1.6.1
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
