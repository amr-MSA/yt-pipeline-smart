import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import telegram_utils
import queue_utils
import long_pipeline
import youtube_dedupe
import fixed_meta
from transcript_utils import split_segments
from cookie_utils import temporary_cookie_file


def test_extract_urls_accepts_multiple_platforms_and_cleans_punctuation():
    text = (
        "TikTok https://www.tiktok.com/@demo/video/123, "
        "Instagram https://www.instagram.com/reel/abc/?x=1. "
        "Vimeo https://vimeo.com/123456"
    )
    assert telegram_utils.extract_urls(text) == [
        "https://www.tiktok.com/@demo/video/123",
        "https://www.instagram.com/reel/abc/?x=1",
        "https://vimeo.com/123456",
    ]


def test_fetch_all_new_messages_accepts_non_youtube_url():
    update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": 7},
            "text": "https://www.tiktok.com/@demo/video/123",
        },
    }
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=[update]):
        links, long_commands = telegram_utils.fetch_all_new_messages("token", tmp, "shorts")
    assert links[0]["url"] == "https://www.tiktok.com/@demo/video/123"
    assert long_commands == []


def test_inline_quoted_title_before_url_is_used_as_custom_title():
    assert telegram_utils.extract_inline_title(
        '"ووو"https://www.instagram.com/reel/DedwjxON0HB/?x=1'
    ) == "ووو"
    assert telegram_utils.extract_inline_title(
        '“عنوان تجريبي” https://youtu.be/demo'
    ) == "عنوان تجريبي"

    update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": 7},
            "text": '"ووو"https://www.instagram.com/reel/DedwjxON0HB/?x=1',
        },
    }
    with patch.object(telegram_utils, "_fetch_updates", return_value=[update]):
        links, _ = telegram_utils.fetch_all_new_messages("token", "/tmp", "shorts")
    assert links[0]["title_mode"] == "custom"
    assert links[0]["title_override"] == "ووو"


def test_inline_title_is_applied_to_long_bot_links():
    updates = [
        {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "chat": {"id": 7},
                "text": '"عنوان اللونق" https://example.com/video',
            },
        }
    ]
    with patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        links = telegram_utils.fetch_new_links("token", "/tmp", "long")
    assert links[0]["title_mode"] == "custom"
    assert links[0]["title_override"] == "عنوان اللونق"


def test_fetch_all_new_messages_accepts_non_youtube_long_command():
    update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": 7},
            "text": "/long https://vimeo.com/123456",
        },
    }
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=[update]):
        links, long_commands = telegram_utils.fetch_all_new_messages("token", tmp, "shorts")
    assert links == []
    assert long_commands[0]["url"] == "https://vimeo.com/123456"


def test_smart_long_command_is_replayed_from_inbox_without_new_updates():
    update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": 7},
            "text": "/long https://vimeo.com/123456",
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(telegram_utils, "_fetch_updates", return_value=[update]):
            telegram_utils.fetch_all_new_messages("token", tmp, "shorts")
        with patch.object(telegram_utils, "_fetch_updates", return_value=[]):
            _, long_commands = telegram_utils.fetch_all_new_messages("token", tmp, "shorts")
        assert [item["url"] for item in long_commands] == ["https://vimeo.com/123456"]


def test_urls_with_embedded_credentials_are_rejected():
    assert telegram_utils.extract_urls("https://user:password@example.com/video/1") == []


def test_cookie_file_is_private_and_deleted_after_context():
    with temporary_cookie_file("# Netscape\nsecret-cookie") as path:
        assert path is not None
        assert Path(path).exists()
        assert (Path(path).stat().st_mode & 0o777) == 0o600
    assert not Path(path).exists()


def test_smart_long_accepts_long_with_any_case_and_optional_space_or_slash():
    url = "https://www.youtube.com/watch?v=abc123"
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": f"long {url}"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": f"Long{url}"}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 7}, "text": f"/LONG {url}"}},
    ]
    with patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        _, commands = telegram_utils.fetch_all_new_messages("token", "/tmp", "shorts")
    assert [item["url"] for item in commands] == [url, url, url]


