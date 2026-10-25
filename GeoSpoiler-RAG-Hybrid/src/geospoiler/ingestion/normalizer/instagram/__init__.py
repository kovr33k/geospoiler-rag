"""Instagram normalization package."""

from geospoiler.ingestion.normalizer.instagram.pipeline import canonicalize_instagram_url as canonicalize_instagram_url
from geospoiler.ingestion.normalizer.instagram.pipeline import extract_instagram_text as extract_instagram_text

__all__ = ["canonicalize_instagram_url", "extract_instagram_text"]
