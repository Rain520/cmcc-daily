"""执行记录的分页、筛选与删除。"""

import re
import tempfile
import unittest
from html.parser import HTMLParser
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


def _media_applies(condition, width):
    if not condition:
        return True
    found = re.search(r"max-width:\s*(\d+)px", condition)
    if found:
        return width <= int(found.group(1))
    found = re.search(r"min-width:\s*(\d+)px", condition)
    return bool(found) and width >= int(found.group(1))


def resolve_style(css, selector, prop, width):
    """按视口宽度计算选择器最终生效的属性值（同优先级取最后一条声明）。"""
    value = None
    index = 0
    pattern = re.compile(r"([^{}@]+)\{([^{}]*)\}")
    while index < len(css):
        if css.startswith("@media", index):
            head_end = css.index("{", index)
            condition = css[index + 6:head_end].strip()
            depth, cursor = 0, head_end
            while cursor < len(css):
                if css[cursor] == "{":
                    depth += 1
                elif css[cursor] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            body = css[head_end + 1:cursor]
            for rule_selector, declarations in pattern.findall(body):
                if rule_selector.strip() == selector and _media_applies(condition, width):
                    value = _declaration(declarations, prop) or value
            index = cursor + 1
        else:
            match = pattern.match(css, index)
            if match:
                if match.group(1).strip() == selector:
                    value = _declaration(match.group(2), prop) or value
                index = match.end()
            else:
                index += 1
    return value


def _declaration(declarations, prop):
    for declaration in declarations.split(";"):
        name, _, raw = declaration.partition(":")
        if name.strip() == prop:
            return raw.strip()
    return None


VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                 "param", "source", "track", "wbr"}


