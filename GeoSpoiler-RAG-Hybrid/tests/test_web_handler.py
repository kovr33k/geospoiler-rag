import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalizer.web_handler import (  # noqa: E402
    _MAX_ARTICLE_CHARS,
    _NO_WAIT,
    extract_web_text,
)

_ARTICLE = "Порт Нарвы закрыли после удара по причалу, движение судов остановлено. "


class _Crawler:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def arun(self, url, config=None):
        self.calls.append((url, config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _page(markdown=_ARTICLE, *, success=True, error_message=None):
    return SimpleNamespace(success=success, markdown=markdown, error_message=error_message)


def _run(crawler, url):
    with patch("normalizer.web_handler.AsyncWebCrawler", return_value=crawler):
        return asyncio.run(extract_web_text(url))


class WebHandlerTests(unittest.TestCase):
    def test_script_wait_config_is_passed_to_the_first_crawl(self):
        crawler = _Crawler([_page()])
        url = "https://example.com/story"
        text = _run(crawler, url)

        self.assertEqual(len(crawler.calls), 1)
        crawled, config = crawler.calls[0]
        self.assertEqual(crawled, url)
        self.assertEqual(config.wait_until, "networkidle")
        self.assertEqual(config.delay_before_return_html, 4.0)
        self.assertEqual(config.page_timeout, 60000)
        self.assertEqual(config.excluded_tags, ["nav", "header", "footer", "aside", "form"])
        self.assertIn(".cky-modal", config.excluded_selector)
        self.assertNotIn('[role="dialog"]', config.excluded_selector)
        self.assertTrue(config.remove_consent_popups)
        self.assertFalse(config.remove_overlay_elements)
        self.assertTrue(config.exclude_all_images)
        self.assertTrue(text.startswith(f"[Веб: {url}]\n"))
        self.assertIn("Порт Нарвы", text)

    def test_long_article_is_kept_whole(self):
        url = "https://example.com/story"
        article = _ARTICLE * 400  # ~29k chars, a long investigation
        text = _run(_Crawler([_page(article)]), url)

        self.assertEqual(text, f"[Веб: {url}]\n{article}")
        self.assertNotIn("текст обрезан", text)

    def test_runaway_page_is_capped(self):
        url = "https://example.com/feed"
        endless = "ъ" * (_MAX_ARTICLE_CHARS + 500)
        text = _run(_Crawler([_page(endless)]), url)

        self.assertTrue(text.endswith("\n[...текст обрезан]"))
        self.assertEqual(text.count("ъ"), _MAX_ARTICLE_CHARS)

    def test_failed_or_timed_out_crawl_retries_once_without_the_wait(self):
        url = "https://example.com/story"
        failed = _Crawler([
            _page(success=False, markdown="", error_message="blocked"),
            _page(),
        ])
        text = _run(failed, url)
        self.assertEqual(
            [config.wait_until for _url, config in failed.calls],
            ["networkidle", _NO_WAIT.wait_until],
        )
        self.assertNotEqual(_NO_WAIT.wait_until, "networkidle")
        self.assertTrue(failed.calls[1][1].remove_consent_popups)
        self.assertIn("Порт Нарвы", text)

        timed_out = _Crawler([TimeoutError("page timeout"), _page()])
        _run(timed_out, url)
        self.assertIs(timed_out.calls[1][1], _NO_WAIT)

        wrapped = _Crawler([
            RuntimeError("Failed on navigating ACS-GOTO:\nTimeout 60000ms exceeded"),
            _page(),
        ])
        _run(wrapped, url)
        self.assertEqual(len(wrapped.calls), 2)
        self.assertIs(wrapped.calls[1][1], _NO_WAIT)

        still_failed = _Crawler([
            _page(success=False, markdown="", error_message="blocked"),
            _page(success=False, markdown="", error_message="still blocked"),
        ])
        given_up = _run(still_failed, url)
        self.assertEqual(given_up, f"[Веб: {url}]\n[Содержание не удалось извлечь]")
        self.assertEqual(len(still_failed.calls), 2)

        other_error = _Crawler([
            RuntimeError("connection reset"),
            RuntimeError("connection reset"),
        ])
        errored = _run(other_error, url)
        self.assertEqual(errored, f"[Веб: {url} — ошибка обработки]")
        self.assertEqual(len(other_error.calls), 2)
        self.assertIs(other_error.calls[1][1], _NO_WAIT)

    def test_public_telegram_post_is_crawled_as_an_embed(self):
        original = "https://t.me/narva_news/42"
        crawler = _Crawler([_page()])
        text = _run(crawler, original)

        self.assertEqual(
            crawler.calls[0][0],
            "https://t.me/narva_news/42?embed=1&mode=tme",
        )
        self.assertTrue(text.startswith(f"[Веб: {original}]\n"))
        self.assertNotIn("embed=1", text.splitlines()[0])

        for url in (
            "https://t.me/narva_news/42/",
            "https://t.me/narva_news/42?single",
        ):
            with self.subTest(url=url):
                again = _Crawler([_page()])
                _run(again, url)
                self.assertEqual(
                    again.calls[0][0],
                    "https://t.me/narva_news/42?embed=1&mode=tme",
                )

        unchanged = (
            "https://t.me/s/narva_news/42",
            "https://t.me/c/123456789/42",
            "https://t.me/narva_news/42?embed=1",
            "https://example.com/t.me/narva_news/42",
        )
        for url in unchanged:
            with self.subTest(url=url):
                plain = _Crawler([_page()])
                kept = _run(plain, url)
                self.assertEqual(plain.calls[0][0], url)
                self.assertTrue(kept.startswith(f"[Веб: {url}]\n"))

    def test_malformed_url_returns_the_processing_error(self):
        url = "https://[broken"
        crawler = _Crawler([])
        text = _run(crawler, url)

        self.assertEqual(text, f"[Веб: {url} — ошибка обработки]")
        self.assertEqual(crawler.calls, [])


if __name__ == "__main__":
    unittest.main()
