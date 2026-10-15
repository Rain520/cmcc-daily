"""审核预览模式：只用虚构数据、关闭定时计划、任务改为本地模拟。"""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cmcc_app import preview
from cmcc_app.models import Store, normalize_account, utcnow
from cmcc_app.settings import Settings
from cmcc_app.web import create_app


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "preview"

    def prepare(self):
        # 生成示例数据时会把条数打印到标准输出，测试里保持安静。
        with contextlib.redirect_stdout(io.StringIO()):
            preview.prepare(self.data)

    def build(self):
        """按 CMCC_PREVIEW=1 的启动顺序建一个预览应用：先写虚构数据，再切成模拟执行。"""
        self.prepare()
        self.app = create_app(Settings(self.data, "test-password-12345"))
        self.app.config["TESTING"] = True
        self.store = self.app.extensions["cmcc_store"]
        self.manager = self.app.extensions["cmcc_manager"]
        preview.install(self.app, self.data)
        self.client = self.app.test_client()
        csrf = self.client.post("/api/login", json={"password": "test-password-12345"}).json["csrf"]
        self.headers = {"X-CSRF-Token": csrf}
        self.account = next(item for item in self.store.accounts() if item["name"].startswith("示例 · 日常"))

    def test_flag_defaults_to_off(self):
        for value, expected in ((None, False), ("", False), ("0", False), ("off", False),
                                ("1", True), (" true ", True), ("ON", True)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(preview.ENV_FLAG, None)
                    if value is not None:
                        os.environ[preview.ENV_FLAG] = value
                    self.assertIs(preview.enabled(), expected)

    def test_prepare_seeds_fictional_data_once(self):
        self.prepare()
        store = Store(self.data)
        accounts = store.accounts()
        self.assertEqual([item["name"] for item in accounts],
                         [name for _, name, _ in preview.STATUS_FIXTURES])
        self.assertEqual(sorted(item["config"]["phone"] for item in accounts),
                         sorted(phone for phone, _, _ in preview.STATUS_FIXTURES))
        self.assertEqual({item["config"]["app_token"] for item in accounts}, {"preview-token"})
        runs = store.runs(100)
        # 5 个账号各 2 条历史记录，另有 1 条「正在执行」和 1 条「排队中」。
        self.assertEqual(len(runs), 12, runs)
        self.assertEqual({item["status"] for item in runs}, {"success", "failed", "partial", "running", "queued"})
        self.assertEqual((self.data / preview.MARKER_NAME).read_text(encoding="utf-8"), preview.MARKER_TEXT)

        # 再次启动只读标记，不会重复写入示例数据。
        self.prepare()
        self.assertEqual(len(Store(self.data).accounts()), len(preview.STATUS_FIXTURES))

    def test_prepare_refuses_a_data_directory_with_real_records(self):
        store = Store(self.data)
        store.save(normalize_account({"name": "真实实例",
                                      "config": {"phone": "13800009999", "app_token": "example-token"}}))
        with self.assertRaises(ValueError) as caught:
            preview.prepare(self.data)
        self.assertIn("空的独立预览目录", str(caught.exception))
        self.assertFalse((self.data / preview.MARKER_NAME).exists())

    def test_page_banner_and_accounts_flag(self):
        self.build()
        self.assertIn(preview.BANNER, self.client.get("/").get_data(as_text=True))
        payload = self.client.get("/api/accounts").get_json()
        self.assertTrue(payload["preview"])
        self.assertEqual(len(payload["accounts"]), len(preview.STATUS_FIXTURES))

    def test_scheduled_plans_and_activities_never_queue(self):
        self.build()
        before = len(self.store.runs(100))
        # 示例账号开着 10:00 的签到计划，预览里不允许它真的排队。
        self.store.schedule_due("Asia/Shanghai", now=utcnow())
        self.assertEqual(self.store.schedule_activities_due("Asia/Shanghai"), [])
        self.assertEqual(len(self.store.runs(100)), before)

    def test_fixture_runs_stay_put(self):
        self.build()
        fixture = json.loads((self.data / preview.FIXTURE_NAME).read_text(encoding="utf-8"))["runs"]
        self.assertEqual({item["status"] for item in fixture}, {"running", "queued"})
        self.assertIsNone(self.store.take_run())
        self.assertIsNone(self.store.take_run_parallel())
        self.assertEqual({self.store.run(item["id"])["status"] for item in fixture}, {"running", "queued"})
        # 新提交的任务照常排队并被模拟执行。
        run_id = self.store.enqueue(self.account["id"], "sign")
        self.assertEqual(self.store.take_run()["id"], run_id)

    def stop_manager(self):
        """线程退出后再清理临时目录，否则 Windows 上 SQLite 文件仍被占用。"""
        self.manager.stop_event.set()
        self.manager.join(timeout=15)

    def test_builtin_activity_comes_from_the_backend(self):
        """秒杀活动清单由后端预置：预览启动后自动出现，网页上不能新增。"""
        self.build()
        activities = self.store.activities()
        self.assertEqual([item["id"] for item in activities], ["seckill-daily"])
        activity = activities[0]
        self.assertEqual({key: activity["config"][key] for key in ("lead", "interval", "max_attempts")},
                         {"lead": 5, "interval": 0.35, "max_attempts": 120})
        self.assertFalse(activity["enabled"], "内置活动默认不启用，等用户自己打开")
        self.assertEqual(len(self.store.activity_account_ids()), len(preview.STATUS_FIXTURES))
        payload = self.client.get("/api/activities").get_json()["activities"][0]
        self.assertTrue(payload["description"])

    def test_scheduler_started_after_install_keeps_fixtures_running(self):
        """调度线程必须在 install() 之后启动，否则示例里的「正在执行」会被 recover 改成中断。"""
        self.build()
        self.manager.start()
        self.addCleanup(self.stop_manager)
        time.sleep(1)
        fixture = json.loads((self.data / preview.FIXTURE_NAME).read_text(encoding="utf-8"))["runs"]
        self.assertEqual({self.store.run(item["id"])["status"] for item in fixture}, {"running", "queued"})
        self.assertEqual(len(self.store.runs(100)), 12)

    def test_simulated_execution_reports_partial_ai_dou(self):
        self.build()
        run_id = self.store.enqueue(self.account["id"], "sign")
        with patch.object(preview, "WORK_SECONDS", 0.01):
            self.manager.execute(self.store.take_run())
        run = self.store.run(run_id)
        self.assertEqual((run["status"], run["exit_code"]), ("success", 0))
        self.assertIn("审核预览模拟", run["message"])
        self.assertEqual(json.loads(run["result"])["parts"],
                         {"main": {"state": "success", "reason": "ok"},
                          "claim": {"state": "success", "reason": "empty"},
                          "tasks": {"state": "partial", "reason": "business"},
                          "games": {"state": "success", "reason": "ok"}})
        log = (self.data / "logs" / f"{run_id}.log").read_text(encoding="utf-8")
        self.assertIn("未联网、未调用中国移动接口", log)
        self.assertNotIn("preview-token", log)

    def test_simulated_query_and_seckill_runs(self):
        self.build()
        query_id = self.store.enqueue(self.account["id"], "sign", dry_run=True)
        seckill_id = self.store.enqueue(self.account["id"], "seckill")
        with patch.object(preview, "WORK_SECONDS", 0.01):
            self.manager.execute(self.store.take_run())
            self.manager.execute(self.store.take_run_parallel())
        query, seckill = self.store.run(query_id), self.store.run(seckill_id)
        self.assertEqual(json.loads(query["result"])["reason"], "query")
        self.assertEqual(seckill["status"], "success")
        self.assertIn("任务：seckill", (self.data / "logs" / f"{seckill_id}.log").read_text(encoding="utf-8"))

    def test_seckill_runs_are_simulated_in_parallel(self):
        """秒杀按账号并行：execute(parallel=True) 立刻返回，各账号同时模拟。"""
        self.build()
        run_ids = [self.store.enqueue(account["id"], "seckill", activity_id=self.store.activities()[0]["id"])
                   for account in self.store.accounts()[:2]]
        with patch.object(preview, "WORK_SECONDS", 0.6):
            started = time.monotonic()
            for _ in run_ids:
                self.manager.execute(self.store.take_run_parallel(), parallel=True)
            self.assertLess(time.monotonic() - started, 0.6, "并行的秒杀不能挡在调用方")
            self.assertEqual({self.store.run(run_id)["status"] for run_id in run_ids}, {"running"})
            for run_id in run_ids:
                for _ in range(60):
                    if self.store.run(run_id)["status"] != "running":
                        break
                    time.sleep(0.1)
        self.assertEqual({self.store.run(run_id)["status"] for run_id in run_ids}, {"success"})
        self.assertIn("并行模拟", (self.data / "logs" / f"{run_ids[0]}.log").read_text(encoding="utf-8"))

    def test_stopping_the_app_interrupts_a_simulated_run(self):
        self.build()
        run_id = self.store.enqueue(self.account["id"], "sign")
        run = self.store.take_run()
        self.manager.stop_event.set()
        preview.simulate(self.manager, run)
        stopped = self.store.run(run_id)
        self.assertEqual((stopped["status"], stopped["exit_code"]), ("interrupted", 130))
        self.assertEqual(json.loads(stopped["result"])["reason"], "stopped")

    def test_notification_and_backup_import_are_refused(self):
        self.build()
        response = self.client.post(f"/api/accounts/{self.account['id']}/notify-test", headers=self.headers, json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("审核预览", response.get_json()["error"])
        response = self.client.post("/api/backup/import", headers=self.headers, data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("审核预览", response.get_json()["error"])

    def test_exported_backup_only_contains_fictional_data(self):
        self.build()
        response = self.client.post("/api/backup/export", headers=self.headers,
                                    json={"password": "test-password-12345",
                                          "passphrase": "preview-passphrase-1234"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("preview-token", response.get_data(as_text=True))
        self.assertEqual(len(self.store.accounts()), len(preview.STATUS_FIXTURES))


if __name__ == "__main__":
    unittest.main()
