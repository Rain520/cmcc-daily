import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
import cmcc_sign
import cmcc_extra
import cmcc_rate
from cmcc_app import results
from cmcc_app.account_status import summaries
from cmcc_app.backup import _history_row
from cmcc_app.models import Store, normalize_account, next_run
from cmcc_app.settings import Settings
from cmcc_app.web import create_app


def outcome(state="success", reason="ok", auth="valid", parts=None):
    return {"version": 1, "state": state, "reason": reason, "auth": auth,
            "parts": parts or {"main": {"state": state, "reason": reason}}}


class AccountStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Settings(Path(self.temp.name), "preview-password"))
        self.store = self.app.extensions["cmcc_store"]
        self.now = datetime(2026, 10, 14, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.account = normalize_account({"name": "示例账号", "config": {
            "phone": "13800000011", "app_token": "preview-token", "fee_session_cookie": "preview-cookie"},
            "options": {"claim": True, "tasks": True}, "schedules": {
                "sign": {"enabled": True, "time": "10:00"},
                "rate": {"enabled": True, "frequency": "weekly", "time": "09:00", "weekdays": [0]},
                "wed": {"enabled": True, "frequency": "weekly", "time": "14:00", "weekdays": [0, 2]}}})
        for entry in self.account["schedules"].values():
            if entry["enabled"]:
                entry["next_run"] = next_run(entry["time"], "Asia/Shanghai", self.now, entry["frequency"], entry["weekdays"])
        self.store.save(self.account)

    def add(self, task="sign", state="success", age=timedelta(minutes=10), result="auto", dry=False):
        run_id = self.store.enqueue(self.account["id"], task, dry)
        if state not in ("queued", "running"):
            self.store.finish(run_id, state, 0 if state == "success" else 1,
                              result=outcome(state) if result == "auto" else result)
        at = (self.now - age).astimezone(timezone.utc).isoformat()
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET status=?,created_at=?,started_at=?,finished_at=? WHERE id=?",
                         (state, at, at, None if state in ("queued", "running") else at, run_id))
        return run_id

    def summary(self, now=None):
        accounts = self.store.accounts()
        return summaries(self.store, accounts, "Asia/Shanghai", now or self.now)[self.account["id"]]

    def task(self, name, now=None):
        return next(t for t in self.summary(now)["tasks"] if t["task"] == name)

    def test_never_run_and_future_plan(self):
        self.assertEqual(self.summary()["state"], "unknown")
        self.assertEqual(self.task("wed")["state"], "waiting")

    def test_daily_resets_and_weekly_single_preserves_monday_success(self):
        self.add(age=timedelta(days=1))
        self.add("rate", age=timedelta(days=2))
        self.assertEqual(self.task("sign")["state"], "missed")
        self.assertEqual(self.task("rate")["state"], "success")
        self.assertEqual(self.task("sign", self.now.replace(hour=9))["state"], "waiting")

    def test_multi_weekday_does_not_count_monday_as_wednesday(self):
        self.add("wed", age=timedelta(days=2))
        self.assertEqual(self.task("wed")["state"], "waiting")
        self.assertEqual(self.task("wed", self.now.replace(hour=15))["state"], "missed")

    def test_query_does_not_complete_or_hide_failed_execution(self):
        failed = self.add(state="failed", result=outcome("failed", "business"), age=timedelta(hours=1))
        query = self.add(dry=True)
        self.assertEqual(self.summary()["state"], "warning")
        self.assertEqual(self.task("sign")["run_id"], failed)
        self.assertEqual(self.task("sign")["query_id"], query)
        self.assertEqual(self.task("sign")["state"], "failed")

    def test_query_only_has_no_completion(self):
        self.add(dry=True)
        self.assertEqual(self.task("sign")["state"], "waiting")

    def test_auth_recovery_does_not_hide_other_business_failure(self):
        self.add(state="failed", result=outcome("failed", "auth", "invalid"), age=timedelta(hours=3))
        self.assertEqual(self.summary()["state"], "invalid")
        self.add("wed", state="failed", result=outcome("failed", "business"), age=timedelta(hours=2))
        self.assertEqual(self.summary()["state"], "warning")
        self.add("rate")
        self.assertEqual(self.summary()["state"], "warning")

    def test_invalid_auth_survives_later_network_error(self):
        self.add(state="failed", result=outcome("failed", "auth", "invalid"), age=timedelta(hours=2))
        self.add(state="failed", result=outcome("failed", "network", "unknown"))
        self.assertEqual(self.summary()["state"], "invalid")

    def test_fee_cookie_is_independent(self):
        self.add("fee", state="failed", result=outcome("failed", "fee_auth", "invalid"), age=timedelta(hours=2))
        self.add("sign")
        self.assertEqual(self.summary()["state"], "invalid")
        self.add("fee", age=timedelta(minutes=1))
        self.assertEqual(self.summary()["state"], "normal")

    def test_retry_only_recovers_its_task(self):
        self.add(state="failed", age=timedelta(hours=2))
        self.add("wed", state="failed", age=timedelta(hours=1))
        self.add()
        self.assertEqual(self.summary()["state"], "warning")
        self.add("wed", age=timedelta(minutes=1))
        self.assertEqual(self.summary()["state"], "normal")

    def test_running_and_queued_keep_previous_issue(self):
        self.add(state="failed", result=outcome("failed", "auth", "invalid"))
        pending = self.add("rate", state="queued", age=timedelta())
        self.assertEqual(self.summary()["state"], "queued")
        self.assertEqual(self.summary()["issue"], "invalid")
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (pending,))
        self.assertEqual(self.summary()["state"], "running")

    def test_credential_change_resets_stale_health_and_subtasks(self):
        self.add(result=outcome(parts={"main": {"state": "success", "reason": "ok"},
                                      "tasks": {"state": "success", "reason": "ok"}}))
        revised = normalize_account({"config": {"app_token": "preview-token-new"}}, self.store.account(self.account["id"]))
        with patch("cmcc_app.models.utcnow", return_value=self.now):
            self.store.save(revised)
        self.assertEqual(self.summary()["state"], "unknown")
        self.assertEqual(self.task("sign")["parts"][1]["state"], "unexecuted")
        self.add(age=timedelta(minutes=-1))
        self.assertEqual(self.summary(self.now + timedelta(minutes=2))["state"], "normal")

    def test_legacy_records_no_fake_subtask_success(self):
        self.add(result=None)
        self.assertEqual(self.task("sign")["detail"], "详情未记录")
        self.assertTrue(all(p["state"] == "unknown" for p in self.task("sign")["parts"]))

    def test_fee_success_does_not_validate_replaced_app_token(self):
        self.add("fee")
        revised = normalize_account({"config": {"app_token": "preview-new-token"}}, self.store.account(self.account["id"]))
        with patch("cmcc_app.models.utcnow", return_value=self.now):
            self.store.save(revised)
        self.assertEqual(self.summary()["state"], "unknown")

    def test_manual_query_only_task_still_visible(self):
        revised = normalize_account({"schedules": {}}, self.store.account(self.account["id"]))
        self.store.save(revised)
        self.add("wed", dry=True)
        self.assertEqual(self.task("wed")["state"], "unexecuted")
        self.assertIsNotNone(self.task("wed")["query_id"])

    def test_part_codes_reach_the_account_card(self):
        self.add(result=outcome(parts={"main": {"state": "success", "reason": "ok"},
                                      "tasks": {"state": "success", "reason": "skipped"}}))
        sign = self.task("sign")
        self.assertEqual(sign["state"], "success")
        self.assertEqual([p["code"] for p in sign["parts"] if p["key"] == "tasks"], ["skipped"])

    def test_summary_independent_of_history_page_and_auth_required(self):
        self.add(result=outcome("partial", "partial"), state="partial")
        for i in range(25):
            self.add("rate", age=timedelta(minutes=i))
        client = self.app.test_client()
        self.assertEqual(client.get("/api/accounts").status_code, 401)
        client.post("/api/login", json={"password": "preview-password"})
        payload = client.get("/api/accounts").json
        account = payload["accounts"][0]
        self.assertEqual(account["summary"]["state"], "warning")
        self.assertNotIn("preview-token", json.dumps(payload))
        self.assertEqual(next(t for t in account["summary"]["tasks"] if t["task"] == "sign")["state"], "partial")

    def test_backup_preserves_partial_and_safe_structured_results(self):
        run_id = self.add(state="partial", result=outcome("partial", "partial"))
        row = self.store.run(run_id)
        safe = _history_row(row, self.account["id"], "示例")
        self.assertEqual(json.loads(safe["result"])["state"], "partial")
        safe["id"] = "imported-example"
        self.store.add_history([safe])
        self.assertEqual(self.store.run(safe["id"])["result"], safe["result"])


