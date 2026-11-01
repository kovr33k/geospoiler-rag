"""
Web Handler — extracts article text from web URLs using crawl4ai.
"""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

logger = logging.getLogger("geospoiler.normalizer.web")

# Script-rendered pages (ChatGPT, Gemini) stay empty unless the crawl waits.
# A failed or timed-out run is retried once with crawl4ai's own defaults.
_SCRIPT_WAIT = CrawlerRunConfig(
    wait_until="networkidle",
    delay_before_return_html=4.0,
    page_timeout=60000,
    excluded_tags=["nav", "header", "footer", "aside", "form"],
)
# Public post: t.me/<channel>/<id>. Previews (t.me/s/...) and private posts
# (t.me/c/...) are different paths and stay unchanged.
_TELEGRAM_POST_PATH = re.compile(r"^/([^/]+)/(\d+)/?$")


def _telegram_embed_url(url: str) -> str:
    """Fetch a public t.me post as an embed. Other URLs are unchanged."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != "t.me":
        return url
    if "embed=" in parts.query.lower():
        return url
    match = _TELEGRAM_POST_PATH.fullmatch(parts.path)
    if not match or match.group(1).lower() in {"s", "c"}:
        return url
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"/{match.group(1)}/{match.group(2)}",
            "embed=1&mode=tme",
            "",
        )
    )


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if "timeout" in type(exc).__name__.casefold():
        return True
    return "timeout" in str(exc).casefold()


async def _crawl_page(crawler, crawl_url: str):
    """Wait for scripts first. One default retry covers a timeout or a failed run."""
    try:
        result = await crawler.arun(url=crawl_url, config=_SCRIPT_WAIT)
    except Exception as exc:
        if not _is_timeout(exc):
            raise
        logger.warning("Crawl4AI timed out for %s; retrying with defaults", crawl_url)
        return await crawler.arun(url=crawl_url)
    if not result.success:
        logger.warning(
            "Crawl4AI failed for %s: %s; retrying with defaults",
            crawl_url,
            result.error_message,
        )
        return await crawler.arun(url=crawl_url)
    return result


async def extract_web_text(url: str) -> str:
    """
    Extract article text from a web page using Crawl4AI.
    Returns formatted markdown text with content.
    """
    try:
        crawl_url = _telegram_embed_url(url)
        async with AsyncWebCrawler() as crawler:
            result = await _crawl_page(crawler, crawl_url)

            if not result.success:
                logger.warning(f"Crawl4AI failed for {url}: {result.error_message}")
                return f'[Веб: {url}]\n[Содержание не удалось извлечь]'

            markdown_content = result.markdown

            if not markdown_content or len(markdown_content.strip()) < 50:
                return f'[Веб: {url}]\n[Содержание не удалось извлечь]'

            # Limit very long articles
            if len(markdown_content) > 10000:
                markdown_content = markdown_content[:10000] + "\n[...текст обрезан]"

            return f"[Веб: {url}]\n{markdown_content}"

    except Exception as e:
        logger.error(f"Web extraction error for {url}: {e}")
        return f'[Веб: {url} — ошибка обработки]'
