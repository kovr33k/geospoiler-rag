import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalizer.review_queue import mark_reviewed, queue_item  # noqa: E402


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

if __name__ == "__main__":
    unittest.main()
