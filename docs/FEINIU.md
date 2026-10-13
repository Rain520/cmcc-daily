# 飞牛 NAS 部署与数据迁移

镜像使用 `rain5201/cmcc-daily:latest`，通过 `pull_policy: always` 在每次 Compose 部署时拉取镜像，不会在后台定时升级。从 `1.2.0` 起支持自动数据目录权限初始化，以及 `PUID`、`PGID`、`UMASK`。

本次飞牛配置使用 `/vol1/1000/CMCC/data`，与用户提供的实际路径一致。其他部署请将 YAML 的 `source` 替换为自己的专用目录。以下终端命令在 NAS 的 SSH 终端执行，不是在 Windows PowerShell 中执行。

## 全新安装

已有绑定目录时，保留原路径并使用新版完整配置重新部署；只有从命名卷切换时，才需要使用后面的迁移步骤。

1. 选择本应用的专用数据目录，并在 YAML 中填写 `source`。Docker 会自动创建不存在的目录，无需提前执行 `chown`。
2. 在飞牛 Docker 的 Compose 中创建项目，粘贴项目根目录 `compose.fn.yaml` 的完整内容。部署包中对应文件名为 `compose.yaml`。拉取镜像后启动。
3. 访问 `http://NAS的IP:18080`。`CMCC_WEB_PASSWORD` 留空时，首次启动密码为 `123456`；登录后可在右上角修改，至少 6 位。

启动程序先以 root 初始化数据目录及已有文件的所有者和必要的读写权限，然后切换到 `PUID:PGID` 运行网页。默认 `10001:10001`，默认网页进程不保留 Linux capabilities。无需在 NAS 上手动创建同名用户。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `PUID` | `10001` | 网页和任务进程的用户编号；允许 `0` |
| `PGID` | `10001` | 运行用户组编号；允许 `0` |
| `UMASK` | `077` | 普通新建文件的权限掩码；也支持 `022`、`000` |

如确实需要与示例 MoviePilot 一样以 root 运行，可配置 `PUID: "0"`、`PGID: "0"`、`UMASK: "000"`。默认无需这样设置，自动权限初始化同样有效。密码、会话签名 JSON 以及账号数据库仍设置为仅所有者读写。

`PUID/PGID` 是镜像读取的环境变量，不要额外使用 Compose 的 `user` 覆盖启动用户。只有已自行准备目录权限时，才可显式指定非 root `user`，并确保它与 `PUID/PGID` 一致，此时跳过 root 权限初始化。

保留新版 YAML 中的 `cap_add`：`CHOWN`、`DAC_OVERRIDE`、`FOWNER` 用于修复数据权限，`SETUID`、`SETGID` 用于切换运行用户；`read_only`、`no-new-privileges` 和 `cap_drop: ALL` 同时保留。`PUID=0` 时应用保持 root 身份和已授予的能力。

## 启动报错：无法写入 /data/app.lock

`1.2.0` 已增加自动修复。升级时必须**同时替换完整新版 Compose 并拉取新版镜像**，仅使用旧 YAML 拉取 `latest` 会被旧配置的 `cap_drop: ALL` 阻止初始化和切换用户。

在原 Compose 项目目录中执行（或在飞牛界面拉取镜像并重新部署）：

```sh
sudo docker compose pull cmcc
sudo docker compose up -d cmcc
```

保持相同的 `source` 目录，启动会修复已有 `app.lock`、数据库、缓存和日志的权限，保留文件内容和原登录密码。修改 `PUID/PGID` 后也应重新部署，初始化会更新这些文件的所有者。

旧版镜像的手动修复方式如下。新版如仍出现权限错误，应先确认容器使用完整的新配置、挂载可写，并检查 NAS ACL 或存储是否支持更改所有者。

先在飞牛停止此容器，再在 NAS 的 SSH 终端执行下面命令。路径必须与当前 Compose 的 `source` 完全一致，并且是本应用的专用数据目录：

```sh
CMCC_HOST_DATA='/vol1/1000/CMCC/data'
sudo ls -ldn "$CMCC_HOST_DATA"
sudo chown -R 10001:10001 "$CMCC_HOST_DATA"
sudo chmod -R u+rwX "$CMCC_HOST_DATA"
```

第一条检查命令确认目录存在且路径正确后，再执行权限修改。`u+rwX` 给所有者读取、写入和必要的目录访问权限，同时修复已有的 `app.lock`、数据库、缓存和日志权限。无需删除 `app.lock` 或账号数据，也无需设置 `777`。

命令成功后在飞牛重新启动容器。如果仍报同样的错误，提供以下输出（只包含挂载路径和文件权限，不要提供 Token 或数据库内容），进一步排查实际挂载位置、容器运行用户或 NAS ACL：

```sh
CMCC_CONTAINER='替换为实际容器名'
sudo docker inspect "$CMCC_CONTAINER" --format '用户={{.Config.User}} {{range .Mounts}}{{if eq .Destination "/data"}}类型={{.Type}} 路径={{.Source}} 可写={{.RW}}{{end}}{{end}}'
sudo ls -ldn "$CMCC_HOST_DATA"
sudo ls -ln "$CMCC_HOST_DATA/app.lock"
```

## 已有命名卷迁移

先复制数据，再修改 Compose。迁移过程只复制旧数据，保留原命名卷用于回退。不要删除旧卷，也不要执行 `docker compose down -v`。

