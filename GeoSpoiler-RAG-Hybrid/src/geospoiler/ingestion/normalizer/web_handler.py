"""
Web Handler — extracts article text from web URLs using crawl4ai.
"""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

logger = logging.getLogger("geospoiler.normalizer.web")

# The whole article is kept. This cap only stops runaway pages such as endless
# feeds; enrichment splits long text into chunks itself.
_MAX_ARTICLE_CHARS = 120_000
# Cookie banners and consent dialogs are not article text. Generic dialogs and
# overlays stay: Facebook renders the post itself inside one.
_PAGE_CHROME = dict(
    excluded_tags=["nav", "header", "footer", "aside", "form"],
    excluded_selector=(
        "#onetrust-consent-sdk, #CybotCookiebotDialog, #didomi-host, "
        "#usercentrics-root, .qc-cmp2-container, "
        "#cookie-law-info-bar, .cli-modal, .cky-consent-container, .cky-modal"
    ),
    remove_consent_popups=True,
    exclude_all_images=True,
    exclude_social_media_links=True,
)
# Script-rendered pages (ChatGPT, Gemini) stay empty unless the crawl waits.
# A failed or timed-out run is retried once without the wait.
_SCRIPT_WAIT = CrawlerRunConfig(
    wait_until="networkidle",
    delay_before_return_html=4.0,
    page_timeout=60000,
    **_PAGE_CHROME,
)
_NO_WAIT = CrawlerRunConfig(**_PAGE_CHROME)
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


async def _crawl_page(crawler, crawl_url: str):
    """Wait for scripts first. Any failure retries once without the wait."""
    try:
        result = await crawler.arun(url=crawl_url, config=_SCRIPT_WAIT)
        if result.success:
            return result
        detail = result.error_message
    except Exception as exc:
        detail = exc
    logger.warning(
        "Crawl4AI failed for %s: %s; retrying without the wait",
        crawl_url,
        detail,
    )
    return await crawler.arun(url=crawl_url, config=_NO_WAIT)


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

            if len(markdown_content) > _MAX_ARTICLE_CHARS:
                logger.warning(
                    "Web page %s has %d chars; keeping the first %d",
                    url,
                    len(markdown_content),
                    _MAX_ARTICLE_CHARS,
                )
                markdown_content = (
                    markdown_content[:_MAX_ARTICLE_CHARS] + "\n[...текст обрезан]"
                )

            return f"[Веб: {url}]\n{markdown_content}"

    except Exception as e:
        logger.error(f"Web extraction error for {url}: {e}")
        return f'[Веб: {url} — ошибка обработки]'
