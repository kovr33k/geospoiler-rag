"""YouTube extraction must use the project's installed downloader, independent of PATH."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from geospoiler import config
from geospoiler.ingestion.normalizer import youtube_handler


@pytest.mark.parametrize("operation", ["metadata", "subtitles", "audio"])
def test_youtube_uses_active_python_for_every_download(operation, monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--write-subs" in command:
            template = command[command.index("-o") + 1]
            Path(template.replace("%(id)s", "abc123") + ".ru.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nSubtitle evidence\n", encoding="utf-8"
            )
        elif "-x" in command:
            template = command[command.index("-o") + 1]
            Path(template.replace("%(ext)s", "mp3")).write_bytes(b"audio")
        return subprocess.CompletedProcess(command, 0, json.dumps({"id": "abc123"}), "")

    monkeypatch.setattr(youtube_handler.subprocess, "run", run)
    monkeypatch.setattr(config, "MEDIA_CACHE_DIR", tmp_path)
    url = "https://www.youtube.com/watch?v=abc123"
    info = {"id": "abc123", "subtitles": {"ru": [{}]}}

    if operation == "metadata":
        assert youtube_handler._get_video_info(url) == {"id": "abc123"}
    elif operation == "subtitles":
        cues, language, source = youtube_handler._get_timed_subtitles(url, info)
        assert [cue.text for cue in cues] == ["Subtitle evidence"]
        assert (language, source) == ("ru", "subtitles")
    else:
        monkeypatch.setattr(config, "TRANSCRIPTION_ENABLED", True)
        monkeypatch.setattr(config, "TRANSCRIPTION_API_KEY", "controlled-test-key")
        monkeypatch.setattr(youtube_handler, "_split_audio", lambda path, *_: [path])
        monkeypatch.setattr(youtube_handler, "_call_whisper", lambda _: "Audio evidence")
        assert youtube_handler._transcribe_audio(url, info) == "Audio evidence"

    assert len(calls) == 1
    assert calls[0][:3] == [sys.executable, "-m", "yt_dlp"]
    assert calls[0][-1] == url
