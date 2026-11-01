import difflib
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

        transcript = "Расшифровка ролика про завод и условия поставок. " * 8
        with patch.object(
            reviewer_app,
            "_extract_youtube_source",
            return_value=transcript,
        ) as youtube:
            source_text, source_label, clean_instruction = (
                reviewer_app._resolve_prompt_extraction_request(item, prompt_text)
            )

        youtube.assert_called_once_with("https://www.youtube.com/watch?v=vVmfBZvpfHg")
        self.assertEqual(source_text, transcript)
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

        record_key = "Channel_1_abcd.json"
        session_state = {f"prompt_{record_key}": session_state["prompt_0"]}
        reviewer_app._remember_prompt_extraction_result(
            session_state,
            record_key,
            extracted="Filtered text",
            extraction_source="youtube",
            clean_prompt="достать только условия труда",
        )

        self.assertEqual(
            session_state[f"prompt_{record_key}"],
            "https://www.youtube.com/watch?v=vVmfBZvpfHg\n"
            "достать только условия труда",
        )
        self.assertEqual(session_state[f"text_{record_key}"], "Filtered text")
        self.assertEqual(session_state[f"source_{record_key}"], "youtube")
        self.assertEqual(
            session_state[f"clean_prompt_{record_key}"],
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
        reviewer_app.st.button = lambda *args, **kwargs: False

        reviewer_app._render_review_card(
            {
                "status": "processed",
                "channel": "test",
                "message_id": '<unsafe id>',
                "message_date": '"unsafe date"',
                "url": 'https://example.com/?a=1&b="quoted"',
                "_filepath": "queue/test_1_ab.json",
            },
        )

        html = rendered[0]
        self.assertIn('href="https://example.com/?a=1&amp;b=&quot;quoted&quot;"', html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn("&lt;unsafe id&gt;", html)
        self.assertNotIn('<unsafe id>', html)

    def test_record_key_uses_queue_file_name(self):
        reviewer_app = _load_reviewer_app()

        first = str(Path("queue") / "росия_42_ab.json")
        second = str(Path("queue") / "росия_423_ab.json")
        self.assertEqual(reviewer_app._review_record_key({"_filepath": first}), "росия_42_ab.json")
        self.assertNotEqual(
            reviewer_app._review_record_key({"_filepath": first}),
            reviewer_app._review_record_key({"_filepath": second}),
        )

    def test_validate_review_text_rejects_empty_post_link_and_markers(self):
        reviewer_app = _load_reviewer_app()
        post = "смотри https://example.com/post"

        empty = reviewer_app.validate_review_text("  \n", post)
        self.assertIn("Пропустить", empty)

        same = reviewer_app.validate_review_text("  смотри   https://example.com/post ", post)
        self.assertIn("самого поста", same)
        different_url = reviewer_app.validate_review_text("смотри https://other.example/x", post)
        self.assertIn("самого поста", different_url)

        bare_link = reviewer_app.validate_review_text("https://example.com/only", post)
        self.assertIn("букв", bare_link)

        for marker in (
            "[Веб: https://example.com]\n[Содержание не удалось извлечь]",
            "[Веб: https://example.com — ошибка обработки]",
            "[YouTube: не удалось получить информацию — https://youtu.be/abc]",
            "[YouTube: ошибка обработки — https://youtu.be/abc]",
            "[Веб: https://example.com]",
            "Черновик\n[Отправлено в очередь на ручной просмотр: Channel_1_ab.json]",
            "[Уже обработано: Channel_1_ab.json]",
        ):
            with self.subTest(marker=marker):
                self.assertIn("ошибке загрузки", reviewer_app.validate_review_text(marker, post))

        self.assertIsNone(
            reviewer_app.validate_review_text(
                "Порт Нарвы закрыли после удара по причалу.",
                post,
            )
        )

    def test_validate_review_text_rejects_a_near_copy_of_the_post(self):
        reviewer_app = _load_reviewer_app()
        post = "Договор сср перестал действовать после распада союза. https://example.com/a"
        one_letter = "  договор   ссср перестал действовать после распада союза. https://example.com/b"

        rejected = reviewer_app.validate_review_text(one_letter, post)
        self.assertIn("самого поста", rejected)

        different = "Порт Нарвы закрыли после удара по причалу сегодня утром."
        self.assertLess(abs(len(different) - len("Договор сср перестал действовать после распада союза.")), 20)
        self.assertIsNone(reviewer_app.validate_review_text(different, post))

        quoted = (
            "Новый отчёт описывает поставки зерна, закрытие порта и условия сделки. " * 6
            + "В тексте есть цитата поста: "
            + post
        )
        self.assertIsNone(reviewer_app.validate_review_text(quoted, post))
        self.assertIsNone(reviewer_app.validate_review_text(different, "https://example.com/only"))

    def test_formatted_reel_caption_matching_the_post_is_not_approved(self):
        reviewer_app = _load_reviewer_app()
        words = "Подпись ролика сообщает о закрытом порте и остановке судов у причала. "
        caption = []
        letters = 0
        for char in words * 4:
            if char.isalpha():
                if letters == 118:
                    break
                letters += 1
            caption.append(char)
        caption = "".join(caption).strip()
        self.assertEqual(sum(char.isalpha() for char in caption), 118)

        url = "https://www.instagram.com/reel/ABC118/"
        formatted = "\n\n".join((
            f"[Instagram Reel: {url} - @channel]",
            caption,
            "[Текст из самого видео недоступен: Instagram не отдал субтитры, "
            "сохранена только подпись.]",
        ))
        self.assertGreaterEqual(
            sum(char.isalpha() for char in formatted),
            reviewer_app._MIN_AUTOMATIC_DOWNLOAD_LETTERS,
        )
        self.assertIn("самого поста", reviewer_app.validate_review_text(formatted, caption))
        self.assertIsNotNone(reviewer_app.download_quality_error(formatted, url))

        item = {
            "_filepath": "queue/Channel_118_reel.json",
            "url": url,
            "message_text": caption,
        }
        with patch.object(reviewer_app, "_extract_instagram_source", return_value=formatted), \
             patch.object(reviewer_app, "mark_reviewed") as save:
            error = reviewer_app._approve_review_item(item)

        save.assert_not_called()
        self.assertTrue(error)

    def test_validate_review_text_rejects_a_long_near_copy(self):
        reviewer_app = _load_reviewer_app()
        post = (
            "People see people help people near people. " * 5
            + "People see people help people near all."
        )
        near = "Q" + post[1:]
        self.assertEqual(len(post), 254)
        self.assertEqual(len(near), 254)
        self.assertTrue(near.startswith("Qeople"))

        folded_post = post.casefold()
        folded_near = near.casefold()
        with_junk = difflib.SequenceMatcher(None, folded_near, folded_post).ratio()
        without_junk = difflib.SequenceMatcher(
            None, folded_near, folded_post, autojunk=False,
        ).ratio()
        self.assertLess(with_junk, 0.9, with_junk)
        self.assertGreaterEqual(without_junk, 0.9, without_junk)
        self.assertIn("самого поста", reviewer_app.validate_review_text(near, post))

    def test_design_includes_is_not_a_login_wall(self):
        reviewer_app = _load_reviewer_app()
        words = "The design includes a detailed map of the harbor and the yards beside it. "
        article = []
        letters = 0
        for char in words * 8:
            if char.isalpha():
                if letters == 268:
                    break
                letters += 1
            article.append(char)
        article = "".join(article).strip()
        self.assertEqual(sum(char.isalpha() for char in article), 268)
        self.assertIn("design includes", article.casefold())
        self.assertIsNone(
            reviewer_app.download_quality_error(article, "https://example.com/design")
        )

        wall = article.replace("design includes", "sign in", 1)
        self.assertGreaterEqual(sum(char.isalpha() for char in wall), 150)
        self.assertLess(sum(char.isalpha() for char in wall), 600)
        self.assertIsNotNone(
            reviewer_app.download_quality_error(wall, "https://example.com/login")
        )

    def test_russian_news_verb_is_not_a_login_wall(self):
        reviewer_app = _load_reviewer_app()
        lead = "Страна может войти в состав США "
        pad = "после долгого договора и открытого голосования парламента. "
        article = []
        letters = 0
        for char in lead + pad * 6:
            if char.isalpha():
                if letters == 207:
                    break
                letters += 1
            article.append(char)
        article = "".join(article).strip()
        count = sum(char.isalpha() for char in article)
        self.assertEqual(count, 207)
        self.assertGreaterEqual(count, reviewer_app._MIN_AUTOMATIC_DOWNLOAD_LETTERS)
        self.assertLess(count, reviewer_app._LOGIN_WALL_LETTER_LIMIT)
        self.assertIn("войти в состав США", article)
        self.assertIsNone(
            reviewer_app.download_quality_error(article, "https://example.com/brief")
        )

    def test_youtube_metadata_does_not_hide_a_copied_description(self):
        reviewer_app = _load_reviewer_app()
        words = "Подпись ролика сообщает о закрытом порте и остановке судов у причала. "
        caption = []
        letters = 0
        for char in words * 4:
            if char.isalpha():
                if letters == 118:
                    break
                letters += 1
            caption.append(char)
        caption = "".join(caption).strip()
        self.assertEqual(sum(char.isalpha() for char in caption), 118)

        url = "https://youtu.be/abc118desc"
        formatted = "\n".join((
            "[YouTube]",
            "Автор: Канал ГеоСпойлер про порты и поставки",
            "Название: Закрытие порта Нарвы после удара по причалу сегодня",
            f"URL: {url}",
            "",
            "[Описание видео]",
            caption,
        ))
        self.assertGreaterEqual(
            sum(char.isalpha() for char in formatted),
            reviewer_app._MIN_AUTOMATIC_DOWNLOAD_LETTERS,
        )
        scrubbed = reviewer_app._substantive_download_text(formatted)
        self.assertEqual(sum(char.isalpha() for char in scrubbed), 118)
        self.assertNotIn("Автор:", scrubbed)
        self.assertNotIn("Название:", scrubbed)
        self.assertIn("самого поста", reviewer_app.validate_review_text(formatted, caption))
        self.assertIsNotNone(reviewer_app.download_quality_error(formatted, url))

        item = {
            "_filepath": "queue/Channel_118_youtube.json",
            "url": url,
            "message_text": caption,
        }
        with patch.object(reviewer_app, "_extract_youtube_source", return_value=formatted), \
             patch.object(reviewer_app, "mark_reviewed") as save:
            error = reviewer_app._approve_review_item(item)

        save.assert_not_called()
        self.assertTrue(error)

    def test_approve_saves_downloaded_text(self):
        reviewer_app = _load_reviewer_app()
        item = {
            "_filepath": "queue/Channel_1_ab.json",
            "url": "https://example.com/story",
            "message_text": "Короткий пост",
        }
        with patch.object(
            reviewer_app,
            "_extract_source_text_for_prompt",
            return_value=("Порт Нарвы закрыли после удара.", "web"),
        ), patch.object(reviewer_app, "mark_reviewed") as save:
            error = reviewer_app._approve_review_item(item)

        self.assertIsNone(error)
        save.assert_called_once_with(
            "queue/Channel_1_ab.json",
            extracted_text="Порт Нарвы закрыли после удара.",
            extraction_source="web",
        )

    def test_approve_does_not_save_failure_or_marker_text(self):
        reviewer_app = _load_reviewer_app()
        item = {
            "_filepath": "queue/Channel_1_ab.json",
            "url": "https://example.com/story",
            "message_text": "Короткий пост",
        }
        with patch.object(
            reviewer_app,
            "_extract_source_text_for_prompt",
            side_effect=RuntimeError(
                "Не удалось извлечь текст из источника https://example.com/story."
            ),
        ), patch.object(reviewer_app, "mark_reviewed") as save:
            raised = reviewer_app._approve_review_item(item)
        self.assertIn("https://example.com/story", raised)
        save.assert_not_called()

        with patch.object(
            reviewer_app,
            "_extract_source_text_for_prompt",
            return_value=(
                "[Веб: https://example.com/story]\n[Содержание не удалось извлечь]",
                "web",
            ),
        ), patch.object(reviewer_app, "mark_reviewed") as save:
            marker = reviewer_app._approve_review_item(item)
        self.assertIn("ошибке загрузки", marker)
        save.assert_not_called()

    def test_approve_without_url_does_not_save_the_post(self):
        reviewer_app = _load_reviewer_app()
        item = {
            "_filepath": "queue/Channel_1_uninformative.json",
            "url": "",
            "message_text": "Текст самого поста",
        }
        with patch.object(
            reviewer_app,
            "_extract_source_text_for_prompt",
        ) as extract, patch.object(reviewer_app, "mark_reviewed") as save:
            error = reviewer_app._approve_review_item(item)

        extract.assert_not_called()
        save.assert_not_called()
        self.assertIn("Редактировать", error)
        self.assertIn("Пропустить", error)

    def test_youtube_extraction_failure_raises_instead_of_returning_marker(self):
        reviewer_app = _load_reviewer_app()
        url = "https://youtu.be/abc"
        with patch(
            "geospoiler.ingestion.normalizer.youtube_handler.extract_youtube_text",
            return_value=f"[YouTube: не удалось получить информацию — {url}]",
        ):
            with self.assertRaises(RuntimeError) as caught:
                reviewer_app._extract_source_text_for_prompt({"url": url})

        self.assertIn(url, str(caught.exception))
        self.assertNotIn("не удалось получить информацию", str(caught.exception))

    def test_download_quality_rejects_shells_and_keeps_real_articles(self):
        reviewer_app = _load_reviewer_app()
        chatgpt = "https://chatgpt.com/s/t_abc"
        shell = (
            f"[Веб: {chatgpt}]\n"
            "Log in\nSign up for free\nSign in\nGoogle apps"
        )
        shell_error = reviewer_app.download_quality_error(shell, chatgpt)
        self.assertIn(chatgpt, shell_error)
        self.assertIn("нужен вход", shell_error)

        article = "[Веб: https://example.com/story]\n" + ("Нарва " * 200)
        self.assertIsNone(reviewer_app.download_quality_error(article, "https://example.com/story"))

        medium = "Порт Нарвы закрыли после удара. " * 10
        self.assertGreaterEqual(sum(char.isalpha() for char in medium), 150)
        self.assertLess(sum(char.isalpha() for char in medium), 600)
        self.assertIsNone(reviewer_app.download_quality_error(medium, "https://example.com/note"))

        login_wall = medium + "\nPlease log in to continue."
        self.assertIn(
            "https://example.com/login",
            reviewer_app.download_quality_error(login_wall, "https://example.com/login"),
        )

        long_with_phrase = ("Название статьи про порт. " * 50) + "\nSign in is mentioned once."
        self.assertIsNone(
            reviewer_app.download_quality_error(long_with_phrase, "https://example.com/long")
        )

        vk = "https://vk.com/video-1_2"
        invisible = "[Веб: " + vk + "]\n" + ("\u200b\u200c\u200d\u200e\u200f\u2060\ufeff" * 80)
        self.assertIn(vk, reviewer_app.download_quality_error(invisible, vk))

        self.assertIsNone(
            reviewer_app.validate_review_text(
                "Короткая заметка редактора.",
                "Текст самого поста",
            )
        )

    def test_approve_does_not_save_a_login_shell(self):
        reviewer_app = _load_reviewer_app()
        url = "https://chatgpt.com/s/t_abc"
        item = {
            "_filepath": "queue/Channel_1_ab.json",
            "url": url,
            "message_text": "Короткий пост",
        }
        shell = f"[Веб: {url}]\nLog in\nSign up for free\nSign in\nGoogle apps"
        with patch.object(reviewer_app, "_extract_web_source", return_value=shell), \
             patch.object(reviewer_app, "mark_reviewed") as save:
            error = reviewer_app._approve_review_item(item)

        self.assertIn(url, error)
        self.assertIn("нужен вход", error)
        save.assert_not_called()

    def test_clear_review_draft_removes_only_that_record(self):
        reviewer_app = _load_reviewer_app()
        session = {
            "text_a.json": "черновик",
            "prompt_a.json": "инструкция",
            "source_a.json": "web",
            "clean_prompt_a.json": "чистая",
            "editing_a.json": True,
            "text_b.json": "чужой черновик",
        }

        reviewer_app._clear_review_draft(session, "a.json")

        self.assertEqual(session, {"text_b.json": "чужой черновик"})

    def test_thin_download_buttons_keep_the_draft_and_skip_llm(self):
        reviewer_app = _load_reviewer_app()
        url = "https://gemini.google.com/share/abc"
        record_key = "Channel_1_ab.json"
        session = {
            f"editing_{record_key}": True,
            f"text_{record_key}": "черновик",
            f"prompt_{record_key}": "достань факты",
        }
        errors = []

        class _Layout:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                return False

            def __call__(self, *args, **kwargs):
                return self

        def columns(spec):
            count = spec if isinstance(spec, int) else len(spec)
            return [_Layout() for _ in range(count)]

        def text_area(label, value="", key=None, **kwargs):
            if key in session:
                return session[key]
            return value

        reviewer_app.st.session_state = session
        reviewer_app.st.error = errors.append
        reviewer_app.st.warning = lambda *args, **kwargs: None
        reviewer_app.st.markdown = lambda *args, **kwargs: None
        reviewer_app.st.text = lambda *args, **kwargs: None
        reviewer_app.st.rerun = lambda: None
        reviewer_app.st.columns = columns
        reviewer_app.st.spinner = _Layout()
        reviewer_app.st.expander = _Layout()
        reviewer_app.st.text_area = text_area
        reviewer_app.st.file_uploader = lambda *args, **kwargs: None
        reviewer_app.st.button = lambda label, **kwargs: label in {
            "📥 Извлечь весь текст из источника",
            "🎯 Извлечь по инструкции",
        }
        shell = f"[Веб: {url}]\nSign in\nGoogle apps"
        with patch.object(reviewer_app, "_extract_web_source", return_value=shell), \
             patch.object(reviewer_app, "_extract_with_prompt") as llm, \
             patch.object(reviewer_app, "mark_reviewed") as save:
            reviewer_app._render_review_card(
                {
                    "status": "pending",
                    "channel": "Channel",
                    "message_id": 1,
                    "url": url,
                    "message_text": "Пост",
                    "_filepath": str(Path("queue") / record_key),
                }
            )

        llm.assert_not_called()
        save.assert_not_called()
        self.assertTrue(errors)
        self.assertTrue(all(url in error for error in errors))
        self.assertEqual(session[f"text_{record_key}"], "черновик")
        self.assertEqual(session[f"prompt_{record_key}"], "достань факты")
        self.assertNotIn(f"source_{record_key}", session)


if __name__ == "__main__":
    unittest.main()
