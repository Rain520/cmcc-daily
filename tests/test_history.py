"""执行记录的分页、筛选与删除。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cmcc_app
from cmcc_app.models import normalize_account
from cmcc_app.settings import Settings
from cmcc_app.web import create_app

PASSWORD = "test-password-12345"


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(Settings(self.root, PASSWORD))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["cmcc_store"]
        self.csrf = self.client.post("/api/login", json={"password": PASSWORD}).json["csrf"]
        self.headers = {"X-CSRF-Token": self.csrf}
        self.accounts = [self.add_account("13800001111", "账号甲"),
                         self.add_account("13900002222", "账号乙")]

    def add_account(self, phone, name):
        account = normalize_account({"name": name, "config": {"phone": phone, "app_token": "token"}})
        self.store.save(account)
        return account

    def add_run(self, account, task="sign", status="success", source="schedule", days_ago=0,
                message="执行完成", log_text=None, dry_run=False):
        run_id = self.store.enqueue(account["id"], task, dry_run)
        self.store.take_run()
        self.store.finish(run_id, status, 0 if status == "success" else 1, message)
        created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with self.store.connect() as conn:
            conn.execute("UPDATE runs SET created_at=?,source=? WHERE id=?", (created, source, run_id))
        if log_text is not None:
            (self.root / "logs" / f"{run_id}.log").write_text(log_text, encoding="utf-8")
        return run_id

    def runs(self, query=""):
        response = self.client.get(f"/api/runs{query}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json

    def test_pagination_reports_total_and_pages(self):
        for index in range(25):
            self.add_run(self.accounts[0], days_ago=index)
        first = self.runs("?page=1&page_size=10")
        self.assertEqual((len(first["runs"]), first["total"], first["pages"], first["page_size"]),
                         (10, 25, 3, 10))
        self.assertEqual(len(self.runs("?page=3&page_size=10")["runs"]), 5)
        self.assertEqual(self.runs("?page=9&page_size=10")["runs"], [])
        self.assertEqual(self.runs("?page=1&page_size=999")["page_size"], 20)
        self.assertEqual(self.runs("?page=0")["page"], 1)

    def test_filters_by_account_task_status_source_and_date(self):
        self.add_run(self.accounts[0], task="sign", message="签到完成")
        self.add_run(self.accounts[0], task="rate", status="failed", source="manual", message="评价失败")
        self.add_run(self.accounts[1], task="sign", status="interrupted", days_ago=10, message="已停止")
        self.assertEqual(self.runs(f"?account_id={self.accounts[0]['id']}")["total"], 2)
        self.assertEqual(self.runs("?task=rate")["total"], 1)
        self.assertEqual(self.runs("?status=failed")["total"], 1)
        self.assertEqual(self.runs("?source=manual")["total"], 1)
        self.assertEqual(self.runs("?status=不支持的状态")["total"], 3)
        local = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        today = local.strftime("%Y-%m-%d")
        yesterday = (local - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(self.runs(f"?date_from={today}")["total"], 2)
        self.assertEqual(self.runs(f"?date_to={today}")["total"], 3)  # 结束日期含当天
        self.assertEqual(self.runs(f"?date_to={yesterday}")["total"], 1)
        self.assertEqual(self.runs(f"?date_from={yesterday}&date_to={today}")["total"], 2)
        self.assertEqual(self.runs("?date_from=2026-13-45")["total"], 3)  # 非法日期忽略
        self.assertEqual(self.runs(f"?account_id={self.accounts[0]['id']}&status=failed")["total"], 1)

    def test_status_counts_show_whole_filter_result(self):
        self.add_run(self.accounts[0], status="success")
        self.add_run(self.accounts[0], status="failed")
        self.add_run(self.accounts[1], status="success")
        self.assertEqual(self.runs()["counts"], {"success": 2, "failed": 1})
        # 状态自身的筛选不参与统计，方便显示「共 N 条 · 成功 X · 失败 Y」。
        self.assertEqual(self.runs("?status=failed")["counts"], {"success": 2, "failed": 1})
        self.assertEqual(self.runs(f"?account_id={self.accounts[1]['id']}")["counts"], {"success": 1})

    def test_keyword_searches_records_and_logs(self):
        self.add_run(self.accounts[0], message="执行完成")
        target = self.add_run(self.accounts[0], task="rate", message="执行完成",
                              log_text="评价币余额 0，本周已评过")
        by_log = self.runs("?keyword=评价币")
        self.assertEqual([row["id"] for row in by_log["runs"]], [target])
        self.assertEqual(by_log["keyword"], "评价币")
        self.assertGreaterEqual(by_log["scanned"], 2)
        self.assertFalse(by_log["truncated"])
        self.assertEqual(self.runs("?keyword=账号甲")["total"], 2)
        self.assertEqual(self.runs("?keyword=评价失败")["total"], 0)

    def test_delete_selected_removes_records_and_logs(self):
        keep = self.add_run(self.accounts[0], message="保留")
        removed = self.add_run(self.accounts[1], task="rate", log_text="日志内容")
        response = self.client.post("/api/runs/delete", headers=self.headers, json={"ids": [removed]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json, {"ok": True, "deleted": 1, "skipped": 0})
        self.assertIsNone(self.store.run(removed))
        self.assertFalse((self.root / "logs" / f"{removed}.log").exists())
        self.assertIsNotNone(self.store.run(keep))

    def test_delete_keeps_pending_records(self):
        pending = self.store.enqueue(self.accounts[0]["id"], "sign")
        self.store.take_run()
        finished = self.add_run(self.accounts[1], task="rate")
        response = self.client.post("/api/runs/delete", headers=self.headers,
                                    json={"ids": [pending, finished]})
        self.assertEqual(response.json, {"ok": True, "deleted": 1, "skipped": 1})
        self.assertIsNotNone(self.store.run(pending))
        self.assertIsNone(self.store.run(finished))

    def test_clear_filtered_removes_only_finished_records(self):
        pending = self.store.enqueue(self.accounts[0]["id"], "sign")
        self.store.take_run()
        for index in range(3):
            self.add_run(self.accounts[1], days_ago=index)
        response = self.client.post("/api/runs/delete", headers=self.headers,
                                    json={"scope": "filtered", "filters": {}})
        self.assertEqual(response.json["deleted"], 3)
        self.assertIsNotNone(self.store.run(pending))
        self.assertEqual(self.runs("?page_size=10")["total"], 1)
        again = self.client.post("/api/runs/delete", headers=self.headers,
                                 json={"scope": "filtered", "filters": {"status": "failed"}})
        self.assertEqual(again.json["deleted"], 0)
        self.assertIsNotNone(self.store.run(pending))

    def test_delete_requires_login_csrf_and_payload(self):
        run_id = self.add_run(self.accounts[0])
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.post("/api/runs/delete", json={"ids": [run_id]}).status_code, 401)
        self.assertEqual(self.client.post("/api/runs/delete", json={"ids": [run_id]}).status_code, 403)
        for payload in ({}, {"ids": []}, {"ids": ["x"] * 501}, {"ids": [1, 2]}, {"scope": "all"}):
            response = self.client.post("/api/runs/delete", headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 400, payload)
        self.assertIsNotNone(self.store.run(run_id))


class HistoryViewTests(unittest.TestCase):
    """执行记录是独立界面，不再跟在账号总览下面。"""

    def setUp(self):
        root = Path(cmcc_app.__file__).resolve().parent
        self.html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.script = (root / "static" / "app.js").read_text(encoding="utf-8")

    def test_views_are_separate_and_navigable(self):
        self.assertIn('<div id="accounts-view"', self.html)
        self.assertIn('<div id="history-view" hidden>', self.html)
        self.assertLess(self.html.index('id="accounts-view"'), self.html.index('id="history-view"'))
        for name in ("accounts", "history"):
            self.assertIn(f'data-view="{name}"', self.html)
            self.assertIn(f'id="{name}-view"', self.html)
        self.assertIn('id="breadcrumb-view"', self.html)
        # 执行记录自己的标题栏包含计数、摘要与刷新
        heading = self.html[self.html.index('id="history-view"'):self.html.index('<section id="history"')]
        for element in ("run-count", "history-summary", "refresh"):
            self.assertIn(f'id="{element}"', heading, element)
        self.assertIn("function showView", self.script)
        self.assertIn('state.view === "history"', self.script)

    def test_history_view_keeps_the_history_controls(self):
        view = self.html[self.html.index('id="history-view"'):]
        for element in ("filter-account", "filter-keyword", "select-page", "delete-selected",
                        "page-size", "run-list", "empty-runs"):
            self.assertIn(f'id="{element}"', view, element)


if __name__ == "__main__":
    unittest.main()
