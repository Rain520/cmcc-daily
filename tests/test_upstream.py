import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cmcc_fee
import cmcc_rate
import cmcc_seckill
import cmcc_sign


class UpstreamAdaptationTests(unittest.TestCase):
    def test_cache_persists_and_full_phone_is_checked(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CMCC_DATA_DIR": root}):
            first = SimpleNamespace(phone="13800001234")
            second = SimpleNamespace(phone="13900001234")
            cmcc_sign.save_jwt_cache(first, "first-jwt")
            cmcc_sign.save_jwt_cache(second, "second-jwt")
            self.assertEqual(cmcc_sign.load_jwt_cache(first)[0], "first-jwt")
            self.assertEqual(cmcc_sign.load_jwt_cache(second)[0], "second-jwt")
            initial = json.loads(cmcc_sign.jwt_cache_file(first).read_text())
            cmcc_sign.save_jwt_cache(first, "renewed")
            updated = json.loads(cmcc_sign.jwt_cache_file(first).read_text())
            self.assertEqual(initial["first_seen"], updated["first_seen"])
            self.assertEqual(updated["jwt"], "renewed")
            updated["phone"] = second.phone
            cmcc_sign.jwt_cache_file(first).write_text(json.dumps(updated))
            self.assertIsNone(cmcc_sign.load_jwt_cache(first))

    def test_expired_or_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CMCC_DATA_DIR": root}):
            cfg = SimpleNamespace(phone="13800001234")
            cmcc_sign.save_jwt_cache(cfg, "jwt")
            path = cmcc_sign.jwt_cache_file(cfg)
            path.write_text(json.dumps({"phone": cfg.phone, "jwt": "old", "saved_at": time.time() - 31 * 86400}))
            self.assertIsNone(cmcc_sign.load_jwt_cache(cfg))
            path.write_text("invalid-json")
            self.assertIsNone(cmcc_sign.load_jwt_cache(cfg))

    def test_fee_env_overrides_file_without_requiring_app_token(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(json.dumps({"phone": "13800001234", "fee_session_cookie": "from-file"}), encoding="utf-8-sig")
            with patch.dict(os.environ, {"CMCC_FEE_SESSION": "from-env"}, clear=True), \
                 patch.object(cmcc_fee, "query_fee", return_value={}) as query, \
                 patch.object(cmcc_fee, "notify"):
                self.assertEqual(cmcc_fee.run(str(path)), 0)
                self.assertEqual(query.call_args.args[1], "from-env")

    def test_seckill_dry_run_never_supplements_checkin(self):
        now = time.time() * 1000
        zone = {"id": 1, "startTime": now + 60000, "endTime": now + 600000,
                "prize": {"id": 2, "name": "test"}}
        def response(_session, _referer, url, payload=None):
            if url == cmcc_seckill.SECCONFIG_URL:
                return {"data": {"secKillData": {"secKillZones": [zone]}}}
            if url == cmcc_seckill.TODAY_MARK_URL:
                return {"data": {"markStatus": "unmarked"}}
            self.fail("dry-run attempted a mutation")
        with patch.object(cmcc_seckill, "exchange_session", return_value=(Mock(), "https://example.com")), \
             patch.object(cmcc_seckill, "server_now_ms", return_value=int(now)), \
             patch.object(cmcc_seckill, "api_post", side_effect=response), \
             patch.object(cmcc_seckill, "ensure_eligible") as eligible, \
             patch.object(cmcc_seckill, "do_mark") as mark:
            result = cmcc_seckill.run(SimpleNamespace(phone="13800001234"), True, False, None, .35, .4, 120)
            self.assertEqual(result, 1)
            eligible.assert_not_called()
            mark.assert_not_called()

    def test_rating_query_failure_is_not_reported_as_success(self):
        with patch.object(cmcc_rate, "exchange_session", return_value=(Mock(), "https://example.com")), \
             patch.object(cmcc_rate, "_request", return_value={"code": "NETWORK_ERROR", "msg": "offline"}):
            with self.assertRaises(RuntimeError):
                cmcc_rate.run(SimpleNamespace(phone="13800001234"), True, None)


if __name__ == "__main__":
    unittest.main()