class StructuredResultTests(unittest.TestCase):
    def setUp(self):
        results.start("sign", {"claim": True, "tasks": True})
        self.addCleanup(lambda: results._current.set(None))

    def test_main_success_does_not_hide_child_failure(self):
        results.auth(True)
        results.mark()
        results.begin("claim")
        results.error(requests.Timeout("private-detail"))
        results.mark()
        report = results.finish(0)
        self.assertEqual(report["state"], "partial")
        self.assertEqual(report["parts"]["claim"]["reason"], "network")
        self.assertNotIn("private-detail", json.dumps(report))

    def test_expected_skips_are_not_treated_as_partial(self):
        """需要真实操作而跳过的任务属于正常情况，不应把整次执行标成部分成功。"""
        results.start("sign", {"tasks": True})
        results.auth(True)
        results.mark()
        results.begin("tasks")
        results.mark("success", "skipped")
        report = results.finish(0)
        self.assertEqual(report["state"], "success")
        self.assertEqual(report["parts"]["tasks"]["reason"], "skipped")

    def test_query_parts_not_marked_complete(self):
        results.start("sign", {"claim": True}, True)
        results.mark()
        results.begin("claim")
        results.mark()
        self.assertEqual(results.finish(0)["parts"]["claim"]["state"], "query")

    def test_upstream_claim_exception_reports_partial(self):
        cfg = SimpleNamespace(phone="13800000011")
        signed = {"userinfo": {}, "markstatus": [{"date": datetime.now().strftime("%Y%m%d"), "status": "1"}]}
        with patch.object(cmcc_sign, "exchange_session", return_value=(Mock(), "https://example.com")), \
                patch.object(cmcc_sign, "query_markstatus", side_effect=[signed, requests.Timeout()]), \
                patch.object(cmcc_sign, "notify_task"):
            code = cmcc_sign.run(cfg, False)
        self.assertEqual(results.finish(code)["parts"]["claim"]["state"], "failed")
        self.assertEqual(results.finish(code)["state"], "partial")

    def test_ai_bean_list_error_cannot_be_reported_as_no_tasks(self):
        results.begin("tasks")
        with patch.object(cmcc_extra, "api_call", return_value={"code": "NETWORK_ERROR"}):
            with self.assertRaises(RuntimeError):
                cmcc_extra.run_mark_tasks(Mock(), "https://example.com", Mock(), False)
        self.assertEqual(results.finish(0)["parts"]["tasks"]["state"], "failed")

    def test_safe_schema_rejects_free_form_data(self):
        report = results.clean({**outcome(), "reason": "preview-secret", "url": "private", "parts": {"secret": {"state": "success"}}})
        self.assertEqual(report["reason"], "unknown")
        self.assertEqual(report["parts"], {})
        self.assertEqual(results.clean({**outcome(), "reason": []})["reason"], "unknown")
        self.assertNotIn("private", json.dumps(report))

    def test_login_error_keeps_useful_reason(self):
        results.mark("failed", "login")
        results.error(RuntimeError("private text"))
        self.assertEqual(results.finish(1)["reason"], "login")

    def exchange(self, replies):
        session = Mock()
        session.get.return_value.text = "loginPath = '/appTokenLogin?sid=preview'"
        session.cookies.keys.return_value = ["QWHD_SESSION_TOKEN"]
        session.post.side_effect = [Mock(json=Mock(return_value=r)) for r in replies]
        cfg = SimpleNamespace(phone="13800000011", app_token="preview-token", act_url="https://example.com",
                              province_code="731", city_code="0731", carrier_operator="002", app_version_code="12.5.2")
        with patch.object(cmcc_sign.requests, "Session", return_value=session), \
                patch.object(cmcc_sign, "load_jwt_cache", return_value=("preview-jwt", 0)), \
                patch.object(cmcc_sign, "save_jwt_cache"), patch.object(cmcc_sign, "assert_safe_url"):
            return cmcc_sign.exchange_session(cfg)

    def test_jwt_fallback_recovery_stays_green(self):
        self.exchange([{"code": "EXPIRED", "msg": "jwt过期"},
                       {"code": "SUCCESS", "data": {"jwt": "preview-renewed", "url": "https://example.com"}}])
        self.assertEqual(results.finish(0)["auth"], "valid")

    def test_final_expired_token_is_red_and_network_error_is_not(self):
        with self.assertRaises(RuntimeError):
            self.exchange([{"code": "EXPIRED", "msg": "jwt过期"}, {"code": "EXPIRED", "msg": "token失效"}])
        results.error(RuntimeError("private"))
        self.assertEqual(results.finish(1)["reason"], "auth")
        results.start("sign", {})
        results.error(requests.Timeout())
        self.assertEqual(results.finish(1)["auth"], "unknown")
        self.assertEqual(results.finish(1)["reason"], "network")

    def test_rating_already_done_and_redemption_success(self):
        responses = {
            "/assess/markStatus": {"chance": False, "score": 10},
            "/account/query": {"balance": 20},
            "/activity/info": {"prizes": [{"id": "example", "name": "示例券", "exchangePrice": 10}]},
            "/assess/prizeStatus": {"prizeStatusHashMap": {"example": {"stockStatus": True, "chanceStatus": True}}},
            "/assess/redeem": {},
        }
        def reply(session, referer, method, path, params=None):
            return {"code": "SUCCESS", "data": responses[path]}
        results.start("rate", {})
        with patch.object(cmcc_rate, "exchange_session", return_value=(Mock(), "https://example.com")), \
                patch.object(cmcc_rate, "_request", side_effect=reply), patch.object(cmcc_rate, "notify_task"):
            code = cmcc_rate.run(SimpleNamespace(phone="13800000011"), False, "example")
        self.assertEqual(code, 0)
        self.assertEqual(results.finish(code)["state"], "success")

    def test_failed_diy_verification_does_not_count_as_task_disappeared(self):
        responses = [{"code": "SUCCESS", "data": [{"taskStage": "UNDO", "taskId": "example"}]},
                     {"code": "SUCCESS"}, {"code": "NETWORK_ERROR"}]
        results.begin("games")
        with patch.object(cmcc_extra, "api_call", side_effect=responses), patch.object(cmcc_extra.time, "sleep"):
            text = cmcc_extra.run_diy_tasks(Mock(), "https://example.com", "example", False)
        self.assertIn("完成 0 个", text)
        self.assertEqual(results.finish(0)["parts"]["games"]["state"], "partial")


if __name__ == "__main__":
    unittest.main()
