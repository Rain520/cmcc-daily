import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from cmcc_sign import Config, masked_phone, notify, notify_channels, notify_task, validate_webhook, webhook_request
from cmcc_app.logging_utils import redact


class TaskTitleTests(unittest.TestCase):
    """任务通知标题固定为「手机号（中间四位隐藏）+ 任务名」。"""

    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def config(self, phone):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({"phone": phone, "app_token": "test-token"}), encoding="utf-8")
        return Config(str(path))

    def captured_title(self, phone, task, detail):
        captured = {}

        def fake(cfg, title, detail_text, timeout=15):
            captured.update(title=title, detail=detail_text)
            return []

        with patch("cmcc_sign.notify_channels", fake):
            notify_task(self.config(phone), task, detail)
        return captured

    def test_title_is_masked_phone_then_task(self):
        captured = self.captured_title("13800000011", "移动评价有礼", "本周已评过（上次 10 分），无需操作\n评价币余额 0")
        self.assertEqual(captured["title"], "138****0011 移动评价有礼")
        self.assertEqual(captured["detail"], "本周已评过（上次 10 分），无需操作\n评价币余额 0")
        self.assertNotIn("手机尾号", captured["detail"])

    def test_masking_keeps_first_three_and_last_four(self):
        self.assertEqual(self.captured_title("13700000022", "移动签到通知", "签到成功")["title"], "137****0022 移动签到通知")
        self.assertEqual(masked_phone("13800001234"), "138****1234")
        self.assertEqual(masked_phone(""), "????")
        self.assertEqual(masked_phone("138-0000-1234"), "138****1234")
        self.assertEqual(masked_phone("12345"), "12345")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config(None)
        self.cfg.webhook_url = "https://example.com/hook/private-key"
        self.cfg.webhook_token = "private-bearer-token"
        session = patch("cmcc_sign.requests.Session")
        self.session = session.start().return_value
        self.addCleanup(session.stop)
        self.session.post.return_value.status_code = 204

    def test_json_authorization_timeout_and_no_redirects(self):
        notify(self.cfg, "签到结果", "今天已签到\n累计 10 天")
        self.session.post.assert_called_once_with(
            self.cfg.webhook_url, timeout=15, allow_redirects=False,
            json={"title": "签到结果", "body": "今天已签到\n累计 10 天"},
            headers={"Authorization": "Bearer private-bearer-token"})
        self.assertFalse(self.session.trust_env)
        self.session.close.assert_called_once()

    def test_optional_authorization_and_legacy_config(self):
        self.cfg.webhook_token = ""
        notify(self.cfg, "title", "body")
        self.assertEqual(self.session.post.call_args.kwargs["headers"], {})
        self.session.post.reset_mock()
        notify(Config(None), "title", "body")
        self.session.post.assert_not_called()

    def test_channels_independent_and_webhook_body_not_truncated(self):
        self.cfg.serverchan_sendkey = "SCTsecret"
        self.cfg.bark_url = "https://example.com/bark-secret"

        def fake_post(url, **kwargs):
            if "sctapi" in url:
                raise requests.Timeout("secret-url")
            return Mock(status_code=200)

        self.session.post.side_effect = fake_post
        with self.assertLogs("cmcc_sign", level="INFO") as logs:
            notify(self.cfg, "title", "中" * 5000)
        calls = self.session.post.call_args_list
        self.assertEqual(len(calls), 3)
        bodies = {call.args[0]: call.kwargs["json"]["body"] for call in calls if "json" in call.kwargs}
        self.assertEqual(len(bodies), 2)
        self.assertLess(len(bodies["https://example.com/bark-secret"]), 4000)
        self.assertEqual(len(bodies[self.cfg.webhook_url]), 5000)
        serverchan = [call for call in calls if "sctapi" in call.args[0]][0]
        self.assertEqual(serverchan.kwargs["data"]["title"], "title")
        output = "\n".join(logs.output)
        self.assertNotIn("secret-url", output)
        self.assertIn("Server酱 发送失败", output)
        self.assertIn("Bark 发送成功", output)

    def test_each_channel_targets_its_own_endpoint(self):
        self.cfg.serverchan_sendkey = "SCTsecret"
        self.cfg.bark_url = "https://example.com/bark-key"
        self.session.post.side_effect = None
        self.session.post.return_value = Mock(status_code=200)
        notify(self.cfg, "t", "d")
        calls = self.session.post.call_args_list
        urls = [call.args[0] for call in calls]
        self.assertEqual(len(urls), 3)
        self.assertIn("https://sctapi.ftqq.com/SCTsecret.send", urls)
        self.assertIn("https://example.com/bark-key", urls)
        self.assertIn(self.cfg.webhook_url, urls)
        self.assertEqual(sorted(len(call.args) for call in calls), [1, 1, 1])
        data_calls = [call for call in calls if "data" in call.kwargs]
        json_calls = [call for call in calls if "json" in call.kwargs]
        self.assertEqual([call.args[0] for call in data_calls], ["https://sctapi.ftqq.com/SCTsecret.send"])
        self.assertEqual(sorted(call.args[0] for call in json_calls),
                         sorted(["https://example.com/bark-key", self.cfg.webhook_url]))

    def test_notify_channels_reports_each_channel_without_leaking(self):
        self.assertEqual(notify_channels(Config(None), "t", "d"), [])
        self.cfg.webhook_url = "http://insecure.example.com/hook"
        self.session.post.reset_mock()
        results = notify_channels(self.cfg, "t", "d")
        self.assertEqual([r["channel"] for r in results], ["Webhook"])
        self.assertFalse(results[0]["ok"])
        self.assertIn("HTTPS", results[0]["message"])
        self.session.post.assert_not_called()
        self.cfg.webhook_url = "https://example.com/hook"
        self.session.post.side_effect = None
        self.session.post.return_value = Mock(status_code=200)
        self.assertTrue(notify_channels(self.cfg, "t", "d")[0]["ok"])
        self.assertEqual(notify_channels(self.cfg, "t", "d")[0]["message"], "HTTP 200")
        self.session.post.return_value = Mock(status_code=500)
        failed = notify_channels(self.cfg, "t", "d")[0]
        self.assertFalse(failed["ok"])
        self.assertIn("500", failed["message"])
        self.session.post.side_effect = requests.ConnectionError("boom at https://example.com/hook")
        broken = notify_channels(self.cfg, "t", "d")[0]
        self.assertFalse(broken["ok"])
        self.assertIn("ConnectionError", broken["message"])
        self.assertNotIn("example.com", broken["message"])
        self.cfg.webhook_token = ""
        self.cfg.webhook_url = ""
        self.cfg.serverchan_sendkey = "SCTsecret"
        self.cfg.bark_url = ""
        self.session.post.side_effect = None
        self.session.post.return_value = Mock(status_code=200)
        self.assertEqual([r["channel"] for r in notify_channels(self.cfg, "t", "d")], ["Server酱"])

    def test_webhook_payload_adapts_to_receiving_service(self):
        kind, kwargs = webhook_request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcde", "标题", "正文")
        self.assertEqual(kind, "wecom")
        self.assertEqual(kwargs["json"], {"msgtype": "text", "text": {"content": "标题\n\n正文"}})
        kind, kwargs = webhook_request("https://oapi.dingtalk.com/robot/send?access_token=abc", "标题", "正文")
        self.assertEqual((kind, kwargs["json"]["msgtype"]), ("dingtalk", "text"))
        kind, kwargs = webhook_request("https://open.feishu.cn/open-apis/bot/v2/hook/abc", "标题", "正文")
        self.assertEqual((kind, kwargs["json"]["msg_type"]), ("feishu", "text"))
        self.assertEqual(kwargs["json"]["content"]["text"], "标题\n\n正文")
        kind, kwargs = webhook_request("https://example.com/hook", "标题", "正文")
        self.assertEqual((kind, kwargs["json"]), ("generic", {"title": "标题", "body": "正文"}))
        _, long_kwargs = webhook_request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcde", "标题", "中" * 3000)
        content = long_kwargs["json"]["text"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 2000)
        self.assertTrue(content.endswith("…（内容过长已截断）"))

    def test_business_error_code_is_reported_as_failure(self):
        self.cfg.webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcde"
        response = Mock(status_code=200)
        response.json.return_value = {"errcode": 93000, "errmsg": "invalid webhook url"}
        self.session.post.return_value = response
        self.session.post.side_effect = None
        failed = notify_channels(self.cfg, "标题", "正文")[0]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["channel"], "企业微信 Webhook")
        self.assertIn("93000", failed["message"])
        self.assertIn("invalid webhook url", failed["message"])
        self.assertNotIn("abcde", failed["message"])
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        accepted = notify_channels(self.cfg, "标题", "正文")[0]
        self.assertTrue(accepted["ok"])
        self.assertIn("errcode 0", accepted["message"])
        response.json.return_value = {"code": 19001, "msg": "param invalid"}
        self.assertIn("19001", notify_channels(self.cfg, "标题", "正文")[0]["message"])
        response.json.side_effect = ValueError("not json")
        self.assertTrue(notify_channels(self.cfg, "标题", "正文")[0]["ok"])

    def test_errors_do_not_leak_url_token_or_response(self):
        for status in (302, 401, 500):
            with self.subTest(status=status):
                self.session.post.return_value = Mock(status_code=status, text="response-secret")
                with self.assertLogs("cmcc_sign", level="WARNING") as logs:
                    notify(self.cfg, "title", "body")
                output = "\n".join(logs.output)
                self.assertIn(str(status), output)
                for secret in (self.cfg.webhook_url, self.cfg.webhook_token, "response-secret"):
                    self.assertNotIn(secret, output)
        self.session.post.side_effect = requests.ConnectionError(self.cfg.webhook_url)
        with self.assertLogs("cmcc_sign", level="WARNING") as logs:
            notify(self.cfg, "title", "body")
        self.assertNotIn(self.cfg.webhook_url, "\n".join(logs.output))

    def test_invalid_settings_rejected_before_sending(self):
        for url in ("http://example.com", "https://127.0.0.1", "https://10.0.0.1",
                    "https://localhost", "https://user:pass@example.com",
                    "https://example.com/#secret", "https://example.com:bad", "https://example.com/a\nb"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_webhook(url)
        self.cfg.webhook_token = "a\r\nInjected: header"
        with self.assertLogs("cmcc_sign", level="WARNING"):
            notify(self.cfg, "title", "body")
        self.session.post.assert_not_called()

    def test_file_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps({"webhook_url": self.cfg.webhook_url,
                                        "webhook_token": self.cfg.webhook_token}), encoding="utf-8")
            self.assertEqual(Config(str(path)).webhook_url, self.cfg.webhook_url)
            with patch.dict(os.environ, {"CMCC_WEBHOOK_URL": "https://example.org/hook",
                                         "CMCC_WEBHOOK_TOKEN": "env-token"}):
                cfg = Config(str(path))
                self.assertEqual(cfg.webhook_url, "https://example.org/hook")
                self.assertEqual(cfg.webhook_token, "env-token")

    def test_saved_webhook_secrets_redacted(self):
        config = {"webhook_url": self.cfg.webhook_url, "webhook_token": self.cfg.webhook_token}
        output = redact(f"{self.cfg.webhook_url} {self.cfg.webhook_token}", config)
        for secret in config.values():
            self.assertNotIn(secret, output)
