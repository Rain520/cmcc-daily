import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cmcc_app.auth import AuthStore
from cmcc_app.logging_utils import redact
from cmcc_app.models import Store, normalize_account, public_account, next_run
from cmcc_app.settings import Settings
from cmcc_app.storage import ProcessLock, account_key, atomic_json
from cmcc_app.worker import task_args
from zoneinfo import ZoneInfo


def account_data(phone="13800001234"):
    return {"name": "测试账号", "config": {"phone": phone, "app_token": "test-token-do-not-use"},
            "enabled": False, "schedule_time": "08:23"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.account = normalize_account(account_data())
        self.store.save(self.account)

    def test_unique_phone_and_pending_task(self):
        with self.assertRaises(ValueError):
            self.store.save(normalize_account(account_data()))
        run_id = self.store.enqueue(self.account["id"], "sign")
        with self.assertRaises(ValueError):
            self.store.enqueue(self.account["id"], "sign")
        with self.assertRaises(ValueError):
            self.store.delete(self.account["id"])
        self.store.finish(run_id, "success", 0)
        self.assertIsNotNone(self.store.enqueue(self.account["id"], "sign"))

    def test_concurrent_enqueue_only_accepts_one(self):
        def enqueue(_):
            try:
                return self.store.enqueue(self.account["id"], "sign")
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(enqueue, range(8)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_persist_and_recover_without_retrying_running_task(self):
        run_id = self.store.enqueue(self.account["id"], "sign")
        self.store.take_run()
        restarted = Store(Path(self.temp.name))
        restarted.recover()
        self.assertEqual(restarted.run(run_id)["status"], "interrupted")
        self.assertIsNone(restarted.take_run())
        self.assertEqual(restarted.account(self.account["id"])["config"]["app_token"], "test-token-do-not-use")

    def test_due_schedule_coalesces_missed_days_and_deduplicates(self):
        now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)  # 北京 09:00
        self.account["schedules"]["sign"].update(enabled=True, next_run="2026-10-09T00:23:00+00:00")
        self.store.save(self.account)
        self.store.schedule_due("Asia/Shanghai", now)
        self.store.schedule_due("Asia/Shanghai", now)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.store.account(self.account["id"])["next_run"], "2026-10-13T00:23:00+00:00")
        self.assertEqual(self.store.runs()[0]["source"], "schedule")

    def test_edit_cannot_restore_stale_next_run(self):
        self.account["schedules"]["sign"].update(enabled=True, next_run="2026-10-11T00:23:00+00:00")
        self.store.save(self.account)
        old = self.store.account(self.account["id"])
        self.store.schedule_due("Asia/Shanghai", datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc))
        self.store.save(normalize_account({"name": "新名字"}, old))
        self.assertEqual(self.store.account(old["id"])["next_run"], "2026-10-13T00:23:00+00:00")

    def test_schedule_while_manual_pending_does_not_queue_twice(self):
        self.store.enqueue(self.account["id"], "sign")
        self.account["schedules"]["sign"].update(enabled=True, next_run="2026-10-11T00:23:00+00:00")
        self.store.save(self.account)
        self.store.schedule_due("Asia/Shanghai", datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(len(self.store.runs()), 1)

    def schedule_account(self, **schedules):
        """按指定计划保存一个账号，用于验证任务之间互不影响。"""
        account = normalize_account(account_data(phone="13900004321"))
        for task, entry in schedules.items():
            account["schedules"][task].update(entry)
        self.store.save(account)
        return account

    def test_each_task_follows_its_own_schedule(self):
        now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)  # 北京 09:00
        account = self.schedule_account(
            sign={"enabled": True, "time": "08:00", "next_run": "2026-10-12T00:00:00+00:00"},
            rate={"enabled": True, "time": "10:00", "next_run": "2026-10-12T02:00:00+00:00"})
        self.store.schedule_due("Asia/Shanghai", now)
        self.assertEqual([run["task"] for run in self.store.runs()], ["sign"], "评价有礼 10:00 未到，不应排队")
        self.store.finish(self.store.runs()[0]["id"], "success", 0)
        self.store.schedule_due("Asia/Shanghai", datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(sorted(run["task"] for run in self.store.runs()), ["rate", "sign"])
        saved = self.store.account(account["id"])
        self.assertEqual(saved["schedules"]["sign"]["next_run"], "2026-10-13T00:00:00+00:00")
        self.assertEqual(saved["schedules"]["rate"]["next_run"], "2026-10-13T02:00:00+00:00")
        self.assertFalse(saved["schedules"]["wed"]["enabled"])

    def test_weekly_schedule_picks_the_selected_weekday(self):
        now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
        value = datetime.fromisoformat(next_run("10:00", "Asia/Shanghai", now, "weekly", [2]))
        local = value.astimezone(ZoneInfo("Asia/Shanghai"))
        self.assertEqual(local.weekday(), 2, "只应落在选中的星期三")
        self.assertEqual((local.hour, local.minute), (10, 0))
        self.assertGreater(value, now)
        self.assertLessEqual(value - now, timedelta(days=7))

    def test_other_tasks_queue_behind_a_pending_task(self):
        """同一账号的不同任务互不阻塞：签到在跑时，评价有礼照样入队等执行。"""
        self.store.enqueue(self.account["id"], "sign")
        queued = self.store.enqueue(self.account["id"], "rate")
        self.assertEqual(self.store.run(queued)["status"], "queued")
        with self.assertRaises(ValueError):
            self.store.enqueue(self.account["id"], "sign")          # 同一任务不能重复提交
        self.assertEqual(sorted(run["task"] for run in self.store.runs()), ["rate", "sign"])
        # 队列按发起顺序串行执行
        self.assertEqual(self.store.take_run()["task"], "sign")
        self.assertEqual(self.store.take_run()["task"], "rate")
        self.assertIsNone(self.store.take_run())
        pending = self.store.pending_tasks()
        self.assertEqual(sorted((item["task"], item["status"]) for item in pending),
                         [("rate", "running"), ("sign", "running")])

    def test_due_schedule_queues_behind_another_pending_task(self):
        now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
        account = self.schedule_account(
            rate={"enabled": True, "time": "10:00", "next_run": "2026-10-12T00:00:00+00:00"})
        self.store.enqueue(account["id"], "sign")  # 同一账号已有别的任务在排队
        self.store.schedule_due("Asia/Shanghai", now)
        self.assertEqual(sorted(run["task"] for run in self.store.runs()), ["rate", "sign"])
        self.assertEqual(self.store.account(account["id"])["schedules"]["rate"]["next_run"],
                         "2026-10-12T02:00:00+00:00", "入队成功后按计划推进到今天 10:00")

    def test_legacy_row_is_read_as_daily_sign_schedule(self):
        with self.store.connect() as conn:
            conn.execute("""UPDATE accounts SET schedules=NULL, enabled=1, schedule_time='07:30',
                            next_run='2026-10-12T00:00:00+00:00' WHERE id=?""", (self.account["id"],))
        saved = self.store.account(self.account["id"])
        self.assertEqual(saved["schedules"]["sign"]["time"], "07:30")
        self.assertTrue(saved["schedules"]["sign"]["enabled"])
        self.assertEqual(saved["next_run"], "2026-10-12T00:00:00+00:00")
        self.assertFalse(saved["schedules"]["rate"]["enabled"])

    def test_legacy_weekly_puzzle_option_is_ignored(self):
        """旧账号勾过「签到时一并执行周三拼图」，升级后由独立任务负责，不再重复执行。"""
        with self.store.connect() as conn:
            conn.execute("UPDATE accounts SET options=? WHERE id=?",
                         (json.dumps({"claim": True, "tasks": False, "games": False, "wed": True}),
                          self.account["id"]))
        account = self.store.account(self.account["id"])
        self.assertFalse(account["options"]["wed"])
        self.assertNotIn("--wed", task_args("sign", "config.json", account["options"], False))

    def test_history_cleanup_removes_only_finished_old_logs(self):
        run_id = self.store.enqueue(self.account["id"], "sign")
        self.store.finish(run_id, "success", 0)
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET finished_at='2020-01-01T00:00:00+00:00' WHERE id=?", (run_id,))
        log_dir = Path(self.temp.name) / "logs"
        log_dir.mkdir()
        log_file = log_dir / f"{run_id}.log"
        log_file.write_text("old", encoding="utf-8")
        pending = self.store.enqueue(self.account["id"], "sign")
        self.store.prune(30)
        self.assertFalse(log_file.exists())
        self.assertIsNone(self.store.run(run_id))
        self.assertEqual(self.store.run(pending)["status"], "queued")


class ConfigTests(unittest.TestCase):
    def test_secret_preservation_and_public_response(self):
        account = normalize_account(account_data())
        updated = normalize_account({"config": {"app_token": ""}}, account)
        self.assertEqual(updated["config"]["app_token"], account["config"]["app_token"])
        public = public_account(updated)
        self.assertNotIn("app_token", public["config"])
        self.assertTrue(public["has_app_token"])
        self.assertNotIn("test-token", json.dumps(public))

    def test_optional_secret_can_be_removed(self):
        data = account_data()
        data["config"]["serverchan_sendkey"] = "abc123"
        original = normalize_account(data)
        updated = normalize_account({"clear_secrets": ["serverchan_sendkey"]}, original)
        self.assertEqual(updated["config"]["serverchan_sendkey"], "")

    def test_bad_types_and_unknown_fields_rejected(self):
        invalid = [{"enabled": "false"}, {"config": []}, {"options": {"tasks": "false"}},
                   {"schedule_time": "24:00"}, {"name": 7}, {"config": {"phone": 13800001234}},
                   {"options": {"shell": "echo"}}, {"config": {"arbitrary_path": "/etc"}}]
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValueError):
                normalize_account({**account_data(), **update})

    def test_editing_phone_rejected(self):
        with self.assertRaises(ValueError):
            normalize_account({"config": {"phone": "13900001234"}}, normalize_account(account_data()))

    def test_task_schedules_are_validated_and_independent(self):
        base = account_data()
        with self.assertRaisesRegex(ValueError, "至少要选择一天"):
            normalize_account({**base, "schedules": {"rate": {"enabled": True, "frequency": "weekly"}}})
        with self.assertRaisesRegex(ValueError, "执行时间"):
            normalize_account({**base, "schedules": {"rate": {"enabled": True, "time": "25:00"}}})
        with self.assertRaisesRegex(ValueError, "星期取值无效"):
            normalize_account({**base, "schedules": {"rate": {"enabled": True, "frequency": "weekly",
                                                             "weekdays": [7]}}})
        with self.assertRaisesRegex(ValueError, "只能是每天或每周"):
            normalize_account({**base, "schedules": {"rate": {"enabled": True, "frequency": "monthly"}}})
        account = normalize_account({**base, "schedules": {
            "rate": {"enabled": True, "time": "10:00", "frequency": "weekly", "weekdays": [0, 2]},
            "fee": {"enabled": True, "time": "09:30"},
        }})
        self.assertEqual(sorted(account["schedules"]), ["fee", "rate", "sign", "wed"])
        self.assertTrue(account["schedules"]["rate"]["enabled"])
        self.assertEqual(account["schedules"]["rate"]["weekdays"], [0, 2])
        self.assertFalse(account["schedules"]["sign"]["enabled"])
        self.assertTrue(account["enabled"], "有任意任务开启时账号视为自动运行")
        self.assertIsNone(normalize_account({**base, "schedules": {
            "rate": {"enabled": False}}})["next_run"])

    def test_weekly_puzzle_is_not_part_of_the_sign_run(self):
        account = normalize_account({**account_data(), "options": {"wed": True, "claim": False}})
        self.assertFalse(account["options"]["wed"])
        self.assertFalse(account["options"]["claim"])
        args = task_args("sign", "config.json", account["options"], False)
        self.assertNotIn("--wed", args)
        self.assertIn("--no-claim", args)

    def test_timezone_and_midnight(self):
        now = datetime(2026, 10, 12, 15, 59, tzinfo=timezone.utc)
        self.assertEqual(next_run("00:01", "Asia/Shanghai", now), "2026-10-12T16:01:00+00:00")
        self.assertEqual(next_run("08:23", "Asia/Shanghai", now), "2026-10-13T00:23:00+00:00")

    def test_password_default_and_length_rule(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings.from_env().password, "123456")
        with patch.dict(os.environ, {"CMCC_WEB_PASSWORD": "12345"}, clear=True), \
                self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {"CMCC_WEB_PASSWORD": "abc123"}, clear=True):
            self.assertEqual(Settings.from_env().password, "abc123")

    def test_invalid_timezone_has_clear_error(self):
        with patch.dict(os.environ, {"CMCC_WEB_PASSWORD": "test-password-only", "TZ": "invalid/not-a-zone"}, clear=True):
            with self.assertRaisesRegex(ValueError, "IANA"):
                Settings.from_env()

    def test_task_arguments_and_read_only_flag(self):
        args = task_args("sign", Path("/private/config.json"), {"claim": False, "tasks": True}, True)
        self.assertIn("--dry-run", args)
        self.assertIn("--tasks", args)
        self.assertIn("--no-claim", args)
        self.assertNotIn("--games", args)
        self.assertEqual(task_args("fee", Path("a.json"), {}, True), ["--config", "a.json"])

    def test_cache_keys_distinguish_same_phone_suffix(self):
        self.assertNotEqual(account_key("13800001234"), account_key("13900001234"))

    def test_private_atomic_write_and_process_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nested" / "cache.json"
            atomic_json(path, {"token": "first"})
            atomic_json(path, {"token": "second"})
            self.assertEqual(json.loads(path.read_text())["token"], "second")
            self.assertEqual(list(path.parent.glob(".write-*")), [])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            lock_path = Path(root) / "app.lock"
            with ProcessLock(lock_path):
                with self.assertRaises(RuntimeError):
                    with ProcessLock(lock_path):
                        self.fail("must not enter")
            with ProcessLock(lock_path):
                pass

    def test_log_redaction(self):
        text = "JSESSIONID=abc; UID=def; https://example.com/?token=secret&sid=abcdef 13800001234 token-value"
        output = redact(text, {"app_token": "token-value"})
        for value in ("abc;", "def;", "secret", "abcdef", "13800001234", "token-value"):
            self.assertNotIn(value, output)


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_seed_change_persistence_and_reset(self):
        store = AuthStore(self.root, "123456")
        self.assertTrue(store.verify("123456"))
        self.assertTrue(store.is_default)
        self.assertFalse(store.verify("1234567"))
        self.assertFalse(store.verify(None))
        self.assertFalse(store.verify("x" * 200))
        for bad in ("12345", "", "with\nnewline"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                store.set_password(bad)
        store.set_password("new-password-9")
        self.assertFalse(store.verify("123456"))
        self.assertFalse(store.is_default)
        record = (self.root / "auth.json").read_text(encoding="utf-8")
        self.assertNotIn("new-password-9", record)
        self.assertNotIn("123456", record)
        if os.name != "nt":
            self.assertEqual((self.root / "auth.json").stat().st_mode & 0o777, 0o600)
        # 重启使用数据卷中的密码，环境变量只在首次启动生效
        restarted = AuthStore(self.root, "another-password")
        self.assertTrue(restarted.verify("new-password-9"))
        self.assertFalse(restarted.verify("another-password"))
        # 只有显式重置才重新采用环境变量
        AuthStore(self.root, "reset-password-1", reset=True)
        self.assertTrue(AuthStore(self.root, "ignored-password").verify("reset-password-1"))

    def test_fingerprint_changes_with_password_and_corruption_is_reported(self):
        store = AuthStore(self.root, "123456")
        before = store.fingerprint
        store.set_password("changed-password")
        self.assertNotEqual(before, store.fingerprint)
        (self.root / "auth.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            AuthStore(self.root, "123456")


if __name__ == "__main__":
    unittest.main()
