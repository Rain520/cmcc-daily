import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .storage import atomic_json
from .models import utcnow

log = logging.getLogger(__name__)
MAX_LOG_BYTES = 2 * 1024 * 1024


class Manager(threading.Thread):
    def __init__(self, store, settings):
        super().__init__(name="cmcc-scheduler", daemon=True)
        self.store, self.settings = store, settings
        self.stop_event = threading.Event()
        self.heartbeat = time.monotonic()
        self.cancelled = set()
        self.cancel_lock = threading.Lock()
        self.parallel_running = set()
        self.data = settings.data_dir
        for folder in ("logs", "runtime", "cache"):
            (self.data / folder).mkdir(parents=True, exist_ok=True)

    def healthy(self):
        return self.is_alive() and time.monotonic() - self.heartbeat < 60

    def cancel(self, run_id):
        with self.cancel_lock:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE runs SET status='interrupted',message='排队任务已取消',finished_at=? "
                             "WHERE id=? AND status='queued'", (utcnow().isoformat(), run_id))
                if conn.execute("SELECT 1 FROM runs WHERE id=? AND status='running'", (run_id,)).fetchone():
                    self.cancelled.add(run_id)

    def is_cancelled(self, run_id):
        with self.cancel_lock:
            return run_id in self.cancelled

    def run(self):
        self.store.recover()
        # 清除进程崩溃遗留的短期凭证副本，不触碰长期 JWT 缓存。
        for path in (self.data / "runtime").glob("*.json"):
            path.unlink()
        last_prune = 0
        try:
            while not self.stop_event.is_set():
                self.heartbeat = time.monotonic()
                self.store.schedule_due(self.settings.timezone)
                for job in self.store.schedule_activities_due(self.settings.timezone):
                    log.info("活动 %s 的秒杀任务已排队", job["account_id"])
                if self.heartbeat - last_prune > 3600:
                    self.store.prune(self.settings.retention_days)
                    last_prune = self.heartbeat
                if run := self.store.take_run_parallel():
                    # 秒杀按账号并行：各账号独立子进程同时开火，互不占用常规队列。
                    self.execute(run, parallel=True)
                elif run := self.store.take_run():
                    self.execute(run)
                else:
                    self.stop_event.wait(1)
        except Exception:
            log.exception("调度器异常停止，健康检查将失败")

    def _parallel_done(self, run_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row is None or row[0] not in ("queued", "running")

    def execute(self, run, parallel=False):
        if parallel:
            thread = threading.Thread(target=self._execute_body, args=(run,),
                                      name=f"seckill-{run['id'][:8]}", daemon=True)
            self.parallel_running.add(run["id"])
            thread.start()
            return

        self._execute_body(run)

    def _execute_body(self, run):
        run_id = run["id"]
        config_path = self.data / "runtime" / f"{run_id}.json"
        log_path = self.data / "logs" / f"{run_id}.log"
        result_path = config_path.with_suffix(".result.json")
        result = None
        finished_normally = False
        process = None
        exit_code, status, message = 1, "failed", ""
        try:
            account = self.store.account(run["account_id"])
            if account is None:
                raise ValueError("账号已删除")
            atomic_json(config_path, account["config"])
            # 网页中的每个账号独立配置；宿主机的账号变量和管理密码不传给任务。
            env = {k: v for k, v in os.environ.items() if not k.startswith("CMCC_")}
            env.update(CMCC_DATA_DIR=str(self.data), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                       TZ=self.settings.timezone)
            command = [sys.executable, "-m", "cmcc_app.worker", run["task"], str(config_path),
                       json.dumps(account["options"]), "1" if run["dry_run"] else "0"]
            if run["task"] == "seckill":
                # 活动的提前量/重试间隔/最大尝试次数要传给上游脚本，否则它只会用自带默认值。
                command.append(json.dumps(self.store.seckill_settings(run.get("activity_id"))))
            with log_path.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                                           env=env, stdout=output, stderr=subprocess.STDOUT)
                started = time.monotonic()
                while process.poll() is None:
                    self.heartbeat = time.monotonic()
                    if self.stop_event.is_set() or self.is_cancelled(run_id):
                        status, message = "interrupted", "任务已停止，未自动重试"
                        break
                    if self.heartbeat - started > self.settings.task_timeout:
                        status, message = "failed", "任务超时，已停止"
                        break
                    if log_path.stat().st_size > MAX_LOG_BYTES:
                        status, message = "failed", "日志超过 2 MiB，已停止任务"
                        break
                    self.stop_event.wait(0.5)
                else:
                    finished_normally = True
                    exit_code = process.returncode
                    status = "success" if exit_code == 0 else "failed"
                    message = "执行完成，请查看活动详情" if exit_code == 0 else "执行失败，请查看日志"
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    exit_code = process.returncode
                if finished_normally and result_path.exists() and result_path.stat().st_size < 16384:
                    from .results import for_task, REASONS
                    try:
                        result = for_task(run["task"], json.loads(result_path.read_text(encoding="utf-8")), run["dry_run"])
                    except (ValueError, OSError):
                        result = None
                    if result and result["state"] not in ("success", "partial", "failed", "interrupted"):
                        result = None
                    if result and (status == "success" or (run["task"] == "sign" and result["state"] == "success")):
                        status = result["state"]
                        message = REASONS[result["reason"]]
                output.write(f"\n[应用] {message}（退出码 {exit_code}）\n")
        except Exception as exc:
            # 不将可能包含请求凭证的异常正文写入服务日志。
            message = f"任务未能完成（{type(exc).__name__}）"
            log.error("任务 %s 未完成：%s", run_id, type(exc).__name__)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if not finished_normally:
                reason = "stopped" if status == "interrupted" else "timeout" if "超时" in message else "business"
                result = {"version": 1, "state": status, "reason": reason, "auth": "unknown", "parts": {}}
            config_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            with self.cancel_lock:
                self.cancelled.discard(run_id)
                self.parallel_running.discard(run_id)
                self.store.finish(run_id, status, exit_code, message, result)
