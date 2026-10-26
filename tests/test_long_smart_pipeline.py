import datetime
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import long_smart_pipeline as pipeline
import queue_utils


class FakeHttpFailure(Exception):
    def __init__(self, status):
        super().__init__("simulated response with private data omitted")
        self.resp = SimpleNamespace(status=status)


class LongSmartPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.temp.name, "state")
        self.download_dir = os.path.join(self.temp.name, "downloads")
        os.makedirs(self.download_dir, exist_ok=True)
        self.queue_id = "pipeline-test"
        queue_utils.save_new_batch(
            state_dir=self.state_dir,
            queue_id=self.queue_id,
            request_key="123:456",
            video_id="fixture-video",
            source_url="https://example.invalid/fixture",
            source_title="fixture",
            chat_id=123,
            clips=[
                {"start": 0, "end": 10, "type": "long", "title": "first", "description": "d"},
                {"start": 10, "end": 20, "type": "long", "title": "second", "description": "d"},
            ],
            created_at="2026-10-25T10:00:00+00:00",
            fifo_order=10,
        )
        self.patches = [
            patch.object(pipeline, "STATE_DIR", self.state_dir),
            patch.object(pipeline, "DOWNLOAD_DIR", self.download_dir),
            patch.object(pipeline.queue_utils, "merge_latest_state"),
            patch.object(pipeline.publish_quota, "can_publish", return_value=True),
            patch.object(pipeline, "get_youtube_client", return_value=object()),
            patch.object(pipeline, "recent_request_markers", return_value={}),
            patch.object(pipeline, "send_message"),
            patch.object(pipeline.fixed_meta, "build_final_title", return_value="fixture title"),
            patch.object(pipeline.fixed_meta, "build_final_description", return_value="fixture description"),
        ]
        for item in self.patches:
            item.start()
        self.env = patch.dict(os.environ, {"YOUTUBE_TOKEN_JSON": "{}", "SHORTS_BOT_TOKEN": "test-token"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def read_batch(self):
        path = os.path.join(self.state_dir, "long_smart_queue", f"{self.queue_id}.json")
        return queue_utils.load_batch(path)

    def fake_download(self, url, start, end, out_path, cookies_env):
        with open(out_path, "wb") as handle:
            handle.write(b"fixture only; never uploaded")

    def test_confirmed_failure_gets_one_next_day_retry_then_skips_and_continues(self):
        outcomes = [FakeHttpFailure(403), FakeHttpFailure(403), "confirmed-fixture-id"]
        upload_calls = []

        def fake_upload(*args, **kwargs):
            upload_calls.append(args)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(pipeline, "_download_clip_range", side_effect=self.fake_download), \
                patch.object(pipeline, "_upload", side_effect=fake_upload):
            pipeline.run_daily_batch()
            first_run = self.read_batch()
            self.assertEqual(first_run["clips"][0]["status"], "failed_retryable")
            self.assertEqual(first_run["clips"][0]["confirmed_failures"], 1)
            self.assertEqual(len(upload_calls), 1)

            # A second invocation on the same UTC date must not retry the item.
            pipeline.run_daily_batch()
            self.assertEqual(len(upload_calls), 1)

            # Simulate the next daily execution by making the stored due date today.
            path = os.path.join(self.state_dir, "long_smart_queue", f"{self.queue_id}.json")
            due_batch = self.read_batch()
            due_batch["clips"][0]["retry_after_date"] = datetime.datetime.now(
                datetime.timezone.utc
            ).date().isoformat()
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(due_batch, handle)

            pipeline.run_daily_batch()

        completed = self.read_batch()
        self.assertEqual(completed["clips"][0]["status"], "skipped")
        self.assertEqual(completed["clips"][0]["confirmed_failures"], 2)
        self.assertEqual(completed["clips"][1]["status"], "published")
        self.assertEqual(len(upload_calls), 3)

    def test_ambiguous_http_5xx_stops_and_is_never_retried_automatically(self):
        upload_calls = []

        def fake_upload(*args, **kwargs):
            upload_calls.append(args)
            raise FakeHttpFailure(500)

        with patch.object(pipeline, "_download_clip_range", side_effect=self.fake_download), \
                patch.object(pipeline, "_upload", side_effect=fake_upload):
            pipeline.run_daily_batch()
            self.assertEqual(self.read_batch()["clips"][0]["status"], "upload_unknown")
            pipeline.run_daily_batch()

        batch = self.read_batch()
        self.assertEqual(batch["clips"][0]["status"], "upload_unknown")
        self.assertEqual(batch["clips"][1]["status"], "pending")
        self.assertEqual(len(upload_calls), 1)
        self.assertNotIn("error", batch["clips"][0])

    def test_completed_queue_does_not_start_the_next_queue_in_same_run(self):
        queue_utils.save_new_batch(
            state_dir=self.state_dir,
            queue_id="next-file",
            request_key="123:789",
            video_id="second-fixture",
            source_url="https://example.invalid/second",
            source_title="second fixture",
            chat_id=123,
            clips=[{"start": 0, "end": 10, "type": "long", "title": "next", "description": "d"}],
            created_at="2026-10-25T10:01:00+00:00",
            fifo_order=11,
        )
        upload_calls = []

        def fake_upload(*args, **kwargs):
            upload_calls.append(args)
            return f"fixture-id-{len(upload_calls)}"

        with patch.object(pipeline, "_download_clip_range", side_effect=self.fake_download), \
                patch.object(pipeline, "_upload", side_effect=fake_upload):
            pipeline.run_daily_batch()

        completed_first = self.read_batch()
        next_path = os.path.join(self.state_dir, "long_smart_queue", "next-file.json")
        next_batch = queue_utils.load_batch(next_path)
        self.assertTrue(all(clip["status"] == "published" for clip in completed_first["clips"]))
        self.assertEqual(next_batch["clips"][0]["status"], "pending")
        self.assertEqual(len(upload_calls), 2)

    def test_http_4xx_is_confirmed_but_5xx_is_ambiguous(self):
        self.assertTrue(pipeline._is_confirmed_upload_failure(FakeHttpFailure(403)))
        self.assertTrue(pipeline._is_confirmed_upload_failure(FakeHttpFailure(429)))
        self.assertFalse(pipeline._is_confirmed_upload_failure(FakeHttpFailure(500)))
        self.assertFalse(pipeline._is_confirmed_upload_failure(TimeoutError("private data")))
        self.assertEqual(
            pipeline._safe_failure_label(TimeoutError("private URL and Telegram details")),
            "TimeoutError",
        )


if __name__ == "__main__":
    unittest.main()
