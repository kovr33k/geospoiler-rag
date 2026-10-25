"""
Web Handler — extracts article text from web URLs using crawl4ai.
"""

import logging

from crawl4ai import AsyncWebCrawler

logger = logging.getLogger("geospoiler.normalizer.web")


async def extract_web_text(url: str) -> str:
    """
    Extract article text from a web page using Crawl4AI.
    Returns formatted markdown text with content.
    """
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)

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
