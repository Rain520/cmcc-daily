import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cmcc_app.settings import Settings
from cmcc_app.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(Settings(self.root, "test-password-12345"))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.manager = self.app.extensions["cmcc_manager"]
        self.store = self.app.extensions["cmcc_store"]
        result = self.client.post("/api/login", json={"password": "test-password-12345"})
        self.csrf = result.json["csrf"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def add_account(self):
        response = self.client.post("/api/accounts", headers=self.headers, json={
            "name": "我的测试账号 <script>", "config": {"phone": "13800001234", "app_token": "very-private-token"},
            "enabled": False,
        })
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("very-private-token", response.get_data(as_text=True))
        return response.json["account"]

    def test_login_required_and_logout(self):
        anonymous = self.app.test_client()
        for endpoint in ("/api/accounts", "/api/runs", "/api/session"):
            self.assertEqual(anonymous.get(endpoint).status_code, 401)
        self.assertEqual(anonymous.post("/api/accounts", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/logout", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    def test_csrf_required_for_mutations(self):
        self.assertEqual(self.client.post("/api/accounts", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/accounts", headers={"X-CSRF-Token": "wrong"}, json={}).status_code, 403)

    def test_login_throttle(self):
        anonymous = self.app.test_client()
        for _ in range(5):
            self.assertEqual(anonymous.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(anonymous.post("/api/login", json={"password": "wrong"}).status_code, 429)

    def test_configuration_round_trip_never_returns_saved_secrets(self):
        account = self.add_account()
        response = self.client.put(f"/api/accounts/{account['id']}", headers=self.headers,
                                   json={"name": "已编辑", "config": {"app_token": ""}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.account(account["id"])["config"]["app_token"], "very-private-token")
        response = self.client.get("/api/accounts")
        self.assertNotIn("very-private-token", response.get_data(as_text=True))
        self.assertTrue(response.json["accounts"][0]["has_app_token"])

    def test_webhook_settings_persist_preserve_and_clear_without_leaking(self):
        account = self.add_account()
        path = f"/api/accounts/{account['id']}"
        config = {"webhook_url": "https://example.com/hook/secret-key", "webhook_token": "secret-bearer"}
        response = self.client.put(path, headers=self.headers, json={"config": config})
        self.assertEqual(response.status_code, 200)
        for response in (response, self.client.get("/api/accounts")):
            for secret in config.values():
                self.assertNotIn(secret, response.get_data(as_text=True))
        public = self.client.get("/api/accounts").json["accounts"][0]
        self.assertTrue(public["has_webhook_url"])
        self.assertTrue(public["has_webhook_token"])
        self.assertEqual(self.client.put(path, headers=self.headers, json={
            "config": {"webhook_url": "", "webhook_token": ""}}).status_code, 200)
        for field, value in config.items():
            self.assertEqual(self.store.account(account["id"])["config"][field], value)
        self.assertEqual(self.client.put(path, headers=self.headers, json={
            "config": {"webhook_url": "http://localhost"}}).status_code, 400)
        self.assertEqual(self.client.put(path, headers=self.headers, json={
            "clear_secrets": ["webhook_url", "webhook_token"]}).status_code, 200)
        self.assertFalse(self.store.account(account["id"])["config"]["webhook_url"])
        self.assertFalse(self.store.account(account["id"])["config"]["webhook_token"])

    def test_enqueue_duplicate_and_cancellation(self):
        account = self.add_account()
        with patch.object(self.manager, "healthy", return_value=True):
            response = self.client.post(f"/api/accounts/{account['id']}/run", headers=self.headers,
                                       json={"task": "sign", "dry_run": True})
            self.assertEqual(response.status_code, 202)
            run_id = response.json["run_id"]
            duplicate = self.client.post(f"/api/accounts/{account['id']}/run", headers=self.headers, json={})
            self.assertEqual(duplicate.status_code, 400)
            self.assertEqual(self.client.delete(f"/api/accounts/{account['id']}", headers=self.headers).status_code, 400)
            cancelled = self.client.post(f"/api/runs/{run_id}/cancel", headers=self.headers, json={})
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(self.store.run(run_id)["status"], "interrupted")
        self.assertEqual(self.client.delete(f"/api/accounts/{account['id']}", headers=self.headers).status_code, 200)

    def test_account_run_all_queues_every_runnable_task(self):
        account = self.add_account()
        path = f"/api/accounts/{account['id']}/run-all"
        with patch.object(self.manager, "healthy", return_value=True):
            first = self.client.post(path, headers=self.headers, json={})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(sorted(item["task"] for item in first.json["queued"]), ["rate", "seckill", "sign", "wed"])
            self.assertEqual(first.json["skipped"], [])
            repeat = self.client.post(path, headers=self.headers, json={})
            self.assertEqual(repeat.json["queued"], [])
            self.assertEqual(len(repeat.json["skipped"]), 4)
            self.assertTrue(all("排队" in item["reason"] for item in repeat.json["skipped"]), repeat.json["skipped"])
            self.assertEqual(self.client.post("/api/accounts/missing/run-all", headers=self.headers, json={}).status_code, 404)
        self.assertEqual(self.client.post(path, headers=self.headers, json={}).status_code, 503)

    def test_benign_partial_is_not_retried_by_pending_scope(self):
        """「再做也不会变」的结果（AI 豆需要真实操作、券暂时无库存）按已完成处理，不再重跑。"""
        account = self.add_account()
        self.assertEqual(self.client.put(f"/api/accounts/{account['id']}", headers=self.headers,
                                         json={"options": {"claim": True, "tasks": True, "games": True}}
                                         ).status_code, 200, "打开三个附加步骤，卡片才会列出对应子结果")
        run_id = self.store.enqueue(account["id"], "sign")
        self.store.finish(run_id, "success", 0, "执行完成", {
            "version": 1, "state": "success", "reason": "skipped", "auth": "valid",
            "parts": {"main": {"state": "success", "reason": "ok"},
                      "claim": {"state": "success", "reason": "empty"},
                      "tasks": {"state": "success", "reason": "skipped"},
                      "games": {"state": "success", "reason": "unavailable"}}})
        overview = self.client.get("/api/accounts").json["accounts"][0]
        sign = next(item for item in overview["summary"]["tasks"] if item["task"] == "sign")
        self.assertEqual(sign["state"], "success", overview["summary"])
        self.assertEqual([part["code"] for part in sign["parts"]], ["empty", "skipped", "unavailable"])
        with patch.object(self.manager, "healthy", return_value=True):
            response = self.client.post(f"/api/accounts/{account['id']}/run-all", headers=self.headers,
                                       json={"mode": "pending"})
        queued = sorted(item["task"] for item in response.json["queued"])
        self.assertNotIn("sign", queued,
                         "提示需要真实操作／暂时无库存的签到不再列入「只执行未完成和出错的任务」")
        self.assertEqual(queued, ["rate", "seckill", "wed"], "其余从未执行过的任务照常提交")
        self.assertEqual([item["task"] for item in response.json["skipped"]], [], "签到直接被排除，不算跳过")

    def test_account_run_all_includes_fee_only_with_cookie(self):
        account = self.add_account()
        self.assertEqual(self.client.put(f"/api/accounts/{account['id']}", headers=self.headers,
                                        json={"config": {"fee_session_cookie": "example-cookie"}}).status_code, 200)
        with patch.object(self.manager, "healthy", return_value=True):
            response = self.client.post(f"/api/accounts/{account['id']}/run-all", headers=self.headers, json={})
        self.assertEqual(sorted(item["task"] for item in response.json["queued"]), ["fee", "rate", "seckill", "sign", "wed"])

    def test_batch_run_modes(self):
        account = self.add_account()
        self.assertEqual(self.client.put(f"/api/accounts/{account['id']}", headers=self.headers, json={"schedules": {
            "sign": {"enabled": True, "time": "10:00"},
            "rate": {"enabled": True, "time": "09:00", "frequency": "weekly", "weekdays": [0]}}}).status_code, 200)
        with patch.object(self.manager, "healthy", return_value=True):
            enabled = self.client.post("/api/run-batch", headers=self.headers, json={"mode": "enabled"})
            self.assertEqual(enabled.status_code, 202)
            self.assertEqual(sorted(item["task"] for item in enabled.json["queued"]), ["rate", "sign"])
            pending = self.client.post("/api/run-batch", headers=self.headers, json={"mode": "enabled"})
            self.assertEqual(pending.json["queued"], [])
            self.assertEqual(sorted(item["task"] for item in pending.json["skipped"]), ["rate", "sign"])
            self.assertEqual(self.client.post("/api/run-batch", headers=self.headers, json={"mode": "other"}).status_code, 400)
        for run in self.store.runs():
            self.store.finish(run["id"], "success", 0)
        with patch.object(self.manager, "healthy", return_value=True):
            only_pending = self.client.post("/api/run-batch", headers=self.headers, json={"mode": "pending"})
            self.assertEqual(sorted(item["task"] for item in only_pending.json["queued"]), ["seckill", "wed"],
                             "已成功的不再提交，只补未执行过的（秒杀从未执行过）")
            self.store.finish(only_pending.json["queued"][0]["run_id"], "success", 0)
            everything = self.client.post("/api/run-batch", headers=self.headers, json={"mode": "all"})
            self.assertEqual(sorted(item["task"] for item in everything.json["queued"]), ["rate", "sign", "wed"],
                             "全部执行包含已成功的任务（秒杀已在 pending 阶段被 finish 成功，属于已成功）")
        self.assertEqual(self.client.post("/api/run-batch", headers=self.headers, json={"mode": "enabled"}).status_code, 503)

    def test_account_run_all_modes(self):
        account = self.add_account()
        path = f"/api/accounts/{account['id']}/run-all"
        with patch.object(self.manager, "healthy", return_value=True):
            first = self.client.post(path, headers=self.headers, json={"mode": "pending"})
            self.assertEqual(sorted(item["task"] for item in first.json["queued"]), ["rate", "seckill", "sign", "wed"])
            self.assertEqual(self.client.post(path, headers=self.headers, json={"mode": "bad"}).status_code, 400)
        for run in self.store.runs():
            self.store.finish(run["id"], "success", 0)
        with patch.object(self.manager, "healthy", return_value=True):
            again = self.client.post(path, headers=self.headers, json={"mode": "pending"})
            self.assertEqual(again.json["queued"], [], "已执行成功的不再提交")
            self.assertEqual(len(again.json["skipped"]), 0, "已成功的任务直接不列入提交范围")
            everything = self.client.post(path, headers=self.headers, json={"mode": "all"})
            self.assertEqual(sorted(item["task"] for item in everything.json["queued"]), ["rate", "seckill", "sign", "wed"])

    def test_activity_settings_and_manual_run(self):
        """活动清单由后端维护：网页只能改执行参数，不能新增或删除。"""
        account = self.add_account()
        activity = self.client.get("/api/activities").json["activities"][0]
        self.assertEqual(activity["id"], "seckill-daily")
        self.assertEqual(activity["account_count"], 1)
        self.assertTrue(activity["description"], "卡片上要显示后端写的活动说明")
        created = self.client.post("/api/activities", headers=self.headers, json={
            "name": "示例秒杀", "kind": "seckill", "time": "11:59:55", "lead": 3})
        self.assertEqual(created.status_code, 400, "网页不能新增活动")
        self.assertIn("服务端维护", created.json["error"])
        self.assertEqual(self.client.delete(f"/api/activities/{activity['id']}",
                                            headers=self.headers).status_code, 400, "网页不能删除活动")
        # 表单把三个抢购参数平铺提交，必须原样保存；名称、类型、参与账号改不动。
        second = self.client.post("/api/accounts", headers=self.headers, json={
            "name": "另一个", "config": {"phone": "13900001234", "app_token": "example-token"}}).json["account"]["id"]
        updated = self.client.put(f"/api/activities/{activity['id']}", headers=self.headers,
                                  json={"time": "12:00:00", "lead": 3, "interval": 0.5, "max_attempts": 40,
                                        "weekdays": [1, 3], "enabled": True, "name": "改名试试",
                                        "kind": "other", "accounts": []})
        self.assertEqual(updated.status_code, 200)
        body = updated.json["activity"]
        self.assertEqual({key: body["config"][key] for key in ("lead", "interval", "max_attempts")},
                         {"lead": 3, "interval": 0.5, "max_attempts": 40})
        self.assertEqual((body["time"], body["weekdays"], body["enabled"]), ("12:00:00", [1, 3], True))
        self.assertEqual((body["name"], body["kind"]), (activity["name"], "seckill"), "清单字段不可改")
        self.assertEqual(body["account_count"], 2, "全部账号参与，新增账号自动加入")
        stored = self.store.activity(activity["id"])
        self.assertEqual({key: stored["config"][key] for key in ("lead", "interval", "max_attempts")},
                         {"lead": 3, "interval": 0.5, "max_attempts": 40})
        self.assertEqual(self.client.put(f"/api/activities/{activity['id']}", headers=self.headers,
                                         json={"time": "25:00"}).status_code, 400)
        self.assertEqual(self.client.put(f"/api/activities/{activity['id']}", headers=self.headers,
                                         json={"lead": 99}).status_code, 400)
        with patch.object(self.manager, "healthy", return_value=True):
            run = self.client.post(f"/api/activities/{activity['id']}/run", headers=self.headers, json={})
            self.assertEqual(run.status_code, 202)
            self.assertEqual(sorted(item["task"] for item in run.json["queued"]), ["seckill", "seckill"],
                             "两个账号各自排队秒杀")
            self.assertTrue(all(self.store.run(item["run_id"])["activity_id"] == activity["id"]
                                for item in run.json["queued"]), "秒杀记录要记住自己来自哪个活动")
            again = self.client.post(f"/api/activities/{activity['id']}/run", headers=self.headers, json={})
            self.assertEqual(again.json["queued"], [])
            self.assertEqual(len(again.json["skipped"]), 2)
        self.assertEqual({item["account_id"] for item in run.json["queued"]}, {account["id"], second})

    def test_seckill_parallel_take_run_and_result_endpoint(self):
        first = self.add_account()
        second = self.client.post("/api/accounts", headers=self.headers, json={
            "name": "另一个", "config": {"phone": "13900001234", "app_token": "example-token"}}).json["account"]
        activity = self.client.get("/api/activities").json["activities"][0]
        with patch.object(self.manager, "healthy", return_value=True):
            run = self.client.post(f"/api/activities/{activity['id']}/run", headers=self.headers, json={})
            self.assertEqual(len(run.json["queued"]), 2)
        # 秒杀可以并行取走两个任务（不同账号），常规 take_run 不会抢秒杀任务。
        taken = [self.store.take_run_parallel(), self.store.take_run_parallel()]
        self.assertEqual(sorted(item["task"] for item in taken), ["seckill", "seckill"])
        self.assertNotEqual(taken[0]["account_id"], taken[1]["account_id"])
        for item in taken:
            self.store.finish(item["id"], "success", 0, "执行完成")
        payload = self.client.get("/api/activities").json
        entry = payload["activities"][0]
        results = [value for value in entry["results"].values() if value]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["success"] for item in results))
        self.assertEqual(entry["pending"], {})

    def test_activities_schedule_enqueues_for_selected_accounts(self):
        account = self.add_account()
        activity = self.client.get("/api/activities").json["activities"][0]
        self.client.put(f"/api/activities/{activity['id']}", headers=self.headers, json={"enabled": True})
        from cmcc_app.models import utcnow
        from datetime import timedelta
        def set_fire(offset):
            with self.store.connect() as conn:
                conn.execute("UPDATE activities SET next_run=? WHERE id=?",
                             ((utcnow() + timedelta(seconds=offset)).isoformat(), activity["id"]))

        set_fire(120)  # 离场次还早：超过「提前量 + 预热 1 分钟」，此时不排队。
        self.assertEqual(self.store.schedule_activities_due("Asia/Shanghai"), [],
                         "预热窗口之外不排队")
        set_fire(-1)   # 已到开抢时刻（或刚好晚一点）时补排队。
        queued = self.store.schedule_activities_due("Asia/Shanghai")
        self.assertEqual([item["task"] for item in queued], ["seckill"])
        runs = self.store.runs()
        self.assertEqual([run["task"] for run in runs], ["seckill"])
        self.assertEqual(runs[0]["source"], "schedule")
        self.assertEqual(runs[0]["activity_id"], activity["id"])
        again = self.store.schedule_activities_due("Asia/Shanghai")
        self.assertEqual(again, [], "同一场次只入队一次")

    def test_other_tasks_queue_while_one_is_pending(self):
        account = self.add_account()
        path = f"/api/accounts/{account['id']}/run"
        with patch.object(self.manager, "healthy", return_value=True):
            self.assertEqual(self.client.post(path, headers=self.headers, json={"task": "sign"}).status_code, 202)
            duplicate = self.client.post(path, headers=self.headers, json={"task": "sign"})
            self.assertEqual(duplicate.status_code, 400)
            self.assertIn("每日签到", duplicate.json["error"])
            other = self.client.post(path, headers=self.headers, json={"task": "rate"})
            self.assertEqual(other.status_code, 202)
        payload = self.client.get("/api/accounts").json
        self.assertEqual(payload["pending"], [account["id"]])
        self.assertEqual(sorted(item["task"] for item in payload["pending_tasks"]), ["rate", "sign"])

    def test_fee_requires_its_own_cookie(self):
        account = self.add_account()
        with patch.object(self.manager, "healthy", return_value=True):
            response = self.client.post(f"/api/accounts/{account['id']}/run", headers=self.headers, json={"task": "fee"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Cookie", response.json["error"])

    def test_logs_redacted_and_nonexistent_path_rejected(self):
        account = self.add_account()
        run_id = self.store.enqueue(account["id"], "sign")
        (self.root / "logs" / f"{run_id}.log").write_text("very-private-token 13800001234 ?token=server-secret", encoding="utf-8")
        response = self.client.get(f"/api/runs/{run_id}/log")
        self.assertEqual(response.status_code, 200)
        for secret in ("very-private-token", "13800001234", "server-secret"):
            self.assertNotIn(secret, response.json["log"])
        self.assertEqual(self.client.get("/api/runs/not-a-run/log").status_code, 404)

    def test_password_change_endpoint_and_session_reissue(self):
        stale = self.client.get_cookie("session").value
        self.assertEqual(self.client.post("/api/password", json={}).status_code, 403)
        for payload, status in (
            ({"current_password": "wrong", "new_password": "new-password"}, 401),
            ({"current_password": "test-password-12345", "new_password": "12345"}, 400),
            ({"current_password": "test-password-12345", "new_password": "test-password-12345"}, 400),
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/password", headers=self.headers, json=payload)
                self.assertEqual(response.status_code, status)
        changed = self.client.post("/api/password", headers=self.headers,
                                   json={"current_password": "test-password-12345", "new_password": "new-password"})
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.json["weak_password"])
        current = self.client.get_cookie("session").value
        self.assertNotEqual(stale, current)
        self.client.set_cookie("session", stale)
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)
        self.client.set_cookie("session", current)
        self.assertEqual(self.client.get("/api/accounts").status_code, 200)
        self.assertEqual(self.client.post("/api/accounts", headers=self.headers, json={}).status_code, 403)
        self.headers["X-CSRF-Token"] = changed.json["csrf"]
        self.assertEqual(self.client.post("/api/accounts", headers=self.headers, json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/login", json={"password": "test-password-12345"}).status_code, 401)
        fresh = self.app.test_client()
        self.assertEqual(fresh.post("/api/login", json={"password": "new-password"}).status_code, 200)

    def test_password_change_attempts_are_throttled(self):
        for _ in range(5):
            response = self.client.post("/api/password", headers=self.headers,
                                        json={"current_password": "wrong", "new_password": "new-password"})
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post("/api/password", headers=self.headers,
                                   json={"current_password": "test-password-12345", "new_password": "new-password"})
        self.assertEqual(blocked.status_code, 429)

    def test_default_password_flagged_and_ui_password_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            first = create_app(Settings(root, "123456"))
            client = first.test_client()
            login = client.post("/api/login", json={"password": "123456"})
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json["weak_password"])
            self.assertTrue(client.get("/api/session").json["weak_password"])
            csrf = login.json["csrf"]
            changed = client.post("/api/password", headers={"X-CSRF-Token": csrf},
                                  json={"current_password": "123456", "new_password": "12345678"})
            self.assertEqual(changed.status_code, 200)
            self.assertFalse(changed.json["weak_password"])
            # 重新创建应用（模拟容器重启）时环境变量不再覆盖数据卷中的密码
            restarted = create_app(Settings(root, "different-password-67890"))
            other = restarted.test_client()
            self.assertEqual(other.post("/api/login", json={"password": "123456"}).status_code, 401)
            self.assertEqual(other.post("/api/login", json={"password": "different-password-67890"}).status_code, 401)
            self.assertEqual(other.post("/api/login", json={"password": "12345678"}).status_code, 200)

    def test_cookie_and_security_headers(self):
        response = self.client.post("/api/login", json={"password": "test-password-12345"})
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_notify_test_endpoint_uses_unsaved_values_without_persisting(self):
        account = self.add_account()
        path = f"/api/accounts/{account['id']}/notify-test"
        self.assertEqual(self.app.test_client().post(path, json={}).status_code, 401)
        self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/accounts/missing/notify-test", headers=self.headers,
                                          json={}).status_code, 404)
        empty = self.client.post(path, headers=self.headers, json={})
        self.assertEqual(empty.status_code, 400)
        self.assertIn("通知渠道", empty.json["error"])
        invalid = self.client.post(path, headers=self.headers,
                                   json={"webhook_url": "http://127.0.0.1/hook"})
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(invalid.json["results"][0]["ok"])
        self.assertIn("HTTPS", invalid.json["results"][0]["message"])
        self.assertEqual(self.client.post(path, headers=self.headers,
                                          json={"webhook_url": 7}).status_code, 400)
        with patch("cmcc_sign.requests.Session") as session:
            session.return_value.post.return_value.status_code = 200
            sent = self.client.post(path, headers=self.headers, json={
                "webhook_url": "https://example.com/hook/secret", "webhook_token": "secret-token"})
        self.assertEqual(sent.status_code, 200)
        self.assertTrue(sent.json["results"][0]["ok"])
        self.assertEqual(session.return_value.post.call_args.args[0], "https://example.com/hook/secret")
        self.assertEqual(session.return_value.post.call_args.kwargs["headers"],
                         {"Authorization": "Bearer secret-token"})
        self.assertNotIn("example.com", sent.get_data(as_text=True))
        self.assertNotIn("secret-token", sent.get_data(as_text=True))
        self.assertFalse(self.store.account(account["id"])["config"]["webhook_url"])
        self.assertFalse(self.store.account(account["id"])["config"]["webhook_token"])

    def test_notify_test_works_for_accounts_saved_before_webhook_field_existed(self):
        account = self.add_account()
        with self.store.connect() as conn:
            # 模拟升级前的数据库：配置里没有 webhook_url / webhook_token
            conn.execute("UPDATE accounts SET config=? WHERE id=?",
                         (json.dumps({"phone": "13800001234", "app_token": "old-token"}), account["id"]))
        listed = self.client.get("/api/accounts").json["accounts"][0]
        self.assertFalse(listed["has_webhook_url"])
        self.assertFalse(listed["has_webhook_token"])
        response = self.client.post(f"/api/accounts/{account['id']}/notify-test", headers=self.headers,
                                    json={"webhook_url": "http://127.0.0.1/hook"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["results"][0]["ok"])
        self.assertIn("HTTPS", response.json["results"][0]["message"])

    def test_health_depends_on_scheduler(self):
        self.assertEqual(self.client.get("/healthz").status_code, 503)
        with patch.object(self.manager, "healthy", return_value=True):
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_frontend_assets_and_json_validation(self):
        for path in ("/", "/static/styles.css", "/static/app.js"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/accounts", headers=self.headers, json=[])
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