1. 在飞牛界面保留一份当前 Compose 配置，记录项目名。查出当前容器名：

   ```sh
   sudo docker ps -a --format 'table {{.Names}}\t{{.Image}}'
   ```

   下面的容器名必须替换为实际名称，目录同样替换为实际绝对路径：

   ```sh
   CMCC_CONTAINER='替换为实际容器名'
   CMCC_HOST_DATA='/vol1/1000/CMCC/data'
   sudo docker inspect "$CMCC_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}类型={{.Type}} 卷名={{.Name}} 路径={{.Source}}{{end}}{{end}}'
   ```

   确认类型为 `volume`，记录显示的完整卷名。它通常带 Compose 项目前缀，不能直接假定为 `cmcc-data`。

2. 创建目标目录并检查它为空。下面每条命令都应成功后再继续；如果目录已有文件，改用另一个新的专用目录，不要混合两套数据。

   ```sh
   sudo mkdir -p "$CMCC_HOST_DATA"
   sudo ls -la "$CMCC_HOST_DATA"
   ```

3. 等当前任务完成后停止旧容器，保留容器供复制使用，再复制整个数据目录。每条命令成功后再执行下一条：

   ```sh
   sudo docker stop --time 30 "$CMCC_CONTAINER"
   sudo docker cp -a "${CMCC_CONTAINER}:/data/." "$CMCC_HOST_DATA/"
   sudo ls -lan "$CMCC_HOST_DATA"
   ```

   `docker cp -a` 可从已停止的容器复制，并保留文件所有权；`/data/.` 会把目录内容直接放到目标目录。应能看到原来的 `cmcc.sqlite3`、`auth.json`、`session-key.json` 和 `cache/`、`logs/`、`runtime/`；若存在 SQLite 的 `-wal`、`-shm` 文件，也会一并复制。不要只复制数据库或只复制 Token 缓存。如果复制失败，先保持原配置，用 `sudo docker start "$CMCC_CONTAINER"` 恢复旧服务。

4. 编辑飞牛中**同一个 Compose 项目**，只替换 `services.cmcc.volumes` 为：

   ```yaml
       volumes:
         - type: bind
           source: /vol1/1000/CMCC/data
           target: /data
           bind:
             create_host_path: true
   ```

   `source` 与实际复制目录必须一致。同时删除文件末尾的顶层声明：

   ```yaml
   volumes:
     cmcc-data:
   ```

   升级到 `1.2.0` 时请使用更新后的完整 `compose.fn.yaml`，保留原来自己设置的端口和其他环境变量，并带上新的 `PUID/PGID/UMASK` 与 `cap_add`。不要新建第二个同时执行任务的实例。

5. 在飞牛界面重新部署该项目。使用终端管理 Compose 时，在原项目目录使用原项目名、原文件名执行 `sudo docker compose up -d`；若文件名不是默认的 `compose.yaml`，需加 `-f 实际文件名`。
6. 重新确认当前容器名，并检查 `/data` 挂载：

   ```sh
   CMCC_CONTAINER='替换为重新部署后的实际容器名'
   sudo docker inspect "$CMCC_CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}类型={{.Type}} 路径={{.Source}} 可写={{.RW}}{{end}}{{end}}'
   sudo docker logs --tail 50 "$CMCC_CONTAINER"
   ```

   挂载应显示 `bind`、正确的 NAS 目录和 `可写=true`。打开网页，使用迁移前的密码登录，确认账号、通知配置、计划和执行记录完整。迁移完成后保留旧卷，直到确认新部署正常且已有备份。

## 换服务器迁移

换 NAS 或重建实例时，用网页里的备份文件迁移，不需要重新添加账号或重新抓包。

1. 旧实例登录后打开右上角 **备份与迁移**，填写当前管理密码和备份密码，下载 `cmcc-backup-<时间>.json`。备份包含账号、App Token、通知配置、每日计划、JWT 缓存和执行记录，已用备份密码加密。
2. 在新服务器用**同一份 `compose.fn.yaml`** 部署并启动（`source` 指向新服务器上的数据目录），访问 `http://新NAS的IP:18080`，先用环境变量密码或默认密码 `123456` 登录。
3. 在新实例打开 **备份与迁移**，选择备份文件、填写备份密码。合并导入会跳过已存在的手机号；替换导入会清空新实例中的账号后导入。勾选「恢复备份中的管理密码」后即可继续使用旧实例的密码。
4. 检查账号、计划时间、通知渠道和执行记录，确认无误后再停用旧实例。

也可以直接复制数据目录：停止旧实例，把整个绑定目录复制到新服务器（保持文件所有者可写即可，启动时会自动修复权限），再启动新实例。这种方式会连日志文件一起带走，适合离线搬迁；使用网页备份更简单，且文件本身已加密。

## 回退与后续更新

若迁移后无法启动，先停止使用绑定挂载的容器，在原项目恢复备份的 Compose 配置并重新部署，即可回到保留的旧卷。若项目名发生变化，需将记录下来的完整旧卷名声明为外部卷，避免 Compose 创建新空卷。回退会恢复到迁移前的旧数据，不包含迁移后对绑定目录的修改。

以后更新镜像时保留相同的 `source` 目录，在飞牛界面拉取并重新部署，或在原项目目录执行 `sudo docker compose pull` 后执行 `sudo docker compose up -d`。备份时先停止服务，再备份整个宿主机数据目录，完成后启动服务。

Docker 官方参考：[绑定挂载配置](https://docs.docker.com/reference/compose-file/services/#volumes)、[容器文件复制](https://docs.docker.com/reference/cli/docker/container/cp/)。
