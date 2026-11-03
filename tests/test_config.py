from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig, ConfigStore, normalize_server, validate
from app.redact import Redactor
from app.secrets import protect, unprotect


class ConfigTests(unittest.TestCase):
    def test_server_normalization_accepts_plain_host_and_pasted_api_path(self):
        self.assertEqual(normalize_server("101.201.108.188:8787"),
                         "http://101.201.108.188:8787")
        self.assertEqual(normalize_server("http://example.test:8787/watch-api/status/"),
                         "http://example.test:8787")
        self.assertEqual(normalize_server("https://example.test/base/"),
                         "https://example.test/base")

    def test_invalid_server_rejected(self):
        with self.assertRaises(ValueError):
            normalize_server("ftp://example.test")
        with self.assertRaises(ValueError):
            normalize_server("http://")

    def test_validation_reports_all_missing_fields(self):
        cfg = AppConfig(server="", token="", name="", capacity=1,
                        account="", password="")
        errors = validate(cfg)
        self.assertTrue(any("服务器" in item for item in errors))
        self.assertTrue(any("令牌" in item for item in errors))
        self.assertTrue(any("客户端名称" in item for item in errors))
        self.assertTrue(any("萌卡账号" in item for item in errors))
        self.assertTrue(any("萌卡密码" in item for item in errors))

    def test_capacity_is_bounded_by_validation_not_silently(self):
        cfg = AppConfig(server="http://example.test", token="x", name="pc",
                        capacity=9, account="a", password="p")
        self.assertTrue(any("并发" in item for item in validate(cfg)))


class RedactTests(unittest.TestCase):
    def test_long_secrets_are_replaced_everywhere(self):
        redactor = Redactor(["TOKEN-123456", "hunter2"])
        text = redactor("token=TOKEN-123456 password=hunter2")
        self.assertNotIn("TOKEN-123456", text)
        self.assertNotIn("hunter2", text)
        self.assertEqual(text.count("***"), 2)

    def test_short_values_are_not_redacted_to_avoid_masking_normal_text(self):
        redactor = Redactor(["1", "ab"])
        self.assertEqual(redactor("abc 1"), "abc 1")


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class SecretStorageTests(unittest.TestCase):
    def test_dpapi_roundtrip(self):
        encrypted = protect("token-value-123")
        self.assertIsNotNone(encrypted)
        self.assertTrue(encrypted.startswith("dpapi-v1:"))
        self.assertNotIn("token-value-123", encrypted)
        self.assertEqual(unprotect(encrypted), "token-value-123")
        self.assertIsNone(unprotect("not-a-dpapi-value"))

    def test_config_store_encrypts_token_and_password(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(server="http://example.test", token="secret-token",
                               name="pc-test", capacity=1, account="account",
                               password="secret-password", remember_password=True)
            self.assertEqual(store.save(config), [])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", raw)
            self.assertNotIn("secret-password", raw)
            loaded = store.load()
            self.assertEqual(loaded.config.token, "secret-token")
            self.assertEqual(loaded.config.password, "secret-password")

    def test_password_is_not_saved_when_remember_is_off(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(server="http://example.test", token="secret-token",
                               name="pc-test", capacity=1, account="account",
                               password="secret-password", remember_password=False)
            store.save(config)
            loaded = store.load()
            self.assertEqual(loaded.config.password, "")


if __name__ == "__main__":
    unittest.main()