def test_boundary_marker_discards_links_before_it():
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "https://example.com/old"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": "."}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 7}, "text": "https://example.com/new"}},
    ]
    with patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        links, _ = telegram_utils.fetch_all_new_messages("token", "/tmp", "shorts")
    assert [item["url"] for item in links] == ["https://example.com/new"]


def test_numeric_marker_is_boundary_and_marker_with_text_is_not():
    assert telegram_utils.is_boundary_marker("42")
    assert telegram_utils.is_boundary_marker("★")
    assert not telegram_utils.is_boundary_marker("batch 42")


def test_batch_summary_groups_items_by_chat():
    items = [
        {"chat_id": 7},
        {"chat_id": 7},
        {"chat_id": 8},
    ]
    with patch.object(telegram_utils, "send_message") as send:
        telegram_utils.send_batch_summary("token", items, "رابط")
    assert send.call_count == 2
    send.assert_any_call("token", 7, "📊 تم قبول 2 روابط للمعالجة في هذه الدفعة.")
    send.assert_any_call("token", 8, "📊 تم قبول 1 رابط للمعالجة في هذه الدفعة.")


def test_title_commands_update_the_previous_link():
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "https://example.com/a"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": "/take"}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 7}, "text": "https://example.com/b"}},
        {"update_id": 4, "message": {"message_id": 4, "chat": {"id": 7}, "text": "/t \"عنوان مخصص\""}},
        {"update_id": 5, "message": {"message_id": 5, "chat": {"id": 7}, "text": "/d"}},
    ]
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        links = telegram_utils.fetch_new_links("token", tmp, "long")
    assert links[0]["title_mode"] == "source"
    assert links[1]["title_mode"] == "default"
    assert links[1].get("title_override") is None


def test_pipeline_report_is_a_boundary_for_old_links():
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "https://example.com/old"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": "📊 تقرير الدفعة — تم قبول 1 رابط:\nhttps://youtu.be/old"}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 7}, "text": "https://example.com/new"}},
    ]
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        links = telegram_utils.fetch_new_links("token", tmp, "long")
    assert [item["url"] for item in links] == ["https://example.com/new"]


def test_legacy_success_message_is_a_boundary_too():
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7}, "text": "https://example.com/old"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 7}, "text": "✅ تم نشر الفيديو بنجاح:\nhttps://youtu.be/old"}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 7}, "text": "https://example.com/new"}},
    ]
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        links = telegram_utils.fetch_new_links("token", tmp, "long")
    assert [item["url"] for item in links] == ["https://example.com/new"]


def test_duplicate_links_have_independent_message_keys():
    first = {"chat_id": 7, "message_id": 101, "url": "https://youtu.be/same"}
    second = {"chat_id": 7, "message_id": 102, "url": "https://youtu.be/same"}
    assert telegram_utils.message_key(first) != telegram_utils.message_key(second)


def test_duplicate_urls_in_different_messages_are_independent_requests():
    first = {"chat_id": 7, "message_id": 100, "url": "https://youtu.be/same"}
    second = {"chat_id": 7, "message_id": 101, "url": "https://youtu.be/same"}
    assert telegram_utils.message_key(first) == "7:100"
    assert telegram_utils.message_key(second) == "7:101"
    assert queue_utils.queue_id_for_request(telegram_utils.message_key(first)) != queue_utils.queue_id_for_request(telegram_utils.message_key(second))


def test_same_message_has_one_request_key():
    first = {"chat_id": 7, "message_id": 100, "url": "https://youtu.be/a"}
    repeated = {"chat_id": 7, "message_id": 100, "url": "https://youtu.be/a"}
    assert telegram_utils.message_key(first) == telegram_utils.message_key(repeated)


def test_long_smart_creates_separate_queues_for_same_url_messages():
    with tempfile.TemporaryDirectory() as tmp:
        clips = [{"type": "long", "start": 0, "end": 60, "title": "x", "description": "y"}]
        first_key = "7:100"
        second_key = "7:101"
        queue_utils.save_new_batch(
            tmp, queue_utils.queue_id_for_request(first_key), first_key,
            "video-id", "https://youtu.be/same", "title", 7, clips,
        )
        queue_utils.save_new_batch(
            tmp, queue_utils.queue_id_for_request(second_key), second_key,
            "video-id", "https://youtu.be/same", "title", 7, clips,
        )
        assert len(queue_utils.list_active_batches(tmp)) == 2


