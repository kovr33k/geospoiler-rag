import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import llm_backend
from normalizer.media_vision import VisualExtractionResult
from normalizer.visual_backfill import (
    _atomic_write_pair,
    _has_current_visual_identity,
    _iter_candidates,
    _update_candidate,
)


class VisualBackfillTests(unittest.TestCase):
    def test_update_candidate_persists_status_and_does_not_duplicate_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "channel" / "1.txt"
            meta_path = normalized.with_suffix(".meta.json")
            normalized.parent.mkdir(parents=True)
            normalized.write_text("[Channel: channel]\n\nCaption\n", encoding="utf-8")
            meta_path.write_text(
                json.dumps(
                    {
                        "channel_name": "channel",
                        "message_id": 1,
                        "media": [
                            {
                                "media_type": "image",
                                "message_id": 1,
                                "file_path": "media_cache/channel/msg_1.jpg",
                                "download_status": "downloaded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            candidate = {
                "meta_path": meta_path,
                "normalized_path": normalized,
                "channel_name": "channel",
                "message_id": 1,
                "media": json.loads(meta_path.read_text(encoding="utf-8"))["media"][0],
                "media_type": "image",
            }
            result = VisualExtractionResult(
                status="success",
                text="Видимый текст на изображении.",
                model="codex-cli:gpt-5.6-luna@xhigh",
                artifact_path="output/visuals/msg_1.json",
                frame_count=1,
            )

            self.assertTrue(_update_candidate(candidate, result))
            self.assertFalse(_update_candidate(candidate, result))

            saved_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_meta["media"][0]["visual_status"], "success")
            self.assertEqual(len(saved_meta["visual_extractions"]), 1)
            self.assertEqual(normalized.read_text(encoding="utf-8").count("Видимый текст"), 1)


    def test_atomic_pair_restores_both_files_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "1.txt"
            meta = root / "1.meta.json"
            normalized.write_text("old text", encoding="utf-8")
            meta.write_text("old meta", encoding="utf-8")
            original_text = normalized.read_bytes()
            original_meta = meta.read_bytes()
            import os

            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(source, target):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated sidecar replace failure")
                return real_replace(source, target)

            with patch("normalizer.visual_backfill.os.replace", side_effect=flaky_replace):
                with self.assertRaises(OSError):
                    _atomic_write_pair(
                        normalized,
                        b"new text",
                        meta,
                        b"new meta",
                        original_text=original_text,
                        original_meta=original_meta,
                    )

            self.assertEqual(normalized.read_bytes(), original_text)
            self.assertEqual(meta.read_bytes(), original_meta)

    def test_current_visual_identity_skips_paid_reanalysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "image.jpg"
            media.write_bytes(b"image")
            artifact = root / "visual.json"
            digest = hashlib.sha256(media.read_bytes()).hexdigest()
            with patch.object(config, "PROJECT_ROOT", root), patch.object(
                config, "MEDIA_VISION_PROMPT_VERSION", "test-prompt"
            ), patch.object(config, "MEDIA_VISION_FRAME_INTERVAL_SEC", 2.0), patch.object(
                config, "MEDIA_VISION_MAX_FRAMES", 12
            ), patch.object(config, "MEDIA_VISION_FRAME_BATCH_SIZE", 6), patch.object(
                llm_backend, "active_model_for", return_value="codex-test"
            ):
                artifact.write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "text": "Visible text",
                            "input_sha256": digest,
                            "model": "codex-test",
                            "frame_count": 1,
                            "prompt_version": "test-prompt",
                            "frame_interval_seconds": 2.0,
                            "max_frames": 12,
                            "batch_size": 6,
                        }
                    ),
                    encoding="utf-8",
                )
                candidate = {
                    "media": {
                        "visual_status": "success",
                        "visual_artifact_path": str(artifact),
                        "file_path": str(media),
                    }
                }
                self.assertTrue(_has_current_visual_identity(candidate))

    def test_current_visual_identity_rejects_invalid_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "image.jpg"
            media.write_bytes(b"image")
            artifact = root / "visual.json"
            digest = hashlib.sha256(media.read_bytes()).hexdigest()
            artifact.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "text": "Visible text",
                        "input_sha256": digest,
                        "model": "codex-test",
                        "frame_count": 0,
                        "prompt_version": "test-prompt",
                        "frame_interval_seconds": 2.0,
                        "max_frames": 12,
                        "batch_size": 6,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "PROJECT_ROOT", root), patch.object(
                config, "MEDIA_VISION_PROMPT_VERSION", "test-prompt"
            ), patch.object(config, "MEDIA_VISION_FRAME_INTERVAL_SEC", 2.0), patch.object(
                config, "MEDIA_VISION_MAX_FRAMES", 12
            ), patch.object(config, "MEDIA_VISION_FRAME_BATCH_SIZE", 6), patch.object(
                llm_backend, "active_model_for", return_value="codex-test"
            ):
                self.assertFalse(
                    _has_current_visual_identity(
                        {
                            "media": {
                                "visual_status": "success",
                                "visual_artifact_path": str(artifact),
                                "file_path": str(media),
                            }
                        }
                    )
                )

    def test_current_visual_identity_rejects_empty_or_failed_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "image.jpg"
            media.write_bytes(b"image")
            artifact = root / "visual.json"
            digest = hashlib.sha256(media.read_bytes()).hexdigest()
            artifact.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "text": "",
                        "input_sha256": digest,
                        "model": "codex-test",
                        "prompt_version": "test-prompt",
                        "frame_interval_seconds": 2.0,
                        "max_frames": 12,
                        "batch_size": 6,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "PROJECT_ROOT", root), patch.object(
                config, "MEDIA_VISION_PROMPT_VERSION", "test-prompt"
            ), patch.object(config, "MEDIA_VISION_FRAME_INTERVAL_SEC", 2.0), patch.object(
                config, "MEDIA_VISION_MAX_FRAMES", 12
            ), patch.object(config, "MEDIA_VISION_FRAME_BATCH_SIZE", 6), patch.object(
                llm_backend, "active_model_for", return_value="codex-test"
            ):
                self.assertFalse(
                    _has_current_visual_identity(
                        {
                            "media": {
                                "visual_status": "success",
                                "visual_artifact_path": str(artifact),
                                "file_path": str(media),
                            }
                        }
                    )
                )

    def test_iter_candidates_skips_malformed_sidecar_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "channel" / "1.txt"
            normalized.parent.mkdir(parents=True)
            normalized.write_text("body", encoding="utf-8")
            normalized.with_suffix(".meta.json").write_text(
                json.dumps(
                    {
                        "channel_name": "channel",
                        "message_id": "not-an-integer",
                        "media": [
                            {
                                "media_type": "image",
                                "message_id": 1,
                                "file_path": str(root / "image.jpg"),
                                "download_status": "downloaded",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_iter_candidates(root), [])
if __name__ == "__main__":
    unittest.main()
