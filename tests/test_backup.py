import io
import json
import tempfile
import unittest
from pathlib import Path

from cmcc_app import backup
from cmcc_app.settings import Settings
from cmcc_app.storage import account_key
from cmcc_app.web import create_app

PASSWORD = "test-password-12345"
PASSPHRASE = "backup-passphrase-123"


def account_body(phone="13800001234", name="我的测试账号"):
    return {
        "name": name,
        "config": {"phone": phone, "app_token": "very-private-token", "serverchan_sendkey": "SCT123",
                   "bark_url": "https://api.day.app/very-private-key", "webhook_url": "https://example.com/hook",
                   "webhook_token": "hook-secret", "fee_session_cookie": "session-cookie-value",
                   "province_code": "731", "city_code": "0731"},
        "options": {"claim": True, "tasks": True},
        "enabled": True,
        "schedule_time": "08:23",
    }


class Instance:
    """按网页流程操作一个独立实例，用于验证跨服务器迁移。"""

    def __init__(self, password=PASSWORD):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.password = password
        self.app = create_app(Settings(self.root, password))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["cmcc_store"]
        self.auth = self.app.extensions["cmcc_auth"]
        self.csrf = self.client.post("/api/login", json={"password": password}).json["csrf"]

    @property
    def headers(self):
        return {"X-CSRF-Token": self.csrf}

    def add_account(self, **kwargs):
        response = self.client.post("/api/accounts", headers=self.headers, json=account_body(**kwargs))
        assert response.status_code == 201, response.get_data(as_text=True)
        self.csrf = self.csrf
        return response.json["account"]

    def close(self):
        self.temp.cleanup()

    def export(self, passphrase=PASSPHRASE, password=PASSWORD, **options):
        payload = {"password": password, "passphrase": passphrase,
                   "include_password": True, "include_history": True, "include_cache": True}
        payload.update(options)
        return self.client.post("/api/backup/export", headers=self.headers, json=payload)

    def import_file(self, raw, passphrase=PASSPHRASE, mode="merge", restore_password="true", import_history="true"):
        return self.client.post("/api/backup/import", headers=self.headers, content_type="multipart/form-data",
                                data={"file": (io.BytesIO(raw), "cmcc-backup.json"), "passphrase": passphrase,
                                      "mode": mode, "restore_password": restore_password,
                                      "import_history": import_history})


