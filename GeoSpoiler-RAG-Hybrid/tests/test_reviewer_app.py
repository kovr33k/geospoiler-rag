import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_reviewer_app():
    fake_streamlit = types.SimpleNamespace(
        set_page_config=lambda **kwargs: None,
        markdown=lambda *args, **kwargs: None,
    )
    sys.modules["streamlit"] = fake_streamlit
    sys.modules.pop("reviewer_app", None)
    return importlib.import_module("reviewer_app")


class ReviewerAppTests(unittest.TestCase):
    def test_reviewer_omits_wiki_tab_when_master_switch_is_off(self):
        reviewer_app = _load_reviewer_app()
        content_review = Mock()

        with patch.object(reviewer_app.config, "WIKI_ENABLED", False):
            with patch.object(
                reviewer_app,
                "_render_content_review",
                content_review,
            ):
                reviewer_app.main()

        content_review.assert_called_once_with()

    def test_source_kind_for_prompt_extraction_detects_supported_sources(self):
        reviewer_app = _load_reviewer_app()

        self.assertEqual(reviewer_app._source_kind_for_url("https://youtu.be/abc"), "youtube")
        self.assertEqual(
            reviewer_app._source_kind_for_url("https://www.youtube.com/watch?v=abc"),
            "youtube",
        )
        self.assertEqual(
            reviewer_app._source_kind_for_url("https://www.instagram.com/reel/abc/"),
            "instagram",
        )
        self.assertEqual(reviewer_app._source_kind_for_url("https://example.com/post"), "web")
        self.assertEqual(reviewer_app._source_kind_for_url(""), "message")

    def test_extract_with_prompt_calls_configured_chat_model(self):
        reviewer_app = _load_reviewer_app()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "Only the requested topic"}}]
        }

        with (
            patch.object(reviewer_app.config, "LLM_PROFILE", "current"),
            patch.object(reviewer_app.config, "FALLBACK_SYNTH_API_KEY", "test-key"),
            patch.object(reviewer_app.config, "FALLBACK_SYNTH_BASE_URL", "https://llm.example/v1"),
            patch.object(reviewer_app.config, "FALLBACK_SYNTH_MODEL", "test-model"),
            patch.object(reviewer_app.requests, "post", return_value=response) as post,
        ):
            result = reviewer_app._extract_with_prompt(
                "Full video transcript",
                "Extract only the requested topic",
                "youtube",
            )

        self.assertEqual(result, "Only the requested topic")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "test-model")
        self.assertIn("Full video transcript", payload["messages"][1]["content"])
        self.assertIn("Extract only the requested topic", payload["messages"][1]["content"])

    def test_prompt_text_can_provide_source_url_and_clean_instruction(self):
        reviewer_app = _load_reviewer_app()

        item = {
            "url": "",
            "message_text": "Original Telegram text",
        }
        prompt_text = (
            "https://www.youtube.com/watch?v=vVmfBZvpfHg\n"
            "о том как благодаря россии тем что корея стала заводом для России"
        )

        with patch.object(
            reviewer_app,
            "_extract_youtube_source",
            return_value="YouTube transcript",
        ) as youtube:
            source_text, source_label, clean_instruction = (
                reviewer_app._resolve_prompt_extraction_request(item, prompt_text)
            )

        youtube.assert_called_once_with("https://www.youtube.com/watch?v=vVmfBZvpfHg")
        self.assertEqual(source_text, "YouTube transcript")
        self.assertEqual(source_label, "youtube")
        self.assertEqual(
            clean_instruction,
            "о том как благодаря россии тем что корея стала заводом для России",
        )

    def test_prompt_extraction_result_does_not_rewrite_prompt_widget_state(self):
        reviewer_app = _load_reviewer_app()
        session_state = {
            "prompt_0": (
                "https://www.youtube.com/watch?v=vVmfBZvpfHg\n"
                "достать только условия труда"
            )
        }

        reviewer_app._remember_prompt_extraction_result(
            session_state,
            0,
            extracted="Filtered text",
            extraction_source="youtube",
            clean_prompt="достать только условия труда",
        )

        self.assertEqual(
            session_state["prompt_0"],
            "https://www.youtube.com/watch?v=vVmfBZvpfHg\n"
            "достать только условия труда",
        )
        self.assertEqual(session_state["text_0"], "Filtered text")
        self.assertEqual(session_state["source_0"], "youtube")
        self.assertEqual(
            session_state["clean_prompt_0"],
            "достать только условия труда",
        )

    def test_format_review_reason_labels_and_translates_known_reason(self):
        reviewer_app = _load_reviewer_app()

        self.assertEqual(
            reviewer_app._format_review_reason("YouTube link in post with text"),
            "Причина попадания в ревью: YouTube-ссылка в посте с текстом",
        )

    def test_format_review_reason_preserves_unknown_reason_with_label(self):
        reviewer_app = _load_reviewer_app()

        self.assertEqual(
            reviewer_app._format_review_reason("Custom reason"),
            "Причина попадания в ревью: Custom reason",
        )

    def test_processed_review_card_renders_safe_clickable_http_link(self):
        reviewer_app = _load_reviewer_app()
        rendered = []
        reviewer_app.st.markdown = lambda value, **kwargs: rendered.append(value)

        reviewer_app._render_review_card(
            {
                "status": "processed",
                "channel": "test",
                "message_id": '<unsafe id>',
                "message_date": '"unsafe date"',
                "url": 'https://example.com/?a=1&b="quoted"',
            },
            0,
        )

        html = rendered[0]
        self.assertIn('href="https://example.com/?a=1&amp;b=&quot;quoted&quot;"', html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn("&lt;unsafe id&gt;", html)
        self.assertNotIn('<unsafe id>', html)


if __name__ == "__main__":
    unittest.main()
