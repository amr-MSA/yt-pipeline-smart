import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import telegram_utils


class TelegramFifoTests(unittest.TestCase):
    def test_long_aliases_and_gemini_prefix_route_to_long_smart(self):
        cases = [
            ("/long https://youtu.be/fixture123", "https://youtu.be/fixture123"),
            ("\\long https://youtu.be/fixture123", "https://youtu.be/fixture123"),
            ("//long https://youtu.be/fixture123", "https://youtu.be/fixture123"),
            ("جيمناي https://youtu.be/fixture123", "https://youtu.be/fixture123"),
            ("Gemini https://youtu.be/fixture123", "https://youtu.be/fixture123"),
            ("/LONG@bot https://youtu.be/fixture123", "https://youtu.be/fixture123"),
        ]
        for index, (text, expected_url) in enumerate(cases, start=1):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as state_dir:
                updates = [{
                    "update_id": 98700 + index,
                    "message": {
                        "text": text,
                        "chat": {"id": 123},
                        "message_id": 500 + index,
                        "date": 1793012300 + index,
                    },
                }]
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
            self.assertEqual(commands[0]["url"], expected_url)
            self.assertEqual(commands[0]["text"], expected_url)

    def test_gemini_prefix_is_not_queued_as_a_direct_short_link(self):
        updates = [{
            "update_id": 98799,
            "message": {
                "text": "جيمناي https://youtu.be/fixture123",
                "chat": {"id": 123},
                "message_id": 599,
                "date": 1793012399,
            },
        }]
        with tempfile.TemporaryDirectory() as state_dir:
            with (
                patch.object(telegram_utils, "_fetch_updates", return_value=updates),
                patch.object(telegram_utils, "load_inbox", return_value={}),
                patch.object(telegram_utils, "save_inbox"),
                patch.object(telegram_utils, "_acknowledge_updates"),
            ):
                links = telegram_utils.fetch_new_links(
                    "unused-test-token", state_dir, "shorts"
                )

        self.assertEqual(links, [])

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
