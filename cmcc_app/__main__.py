import logging
import os
import signal
import sys
import time
from urllib.request import urlopen

from .settings import Settings
from .storage import ProcessLock


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "healthcheck":
        try:
            with urlopen(f"http://127.0.0.1:{os.environ.get('CMCC_PORT', '8000')}/healthz", timeout=5) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    if command not in ("serve", "check-config"):
        print("用法：python -m cmcc_app [serve|check-config|healthcheck]", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
        if command == "check-config":
            print(f"配置有效；时区 {settings.timezone}；数据目录 {settings.data_dir}")
            if not (settings.data_dir / "auth.json").exists():
                print("提示：首次启动的管理密码为环境变量中的值，未设置时为 123456；登录后可在网页修改。")
            return 0
        if hasattr(os, "umask"):
            os.umask(0o077)
        os.environ["TZ"] = settings.timezone
        if hasattr(time, "tzset"):
            time.tzset()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        from waitress import create_server
        from .web import create_app
        with ProcessLock(settings.data_dir / "app.lock"):
            app = create_app(settings, start_manager=True)
            manager = app.extensions["cmcc_manager"]
            server = None
            try:
                server = create_server(app, host=os.environ.get("CMCC_BIND", "0.0.0.0"),
                                       port=int(os.environ.get("CMCC_PORT", "8000")), threads=4,
                                       clear_untrusted_proxy_headers=True)

                def stop(*_):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, stop)
                logging.info("CMCC 网页已启动；时区 %s", settings.timezone)
                server.run()
            except KeyboardInterrupt:
                logging.info("正在停止应用…")
            finally:
                manager.stop_event.set()
                manager.join(timeout=20)
                if server is not None:
                    server.close()
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
