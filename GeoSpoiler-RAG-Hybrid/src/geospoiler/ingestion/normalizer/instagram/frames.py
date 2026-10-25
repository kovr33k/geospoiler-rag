"""Backward-compatible Instagram facade for shared media frame utilities."""

from geospoiler.ingestion.normalizer.media_frames import (
    _compute_edge_density,
    _compute_phash,
    _dedup_frames,
    _extract_frames,
    _filter_empty_frames,
    _hamming_distance,
)

__all__ = [
    "_compute_edge_density",
    "_compute_phash",
    "_dedup_frames",
    "_extract_frames",
    "_filter_empty_frames",
    "_hamming_distance",
]
