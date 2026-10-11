"""Offline checks for the public diagnostic command."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import _env  # noqa: F401
import bot


class TestDoctor(unittest.TestCase):
    def run_doctor(self, *args, cfg=None):
        output = io.StringIO()
        with patch.object(bot.tg, "load_config", return_value=cfg), \
                patch.object(bot.shutil, "which", return_value="/usr/bin/curl"), \
                redirect_stdout(output):
            result = bot.doctor(*args)
        return result, output.getvalue()

    def test_local_runtime_and_resolved_config(self):
        result, output = self.run_doctor(cfg={
            "bot_token": "private-token", "chat_id": "123", "allowed_user_ids": [1]
        })
        self.assertEqual(result, 0)
        self.assertIn("OK config.toml found and parsed", output)
        self.assertIn("OK bot token resolved", output)
        self.assertNotIn("private-token", output)

    def test_missing_config_fails_clearly(self):
        result, output = self.run_doctor(cfg=None)
        self.assertEqual(result, 1)
        self.assertIn("FAIL config.toml found and parsed", output)

    def test_telegram_check_is_opt_in_and_uses_get_me(self):
        cfg = {"bot_token": "private-token", "chat_id": "123"}
        with patch.object(bot.tg, "api", return_value=({"id": 1}, "")) as api:
            result, output = self.run_doctor(True, False, cfg=cfg)
        self.assertEqual(result, 0)
        api.assert_called_once_with(cfg, "getMe", {}, timeout=15, retries=1)
        self.assertIn("OK Telegram getMe reachable", output)

    def test_test_message_requires_explicit_flag(self):
        cfg = {"bot_token": "private-token", "chat_id": "123"}
        with patch.object(bot.tg, "api", return_value=({"message_id": 7}, "")) as api:
            result, output = self.run_doctor(False, True, cfg=cfg)
        self.assertEqual(result, 0)
        api.assert_called_once_with(cfg, "sendMessage", {
            "chat_id": "123", "text": "Firstmate Telegram Bridge doctor test"
        }, timeout=15, retries=1)
        self.assertIn("OK Telegram test message sent", output)


if __name__ == "__main__":
    unittest.main()
