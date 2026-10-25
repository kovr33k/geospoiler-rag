"""YouTube search API over the shared FTS database.

The SQL implementation stays shared with card FTS to preserve its schema and
transaction behavior. Callers can use this module without knowing that layout.
"""

from geospoiler.retrieval.cards.card_fts import (
    YouTubeSegmentFtsBuildStats,
    YouTubeSegmentFtsMatch,
    list_youtube_segment_ids,
    rebuild_youtube_segment_index,
    search_youtube_segments,
)

__all__ = [
    "YouTubeSegmentFtsBuildStats", "YouTubeSegmentFtsMatch",
    "list_youtube_segment_ids", "rebuild_youtube_segment_index", "search_youtube_segments",
]