def test_long_smart_splits_long_transcript_and_preserves_part_order():
    segments = [
        {"start": i * 10, "end": i * 10 + 10, "text": "x" * 30}
        for i in range(20)
    ]
    chunks = split_segments(segments, max_chars=220)
    assert len(chunks) > 1
    with tempfile.TemporaryDirectory() as tmp:
        for index, _chunk in enumerate(chunks):
            queue_utils.save_new_batch(
                tmp,
                queue_utils.queue_id_for_request(f"7:1:part:{index}"),
                "7:1",
                "video-id",
                "https://youtu.be/demo",
                "title",
                7,
                [{"type": "long", "start": 0, "end": 1, "title": "t", "description": "d"}],
                part_index=index,
                part_count=len(chunks),
            )
        assert [item["part_index"] for item in queue_utils.list_active_batches(tmp)] == list(
            range(len(chunks))
        )


def test_upload_success_state_survives_delete_failure():
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        state = {
            telegram_utils.message_key({"chat_id": 7, "message_id": 101}): {
                "chat_id": 7,
                "message_id": 101,
                "video_id": "abc123",
                "url": "https://youtu.be/abc123",
            }
        }
        telegram_utils.save_uploaded_messages(str(state_dir), "long", state)
        assert telegram_utils.load_uploaded_messages(str(state_dir), "long") == state

        with patch.object(telegram_utils.requests, "post", side_effect=telegram_utils.requests.RequestException):
            assert not telegram_utils.delete_message("token", 7, 101)
        assert telegram_utils.load_uploaded_messages(str(state_dir), "long") == state


def test_delete_requires_telegram_ok():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False}

    with patch.object(telegram_utils.requests, "post", return_value=Response()):
        assert not telegram_utils.delete_message("token", 7, 101)


def test_manual_run_selects_only_latest_and_keeps_older_links_pending():
    updates = [
        {"update_id": 1, "message": {"message_id": 10, "chat": {"id": 7}, "date": 100, "text": "https://example.com/old"}},
        {"update_id": 2, "message": {"message_id": 11, "chat": {"id": 7}, "date": 200, "text": "https://example.com/latest"}},
    ]
    with tempfile.TemporaryDirectory() as tmp, patch.object(telegram_utils, "_fetch_updates", return_value=updates):
        selected = telegram_utils.fetch_new_links("token", tmp, "long", run_mode="manual")
        assert [item["url"] for item in selected] == ["https://example.com/latest"]
        assert {item["url"] for item in telegram_utils.load_pending_links(tmp, "long")} == {
            "https://example.com/old", "https://example.com/latest"
        }


def test_acknowledging_manual_success_removes_only_that_link():
    with tempfile.TemporaryDirectory() as tmp:
        old = {"chat_id": 7, "message_id": 10, "url": "https://example.com/old"}
        latest = {"chat_id": 7, "message_id": 11, "url": "https://example.com/latest"}
        telegram_utils.save_pending_links(tmp, "long", [old, latest])
        telegram_utils.acknowledge_pending_link(tmp, "long", latest)
        remaining = telegram_utils.load_pending_links(tmp, "long")
        assert len(remaining) == 1
        assert remaining[0]["url"] == old["url"]


def test_canonical_url_collapses_youtube_variants_and_tracking_parameters():
    assert telegram_utils.canonicalize_url("https://youtu.be/abc123?si=tracking") == (
        "https://youtube.com/watch?v=abc123"
    )
    assert telegram_utils.canonicalize_url(
        "https://www.youtube.com/watch?v=abc123&utm_source=test&feature=share"
    ) == "https://youtube.com/watch?v=abc123"


def test_daily_fetch_returns_old_pending_inbox_items_for_retry():
    with tempfile.TemporaryDirectory() as tmp:
        old = {
            "url": "https://example.com/old",
            "canonical_url": "https://example.com/old",
            "chat_id": 7,
            "message_id": 10,
            "date": 1,
            "title_mode": "default",
        }
        telegram_utils.save_inbox(tmp, "long", {old["canonical_url"]: old})
        with patch.object(telegram_utils, "_fetch_updates", return_value=[]):
            links = telegram_utils.fetch_new_links("token", tmp, "long", window_seconds=1)
        assert [item["url"] for item in links] == [old["url"]]