class BackupCryptoTests(unittest.TestCase):
    def test_round_trip_keeps_secrets_and_hides_them_on_disk(self):
        payload = {"created_at": "2026-10-13T00:00:00+00:00", "app_version": "1.3.0",
                   "accounts": [account_body()]}
        raw = backup.encrypt_payload(payload, PASSPHRASE)
        self.assertNotIn(b"very-private-token", raw)
        self.assertNotIn(PASSPHRASE.encode(), raw)
        restored = backup.decrypt_payload(raw, PASSPHRASE)
        self.assertEqual(restored["accounts"][0]["config"]["app_token"], "very-private-token")
        self.assertEqual(backup.decrypt_payload(raw, PASSPHRASE)["created_at"], payload["created_at"])

    def test_wrong_passphrase_and_damaged_file_are_reported(self):
        raw = backup.encrypt_payload({"accounts": []}, PASSPHRASE)
        with self.assertRaisesRegex(ValueError, "备份密码不正确"):
            backup.decrypt_payload(raw, "another-passphrase-1")
        envelope = json.loads(raw)
        envelope["created_at"] = "2030-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "备份密码不正确"):
            backup.decrypt_payload(json.dumps(envelope).encode(), PASSPHRASE)
        with self.assertRaisesRegex(ValueError, "已损坏"):
            backup.decrypt_payload(json.dumps({**envelope, "payload": "not-base64!"}).encode(), PASSPHRASE)
        with self.assertRaisesRegex(ValueError, "不是有效的备份文件"):
            backup.decrypt_payload(b"hello", PASSPHRASE)
        with self.assertRaisesRegex(ValueError, "不是本应用的备份文件"):
            backup.decrypt_payload(json.dumps({"format": "other"}).encode(), PASSPHRASE)
        with self.assertRaises(ValueError):
            backup.decrypt_payload(json.dumps({**envelope, "format_version": 99}).encode(), PASSPHRASE)

    def test_passphrase_strength_is_enforced(self):
        for value in ("", "short", "1234567", "x" * 129, "has\nnewline"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    backup.check_passphrase(value)
        self.assertEqual(backup.check_passphrase(PASSPHRASE), PASSPHRASE)


class BackupApiTests(unittest.TestCase):
    def setUp(self):
        self.instance = Instance()
        self.addCleanup(self.instance.close)
        self.account = self.instance.add_account()
        # 一条历史记录、对应日志和一个 JWT 缓存文件。
        run_id = self.instance.store.enqueue(self.account["id"], "sign")
        self.instance.store.take_run()
        self.instance.store.finish(run_id, "success", 0, "执行完成")
        (self.instance.root / "logs" / f"{run_id}.log").write_text("签到成功，获得 10 分\n", encoding="utf-8")
        cache = self.instance.root / "cache" / f"jwt-{account_key('13800001234')}.json"
        cache.write_text(json.dumps({"phone": "13800001234", "jwt": "cached-jwt-value",
                                     "saved_at": 1, "first_seen": 1}), encoding="utf-8")
        self.run_id = run_id

    def test_export_requires_login_password_and_csrf(self):
        anonymous = self.instance.app.test_client()
        self.assertEqual(anonymous.post("/api/backup/export", json={}).status_code, 401)
        self.assertEqual(self.instance.client.post("/api/backup/export", json={}).status_code, 403)
        wrong = self.instance.export(password="wrong-password")
        self.assertEqual(wrong.status_code, 403)
        self.assertIn("管理密码不正确", wrong.json["error"])
        weak = self.instance.export(passphrase="short")
        self.assertEqual(weak.status_code, 400)
        for _ in range(5):
            self.instance.export(password="wrong-password")
        self.assertEqual(self.instance.export(password="wrong-password").status_code, 429)
        self.assertEqual(self.instance.export().status_code, 429)

    def test_export_download_is_encrypted_json(self):
        response = self.instance.export()
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="cmcc-backup-', response.headers["Content-Disposition"])
        self.assertNotIn("very-private-token", response.get_data(as_text=True))
        envelope = json.loads(response.get_data(as_text=True))
        self.assertEqual(envelope["format"], "cmcc-backup")
        self.assertEqual(envelope["kdf"]["name"], "pbkdf2-hmac-sha256")
        payload = backup.decrypt_payload(response.get_data(), PASSPHRASE)
        self.assertEqual(payload["accounts"][0]["config"]["webhook_token"], "hook-secret")
        self.assertEqual(payload["counts"], {"accounts": 1, "runs": 1, "logs": 1, "cache": 1, "password": True})

    def test_import_rejects_wrong_passphrase_and_foreign_file(self):
        response = self.instance.import_file(b"{}", passphrase=PASSPHRASE)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不是本应用的备份文件", response.json["error"])
        raw = self.instance.export().get_data()
        response = self.instance.import_file(raw, passphrase="wrong-passphrase")
        self.assertEqual(response.status_code, 400)
        self.assertIn("备份密码不正确", response.json["error"])
        self.assertEqual(len(self.instance.store.accounts()), 1)

    def test_merge_import_skips_existing_and_adds_new(self):
        raw = self.instance.export().get_data()
        self.instance.add_account(phone="13900005678", name="第二个账号")
        # 合并导入：已存在的手机号跳过，不再重复添加。
        response = self.instance.import_file(raw)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["added"], 0)
        self.assertEqual(response.json["skipped_total"], 1)
        self.assertEqual(len(self.instance.store.accounts()), 2)

    def test_replace_import_restores_accounts_history_cache_and_password(self):
        raw = self.instance.export().get_data()
        self.instance.store.clear()
        self.assertEqual(len(self.instance.store.accounts()), 0)
        response = self.instance.import_file(raw, mode="replace")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        summary = response.json
        self.assertEqual((summary["added"], summary["runs"], summary["logs"], summary["cache"]), (1, 1, 1, 1))
        accounts = self.instance.client.get("/api/accounts", headers=self.instance.headers).json["accounts"]
        self.assertEqual(len(accounts), 1)
        saved = self.instance.store.account(accounts[0]["id"])
        for field in ("app_token", "webhook_token", "fee_session_cookie"):
            self.assertTrue(saved["config"][field])
        self.assertTrue(saved["enabled"] and saved["options"]["tasks"] and saved["schedule_time"] == "08:23")
        self.assertEqual(self.instance.client.get("/api/runs").json["runs"][0]["id"], self.run_id)
        log = self.instance.client.get(f"/api/runs/{self.run_id}/log").json["log"]
        self.assertIn("签到成功", log)
        self.assertTrue((self.instance.root / "cache" / f"jwt-{account_key('13800001234')}.json").exists())

    def test_import_summary_never_leaks_secrets(self):
        raw = self.instance.export().get_data()
        response = self.instance.import_file(raw)
        text = response.get_data(as_text=True)
        for secret in ("very-private-token", "hook-secret", "SCT123", "session-cookie-value"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.instance.client.get("/api/accounts").status_code, 200)


