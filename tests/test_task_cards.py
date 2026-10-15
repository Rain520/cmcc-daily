"""卡片隐藏与签到主结果的行为验证；所有账号及任务数据完全虚构。"""
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from cmcc_app import results
from cmcc_app.models import Store, utcnow
from cmcc_app.settings import Settings
from cmcc_app.web import create_app


def sign_result(main="success", child="partial"):
    return {"version": 1, "state": "partial", "reason": "partial", "auth": "valid",
            "parts": {"main": {"state": main, "reason": "ok" if main == "success" else "business"},
                      "tasks": {"state": child, "reason": "business"}}}


class TaskCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(Settings(self.root, "preview-password"))
        self.client = self.app.test_client()
        csrf = self.client.post("/api/login", json={"password": "preview-password"}).json["csrf"]
        self.headers = {"X-CSRF-Token": csrf}
        self.store = self.app.extensions["cmcc_store"]
        self.manager = self.app.extensions["cmcc_manager"]
        self.account = self.client.post("/api/accounts", headers=self.headers, json={
            "name": "示例卡片", "config": {"phone": "13800000011", "app_token": "preview-token"},
            "options": {"tasks": True}, "schedules": {"sign": {"enabled": True, "time": "08:23"}}}).json["account"]
        self.account_id = self.account["id"]

    def summary(self):
        return self.client.get("/api/accounts").json["accounts"][0]["summary"]

    def entry(self, task):
        return next(row for row in self.summary()["tasks"] if row["task"] == task)

    def finish_run(self, task, status="success", result=None, dry=False):
        run_id = self.store.enqueue(self.account_id, task, dry)
        self.store.finish(run_id, status, 0, result=result)
        return run_id

    def dismiss(self, task, run_id, headers=None):
        return self.client.post(f"/api/accounts/{self.account_id}/tasks/{task}/dismiss",
                                headers=self.headers if headers is None else headers, json={"run_id": run_id})

    def test_dismiss_keeps_history_logs_schedule_and_survives_restart(self):
        run_id = self.finish_run("seckill", "interrupted")
        log = self.root / "logs" / f"{run_id}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("示例停止日志", encoding="utf-8")
        before = self.store.account(self.account_id)["schedules"]
        self.assertEqual(self.entry("seckill")["dismiss_run_id"], run_id)
        self.assertEqual(self.dismiss("seckill", run_id).status_code, 200)
        self.assertTrue(self.entry("seckill")["hidden"])
        self.assertEqual(self.store.run(run_id)["status"], "interrupted")
        self.assertEqual(log.read_text(encoding="utf-8"), "示例停止日志")
        self.assertEqual(self.store.account(self.account_id)["schedules"], before)
        from cmcc_app.account_status import summaries
        reopened = Store(self.root)
        status = summaries(reopened, reopened.accounts(), "Asia/Shanghai")[self.account_id]
        self.assertTrue(next(row for row in status["tasks"] if row["task"] == "seckill")["hidden"])
        self.assertEqual(status["state"], "normal")

    def test_new_manual_run_reappears_and_cannot_be_hidden_while_active(self):
        previous = self.finish_run("seckill", "failed")
        self.dismiss("seckill", previous)
        new = self.store.enqueue(self.account_id, "seckill")
        self.assertFalse(self.entry("seckill")["hidden"])
        self.assertIsNone(self.entry("seckill")["dismiss_run_id"])
        self.assertEqual(self.dismiss("seckill", previous).status_code, 400)
        self.store.take_run_parallel()
        self.assertEqual(self.entry("seckill")["state"], "running")
        self.assertEqual(self.dismiss("seckill", new).status_code, 400)
        self.store.finish(new, "success", 0)
        self.assertEqual(self.entry("seckill")["dismiss_run_id"], new)
        self.assertEqual(self.dismiss("seckill", previous).status_code, 400)
        self.assertFalse(self.entry("seckill")["hidden"])

    def test_new_scheduled_run_reappears_without_changing_schedule(self):
        previous = self.finish_run("seckill")
        self.dismiss("seckill", previous)
        activity = self.client.get("/api/activities").json["activities"][0]
        self.client.put(f"/api/activities/{activity['id']}", headers=self.headers, json={"enabled": True})
        with self.store.connect() as conn:
            conn.execute("UPDATE activities SET next_run=? WHERE id=?",
                         ((utcnow() - timedelta(seconds=1)).isoformat(), activity["id"]))
        self.store.schedule_activities_due("Asia/Shanghai")
        self.assertFalse(self.entry("seckill")["hidden"])
        self.assertEqual(self.entry("seckill")["state"], "queued")
        self.assertTrue(self.store.activity(activity["id"])["enabled"])

    def test_removal_requires_auth_csrf_and_matching_account_task_result(self):
        run_id = self.finish_run("seckill")
        path = f"/api/accounts/{self.account_id}/tasks/seckill/dismiss"
        self.assertEqual(self.app.test_client().post(path, json={"run_id": run_id}).status_code, 401)
        self.assertEqual(self.dismiss("seckill", run_id, {}).status_code, 403)
        self.assertEqual(self.dismiss("sign", run_id).status_code, 400)
        self.assertEqual(self.dismiss("unknown", run_id).status_code, 400)
        self.assertEqual(self.dismiss("seckill", []).status_code, 400)
        other = self.client.post("/api/accounts", headers=self.headers, json={
            "name": "示例另一个账号", "config": {"phone": "13700000022", "app_token": "preview-token"}}).json["account"]
        self.assertEqual(self.client.post(f"/api/accounts/{other['id']}/tasks/seckill/dismiss",
                                         headers=self.headers, json={"run_id": run_id}).status_code, 400)
        self.assertFalse(self.entry("seckill")["hidden"])

    def test_multiple_active_tasks_never_offer_removal(self):
        self.finish_run("sign")
        self.finish_run("rate")
        self.store.enqueue(self.account_id, "sign")
        self.store.enqueue(self.account_id, "rate")
        self.store.take_run()
        for task in ("sign", "rate"):
            self.assertIn(self.entry(task)["state"], ("running", "queued"))
            self.assertIsNone(self.entry(task)["dismiss_run_id"])
        self.assertEqual(self.summary()["state"], "running")

    def test_sign_success_child_warning_is_green_and_never_retried(self):
        run_id = self.finish_run("sign", "partial", sign_result())
        self.assertEqual(self.store.run(run_id)["status"], "success")
        self.assertEqual(self.summary()["state"], "normal")
        self.assertEqual(self.entry("sign")["state"], "success")
        self.assertEqual(self.entry("sign")["parts"][1]["state"], "partial")
        self.assertIsNone(self.entry("sign")["dismiss_run_id"])
        self.assertEqual(self.dismiss("sign", run_id).status_code, 400)
        with patch.object(self.manager, "healthy", return_value=True):
            for path in (f"/api/accounts/{self.account_id}/run-all", "/api/run-batch"):
                response = self.client.post(path, headers=self.headers, json={"mode": "pending"})
                self.assertEqual(response.status_code, 202)
                self.assertNotIn("sign", [item["task"] for item in response.json["queued"]])
            response = self.client.post(f"/api/accounts/{self.account_id}/run-all", headers=self.headers,
                                        json={"mode": "all"})
            self.assertIn("sign", [item["task"] for item in response.json["queued"]])
        self.assertFalse(self.entry("sign")["hidden"])

    def test_other_tasks_cannot_be_dismissed_and_failure_is_still_retried(self):
        for task in ("sign", "rate", "wed", "fee"):
            with self.subTest(task=task):
                self.assertEqual(self.dismiss(task, "preview-run").status_code, 400)
                with self.assertRaises(ValueError):
                    self.store.dismiss_task_card(self.account_id, task, "preview-run")
        run_id = self.finish_run("rate", "failed")
        self.assertIsNone(self.entry("rate")["dismiss_run_id"])
        with patch.object(self.manager, "healthy", return_value=True):
            response = self.client.post(f"/api/accounts/{self.account_id}/run-all", headers=self.headers,
                                        json={"mode": "pending"})
        self.assertIn("rate", [item["task"] for item in response.json["queued"]])

    def test_migration_updates_only_sign_records_with_explicit_main_success(self):
        ids = []
        for task, dry, result, status in (("sign", False, sign_result(), "partial"),
                ("sign", True, sign_result(), "partial"), ("sign", False, sign_result("failed"), "failed"),
                ("sign", False, None, "partial"), ("wed", False, sign_result(), "partial"),
                ("sign", False, sign_result(), "interrupted")):
            run_id = self.finish_run(task, status, dry=dry)
            with self.store.connect() as conn:
                conn.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result) if result else None, run_id))
            ids.append(run_id)
        Store(self.root)
        self.assertEqual([self.store.run(run_id)["status"] for run_id in ids],
                         ["success", "partial", "failed", "partial", "partial", "interrupted"])
        self.assertEqual(json.loads(self.store.run(ids[0])["result"])["parts"]["tasks"]["state"], "partial")
        self.assertEqual(len(self.store.runs()), len(ids))

    def test_imported_legacy_sign_result_uses_same_main_success_rule(self):
        run_id = self.finish_run("sign")
        record = self.store.run(run_id)
        record.update(id="preview-imported-run", status="partial", result=json.dumps(sign_result()))
        self.assertEqual(self.store.add_history([record]), 1)
        imported = self.store.run(record["id"])
        self.assertEqual(imported["status"], "success")
        self.assertEqual(json.loads(imported["result"])["parts"]["tasks"]["state"], "partial")


class SignOutcomeTests(unittest.TestCase):
    def tearDown(self):
        results._current.set(None)

    def test_addon_business_network_or_auth_failure_keeps_main_success(self):
        for reason in ("business", "network", "auth"):
            with self.subTest(reason=reason):
                results.start("sign", {"tasks": True})
                results.mark("success", "already")
                results.auth(True)
                results.begin("tasks")
                if reason == "auth":
                    results.auth(False)
                else:
                    results.mark("failed", reason)
                result = results.finish(1)
                self.assertEqual((result["state"], result["reason"], result["auth"]), ("success", "already", "valid"))
                self.assertEqual(result["parts"]["tasks"], {"state": "failed", "reason": reason})

    def test_main_failure_query_and_interruption_are_not_promoted(self):
        self.assertEqual(results.for_task("sign", sign_result("failed"))["state"], "partial")
        self.assertEqual(results.for_task("sign", sign_result(), True)["state"], "partial")
        for state, reason in (("interrupted", "stopped"), ("failed", "timeout")):
            record = {**sign_result(), "state": state, "reason": reason}
            self.assertEqual(results.for_task("sign", record)["state"], state)


if __name__ == "__main__":
    unittest.main()