def test_published_url_expiry_is_individual_and_not_file_wide():
    with tempfile.TemporaryDirectory() as tmp:
        telegram_utils.record_published_url(tmp, "long", "https://example.com/old", "v-old", now=100)
        telegram_utils.record_published_url(tmp, "long", "https://example.com/new", "v-new", now=200)
        active = telegram_utils.load_published_urls(tmp, "long", now=100 + 48 * 60 * 60 + 1)
        assert "https://example.com/old" not in active
        assert "https://example.com/new" in active


def test_published_url_uses_canonical_key_and_blocks_variants_within_ttl():
    with tempfile.TemporaryDirectory() as tmp:
        telegram_utils.record_published_url(
            tmp, "long", "https://youtu.be/abc123?si=tracking", "video-id", now=100
        )
        active = telegram_utils.load_published_urls(tmp, "long", now=101)
        assert "https://youtube.com/watch?v=abc123" in active


def test_long_pipeline_uses_project_root_for_persistent_state():
    project_root = Path(__file__).parents[1]
    assert Path(long_pipeline.STATE_DIR) == project_root / "state"


def test_long_title_prefers_original_and_falls_back_to_message_number():
    assert long_pipeline.resolve_title("ارح قلبك بهذه التلاوة العطرة", "source", message_id=202) == (
        "ارح قلبك بهذه التلاوة العطرة"
    )
    assert long_pipeline.resolve_title("Video by creator", "source", message_id=202) == "ميمز العرب #202"
    assert long_pipeline.resolve_title("أي عنوان", "default", message_id=203) == "ميمز العرب #203"


def test_description_attributes_source_handle_and_keeps_rights_notice():
    info = {"uploader_id": "@original_creator"}
    handle = fixed_meta.source_handle_from_info(info)
    description = fixed_meta.build_final_description("وصف المقطع", handle, "https://youtu.be/demo")
    assert "@original_creator" in description
    assert "الحقوق محفوظة" in description


def test_description_falls_back_to_source_url_without_handle():
    description = fixed_meta.build_final_description("", "", "https://example.com/source")
    assert "https://example.com/source" in description
    assert "الحقوق محفوظة" in description


def test_youtube_request_marker_is_stable_and_survives_long_descriptions():
    marker = youtube_dedupe.request_marker("long", "6828354904:202")
    assert marker == "#1-6828354904-202"
    assert youtube_dedupe.request_marker("shorts", "6828354904:202") == "#2-6828354904-202"
    assert youtube_dedupe.request_marker("long-smart", "6828354904:202") == "#2-6828354904-202"
    result = youtube_dedupe.append_request_marker("x" * 6000, marker)
    assert marker in result
    assert len(result) <= 4900


def test_recent_youtube_markers_scan_latest_channel_uploads():
    class Resource:
        def __init__(self, payload):
            self.payload = payload

        def list(self, **kwargs):
            return self

        def execute(self):
            return self.payload

    class Youtube:
        def channels(self):
            return Resource({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "PL1"}}}]})

        def playlistItems(self):
            return Resource({"items": [{"contentDetails": {"videoId": "vid123"}}]})

        def videos(self):
            return Resource({"items": [{"id": "vid123", "snippet": {"description": "x\n#1-7-202"}}]})

    assert youtube_dedupe.recent_request_markers(Youtube()) == {
        "#1-7-202": "vid123"
    }


def test_recent_youtube_markers_keep_legacy_format_readable():
    class Resource:
        def __init__(self, payload):
            self.payload = payload

        def list(self, **kwargs):
            return self

        def execute(self):
            return self.payload

    class Youtube:
        def channels(self):
            return Resource({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "PL1"}}}]})

        def playlistItems(self):
            return Resource({"items": [{"contentDetails": {"videoId": "vid123"}}]})

        def videos(self):
            return Resource({"items": [{"id": "vid123", "snippet": {"description": "yt-pipeline-request:long:7:202"}}]})

    assert youtube_dedupe.recent_request_markers(Youtube()) == {
        "yt-pipeline-request:long:7:202": "vid123"
    }