class MigrationTests(unittest.TestCase):
    """模拟换服务器：旧实例导出，新实例导入后直接可用。"""

    def setUp(self):
        self.old = Instance()
        self.addCleanup(self.old.close)
        self.account = self.old.add_account()
        run_id = self.old.store.enqueue(self.account["id"], "sign")
        self.old.store.take_run()
        self.old.store.finish(run_id, "success", 0, "执行完成")
        (self.old.root / "logs" / f"{run_id}.log").write_text("签到成功，获得 10 分\n", encoding="utf-8")
        self.raw = self.old.export().get_data()

    def test_new_server_needs_no_reconfiguration(self):
        new = Instance(password="other-password-999999")
        self.addCleanup(new.close)
        response = new.import_file(self.raw)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        summary = response.json
        self.assertTrue(summary["password_restored"])
        self.assertEqual(summary["added"], 1)
        # 会话以新签名材料继续可用，界面无需重新登录。
        self.assertEqual(new.client.get("/api/accounts").json["accounts"][0]["name"], "我的测试账号")
        # 原管理密码生效，导入前的密码失效。
        self.assertEqual(new.client.post("/api/login", json={"password": PASSWORD}).status_code, 200)
        self.assertEqual(new.client.post("/api/login", json={"password": "other-password-999999"}).status_code, 401)
        # 执行记录与日志一并迁移。
        runs = new.client.get("/api/runs").json["runs"]
        self.assertEqual([run["status"] for run in runs], ["success"])
        self.assertIn("签到成功", new.client.get(f"/api/runs/{runs[0]['id']}/log").json["log"])
        # 未勾选恢复密码时保留新实例自己的密码。
        third = Instance(password="third-password-1234")
        self.addCleanup(third.close)
        third.import_file(self.raw, restore_password="false")
        self.assertEqual(third.client.post("/api/login", json={"password": "third-password-1234"}).status_code, 200)

    def test_import_without_password_restore_keeps_current_session(self):
        new = Instance(password="other-password-999999")
        self.addCleanup(new.close)
        response = new.import_file(self.raw, restore_password="false")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["password_restored"])
        original_csrf = response.json["csrf"]
        self.assertEqual(new.client.get("/api/accounts", headers={"X-CSRF-Token": original_csrf}).status_code, 200)
        self.assertEqual(new.client.post("/api/accounts", headers={"X-CSRF-Token": original_csrf}, json={}).status_code, 400)

    def test_replace_mode_is_blocked_while_a_task_is_pending(self):
        new = Instance()
        self.addCleanup(new.close)
        account = new.add_account(phone="13700001111", name="排队中的账号")
        new.store.enqueue(account["id"], "sign")
        response = new.import_file(self.raw, mode="replace")
        self.assertEqual(response.status_code, 400)
        self.assertIn("任务", response.json["error"])
        self.assertEqual(len(new.store.accounts()), 1)

    def test_import_sanitizes_broken_entries(self):
        payload = backup.decrypt_payload(self.raw, PASSPHRASE)
        payload["accounts"].append({"name": "坏数据", "config": {"phone": "12345", "app_token": "x"}})
        payload["accounts"].append({"name": "未知字段", "config": {"phone": "13600002222", "app_token": "ok",
                                                                  "unknown_field": "drop-me"}})
        payload["runs"] = [{"id": "queued-run", "account_id": "missing", "task": "sign", "status": "queued"},
                           {"id": "bad-task", "account_id": payload["accounts"][0]["id"], "task": "unknown",
                            "status": "success", "created_at": "2026-10-13T00:00:00+00:00"}]
        raw = backup.encrypt_payload(payload, PASSPHRASE)
        new = Instance()
        self.addCleanup(new.close)
        response = new.import_file(raw, restore_password="false")
        self.assertEqual(response.status_code, 400)
        self.assertIn("手机号", response.json["error"])
        self.assertEqual(len(new.store.accounts()), 0, "校验失败时不应写入部分账号")
        del payload["accounts"][1]
        response = new.import_file(backup.encrypt_payload(payload, PASSPHRASE), restore_password="false")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json["added"], 2)
        self.assertEqual(new.store.runs(), [], "排队中和未知任务的记录不应导入")
        imported = {account["config"]["phone"]: account for account in new.store.accounts()}
        self.assertNotIn("unknown_field", imported["13600002222"]["config"])
        self.assertEqual(imported["13600002222"]["config"]["app_token"], "ok")


if __name__ == "__main__":
    unittest.main()
