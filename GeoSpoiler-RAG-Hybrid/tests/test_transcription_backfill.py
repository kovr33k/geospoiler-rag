import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalizer.transcription_backfill import backfill_transcripts  # noqa: E402
from normalizer.transcription_handler import TranscriptionResult  # noqa: E402


class TranscriptionBackfillTests(unittest.TestCase):
    def test_backfill_appends_transcript_and_updates_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_dir = Path(tmpdir) / "normalized"
            channel_dir = normalized_dir / "Channel"
            channel_dir.mkdir(parents=True)
            txt_path = channel_dir / "10.txt"
            txt_path.write_text("Header\n\n[Audio placeholder]\n", encoding="utf-8")
            meta_path = channel_dir / "10.meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "channel_name": "Channel",
                        "message_id": 10,
                        "media": [
                            {
                                "media_type": "voice",
                                "mime_type": "audio/ogg",
                                "message_id": 10,
                                "file_path": "media_cache/Channel/voice/msg_10.ogg",
                                "download_status": "downloaded",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch(
                "normalizer.transcription_backfill.transcribe_media",
                return_value=TranscriptionResult(
                    status="transcribed",
                    text="backfilled speech",
                    artifact_path="output/transcripts/Channel/10_10_voice.json",
                ),
            ):
                stats = backfill_transcripts(normalized_dir=normalized_dir, limit=1)

            text = txt_path.read_text(encoding="utf-8")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        self.assertEqual(stats.attempted, 1)
        self.assertEqual(stats.transcribed, 1)
        self.assertEqual(stats.normalized_updated, 1)
        self.assertIn("[Voice transcript", text)
        self.assertIn("backfilled speech", text)
        self.assertEqual(meta["transcriptions"][0]["status"], "transcribed")
        self.assertEqual(meta["media"][0]["transcription_status"], "transcribed")

    def test_backfill_dry_run_does_not_modify_normalized_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_dir = Path(tmpdir) / "normalized"
            channel_dir = normalized_dir / "Channel"
            channel_dir.mkdir(parents=True)
            txt_path = channel_dir / "11.txt"
            original_text = "Header\n\n[Video placeholder]\n"
            txt_path.write_text(original_text, encoding="utf-8")
            (channel_dir / "11.meta.json").write_text(
                json.dumps(
                    {
                        "channel_name": "Channel",
                        "message_id": 11,
                        "media": [
                            {
                                "media_type": "video",
                                "mime_type": "video/mp4",
                                "message_id": 11,
                                "file_path": "media_cache/Channel/video/msg_11.mp4",
                                "download_status": "downloaded",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("normalizer.transcription_backfill.transcribe_media") as transcribe:
                stats = backfill_transcripts(
                    normalized_dir=normalized_dir,
                    limit=1,
                    dry_run=True,
                )

            self.assertFalse(transcribe.called)
            self.assertEqual(txt_path.read_text(encoding="utf-8"), original_text)

        self.assertTrue(stats.dry_run)
        self.assertEqual(stats.attempted, 1)
        self.assertEqual(stats.skipped, 1)


if __name__ == "__main__":
    unittest.main()
