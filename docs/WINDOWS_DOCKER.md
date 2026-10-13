# Windows 本机构建

这台电脑上的 Docker Desktop 为当前用户安装，Docker 可执行文件位于：

```text
C:\Users\R\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe
```

若新开的 PowerShell 已能直接运行 `docker`，不需要设置路径。否则仅为当前终端设置：

```powershell
$env:PATH = 'C:\Users\R\AppData\Local\Programs\DockerDesktop\resources\bin;' + $env:PATH
```

启动并确认 `docker version` 同时显示 Client 和 Server：

```powershell
docker desktop start
docker version
```

本机代理位于 `127.0.0.1:7890`。Docker Desktop 使用系统代理，但命令行构建的 Docker Hub 鉴权请求仍可能需要显式设置进程环境变量：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7890'
$env:HTTPS_PROXY = 'http://127.0.0.1:7890'
```

以下代理地址仅适用于这台已运行对应代理的电脑，NAS 不需要照抄。构建参数中的 `host.docker.internal` 让临时构建容器访问宿主机代理，不会成为最终镜像的环境变量：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 `
  --build-arg HTTP_PROXY=http://host.docker.internal:7890 `
  --build-arg HTTPS_PROXY=http://host.docker.internal:7890 `
  --tag cmcc-daily:test --load .

.\.venv\Scripts\python.exe tests\container_smoke.py
```

验证 ARM64 镜像时，在当前终端临时设置 `DOCKER_DEFAULT_PLATFORM=linux/arm64`，再运行同一个冒烟测试。AMD64 主机通过 Docker Desktop 的模拟器执行 ARM64 容器，不等同于真实 ARM NAS 的硬件验证。

通过测试后，使用已登录的 Docker Hub 账号推送：

```powershell
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false --sbom=false `
  -t rain5201/cmcc-daily:1.1.1 -t rain5201/cmcc-daily:latest --push .
```

若提示未授权，在自己的终端执行 `docker login -u rain5201`，按提示输入凭证，不要将令牌提交到项目或发到聊天。
