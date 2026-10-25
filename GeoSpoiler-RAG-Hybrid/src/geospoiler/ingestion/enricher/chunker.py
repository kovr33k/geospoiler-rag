"""
Chunker — splits long-form text into chunks for per-chunk LLM enrichment.

Strategy:
- Texts > CHUNK_THRESHOLD characters get split
- Split at ~CHUNK_SIZE characters with CHUNK_OVERLAP overlap
- If text has chapter markers / timestamps, prefer those as boundaries
- Returns list of chunk dicts with text and char ranges
"""

import logging
import re

logger = logging.getLogger("geospoiler.enricher.chunker")

# ── Configuration ──
CHUNK_THRESHOLD = 3000   # Don't chunk below this
CHUNK_SIZE = 2500        # Target chunk size in characters
CHUNK_OVERLAP = 200      # Overlap between chunks for context continuity

# Patterns that indicate natural section boundaries
_SECTION_BREAK_RE = re.compile(
    r"\n{2,}",  # Double newline = paragraph break
)
_TIMESTAMP_RE = re.compile(
    r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-–—]?\s*",
    re.MULTILINE,
)
_HEADER_RE = re.compile(r"^\[Канал:.*\]\s*$")


def needs_chunking(text: str) -> bool:
    """Check if text is long enough to require chunking."""
    body = _strip_header(text)
    return len(body) > CHUNK_THRESHOLD


def chunk_text(text: str) -> list[dict]:
    """
    Split text into chunks for separate LLM processing.

    Returns:
        List of dicts with:
        - index: chunk sequence number
        - text: chunk content
        - char_range: [start, end] in the body text
    """
    body = _strip_header(text)

    # Try timestamp-based splitting first
    timestamp_chunks = _split_by_timestamps(body)
    if timestamp_chunks and len(timestamp_chunks) >= 2:
        logger.debug(f"Split by timestamps into {len(timestamp_chunks)} chunks")
        return timestamp_chunks

    # Fallback: split by character count at paragraph boundaries
    return _split_by_size(body)


def _split_by_timestamps(body: str) -> list[dict]:
    """Split by timestamp markers if present."""
    matches = list(_TIMESTAMP_RE.finditer(body))
    if len(matches) < 2:
        return []

    chunks = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk_text = body[start:end].strip()

        if len(chunk_text) < 50:
            continue

        # If chunk is too large, sub-split it
        if len(chunk_text) > CHUNK_SIZE * 2:
            sub_chunks = _split_by_size(chunk_text, base_offset=start)
            for sc in sub_chunks:
                sc["index"] = len(chunks)
                chunks.append(sc)
        else:
            chunks.append({
                "index": len(chunks),
                "text": chunk_text,
                "char_range": [start, end],
            })

    return chunks if len(chunks) >= 2 else []


def _split_by_size(body: str, base_offset: int = 0) -> list[dict]:
    """Split text into ~CHUNK_SIZE chunks at paragraph boundaries."""
    paragraphs = _SECTION_BREAK_RE.split(body)
    chunks = []
    current_text = ""
    current_start = 0
    pos = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            pos += 2  # account for \n\n
            continue

        # Would adding this paragraph exceed target size?
        if current_text and len(current_text) + len(para) + 2 > CHUNK_SIZE:
            # Save current chunk
            chunks.append({
                "index": len(chunks),
                "text": current_text.strip(),
                "char_range": [base_offset + current_start, base_offset + pos],
            })
            # Start new chunk with overlap
            overlap_text = _get_overlap(current_text)
            current_text = overlap_text + "\n\n" + para if overlap_text else para
            current_start = pos - len(overlap_text) if overlap_text else pos
        else:
            if not current_text:
                current_start = pos
            current_text += ("\n\n" + para if current_text else para)

        pos += len(para) + 2  # +2 for the \n\n separator

    # Don't forget the last chunk
    if current_text.strip():
        chunks.append({
            "index": len(chunks),
            "text": current_text.strip(),
            "char_range": [base_offset + current_start, base_offset + pos],
        })

    # If we only got 1 chunk, force-split by character count
    if len(chunks) <= 1 and len(body) > CHUNK_THRESHOLD:
        return _force_split(body, base_offset)

    return chunks


def _force_split(body: str, base_offset: int = 0) -> list[dict]:
    """Force-split text by character count when paragraph splitting fails."""
    chunks = []
    pos = 0
    max_chunks = max(len(body) // (CHUNK_SIZE // 2), 10)  # Safety cap

    while pos < len(body) and len(chunks) < max_chunks:
        end = min(pos + CHUNK_SIZE, len(body))

        # Try to break at a sentence boundary
        if end < len(body):
            # Look for sentence end (.!?) in the last 20% of chunk
            search_start = pos + int(CHUNK_SIZE * 0.8)
            for i in range(end, search_start, -1):
                if body[i] in ".!?\n" and i + 1 < len(body) and body[i + 1] in " \n":
                    end = i + 1
                    break

        chunk_text = body[pos:end].strip()
        if chunk_text:
            chunks.append({
                "index": len(chunks),
                "text": chunk_text,
                "char_range": [base_offset + pos, base_offset + end],
            })

        # If we've reached the end, stop — no overlap needed
        if end >= len(body):
            break

        # Next chunk starts with overlap for context continuity
        pos = end - CHUNK_OVERLAP

    return chunks


def _get_overlap(text: str) -> str:
    """Get the last ~CHUNK_OVERLAP characters for context continuity."""
    if len(text) <= CHUNK_OVERLAP:
        return text
    # Try to start overlap at a sentence boundary
    candidate = text[-CHUNK_OVERLAP:]
    for i, c in enumerate(candidate):
        if c in ".!?\n":
            return candidate[i + 1:].strip()
    return candidate.strip()


def _strip_header(text: str) -> str:
    """Remove the metadata header line."""
    lines = text.split("\n")
    body_lines = [ln for ln in lines if not _HEADER_RE.match(ln.strip())]
    return "\n".join(body_lines).strip()
