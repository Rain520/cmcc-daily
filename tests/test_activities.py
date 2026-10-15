"""后端预置的秒杀活动清单：同步、可改字段与参数校验。"""

import tempfile
import unittest
from pathlib import Path

from cmcc_app.activities import ACTIVITIES, definitions
from cmcc_app.models import (SECKILL_DEFAULTS, Store, activity_config, activity_settings,
                             activity_time, activity_weekdays, normalize_account)


def account(name, phone):
    return normalize_account({"name": name, "config": {"phone": phone, "app_token": "preview-token"}})


class ActivityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.first = account("示例账号甲", "13800000011")
        self.store.save(self.first)

    def test_registry_entries_are_well_formed(self):
        self.assertTrue(ACTIVITIES, "至少要有一个内置活动")
        for item in ACTIVITIES:
            with self.subTest(activity=item["id"]):
                self.assertRegex(item["id"], r"^[a-z0-9][a-z0-9-]*$")
                self.assertEqual(item["kind"], "seckill", "kind 同时是执行记录里的任务名")
                self.assertTrue(item["name"].strip())
                self.assertTrue(item["description"].strip())
                self.assertEqual(set(item["defaults"]), set(SECKILL_DEFAULTS))

    def test_sync_adds_missing_activities_disabled(self):
        result = self.store.sync_activities(ACTIVITIES)
        self.assertEqual(result["added"], [item["id"] for item in ACTIVITIES])
        activity = self.store.activities()[0]
        self.assertFalse(activity["enabled"], "内置活动默认停用，等用户自己打开")
        self.assertEqual(activity["time"], definitions()[activity["id"]]["defaults"]["time"])
        self.assertEqual(activity["weekdays"], list(range(7)))
        self.assertEqual(activity["accounts"], [self.first["id"]])
        # 再同步一次不会重复插入
        again = self.store.sync_activities(ACTIVITIES)
        self.assertEqual((again["added"], again["updated"]), ([], [item["id"] for item in ACTIVITIES]))
        self.assertEqual(len(self.store.activities()), len(ACTIVITIES))

    def test_sync_keeps_user_settings_and_refreshes_accounts(self):
        self.store.sync_activities(ACTIVITIES)
        activity = self.store.activities()[0]
        activity.update({"enabled": True, "time": "12:34:56", "weekdays": [0, 2],
                         "config": {**activity["config"], "lead": 1, "interval": 0.5, "max_attempts": 40}})
        self.store.save_activity(activity)
        second = account("示例账号乙", "13700000022")
        self.store.save(second)
        self.store.sync_activities(ACTIVITIES)
        stored = self.store.activities()[0]
        self.assertTrue(stored["enabled"], "用户打开的开关不能被同步覆盖")
        self.assertEqual((stored["time"], stored["weekdays"]), ("12:34:56", [0, 2]))
        self.assertEqual({key: stored["config"][key] for key in ("lead", "interval", "max_attempts")},
                         {"lead": 1, "interval": 0.5, "max_attempts": 40})
        self.assertEqual(stored["accounts"], [self.first["id"], second["id"]], "新增账号自动加入")

    def test_sync_removes_activities_dropped_from_the_list(self):
        self.store.sync_activities(ACTIVITIES)
        result = self.store.sync_activities(())
        self.assertEqual(result["removed"], [item["id"] for item in ACTIVITIES])
        self.assertEqual(self.store.activities(), [])

    def test_sync_keeps_activities_with_pending_runs(self):
        self.store.sync_activities(ACTIVITIES)
        activity = self.store.activities()[0]
        run_id = self.store.enqueue(self.first["id"], "seckill", activity_id=activity["id"])
        result = self.store.sync_activities(())
        self.assertEqual(result["kept"], [activity["id"]], "还有排队中的任务时先不下线")
        self.assertEqual([item["id"] for item in self.store.activities()], [activity["id"]])
        self.store.finish(run_id, "success", 0, "执行完成")
        self.assertEqual(self.store.sync_activities(())["removed"], [activity["id"]])

    def test_activity_accounts_follow_the_current_account_list(self):
        self.store.sync_activities(ACTIVITIES)
        self.assertEqual(self.store.activity_account_ids(), [self.first["id"]])
        second = account("示例账号乙", "13700000022")
        self.store.save(second)
        self.assertEqual(self.store.activity_account_ids(), [self.first["id"], second["id"]])


class ActivitySettingsTests(unittest.TestCase):
    def setUp(self):
        self.activity = {"id": "seckill-daily", "name": "内置活动", "kind": "seckill",
                         "config": dict(SECKILL_DEFAULTS), "time": "11:59:55", "weekdays": [0],
                         "accounts": ["a"], "enabled": False}

    def test_settings_only_take_editable_fields(self):
        updated = activity_settings({"time": "12:00:00", "lead": 3, "interval": 0.5, "max_attempts": 40,
                                     "weekdays": [1, 3], "enabled": True,
                                     "name": "改名", "kind": "别的", "accounts": ["b"]}, self.activity)
        self.assertEqual((updated["time"], updated["weekdays"], updated["enabled"]), ("12:00:00", [1, 3], True))
        self.assertEqual({key: updated["config"][key] for key in ("lead", "interval", "max_attempts")},
                         {"lead": 3, "interval": 0.5, "max_attempts": 40})
        self.assertEqual((updated["name"], updated["kind"], updated["accounts"]),
                         ("内置活动", "seckill", ["a"]))

    def test_settings_accept_nested_config_and_keep_missing_fields(self):
        updated = activity_settings({"config": {"lead": 2}}, self.activity)
        self.assertEqual(updated["config"]["lead"], 2)
        self.assertEqual(updated["config"]["interval"], SECKILL_DEFAULTS["interval"])
        self.assertEqual(updated["time"], "11:59:55")

    def test_settings_reject_invalid_values(self):
        for body in ({"time": "25:00"}, {"time": "12:60"}, {"time": "12:00:60"}, {"lead": 31}, {"lead": -1}, {"interval": 0},
                     {"max_attempts": 0}, {"max_attempts": 601}, {"weekdays": []}, {"weekdays": [7]},
                     {"config": "不是对象"}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    activity_settings(body, self.activity)

    def test_helpers_validate_individual_fields(self):
        self.assertEqual(activity_time({}, {}), SECKILL_DEFAULTS["time"])
        self.assertEqual(activity_time({"time": "09:05"}, {}), "09:05")
        self.assertEqual(activity_weekdays({}, {}), list(range(7)))
        self.assertEqual(activity_weekdays({"weekdays": [3, 1, 1]}, {}), [1, 3])
        config = activity_config({"config": {"lead": 1.9, "interval": 1, "max_attempts": 5.9}})
        self.assertEqual(config["lead"], 1, "提前量取整")
        self.assertEqual(config["interval"], 1.0)
        self.assertEqual(config["max_attempts"], 5, "次数取整")
        with self.assertRaises(ValueError):
            activity_config({"lead": "很多"})


if __name__ == "__main__":
    unittest.main()
