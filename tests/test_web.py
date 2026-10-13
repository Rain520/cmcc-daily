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
