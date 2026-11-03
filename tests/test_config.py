from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from app.config import AccountConfig, AppConfig, ConfigStore, MAX_ACCOUNTS, normalize_server, validate
from app.redact import Redactor
from app.secrets import protect, unprotect


def _accounts(*pairs: tuple[str, str]) -> list[AccountConfig]:
    return [AccountConfig(account=account, password=password) for account, password in pairs]


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
        cfg = AppConfig(server="", token="", name="", accounts=[])
        errors = validate(cfg)
        self.assertTrue(any("服务器" in item for item in errors))
        self.assertTrue(any("令牌" in item for item in errors))
        self.assertTrue(any("客户端名称" in item for item in errors))
        self.assertTrue(any("萌卡账号" in item for item in errors))

    def test_capacity_equals_account_count(self):
        cfg = AppConfig(server="http://example.test", token="x", name="pc",
                        accounts=_accounts(("a", "p"), ("b", "p"), ("c", "p")))
        self.assertEqual(cfg.capacity, 3)
        self.assertEqual([], validate(cfg))

    def test_too_many_accounts_rejected(self):
        pairs = tuple((f"acc{index}", "p") for index in range(MAX_ACCOUNTS + 1))
        cfg = AppConfig(server="http://example.test", token="x", name="pc",
                        accounts=_accounts(*pairs))
        self.assertTrue(any("最多支持" in item for item in validate(cfg)))

    def test_duplicate_account_rejected(self):
        cfg = AppConfig(server="http://example.test", token="x", name="pc",
                        accounts=_accounts(("same", "p"), ("SAME", "p")))
        self.assertTrue(any("重复" in item for item in validate(cfg)))

    def test_account_without_password_rejected(self):
        cfg = AppConfig(server="http://example.test", token="x", name="pc",
                        accounts=_accounts(("lonely", "")))
        self.assertTrue(any("密码" in item for item in validate(cfg)))


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

    def test_config_store_encrypts_token_and_passwords(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(server="http://example.test", token="secret-token",
                               name="pc-test", remember_password=True,
                               accounts=_accounts(("first", "secret-password-1"),
                                                  ("second", "secret-password-2")))
            self.assertEqual(store.save(config), [])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", raw)
            self.assertNotIn("secret-password-1", raw)
            self.assertNotIn("secret-password-2", raw)
            loaded = store.load()
            self.assertEqual(loaded.config.token, "secret-token")
            self.assertEqual([item.account for item in loaded.config.accounts],
                             ["first", "second"])
            self.assertEqual([item.password for item in loaded.config.accounts],
                             ["secret-password-1", "secret-password-2"])
            self.assertEqual(loaded.config.capacity, 2)

    def test_password_is_not_saved_when_remember_is_off(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(server="http://example.test", token="secret-token",
                               name="pc-test", remember_password=False,
                               accounts=_accounts(("account", "secret-password")))
            store.save(config)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["accounts"][0]["password_enc"], "")
            loaded = store.load()
            self.assertEqual([item.account for item in loaded.config.accounts], ["account"])
            self.assertEqual(loaded.config.accounts[0].password, "")

    def test_old_single_account_config_is_migrated(self):
        """旧版本只有一个 account/password_enc，读取后应变成一条账号。"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "server": "http://example.test",
                "token_enc": protect("old-token"),
                "name": "pc-old",
                "capacity": 2,
                "account": "legacy",
                "password_enc": protect("legacy-pass"),
                "remember_password": True,
                "keep_local": False,
            }, ensure_ascii=False), encoding="utf-8")
            loaded = ConfigStore(path).load()
            self.assertTrue(loaded.migrated)
            self.assertEqual([item.account for item in loaded.config.accounts], ["legacy"])
            self.assertEqual(loaded.config.accounts[0].password, "legacy-pass")
            self.assertEqual(loaded.config.token, "old-token")


if __name__ == "__main__":
    unittest.main()
