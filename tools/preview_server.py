r"""1.6.3 审核预览：局域网可访问，任务用本地模拟执行，绝不联网。

- 数据目录：test-results/preview-1.6.3（与正式实例完全隔离的虚构数据）
- 真实网页 + 真实队列：定时计划不触发，任务体由 simulate 取代
- 用途：审核账号状态卡片、状态筛选、手动执行、排队与重复提交提示

用法：
    .\.venv\Scripts\python.exe tools\preview_server.py --reset   # 重新生成虚构数据
    .\.venv\Scripts\python.exe tools\preview_server.py           # 手机 / 局域网访问
"""

import argparse
import json
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import jsonify, request
from waitress import serve

from cmcc_app import runner
from cmcc_app.models import utcnow
from cmcc_app.settings import Settings
from cmcc_app.storage import ProcessLock
from cmcc_app.web import create_app
from tools.preview_seed import main as seed

WORK_SECONDS = 20.0
DATA_DIR = ROOT / "test-results" / "preview-1.6.3"
BANNER = "1.6.3 审核预览 · 全部为虚构数据 · 任务为本地模拟，不联网"


def local_addresses():
    """本机 IPv4 地址；UDP connect 只查路由，不发送任何数据包。"""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.1.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found:
                found.append(info[4][0])
    except OSError:
        pass
    return found


def reset(directory):
    """只清理本项目 test-results 下的审核预览目录。"""
    target, allowed = directory.resolve(), (ROOT / "test-results").resolve()
    if allowed not in target.parents:
        raise SystemExit(f"拒绝清理工作区之外的目录：{target}")
    for path in sorted(target.iterdir(), key=lambda item: item.is_dir()):
        if path.name == "app.lock":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    print(f"已清空审核预览数据：{target}", flush=True)


def fixtures(directory):
    path = directory / "preview-fixtures.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {item["id"]: item for item in data.get("runs", [])
            if isinstance(item, dict) and item.get("id")}


def keep_fixture_status(app, data_dir):
    """审核预览固定保留「正在执行 / 排队中」两条示例记录：只跳过取任务，不改动它们。"""
    store, frozen = app.extensions["cmcc_store"], fixtures(data_dir)
    if not frozen:
        return

    def take_run():
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE status='queued' "
                "AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY created_at LIMIT 1",
                (json.dumps(sorted(frozen)),)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",
                         (utcnow().isoformat(), row["id"]))
            return dict(row)

    store.take_run = take_run


def simulate(manager, run):
    """模拟执行：只写日志和结构化结果，不访问任何外部接口。"""
    run_id, task = run["id"], run["task"]
    log_path = manager.data / "logs" / f"{run_id}.log"
    log_path.write_text("[审核预览] 模拟执行，未联网、未调用中国移动接口。\n"
                        f"任务：{task}\n模式：{'仅查询' if run['dry_run'] else '正式执行'}\n",
                        encoding="utf-8")
    deadline = time.monotonic() + WORK_SECONDS
    while time.monotonic() < deadline:
        manager.heartbeat = time.monotonic()
        if manager.stop_event.is_set() or manager.is_cancelled(run_id):
            manager.store.finish(run_id, "interrupted", 130, "任务已停止（审核预览）",
                                 {"version": 1, "state": "interrupted", "reason": "stopped",
                                  "auth": "unknown", "parts": {}})
            return
        manager.stop_event.wait(0.5)
    if run["dry_run"]:
        result = {"version": 1, "state": "success", "reason": "query", "auth": "valid",
                  "parts": {"main": {"state": "query", "reason": "query"}}}
    else:
        result = {"version": 1, "state": "success", "reason": "ok", "auth": "valid",
                  "parts": {"main": {"state": "success", "reason": "ok"}}}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("模拟结果：执行完成（未访问移动接口）\n")
    manager.store.finish(run_id, "success", 0, "执行完成（审核预览模拟，未访问移动接口）", result)


def main():
    parser = argparse.ArgumentParser(description="1.6.3 审核预览（虚构数据 + 模拟执行）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0 以便手机访问")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--reset", action="store_true", help="清空并重新生成虚构示例数据")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with ProcessLock(DATA_DIR / "app.lock"):
        if args.reset:
            reset(DATA_DIR)
        marker = DATA_DIR / "synthetic-preview.txt"
        if not marker.exists():
            seed(data_dir=DATA_DIR, status_demo=True)
            marker.write_text("Fictional data only", encoding="utf-8")

        app = create_app(Settings(DATA_DIR, "123456"), start_manager=False)
        store, manager = app.extensions["cmcc_store"], app.extensions["cmcc_manager"]
        keep_fixture_status(app, DATA_DIR)
        store.recover = lambda: None                 # 保留示例里的「正在执行」记录
        store.schedule_due = lambda *_, **__: None   # 预览不跑定时计划
        runner.Manager.execute = simulate            # 任务体换成模拟执行
        manager.start()

        @app.before_request
        def guard():
            if request.method == "POST" and (request.path.endswith("/notify-test")
                                             or request.path == "/api/backup/import"):
                return jsonify(error="审核预览不会发送通知或导入备份，请在正式实例中操作。"), 400
            return None

        @app.after_request
        def label(response):
            if request.path == "/" and response.mimetype == "text/html":
                text = response.get_data(as_text=True)
                text = text.replace('<div id="accounts-view">',
                                    f'<div class="password-warning">{BANNER}</div><div id="accounts-view">')
                response.set_data(text)
            elif request.path == "/api/accounts" and response.is_json and response.status_code == 200:
                value = response.get_json()
                value["preview"] = True
                response.set_data(app.json.dumps(value))
            return response

        print(f"审核预览已启动（模拟任务约 {WORK_SECONDS:.0f} 秒，初始密码 123456）：", flush=True)
        for address in ["127.0.0.1", *local_addresses()]:
            print(f"  http://{address}:{args.port}/", flush=True)
        serve(app, host=args.host, port=args.port, threads=6)


if __name__ == "__main__":
    main()