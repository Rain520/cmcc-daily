# 发布容器镜像

已于 2026-10-13 通过本机 Docker Desktop 发布 Docker Hub 镜像 `rain5201/cmcc-daily:1.1.1` 与 `latest`，包含通用 Webhook 推送（企业微信/钉钉/飞书自动适配）、网页修改管理密码、通知测试按钮，支持 `linux/amd64`、`linux/arm64`，匿名拉取已验证。

两标签摘要：`sha256:ec954c71a822d83aaffb70b140230c9699b67848d2cf80f117879768a016e308`（AMD64 清单 `sha256:a5409540a34c47a4aa78780de805afc9bf4e615f9a4953f8e2a34a5a7ed21e21`，ARM64 清单 `sha256:f6775c4adaf7ea9a4d15f5f7696683e0f80a45d6744a179b8665ef4c3b7193ea`）。更早的 `1.0.0`（`sha256:45f06b44...`）已被 `latest` 取代。

多架构发布命令（需要能访问 Docker Hub 的代理时先设置 `HTTP_PROXY`/`HTTPS_PROXY`）：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.1.1 -t rain5201/cmcc-daily:latest --push .
```

飞牛可直接使用 `compose.fn.yaml`，填写管理密码后部署。以下 GitHub Actions 流程保留用于后续自动发布，不是使用现有镜像的前置条件。

## 使用 GitHub Actions 构建

本地电脑不需要安装 Docker。将本项目源码放到自己有管理权限的 GitHub 仓库中，包含 `.github/workflows/publish.yml`。不要上传 `.env`、`data`、真实账号配置或 JWT 缓存。

在该仓库的 Actions 中选择 **Publish container image → Run workflow**，填写：

| 输入 | 说明 |
| --- | --- |
| `registry` | `ghcr.io` 或 `docker.io` |
| `image_name` | 小写的 `账号或组织/cmcc-daily` |
| `tag` | 版本标签，例如 `1.1.1` |
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
CMCC_IMAGE=rain5201/cmcc-daily:1.1.1
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
