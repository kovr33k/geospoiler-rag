import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import llm_backend
from normalizer.instagram import vision as instagram_vision
from normalizer.media_frames import _extract_frames
from normalizer.media_vision import analyze_image, analyze_video


class MediaVisionTests(unittest.TestCase):
    def setUp(self):
        self.original_backend = config.MEDIA_VISION_BACKEND
        self.original_profile = config.LLM_PROFILE
        self.original_model = config.CODEX_LUNA_MODEL
        self.original_visual_dir = config.MEDIA_VISUAL_DIR
        self.original_cache_dir = config.MEDIA_CACHE_DIR
        self.original_max_images = config.MEDIA_VISION_MAX_IMAGES
        self.original_max_frames = config.MEDIA_VISION_MAX_FRAMES
        self.original_batch_size = config.MEDIA_VISION_FRAME_BATCH_SIZE
        config.LLM_PROFILE = "luna"
        config.CODEX_LUNA_MODEL = "codex-test-model"

    def test_instagram_luna_batches_without_legacy_api_key_and_respects_limit(self):
        frames = [Path(f"frame_{index}.jpg") for index in range(4)]
        calls = []

        def fake_batch(batch, start_idx):
            calls.append((batch, start_idx))
            return f"batch-{start_idx}"

        with patch.object(config, "MEDIA_VISION_BACKEND", "luna"), patch.object(
            config, "INSTAGRAM_VISION_API_KEY", ""
        ), patch.object(config, "MEDIA_VISION_MAX_IMAGES", 2), patch.object(
            instagram_vision, "_ocr_single_batch", side_effect=fake_batch
        ):
            result = instagram_vision._ocr_frames_batched(frames, batch_size=5)

        self.assertEqual(result, ["batch-0", "batch-2"])
        self.assertEqual([len(batch) for batch, _ in calls], [2, 2])

    def tearDown(self):
        config.MEDIA_VISION_BACKEND = self.original_backend
        config.LLM_PROFILE = self.original_profile
        config.CODEX_LUNA_MODEL = self.original_model
        config.MEDIA_VISUAL_DIR = self.original_visual_dir
        config.MEDIA_CACHE_DIR = self.original_cache_dir
        config.MEDIA_VISION_MAX_IMAGES = self.original_max_images
        config.MEDIA_VISION_MAX_FRAMES = self.original_max_frames
        config.MEDIA_VISION_FRAME_BATCH_SIZE = self.original_batch_size

    def test_current_backend_analyzes_native_image_via_configured_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.MEDIA_VISUAL_DIR = root / "visuals"
            image = root / "frame.jpg"
            image.write_bytes(b"image")
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "choices": [{"message": {"content": "Current API visual result"}}]
            }
            with patch.object(config, "MEDIA_VISION_BACKEND", "current"), patch.object(
                config, "VISION_API_KEY", "vision-key"
            ), patch.object(config, "VISION_BASE_URL", "https://vision.example/v1"), patch.object(
                config, "VISION_MODEL", "vision-model"
            ), patch("normalizer.media_vision.requests.post", return_value=response) as post:
                result = analyze_image(image)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.model, "vision-model")
            post.assert_called_once()
            self.assertEqual(post.call_args.kwargs["json"]["model"], "vision-model")
            self.assertIn("data:image/jpeg;base64,", post.call_args.kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"])

    def test_current_backend_analyzes_native_video_frames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.MEDIA_VISUAL_DIR = root / "visuals"
            config.MEDIA_CACHE_DIR = root / "cache"
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frames = []
            for index in range(2):
                frame = frames_dir / f"frame_{index:04d}.jpg"
                frame.write_bytes(b"frame")
                frames.append(frame)
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "choices": [{"message": {"content": "Current API frame result"}}]
            }
            with patch.object(config, "MEDIA_VISION_BACKEND", "current"), patch.object(
                config, "VISION_API_KEY", "vision-key"
            ), patch.object(config, "VISION_BASE_URL", "https://vision.example/v1"), patch.object(
                config, "VISION_MODEL", "vision-model"
            ), patch(
                "normalizer.media_vision._extract_frames", return_value=frames
            ), patch(
                "normalizer.media_vision._dedup_frames", side_effect=lambda values: values
            ), patch(
                "normalizer.media_vision._filter_empty_frames", side_effect=lambda values: values
            ), patch("normalizer.media_vision.requests.post", return_value=response) as post:
                result = analyze_video(video, source_key="test:current")

            self.assertEqual(result.status, "success")
            self.assertEqual(result.model, "vision-model")
            self.assertEqual(post.call_count, 1)

    def test_current_backend_artifact_uses_visual_model_and_cache_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.MEDIA_VISUAL_DIR = root / "visuals"
            config.MEDIA_CACHE_DIR = root / "cache"
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frame = frames_dir / "frame_0000.jpg"
            frame.write_bytes(b"frame")
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "choices": [{"message": {"content": "Current API frame result"}}]
            }
            with patch.object(config, "MEDIA_VISION_BACKEND", "current"), patch.object(
                config, "VISION_API_KEY", "vision-key"
            ), patch.object(config, "VISION_BASE_URL", "https://vision.example/v1"), patch.object(
                config, "VISION_MODEL", "vision-model"
            ), patch(
                "normalizer.media_vision._extract_frames", return_value=[frame]
            ), patch(
                "normalizer.media_vision._dedup_frames", side_effect=lambda values: values
            ), patch(
                "normalizer.media_vision._filter_empty_frames", side_effect=lambda values: values
            ), patch("normalizer.media_vision.requests.post", return_value=response) as post:
                first = analyze_video(video, source_key="test:cache")
                artifact_path = Path(first.artifact_path)
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                second = analyze_video(video, source_key="test:cache")
                artifact["status"] = "failed"
                artifact["text"] = ""
                artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
                third = analyze_video(video, source_key="test:cache")
                artifact_path.write_text("[]", encoding="utf-8")
                fourth = analyze_video(video, source_key="test:cache")

            self.assertEqual(artifact["model"], "vision-model")
            self.assertEqual(first.model, "vision-model")
            self.assertEqual(second.model, "vision-model")
            self.assertEqual(third.status, "success")
            self.assertEqual(fourth.status, "success")
            self.assertEqual(post.call_count, 3)

    def test_frame_extraction_passes_native_frame_cap_to_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            with patch("normalizer.media_frames.subprocess.run") as run:
                _extract_frames(video, root / "frames", 2.0, max_frames=4)

            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-frames:v") + 1], "4")
    def test_analyze_image_writes_identity_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.MEDIA_VISUAL_DIR = root / "visuals"
            image = root / "frame.jpg"
            image.write_bytes(b"image")
            with patch.object(
                llm_backend,
                "complete_media_analysis_sync",
                return_value="OCR: visible text",
            ) as complete:
                result = analyze_image(image)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.model, "codex-cli:codex-test-model@xhigh")
            self.assertTrue(Path(result.artifact_path).exists())
            complete.assert_called_once()
            artifact = Path(result.artifact_path).read_text(encoding="utf-8")
            self.assertIn('"prompt_version"', artifact)
            self.assertIn('"input_sha256"', artifact)

    def test_analyze_video_batches_frames_and_cleans_workdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config.MEDIA_VISUAL_DIR = root / "visuals"
            config.MEDIA_CACHE_DIR = root / "cache"
            video = root / "clip.mp4"
            video.write_bytes(b"video")
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frames = []
            for index in range(3):
                frame = frames_dir / f"frame_{index:04d}.jpg"
                frame.write_bytes(b"frame")
                frames.append(frame)
            config.MEDIA_VISION_MAX_IMAGES = 2
            config.MEDIA_VISION_MAX_FRAMES = 2
            config.MEDIA_VISION_FRAME_BATCH_SIZE = 2
            with patch(
                "normalizer.media_vision._extract_frames",
                return_value=frames,
            ), patch(
                "normalizer.media_vision._dedup_frames",
                side_effect=lambda values: values,
            ), patch(
                "normalizer.media_vision._filter_empty_frames",
                side_effect=lambda values: values,
            ), patch.object(
                llm_backend,
                "complete_media_analysis_sync",
                return_value="frame analysis",
            ) as complete:
                result = analyze_video(video, source_key="test:1")

            self.assertEqual(result.status, "success")
            self.assertEqual(result.frame_count, 2)
            self.assertEqual(complete.call_count, 1)
            self.assertFalse((config.MEDIA_CACHE_DIR / "native_video_work").exists())
            self.assertTrue(Path(result.artifact_path).exists())
