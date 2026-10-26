import base64
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import requests

import cli_app
import scripts.luna_reenrich_control as control
from enricher.pipeline import _migrate_non_media_contract
from normalizer.media_paths import MediaPathError, resolve_media_path
from normalizer.media_vision import VisualExtractionResult
from normalizer.transcription_backfill import backfill_transcripts
from normalizer.transcription_handler import (
    TranscriptionRequestError,
    TranscriptionResult,
    _call_transcription_api,
    _is_degenerate_transcript,
)
from normalizer.visual_backfill import _iter_candidates as iter_visual_candidates
from normalizer.visual_backfill import backfill_visuals
from scripts.luna_reenrich_control import (
    _recover_restore_journal,
    _validated_backup_manifest,
)


def _write_candidate(root: Path, *, media_type: str = "video") -> tuple[Path, Path]:
    channel = root / "Channel"
    channel.mkdir(parents=True)
    text_path = channel / "1.txt"
    text_path.write_text("Header\n", encoding="utf-8")
    meta_path = channel / "1.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "channel_name": "Channel",
                "message_id": 1,
                "media": [
                    {
                        "media_type": media_type,
                        "message_id": 1,
                        "file_path": f"media_cache/Channel/{media_type}/1.bin",
                        "download_status": "downloaded",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return text_path, meta_path


class CurrentTzHardeningTests(unittest.TestCase):
    def test_media_backfill_is_dry_run_unless_execute_live(self):
        parser = cli_app.build_parser()
        safe = parser.parse_args(["media-backfill", "--all"])
        live = parser.parse_args(["media-backfill", "--all", "--execute-live"])
        self.assertFalse(safe.execute_live)
        self.assertTrue(live.execute_live)

    def test_legacy_transcribe_backfill_is_dry_run_unless_execute_live(self):
        parser = cli_app.build_parser()
        safe = parser.parse_args(["transcribe", "backfill", "--limit", "1"])
        live = parser.parse_args(
            ["transcribe", "backfill", "--limit", "1", "--execute-live"]
        )
        self.assertFalse(safe.execute_live)
        self.assertTrue(live.execute_live)

    def test_traversal_path_is_rejected(self):
        with self.assertRaises(MediaPathError):
            resolve_media_path("media_cache/../../secret.txt")

    def test_failed_transcription_is_persisted_in_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _text, meta_path = _write_candidate(root)
            result = TranscriptionResult(
                status="failed",
                error="timeout",
                route="audio/transcriptions",
                attempts=[{"route": "audio/transcriptions", "status": "error"}],
                latency_ms=12.0,
            )
            with patch(
                "normalizer.transcription_backfill.transcribe_media",
                return_value=result,
            ):
                stats = backfill_transcripts(normalized_dir=root, limit=1)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stats.failed, 1)
        self.assertEqual(meta["media"][0]["transcription_status"], "failed")
        self.assertEqual(meta["transcriptions"][0]["route"], "audio/transcriptions")

    def test_cached_visual_canonicalizes_portable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _text, meta_path = _write_candidate(root, media_type="image")
            artifact = root / "visual.json"
            artifact.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "text": "cached visual",
                        "model": "vision",
                        "frame_count": 1,
                        "input_sha256": "a" * 64,
                        "prompt_version": "v1",
                    }
                ),
                encoding="utf-8",
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["media"][0].update(
                {
                    "file_path": r"C:\old\media_cache\Channel\image\1.bin",
                    "visual_status": "success",
                    "visual_artifact_path": str(artifact),
                }
            )
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with patch(
                "normalizer.visual_backfill._has_current_visual_identity",
                return_value=True,
            ), patch(
                "normalizer.visual_backfill.portable_media_path",
                return_value="media_cache/Channel/image/1.bin",
            ):
                stats = backfill_visuals(normalized_dir=root, limit=1)
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(saved["media"][0]["file_path"], "media_cache/Channel/image/1.bin")

    def test_failed_visual_is_persisted_in_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _text, meta_path = _write_candidate(root, media_type="image")
            with patch(
                "normalizer.visual_backfill._analyze",
                return_value=VisualExtractionResult(status="failed", error="vision_timeout"),
            ):
                stats = backfill_visuals(normalized_dir=root, limit=1)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stats.failed, 1)
        self.assertEqual(meta["media"][0]["visual_status"], "failed")
        self.assertEqual(meta["visual_extractions"][0]["error"], "vision_timeout")

    def test_backfill_reader_recovers_interrupted_atomic_pair_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text_path, meta_path = _write_candidate(root, media_type="image")
            original_text = b"Header\n"
            original_meta = meta_path.read_bytes()
            text_path.write_bytes(b"partial replacement")
            meta_path.write_text("{broken", encoding="utf-8")
            journal = meta_path.with_name(f".{meta_path.name}.visual.journal")
            journal.write_text(
                json.dumps(
                    {
                        "schema_version": "atomic_pair_v1",
                        "phase": "first_replaced",
                        "first": str(text_path),
                        "second": str(meta_path),
                        "original_first": base64.b64encode(original_text).decode("ascii"),
                        "original_second": base64.b64encode(original_meta).decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            candidates = iter_visual_candidates(root)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(text_path.read_bytes(), original_text)
            self.assertEqual(meta_path.read_bytes(), original_meta)
            self.assertFalse(journal.exists())

    def test_primary_400_then_fallback_timeout_preserves_both_attempts(self):
        media = Path("audio.mp3")
        primary = TranscriptionRequestError("chat/completions", 400, "bad multipart")
        with patch(
            "normalizer.transcription_handler._prepared_audio",
            return_value=nullcontext(media),
        ), patch(
            "normalizer.transcription_handler._call_chat_audio",
            side_effect=primary,
        ), patch(
            "normalizer.transcription_handler._call_stt_audio",
            side_effect=requests.Timeout("timeout"),
        ):
            with self.assertRaises(TranscriptionRequestError) as caught:
                _call_transcription_api(media)
        self.assertEqual(len(caught.exception.attempts), 2)
        self.assertEqual(caught.exception.attempts[0]["status_code"], 400)
        self.assertEqual(caught.exception.attempts[1]["detail"], "Timeout")

    def test_degenerate_transcript_is_rejected(self):
        repeated = " ".join(["и как бы снова одно и то же"] * 500)
        ordinary = " ".join(f"слово{index}" for index in range(500))
        self.assertTrue(_is_degenerate_transcript(repeated))
        self.assertFalse(_is_degenerate_transcript(ordinary))

    def test_no_speech_is_terminal_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _text, meta_path = _write_candidate(root)
            result = TranscriptionResult(status="no_speech", error="music_only")
            with patch(
                "normalizer.transcription_backfill.transcribe_media",
                return_value=result,
            ):
                stats = backfill_transcripts(normalized_dir=root, limit=1)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(stats.no_speech, 1)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(meta["media"][0]["transcription_status"], "no_speech")

    def test_non_media_contract_migration_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            card_path = root / "card.json"
            source_id = "telegram:1:1"
            card = {
                "schema_version": "enriched_v2",
                "prompt_version": "enriched-v2.1",
                "enrichment_model": "model",
                "enriched_at": "2026-01-01T00:00:0000:00",
                "provenance": {"source_id": source_id},
                "content_type": "telegram_post",
                "summary": "Источник сообщил о переговорах.",
            }
            card_path.write_text(json.dumps(card), encoding="utf-8")
            progress = {
                "enriched": {
                    "Channel/1": {
                        "source_fingerprint": "fingerprint",
                        "schema_version": "enriched_v2",
                        "prompt_version": "enriched-v2.1",
                        "enrichment_model": "model",
                    }
                }
            }
            with patch("enricher.pipeline.config.ENRICHMENT_PROMPT_VERSION", "enriched-v2.1"), patch(
                "enricher.pipeline.llm_backend.model_change_requires_regeneration",
                return_value=False,
            ), patch("enricher.pipeline._save_progress"):
                self.assertTrue(
                    _migrate_non_media_contract(
                        progress, "Channel/1", "fingerprint", card_path, source_id
                    )
                )
                self.assertFalse(
                    _migrate_non_media_contract(
                        progress, "Channel/1", "changed", card_path, source_id
                    )
                )
            saved = json.loads(card_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["media_contract_version"], "media-contract-v2")

    def test_backup_manifest_hash_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            backup = run / "backup" / "file.txt"
            backup.parent.mkdir(parents=True)
            backup.write_text("ok", encoding="utf-8")
            import hashlib

            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            (run / "backup_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "backup_restore_closeout_v1",
                        "verified": True,
                        "root": str(run / "backup"),
                        "files": [
                            {
                                "source": "file.txt",
                                "path": "file.txt",
                                "bytes": 2,
                                "sha256": digest,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(len(_validated_backup_manifest(run)["files"]), 1)
            backup.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                _validated_backup_manifest(run)

    def test_restore_recovery_rolls_back_nonterminal_journal_before_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            run = root / "run"
            target = project / "output.txt"
            retired = run / "failed" / "output.txt"
            backup = run / "backup" / "output.txt"
            target.parent.mkdir(parents=True)
            retired.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            target.write_text("new", encoding="utf-8")
            retired.write_text("original", encoding="utf-8")
            backup.write_text("original", encoding="utf-8")
            digest = hashlib.sha256(b"original").hexdigest()
            journal_path = run / "restore_journal.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": "restore_journal_v2",
                        "phase": "copying",
                        "run_dir": str(run),
                        "failed_root": str(run / "failed"),
                        "targets": [
                            {
                                "relative": "output.txt",
                                "target": str(target),
                                "retired": str(retired),
                                "backup": str(backup),
                                "initial_exists": True,
                                "phase": "copying",
                                "matching": [
                                    {
                                        "source": "output.txt",
                                        "bytes": 8,
                                        "sha256": digest,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(control, "PROJECT_ROOT", project):
                _recover_restore_journal(journal_path, run)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertFalse(retired.exists())
            self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["phase"], "rolled_back")

    def test_restore_recovery_rejects_corrupt_journal_without_mutation(self):
        import scripts.luna_reenrich_control as control

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            run = root / "run"
            target = project / "output.txt"
            target.parent.mkdir(parents=True)
            run.mkdir(parents=True)
            target.write_text("current", encoding="utf-8")
            journal_path = run / "restore_journal.json"
            journal_path.write_text("{broken", encoding="utf-8")
            with patch.object(control, "PROJECT_ROOT", project):
                with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                    _recover_restore_journal(journal_path, run)
            self.assertEqual(target.read_text(encoding="utf-8"), "current")

    def test_restore_recovery_resolves_moving_phase_after_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            run = root / "run"
            target = project / "output.txt"
            retired = run / "failed" / "output.txt"
            backup = run / "backup" / "output.txt"
            retired.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            retired.write_text("original", encoding="utf-8")
            backup.write_text("original", encoding="utf-8")
            digest = hashlib.sha256(b"original").hexdigest()
            journal = {
                "schema_version": "restore_journal_v2",
                "phase": "moving",
                "run_dir": str(run),
                "failed_root": str(run / "failed"),
                "targets": [
                    {
                        "relative": "output.txt",
                        "target": str(target),
                        "retired": str(retired),
                        "backup": str(backup),
                        "initial_exists": True,
                        "phase": "moving",
                        "matching": [{"source": "output.txt", "bytes": 8, "sha256": digest}],
                    }
                ],
            }
            journal_path = run / "restore_journal.json"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            with patch.object(control, "PROJECT_ROOT", project):
                _recover_restore_journal(journal_path, run)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertFalse(retired.exists())


if __name__ == "__main__":
    unittest.main()
