import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetcher.telegram_client import TelegramMessage  # noqa: E402
from normalizer.ai_chat_handler import AIReviewResult  # noqa: E402
from normalizer.instagram.downloader import is_valid_instagram_url  # noqa: E402
from normalizer.pipeline import normalize_batch, normalize_message  # noqa: E402
from normalizer.router import classify  # noqa: E402


class PipelineStatsTests(unittest.TestCase):
    def test_kkinstagram_url_only_is_canonicalized_and_not_body_text(self):
        url = "https://kkinstagram.com/reel/DT_BzWeD9SF/?igsh=abc123"
        msg = TelegramMessage(
            channel_name="Ультра левые и ультра правые",
            channel_id=1,
            channel_username="channel",
            message_id=16,
            date=datetime(2026, 6, 22),
            text=url,
            urls=[url],
        )
        classified = classify(msg)
        saved = {}

        def fake_save(message, text, metadata=None):
            saved["text"] = text
            saved["metadata"] = metadata
            return Path("D:/fake/16.txt")

        with patch("normalizer.pipeline.normalize_text", side_effect=lambda text: text):
            with patch("normalizer.pipeline.extract_instagram_text", return_value="[reel extracted]") as extract:
                with patch("normalizer.pipeline._save_normalized", side_effect=fake_save):
                    with patch(
                        "normalizer.pipeline.translate_to_russian_if_needed",
                        side_effect=lambda text: text,
                    ):
                        result = normalize_message(msg, classified)

        self.assertEqual(
            classified.instagram_urls,
            ["https://www.instagram.com/reel/DT_BzWeD9SF/?igsh=abc123"],
        )
        self.assertEqual(result.link_review_created, 0)
        self.assertEqual(extract.call_count, 1)
        self.assertFalse(saved["metadata"]["has_body_text"])
        self.assertIn("[reel extracted]", saved["text"])

    def test_instagram_allowlist_rejects_hostile_urls(self):
        valid = (
            "https://instagram.com/reel/reel123",
            "https://www.instagram.com/p/post123/?igsh=abc",
            "kkinstagram.com/reel/archive_id",
        )
        for url in valid:
            with self.subTest(url=url):
                self.assertTrue(is_valid_instagram_url(url))

        hostile = (
            "https://evil.example/instagram.com/reel/abc",
            "https://notinstagram.com/reel/abc",
            "https://instagram.com.evil/reel/abc",
            "https://instagram.com@evil.example/reel/abc",
            "https://instagram.com/reel/abc/extra",
            "javascript://instagram.com/reel/abc",
        )
        for url in hostile:
            with self.subTest(url=url):
                self.assertFalse(is_valid_instagram_url(url))
                msg = TelegramMessage(
                    channel_name="Channel",
                    channel_id=1,
                    channel_username="channel",
                    message_id=999,
                    date=datetime(2026, 6, 22),
                    text=url,
                    urls=[url],
                )
                classified = classify(msg)
                self.assertEqual(classified.instagram_urls, [])
                self.assertEqual(classified.web_urls, [url])
    def test_instagram_reel_with_caption_is_extracted_not_queued(self):
        url = "https://www.instagram.com/reel/DN3mCxKWnaL/?igsh=abc123"
        msg = TelegramMessage(
            channel_name="Ультра левые и ультра правые",
            channel_id=1,
            channel_username="channel",
            message_id=18,
            date=datetime(2026, 6, 22),
            text=f"Подпись к ролику\n{url}",
            urls=[url],
        )
        saved = {}

        def fake_save(message, text, metadata=None):
            saved["text"] = text
            saved["metadata"] = metadata
            return Path("D:/fake/18.txt")

        with patch("normalizer.pipeline.normalize_text", side_effect=lambda text: text):
            with patch("normalizer.pipeline.extract_instagram_text", return_value="[reel extracted]") as extract:
                with patch("normalizer.pipeline._save_normalized", side_effect=fake_save):
                    with patch(
                        "normalizer.pipeline.translate_to_russian_if_needed",
                        side_effect=lambda text: text,
                    ):
                        result = normalize_message(msg)

        extract.assert_called_once()
        self.assertEqual(result.link_review_created, 0)
        self.assertTrue(saved["metadata"]["has_body_text"])
        self.assertIn("Подпись к ролику", saved["text"])
        self.assertIn("[reel extracted]", saved["text"])

    def test_normalize_batch_collects_content_type_and_review_stats(self):
        msg1 = TelegramMessage(
            channel_name="Channel",
            channel_id=1,
            channel_username="channel",
            message_id=101,
            date=datetime(2026, 4, 29),
            text="post 1",
            image_paths=["img1.jpg", "img2.jpg"],
            has_video=True,
            urls=[
                "https://youtube.com/watch?v=abc123",
                "https://instagram.com/reel/reel123",
                "https://instagram.com/p/post123",
                "https://chatgpt.com/share/aaa111",
                "https://claude.ai/share/bbb222",
                "https://example.com/story",
            ],
        )
        msg2 = TelegramMessage(
            channel_name="Channel",
            channel_id=1,
            channel_username="channel",
            message_id=102,
            date=datetime(2026, 4, 29),
            text="   ",
            urls=[],
        )

        youtube_artifact = SimpleNamespace(
            video_id="abc123",
            normalized_text="[YouTube]\nАвтор: Test\nНазвание: Test\nURL: https://youtube.com/watch?v=abc123\n\nTranscript",
            metadata=lambda: {"video_id": "abc123", "url": "https://youtube.com/watch?v=abc123"},
        )

        with patch("normalizer.pipeline.normalize_text", side_effect=lambda text: text):
            with patch("normalizer.pipeline.extract_youtube_artifact", return_value=youtube_artifact):
                with patch(
                    "normalizer.pipeline.save_youtube_artifact",
                    return_value={"text_path": "output/normalized_youtube/Channel/101/abc123.youtube.txt", "metadata_path": "meta", "cues_path": "cues"},
                ):
                    with patch("normalizer.pipeline.extract_instagram_text", return_value="[ig]"):
                        with patch("normalizer.pipeline.describe_image", return_value="[img]"):
                            with patch(
                                "normalizer.pipeline.queue_review_item",
                                return_value=SimpleNamespace(
                                    placeholder_text="[review]",
                                    action="queued",
                                    filepath="review.json",
                                ),
                            ):
                                with patch(
                                    "normalizer.pipeline.queue_for_review",
                                    side_effect=[
                                        AIReviewResult("[ai1]", "queued", "a.json"),
                                        AIReviewResult("[ai2]", "already_reviewed", "b.json"),
                                    ],
                                ):
                                    with patch(
                                        "normalizer.pipeline._save_normalized",
                                        return_value=Path("D:/fake/101.txt"),
                                    ):
                                        with patch(
                                            "normalizer.pipeline.translate_to_russian_if_needed",
                                            side_effect=lambda text: text,
                                        ):
                                            result = normalize_batch([msg1, msg2])

        self.assertEqual(result.messages_total, 2)
        self.assertEqual(result.messages_with_text, 1)
        self.assertEqual(result.messages_with_images, 1)
        self.assertEqual(result.images_total, 2)
        self.assertEqual(result.messages_with_native_video, 1)
        self.assertEqual(result.messages_with_youtube, 1)
        self.assertEqual(result.youtube_links_total, 1)
        self.assertEqual(result.messages_with_instagram_reels, 1)
        self.assertEqual(result.instagram_reel_links_total, 1)
        self.assertEqual(result.messages_with_instagram_posts, 1)
        self.assertEqual(result.instagram_post_links_total, 1)
        self.assertEqual(result.messages_with_ai_chat, 1)
        self.assertEqual(result.ai_chat_links_total, 2)
        self.assertEqual(result.messages_with_web, 1)
        self.assertEqual(result.web_links_total, 1)
        self.assertEqual(result.normalized_messages, 1)
        self.assertEqual(result.skipped_messages, 1)
        self.assertEqual(result.failed_messages, 0)
        self.assertEqual(result.ai_review_created, 1)
        self.assertEqual(result.ai_review_already_reviewed, 1)
        self.assertEqual(result.link_review_created, 1)
        self.assertEqual(len(result.texts_with_paths), 1)
        self.assertEqual(Path(result.texts_with_paths[0][0]), Path("D:/fake/101.txt"))
        self.assertEqual(ANY, result.texts_with_paths[0][1])
