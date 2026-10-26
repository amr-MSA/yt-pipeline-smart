import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import queue_utils


class QueueUtilsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.temp.name, "state")

    def tearDown(self):
        self.temp.cleanup()

    def save_batch(self, queue_id="q1", clips=None, created_at="2026-10-25T10:00:00+00:00", fifo_order=1):
        queue_utils.save_new_batch(
            state_dir=self.state_dir,
            queue_id=queue_id,
            request_key=f"42:{fifo_order}",
            video_id="test-video",
            source_url="https://example.invalid/source",
            source_title="fixture",
            chat_id=42,
            clips=clips or [{"start": 0, "end": 10, "type": "shorts", "title": "t", "description": "d"}],
            created_at=created_at,
            fifo_order=fifo_order,
        )
        return os.path.join(self.state_dir, "long_smart_queue", f"{queue_id}.json")

    def load(self, path):
        return queue_utils.load_batch(path)

    def test_first_confirmed_failure_waits_until_next_utc_day(self):
        path = self.save_batch()
        status = queue_utils.mark_clip_failed(
            self.state_dir, "q1", 0, "private source url must not be stored",
            failure_type="youtube_http_403", failed_on="2026-10-26",
        )
        batch = self.load(path)

        self.assertEqual(status, "failed_retryable")
        self.assertEqual(batch["clips"][0]["confirmed_failures"], 1)
        self.assertEqual(batch["clips"][0]["retry_after_date"], "2026-10-27")
        self.assertNotIn("error", batch["clips"][0])
        self.assertIsNone(queue_utils.get_next_pending_index(batch, "2026-10-26"))
        self.assertEqual(queue_utils.get_next_pending_index(batch, "2026-10-27"), 0)

    def test_second_confirmed_failure_skips_and_removes_batch_from_active_queue(self):
        path = self.save_batch()
        queue_utils.mark_clip_failed(
            self.state_dir, "q1", 0, "private details", failure_type="ValueError",
            failed_on="2026-10-26",
        )
        status = queue_utils.mark_clip_failed(
            self.state_dir, "q1", 0, "private details", failure_type="ValueError",
            failed_on="2026-10-27",
        )
        batch = self.load(path)

        self.assertEqual(status, "skipped")
        self.assertEqual(batch["clips"][0]["confirmed_failures"], 2)
        self.assertEqual(batch["clips"][0]["skip_reason"], "confirmed_failure_after_retry")
        self.assertNotIn("error", batch["clips"][0])
        self.assertEqual(queue_utils.list_active_batches(self.state_dir), [])
        self.assertEqual(queue_utils.batch_progress(batch), (1, 1))

    def test_first_unresolved_clip_blocks_later_clips_until_retry_date(self):
        path = self.save_batch(clips=[
            {"start": 0, "end": 10, "type": "shorts", "title": "first", "description": "d"},
            {"start": 10, "end": 20, "type": "shorts", "title": "second", "description": "d"},
        ])
        queue_utils.mark_clip_failed(
            self.state_dir, "q1", 0, failure_type="TimeoutError", failed_on="2026-10-26"
        )
        batch = self.load(path)

        self.assertIsNone(queue_utils.get_next_pending_index(batch, "2026-10-26"))
        self.assertEqual(queue_utils.get_next_pending_index(batch, "2026-10-27"), 0)

        queue_utils.mark_clip_failed(
            self.state_dir, "q1", 0, failure_type="TimeoutError", failed_on="2026-10-27"
        )
        batch = self.load(path)
        self.assertEqual(batch["clips"][0]["status"], "skipped")
        self.assertEqual(queue_utils.get_next_pending_index(batch, "2026-10-27"), 1)

    def test_ambiguous_upload_remains_active_and_blocks_following_clip(self):
        path = self.save_batch(clips=[
            {"start": 0, "end": 10, "type": "shorts", "title": "first", "description": "d"},
            {"start": 10, "end": 20, "type": "shorts", "title": "second", "description": "d"},
        ])
        queue_utils.mark_clip_uploading(self.state_dir, "q1", 0)
        queue_utils.mark_clip_upload_unknown(self.state_dir, "q1", 0, "secret URL and Telegram id")
        batch = self.load(path)

        self.assertEqual(batch["clips"][0]["status"], "upload_unknown")
        self.assertEqual(batch["clips"][0]["unknown_reason"], "publish_result_unconfirmed")
        self.assertNotIn("error", batch["clips"][0])
        self.assertEqual(queue_utils.get_next_pending_index(batch), 0)
        self.assertEqual(len(queue_utils.list_active_batches(self.state_dir)), 1)

    def test_only_confirmed_youtube_success_removes_item_from_active_queue(self):
        path = self.save_batch()
        self.assertEqual(len(queue_utils.list_active_batches(self.state_dir)), 1)
        queue_utils.mark_clip_published(self.state_dir, "q1", 0, "https://youtu.be/confirmed")
        batch = self.load(path)

        self.assertEqual(batch["clips"][0]["status"], "published")
        self.assertEqual(queue_utils.list_active_batches(self.state_dir), [])

    def test_fifo_uses_message_update_order_when_timestamps_match(self):
        created_at = "2026-10-26T12:00:00+00:00"
        self.save_batch("later", created_at=created_at, fifo_order=502)
        self.save_batch("earlier", created_at=created_at, fifo_order=501)
        self.save_batch("oldest", created_at="2026-10-26T11:59:59+00:00", fifo_order=900)

        ids = [batch["queue_id"] for batch in queue_utils.list_active_batches(self.state_dir)]
        self.assertEqual(ids, ["oldest", "earlier", "later"])

    def test_merge_keeps_ambiguous_and_published_state_monotonic(self):
        local = {"status": "skipped", "confirmed_failures": 2}
        remote = {"status": "upload_unknown", "unknown_reason": "publish_result_unconfirmed"}
        self.assertEqual(queue_utils._merge_clip_state(local, remote)["status"], "upload_unknown")

        merged = queue_utils._merge_clip_state(
            {"status": "upload_unknown"},
            {"status": "published", "youtube_url": "https://youtu.be/id"},
        )
        self.assertEqual(merged["status"], "published")
        self.assertEqual(merged["youtube_url"], "https://youtu.be/id")

    def test_legacy_failure_is_migrated_without_persisting_raw_error(self):
        path = self.save_batch()
        batch = self.load(path)
        batch["clips"][0] = {
            "status": "failed_retryable",
            "error": "private source URL and Telegram details",
            "start": 0,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(batch, handle)

        active = queue_utils.list_active_batches(self.state_dir)
        migrated = self.load(path)["clips"][0]
        self.assertEqual(len(active), 1)
        self.assertEqual(migrated["confirmed_failures"], 1)
        self.assertNotIn("error", migrated)
        self.assertIsNotNone(migrated.get("retry_after_date"))
        self.assertEqual(queue_utils.get_next_pending_index(migrated | {"clips": [migrated]}, "1900-01-01"), None)


if __name__ == "__main__":
    unittest.main()