class StructureParser(HTMLParser):
    """记录每个带 id 元素的祖先 id 链，用来验证页面的嵌套结构。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.paths = {}

    def handle_starttag(self, tag, attrs):
        identifier = dict(attrs).get("id")
        if identifier:
            self.paths[identifier] = tuple(item[1] for item in self.stack if item[1])
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, identifier))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return
        raise AssertionError(f"多余的结束标签 </{tag}>")


class HistoryViewTests(unittest.TestCase):
    """执行记录是独立界面，两个视图必须同级且各装各的内容。"""

    def setUp(self):
        root = Path(cmcc_app.__file__).resolve().parent
        self.html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.script = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.style = (root / "static" / "styles.css").read_text(encoding="utf-8")
        self.parser = StructureParser()
        self.parser.feed(self.html)
        self.paths = self.parser.paths

    def test_document_is_balanced(self):
        leftover = [item for item in self.parser.stack if item[0] not in ("html", "body")]
        self.assertEqual(leftover, [], "模板存在未闭合的标签")
        for element in ("accounts-view", "history-view", "account-grid", "run-list", "filter-account"):
            self.assertIn(element, self.paths, element)

    def test_views_are_siblings(self):
        self.assertEqual(self.paths["accounts-view"], self.paths["history-view"])
        self.assertIn('<div id="history-view" hidden>', self.html)

    def test_accounts_view_holds_account_content(self):
        for element in ("stat-accounts", "stat-enabled", "account-grid", "add-account", "empty-accounts",
                        "account-search", "account-search-clear", "account-search-note", "no-match-accounts",
                        "no-match-clear"):
            with self.subTest(element=element):
                self.assertIn("accounts-view", self.paths[element])
                self.assertNotIn("history-view", self.paths[element])

    def test_history_view_holds_filters_paging_and_delete(self):
        for element in ("run-count", "history-summary", "refresh", "filter-account", "filter-task",
                        "filter-status", "filter-source", "filter-from", "filter-to", "filter-keyword",
                        "filter-reset", "select-page", "selection-note", "delete-selected",
                        "delete-filtered", "page-prev", "page-info", "page-next", "page-size",
                        "run-list", "empty-runs"):
            with self.subTest(element=element):
                self.assertIn("history-view", self.paths[element])

    def test_account_search_is_wired(self):
        """账号列表搜索：输入框、清除按钮与无匹配提示都在账号视图里。"""
        for element in ("account-search", "account-search-clear", "no-match-accounts", "no-match-clear"):
            with self.subTest(element=element):
                self.assertIn("accounts-view", self.paths[element])
                self.assertNotIn("history-view", self.paths[element])
        self.assertIn('id="account-search" type="search"', self.html)
        self.assertIn('#no-match-accounts"', self.script)
        self.assertIn("function accountMatches(", self.script)
        self.assertIn("function visibleAccounts(", self.script)
        self.assertIn('$("#account-search").addEventListener("input"', self.script)
        self.assertIn('["#account-search", "#account-search-clear"]'.replace('["#account-search", "#account-search-clear"]', '$("#no-match-clear").addEventListener'), self.script)
        # 网格按过滤结果渲染，统计数字也要跟着变
        self.assertIn("for (const account of matches)", self.script)
        self.assertIn("$('#account-count').textContent = matches.length;".replace("'", '"'), self.script)

    def test_bulk_run_controls_are_wired(self):
        """账号卡片一键执行与页面顶部的一键执行全部。"""
        self.assertIn('id="batch-run"', self.html)
        self.assertIn("accounts-view", self.paths["batch-run"])
        for element in ("batch-dialog", "batch-form", "batch-hint", "batch-error", "batch-submit"):
            self.assertIn(f'id="{element}"', self.html)
        self.assertIn("function runAllForAccount(", self.script)
        self.assertIn("function batchCounts(", self.script)
        self.assertIn("function syncBatchDialog(", self.script)
        self.assertIn('api("/run-batch"', self.script)
        self.assertIn("/accounts/${account.id}/run-all", self.script)
        self.assertIn('button("一键执行"', self.script)
        self.assertIn("has_fee_session_cookie", self.script)
        self.assertIn(".heading-actions{", self.style)

    def test_run_dialog_only_blocks_the_same_task(self):
        """有任务在执行或排队时，手动执行仍可打开，只禁用同一任务并提示重复提交。"""
        for element in ("run-task", "run-dry-run", "run-hint", "run-submit"):
            self.assertIn(f'id="{element}"', self.html)
        self.assertIn("function syncRunDialog(", self.script)
        self.assertIn("option.disabled = busy;", self.script)
        self.assertIn("state.pendingTasks.has(", self.script)
        self.assertIn("pending_tasks", self.script)
        self.assertIn("已经执行成功，确定再次真实执行吗", self.script)
        self.assertIn('$("#run-dry-run").checked = done;', self.script)
        self.assertNotIn("runButton.disabled = pending;", self.script)
        self.assertIn(".run-hint{", self.style)

    def test_mobile_navigation_replaces_the_hidden_sidebar(self):
        """窄屏隐藏侧栏后必须仍有导航，否则手机上无法切换界面。"""
        self.assertIn('<nav id="mobile-nav"', self.html)
        self.assertIn("app-view", self.paths["mobile-nav"])
        self.assertIn("mobile-link", self.html)
        self.assertIn('document.querySelectorAll("[data-view]")', self.script)

    def test_mobile_styles_win_the_cascade(self):
        """按宽度实际计算样式：基础规则必须在媒体查询之前，否则会一直覆盖它。"""
        self.assertLess(self.style.index(".mobile-nav{display:none}"),
                        self.style.index("@media(max-width:420px)"),
                        "基础隐藏规则必须写在 420 断点之前")
        self.assertEqual(resolve_style(self.style, ".mobile-nav", "display", 400), "flex")
        self.assertEqual(resolve_style(self.style, ".mobile-nav", "display", 760), "none")
        self.assertEqual(resolve_style(self.style, ".mobile-nav", "display", 1200), "none")
        self.assertEqual(resolve_style(self.style, ".sidebar", "display", 400), "none")
        self.assertNotEqual(resolve_style(self.style, ".sidebar", "display", 1200), "none")
        self.assertNotEqual(resolve_style(self.style, ".breadcrumb", "display", 400), "none",
                        "1.6.0 的顶栏保持不变，面包屑不隐藏")
        self.assertNotEqual(resolve_style(self.style, ".topbar", "justify-content", 400),
                            "flex-end", "顶栏对齐方式沿用 1.6.0")
        self.assertEqual(resolve_style(self.style, "main", "padding-bottom", 400), "92px")
        self.assertEqual(resolve_style(self.style, ".mobile-link", "display", 400), "flex")

    def test_navigation_switches_between_views(self):
        for name in ("accounts", "history"):
            self.assertIn(f'data-view="{name}"', self.html)
            self.assertIn(f'id="{name}-view"', self.html)
        self.assertIn('id="breadcrumb-view"', self.html)
        self.assertIn("function showView", self.script)
        self.assertIn('state.view === "history"', self.script)
        self.assertIn('showView(window.location.hash.slice(1)', self.script)


if __name__ == "__main__":
    unittest.main()
