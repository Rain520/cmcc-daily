"""Initialize the container data mount, then exec the app with its runtime UID."""

import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def numeric_id(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 2147483647:
        raise ValueError(f"{name} 必须为 0～2147483647 的整数")
    return int(value)


def configured_umask() -> int:
    value = os.environ.get("UMASK", "077")
    if not re.fullmatch(r"[0-7]{3}|0[0-7]{3}", value):
        raise ValueError("UMASK 必须为三位八进制权限掩码，例如 077、022 或 000")
    return int(value, 8)


def data_directory() -> Path:
    path = Path(os.environ.get("CMCC_DATA_DIR", "/data"))
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("CMCC_DATA_DIR 必须为不含符号链接或 .. 的绝对路径")
    # Only operate within a separate data mount, never recursively chown the image.
    mount = next((p for p in (path, *path.parents) if os.path.ismount(p)), None)
    if mount is None or mount == Path("/") or mount.parts[1] in {"proc", "sys", "dev"}:
        raise ValueError("CMCC_DATA_DIR 必须位于独立的数据挂载中，例如 /data")
    return path


def repair_data_permissions(path: Path, uid: int, gid: int) -> None:
    """Repair owner access without following links or rewriting file contents."""
    path.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(path, flags)
    try:
        root_device = os.fstat(root_fd).st_dev

        def repair_fd(fd: int, directory: bool) -> None:
            info = os.fstat(fd)
            if info.st_dev != root_device:
                raise ValueError("数据目录包含其他文件系统，请移除嵌套挂载后重试")
            if not directory and info.st_nlink != 1:
                raise ValueError("数据目录包含硬链接，请将其复制为独立文件后重试")
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.fchown(fd, uid, gid)
            # Do not broaden group/other access or preserve setuid/setgid bits.
            mode = stat.S_IMODE(info.st_mode) & 0o1777
            desired = mode | (0o700 if directory else 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != desired:
                os.fchmod(fd, desired)

        repair_fd(root_fd, True)
        for _, directories, files, directory_fd in os.fwalk(
                ".", topdown=True, follow_symlinks=False, dir_fd=root_fd):
            for name in directories[:]:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    directories.remove(name)
                    continue
                fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    repair_fd(fd, True)
                finally:
                    os.close(fd)
            for name in files:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_fd)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError("数据文件在初始化期间发生变化，请先停止其他实例")
                    repair_fd(fd, False)
                finally:
                    os.close(fd)
    finally:
        os.close(root_fd)


def main() -> int:
    args = sys.argv[1:] or ["serve"]
    try:
        uid = numeric_id("PUID", 10001)
        gid = numeric_id("PGID", 10001)
        os.umask(configured_umask())
        initialize = args[0] == "serve"
        path = data_directory() if initialize else None
        if os.geteuid() == 0:
            if initialize:
                repair_data_permissions(path, uid, gid)
                print(f"数据目录权限初始化完成，运行用户 {uid}:{gid}", flush=True)
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
        elif (os.geteuid(), os.getegid()) != (uid, gid):
            raise ValueError("容器已以非 root 用户启动，PUID/PGID 必须与该用户一致；"
                             "自动修复权限请使用新版 Compose 的默认启动用户")
        if initialize:
            with tempfile.TemporaryFile(dir=path):
                pass
        os.execv(sys.executable, [sys.executable, "-m", "cmcc_app", *args])
    except (ValueError, OSError) as exc:
        print(f"容器初始化失败：{exc}", file=sys.stderr)
        if isinstance(exc, PermissionError):
            print("请使用新版 Compose：cap_add 需要 CHOWN、DAC_OVERRIDE、FOWNER、"
                  "SETUID、SETGID，并确认 /data 挂载可写。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
