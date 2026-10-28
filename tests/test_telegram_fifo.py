import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import telegram_utils


class TelegramFifoTests(unittest.TestCase):
    def test_long_queue_report_with_source_url_is_not_reprocessed(self):
        updates = [{
            "update_id": 98766,
            "message": {
                "text": "📋 تقرير ملفات /long (1/1)" + chr(10) + "🎬 الرابط: https://youtu.be/fixture123",
                "chat": {"id": 123},
                "message_id": 457,
                "date": 1793012346,
            },
        }]
        with tempfile.TemporaryDirectory() as state_dir:
            with (
                patch.object(telegram_utils, "_fetch_updates", return_value=updates),
                patch.object(telegram_utils, "load_smart_long_inbox", return_value={}),
                patch.object(telegram_utils, "save_smart_long_inbox"),
                patch.object(telegram_utils, "_acknowledge_updates"),
            ):
                links, commands = telegram_utils.fetch_all_new_messages(
                    "unused-test-token", state_dir, "shorts"
                )

        self.assertEqual(links, [])
        self.assertEqual(commands, [])

    def test_long_command_keeps_message_date_and_global_update_id(self):
        updates = [{
            "update_id": 98765,
            "message": {
                "text": "/long https://youtu.be/fixture123",
                "chat": {"id": 123},
                "message_id": 456,
                "date": 1793012345,
            },
        }]
        with tempfile.TemporaryDirectory() as state_dir:
            with (
                patch.object(telegram_utils, "_fetch_updates", return_value=updates),
                patch.object(telegram_utils, "load_smart_long_inbox", return_value={}),
                patch.object(telegram_utils, "save_smart_long_inbox"),
                patch.object(telegram_utils, "_acknowledge_updates"),
            ):
                links, commands = telegram_utils.fetch_all_new_messages(
                    "unused-test-token", state_dir, "shorts"
                )

        self.assertEqual(links, [])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["date"], 1793012345)
        self.assertEqual(commands[0]["update_id"], 98765)
        self.assertEqual(commands[0]["chat_id"], 123)
        self.assertEqual(commands[0]["message_id"], 456)


if __name__ == "__main__":
    unittest.main()
