"""Shared CLI runtime helpers."""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

from geospoiler import config

logger = logging.getLogger("geospoiler")

async def _finalize_rag_safely(rag: Any) -> None:
    try:
        await asyncio.wait_for(
            rag.finalize_storages(),
            timeout=config.RAG_FINALIZE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "LightRAG finalize_storages timed out after %ss; continuing.",
            config.RAG_FINALIZE_TIMEOUT_SECONDS,
        )


def setup_logging():
    """Configure logging to file and console (UTF-8 safe on Windows)."""
    log_file = config.LOG_DIR / f"pipeline_{datetime.now().strftime('%Y%m%d')}.log"

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
