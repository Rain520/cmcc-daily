import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cmcc_app.models import Store, normalize_account, normalize_activity
from cmcc_app.runner import Manager
from cmcc_app.settings import Settings


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.account = normalize_account({"name": "测试", "config": {"phone": "13800001234", "app_token": "example-only"}})
        self.store.save(self.account)
        self.settings = Settings(self.root, "test-password-only", task_timeout=5)
        self.manager = Manager(self.store, self.settings)
        self.real_popen = subprocess.Popen

    def execute_fake(self, code, started=None):
        run_id = self.store.enqueue(self.account["id"], "sign")
        run = self.store.take_run()
        def fake_worker(command, **kwargs):
            self.assertNotIn("example-only", " ".join(command))
            self.assertNotIn("CMCC_WEB_PASSWORD", kwargs["env"])
            self.assertNotIn("CMCC_APP_TOKEN", kwargs["env"])
            child = self.real_popen([sys.executable, "-c", code], **kwargs)
            if started:
                started.set()
            return child
        with patch("cmcc_app.runner.subprocess.Popen", side_effect=fake_worker):
            self.manager.execute(run)
        self.assertFalse(list((self.root / "runtime").glob("*.json")))
        return self.store.run(run_id)

    def test_seckill_activity_settings_reach_the_worker(self):
        """活动里的提前量/重试间隔/最大尝试次数要作为第 5 个参数交给上游脚本。"""
        activity = normalize_activity({"name": "示例抢券", "kind": "seckill", "time": "11:59:55",
                                       "lead": 3, "interval": 0.5, "max_attempts": 40,
                                       "weekdays": [0], "accounts": [], "enabled": True},
                                      accounts=[self.account], timezone_name="Asia/Shanghai")
        self.store.save_activity(activity)
        seen = {}

        def fake_worker(command, **kwargs):
            seen["command"] = command
            return self.real_popen([sys.executable, "-c", "print('mock seckill')"], **kwargs)

        run_id = self.store.enqueue(self.account["id"], "seckill", activity_id=activity["id"])
        run = self.store.take_run_parallel()
        self.assertEqual(run["activity_id"], activity["id"])
        with patch("cmcc_app.runner.subprocess.Popen", side_effect=fake_worker):
            self.manager.execute(run)
        command = seen["command"]
        self.assertEqual(command[3], "seckill")
        self.assertEqual(json.loads(command[-1]), {"name": "示例抢券", "time": "11:59:55", "lead": 3,
                                                   "interval": 0.5, "max_attempts": 40})
        self.assertEqual(self.store.run(run_id)["status"], "success")
        # 其他任务不追加这个参数，避免上游脚本收到无法识别的开关。
        seen.clear()
        self.store.enqueue(self.account["id"], "sign")
        with patch("cmcc_app.runner.subprocess.Popen", side_effect=fake_worker):
            self.manager.execute(self.store.take_run())
        self.assertEqual(len(seen["command"]), 7, seen["command"])

    def test_real_child_success_and_failure(self):
        result = self.execute_fake("print('mock execution complete')")
        self.assertEqual(result["status"], "success", result)
        self.assertIn("mock execution complete", (self.root / "logs" / f"{result['id']}.log").read_text(encoding="utf-8"))
        result = self.execute_fake("raise SystemExit(7)")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["status"], "failed")

    def test_structured_result_survives_worker_boundary(self):
        result = self.execute_fake("""
import json, os
from pathlib import Path
path = next((Path(os.environ['CMCC_DATA_DIR']) / 'runtime').glob('*.json'))
path.with_suffix('.result.json').write_text(json.dumps({
    'version': 1, 'state': 'partial', 'reason': 'partial', 'auth': 'valid',
    'parts': {'main': {'state': 'success', 'reason': 'ok'},
              'claim': {'state': 'failed', 'reason': 'network'}}}))
""")
        self.assertEqual(result["status"], "partial")
        self.assertIn('"claim"', result["result"])
        self.assertIn("部分", result["message"])

    def test_timeout_terminates_child(self):
        self.manager.settings = Settings(self.root, "test-password-only", task_timeout=0.1)
        start = time.monotonic()
        result = self.execute_fake("import time; time.sleep(30)")
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(result["status"], "failed")
        self.assertIn("超时", result["message"])

    def test_shutdown_terminates_child(self):
        self.manager.stop_event.set()
        result = self.execute_fake("import time; time.sleep(30)")
        self.assertEqual(result["status"], "interrupted")

    def test_queued_cancel_does_not_run_or_leak_cancel_ids(self):
        run_id = self.store.enqueue(self.account["id"], "sign")
        self.manager.cancel(run_id)
        self.assertIsNone(self.store.take_run())
        self.assertEqual(self.store.run(run_id)["status"], "interrupted")
        self.assertEqual(self.manager.cancelled, set())

    def test_cancel_running_child(self):
        started = threading.Event()
        def cancel():
            if started.wait(5):
                self.manager.cancel(self.store.runs()[0]["id"])
        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            result = self.execute_fake("import time; time.sleep(30)", started)
        finally:
            thread.join(timeout=10)
        self.assertEqual(result["status"], "interrupted")

    def test_parent_credentials_not_passed_to_worker(self):
        with patch.dict(os.environ, {"CMCC_WEB_PASSWORD": "admin-secret", "CMCC_APP_TOKEN": "wrong-account"}):
            result = self.execute_fake("print('isolated')")
        self.assertEqual(result["status"], "success", {
            **result, "log": (self.root / "logs" / f"{result['id']}.log").read_text(encoding="utf-8")
        })


if __name__ == "__main__":
    unittest.main()
