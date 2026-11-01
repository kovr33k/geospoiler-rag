import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalizer.review_queue import (  # noqa: E402
    apply_reviewed_texts,
    mark_reviewed,
    processed_reviews_for_post,
    queue_item,
    reopen_review,
)


class ReviewQueueLoadTests(unittest.TestCase):
    def test_automated_failure_can_reopen_processed_item_with_audit_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import config

            queue_dir = Path(tmpdir)
            url = "https://www.instagram.com/reel/ABC123/"
            suffix = __import__("hashlib").sha1(url.encode()).hexdigest()[:8]
            filepath = queue_dir / f"Channel_7_{suffix}.json"
            filepath.write_text(
                json.dumps(
                    {
                        "review_type": "external_link",
                        "url": url,
                        "channel": "Channel",
                        "message_id": 7,
                        "status": "processed",
                        "extracted_text": "old manual text",
                        "reviewed_at": "2026-08-10T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(config, "REVIEW_QUEUE_DIR", queue_dir):
                result = queue_item(
                    review_type="external_link",
                    channel_name="Channel",
                    message_id=7,
                    url="https://www.instagram.com/reel/ABC123/",
                    reason="Instagram Reel deep extraction failed",
                    reopen_processed=True,
                )

            payload = json.loads(filepath.read_text(encoding="utf-8"))

        self.assertEqual(result.action, "queued")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["reopened_from_status"], "processed")
        self.assertEqual(payload["previous_extracted_text"], "old manual text")

    def test_mark_reviewed_stores_prompt_extraction_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_file = Path(tmpdir) / "item.json"
            review_file.write_text(
                json.dumps(
                    {
                        "review_type": "external_link",
                        "status": "pending",
                        "extracted_text": None,
                    }
                ),
                encoding="utf-8",
            )

            mark_reviewed(
                str(review_file),
                extracted_text="Filtered extraction",
                extraction_prompt="Extract only the port blockade details",
                extraction_source="youtube",
            )

            payload = json.loads(review_file.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "processed")
        self.assertEqual(payload["extracted_text"], "Filtered extraction")
        self.assertEqual(
            payload["extraction_prompt"],
            "Extract only the port blockade details",
        )
        self.assertEqual(payload["extraction_source"], "youtube")

    def test_processed_reviews_match_channel_and_message_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import config

            queue_dir = Path(tmpdir)

            def write(name, **overrides):
                payload = {
                    "review_type": "external_link",
                    "channel": "росия",
                    "message_id": 42,
                    "status": "processed",
                    "extracted_text": "Текст ревью для поста 42.",
                }
                payload.update(overrides)
                (queue_dir / name).write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )

            write("росия_42_aaaa.json")
            write(
                "росия_423_bbbb.json",
                message_id=423,
                extracted_text="Текст другого поста 423.",
            )
            write("росия_42_other.json", channel="другой", extracted_text="Чужой канал.")
            write("росия_42_pending.json", status="pending", extracted_text="Ещё не готово.")
            write("росия_42_skipped.json", status="skipped", extracted_text="Пропуск.")
            write(
                "росия_42_failure.json",
                review_type="enrichment_failure",
                extracted_text="Это не текст источника.",
            )
            write("росия_42_blank.json", extracted_text="  ")

            with patch.object(config, "REVIEW_QUEUE_DIR", queue_dir):
                items = processed_reviews_for_post("росия", 42)

        self.assertEqual([item["_filename"] for item in items], ["росия_42_aaaa.json"])
        self.assertEqual(items[0]["extracted_text"], "Текст ревью для поста 42.")

    def test_apply_reviewed_texts_replaces_placeholders_or_appends(self):
        filename = "Channel_7_ab.json"
        extracted = "Извлечённый текст источника."
        item = {"_filename": filename, "extracted_text": extracted}
        block = f"[Текст из ревью: {filename}]\n{extracted}"

        queued = (
            "[Внешняя ссылка: https://example.com/story]\n"
            f"[Отправлено в очередь на ручной просмотр: {filename}]"
        )
        self.assertEqual(
            apply_reviewed_texts(queued, [item]),
            "[Внешняя ссылка: https://example.com/story]\n" + block,
        )

        already = f"[Instagram Reel: https://www.instagram.com/reel/ABC/]\n[Уже обработано: {filename}]"
        self.assertEqual(
            apply_reviewed_texts(already, [item]),
            "[Instagram Reel: https://www.instagram.com/reel/ABC/]\n" + block,
        )

        untouched = "Короткий пост без строки очереди"
        self.assertEqual(
            apply_reviewed_texts(untouched, [item]),
            f"{untouched}\n\n{block}",
        )
        self.assertIs(apply_reviewed_texts(untouched, []), untouched)

    def test_reopen_review_returns_processed_item_to_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_file = Path(tmpdir) / "Channel_7_ab.json"
            review_file.write_text(
                json.dumps(
                    {
                        "review_type": "external_link",
                        "url": "https://example.com/story",
                        "channel": "Channel",
                        "message_id": 7,
                        "status": "processed",
                        "extracted_text": "Сохранённый текст",
                        "reviewed_at": "2026-08-10T00:00:00+00:00",
                        "extraction_prompt": "только про порт",
                        "extraction_source": "web",
                        "reason": "External web link",
                    }
                ),
                encoding="utf-8",
            )

            reopen_review(str(review_file))
            payload = json.loads(review_file.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "pending")
        self.assertIsNone(payload["extracted_text"])
        self.assertIsNone(payload["reviewed_at"])
        self.assertEqual(payload["previous_extracted_text"], "Сохранённый текст")
        self.assertEqual(payload["previous_reviewed_at"], "2026-08-10T00:00:00+00:00")
        self.assertEqual(payload["reopened_from_status"], "processed")
        self.assertEqual(payload["extraction_prompt"], "только про порт")
        self.assertEqual(payload["extraction_source"], "web")
        self.assertEqual(payload["url"], "https://example.com/story")
        self.assertEqual(payload["reason"], "External web link")

if __name__ == "__main__":
    unittest.main()
