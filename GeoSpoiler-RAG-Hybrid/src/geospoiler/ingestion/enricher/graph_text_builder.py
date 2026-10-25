"""
Graph Text Builder v2 — converts enriched_v2 cards into texts
for LightRAG ingestion and FTS search.

Two outputs per card:
  graph_text  — compact 3-10 sentence text for LightRAG graph extraction.
                Focuses on relationships between entities and events.
  search_text — dense flat text for BM25/FTS retrieval combining all
                card fields into a searchable representation.
"""

import re

from geospoiler.ingestion.enricher.validator import non_russian_prose_reason

# ── Public API ──────────────────────────────────────────────────────────────

def build_graph_text(card: dict) -> str:
    """
    Build a compact LightRAG-optimised graph text from a v2 enriched card.

    Structure: short relational sentences that help LightRAG extract
    entity-relationship triples. No dump of all fields.
    """
    parts = []

    prov = card.get("provenance", {})
    channel = prov.get("channel", "?")
    date = _format_date(prov.get("date", ""))
    source = prov.get("forwarded_from") or channel

    # Header for context
    parts.append(f"Документ из канала {channel} ({date}), источник: {source}.")

    # Summary as main relationship anchor
    summary = (card.get("summary") or "").strip()
    if summary:
        parts.append(summary)

    # Key entities as relationship nodes
    entity_tokens = _extract_entity_texts(card.get("entities", {}))
    if entity_tokens:
        parts.append(f"Упоминаются: {', '.join(entity_tokens[:15])}.")

    # Events as relationship edges
    events = card.get("events", [])
    for ev in events[:5]:
        if isinstance(ev, dict):
            desc = ev.get("description", "")
            actors = ev.get("actors", [])
            location = ev.get("location", "")
            if desc:
                actor_text = f" ({', '.join(actors)})" if actors else ""
                loc_text = f" в {location}" if location else ""
                parts.append(f"Событие{loc_text}{actor_text}: {desc}")

    # Theses as relationship context
    theses = card.get("theses", [])
    for th in theses[:3]:
        if isinstance(th, dict) and th.get("text"):
            speaker = th.get("speaker", "")
            speaker_prefix = f"{speaker}: " if speaker else ""
            parts.append(f"Позиция — {speaker_prefix}{th['text']}")

    graph_text = "\n".join(parts)
    # The graph contract is Russian-only prose.  Keep Russian wording that the
    # model used in summaries/theses, but remove foreign/Ukrainian verbatim
    # quotes from graph prose.  Search text and the quotes field retain the
    # exact evidence for retrieval.  Remove the surrounding quote pair with
    # the span so we never leave an empty «» artifact behind.
    for quote in card.get("quotes", []) or []:
        if not isinstance(quote, dict):
            continue
        quote_text = str(quote.get("text") or "").strip()
        if not quote_text or not _is_non_russian_quote(quote_text):
            continue
        for opener, closer in (("«", "»"), ("“", "”"), ('"', '"')):
            graph_text = graph_text.replace(f"{opener}{quote_text}{closer}", "")
        graph_text = graph_text.replace(quote_text, "")

    cleaned_lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in graph_text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()


def _is_non_russian_quote(text: str) -> bool:
    """Return whether a quote must stay out of Russian graph prose."""
    if non_russian_prose_reason(text) is not None:
        return True
    # Short foreign slogans (for example, ``Kill the Boer``) may not reach
    # the validator's length threshold but are still not Russian prose.
    has_latin = bool(re.search(r"[A-Za-z]", text))
    has_cyrillic = bool(re.search(r"[А-Яа-яЁёІіЇїЄє]", text))
    return has_latin and not has_cyrillic


def build_search_text(card: dict) -> str:
    """
    Build a dense FTS/search text combining all card fields.

    Designed for BM25/keyword matching — includes all searchable tokens.
    """
    parts = []

    prov = card.get("provenance", {})
    content_type = card.get("content_type", "")
    channel = prov.get("channel", "")
    date = _format_date(prov.get("date", ""))

    # Metadata line
    meta_parts = [p for p in [content_type, channel, date] if p]
    if meta_parts:
        parts.append(" ".join(meta_parts))

    # Summary
    summary = (card.get("summary") or "").strip()
    if summary:
        parts.append(summary)

    # Key points
    key_points = card.get("key_points", [])
    if key_points:
        kp_texts = []
        for kp in key_points:
            if isinstance(kp, dict):
                kp_texts.append(kp.get("text", ""))
            elif isinstance(kp, str):
                kp_texts.append(kp)
        kp_text = "; ".join(t for t in kp_texts if t)
        if kp_text:
            parts.append(f"Ключевые пункты: {kp_text}")

    # Entities
    entity_tokens = _extract_entity_texts(card.get("entities", {}))
    if entity_tokens:
        parts.append(f"Сущности: {', '.join(entity_tokens)}")

    # Topics
    topics = card.get("topics", [])
    if topics:
        topic_labels = []
        for t in topics:
            if isinstance(t, dict):
                topic_labels.append(t.get("label", ""))
            elif isinstance(t, str):
                topic_labels.append(t)
        topic_text = ", ".join(t for t in topic_labels if t)
        if topic_text:
            parts.append(f"Темы: {topic_text}")

    # Theses
    theses = card.get("theses", [])
    if theses:
        thesis_texts = []
        for th in theses:
            if isinstance(th, dict):
                thesis_texts.append(th.get("text", ""))
            elif isinstance(th, str):
                thesis_texts.append(th)
        th_text = "; ".join(t for t in thesis_texts if t)
        if th_text:
            parts.append(f"Тезисы: {th_text}")

    # Quotes (condensed)
    quotes = card.get("quotes", [])[:5]
    if quotes:
        q_parts = []
        for q in quotes:
            if isinstance(q, dict) and q.get("text"):
                speaker = q.get("speaker", "?")
                q_parts.append(f'{speaker}: «{q["text"]}»')
        if q_parts:
            parts.append("Цитаты: " + " | ".join(q_parts))

    # Events
    events = card.get("events", [])
    if events:
        ev_parts = []
        for ev in events:
            if isinstance(ev, dict):
                desc = ev.get("description", "")
                if desc:
                    ev_parts.append(desc)
        if ev_parts:
            parts.append("События: " + "; ".join(ev_parts))

    # Search phrases
    phrases = card.get("search_phrases", [])
    if phrases:
        phrase_texts = []
        for a in phrases:
            if isinstance(a, dict):
                phrase_texts.append(a.get("text", ""))
            elif isinstance(a, str):
                phrase_texts.append(a)
        phrase_text = " | ".join(t for t in phrase_texts if t)
        if phrase_text:
            parts.append(f"Поиск: {phrase_text}")

    # Source chain
    source_chain = card.get("source_chain", {})
    original = source_chain.get("original_source", "")
    if original:
        parts.append(f"Источник: {original}")

    return "\n".join(parts)


def populate_graph_texts(card: dict) -> dict:
    """Fill graph_text and search_text fields in a card. Returns the modified card."""
    card["graph_text"] = build_graph_text(card)
    card["search_text"] = build_search_text(card)
    return card


# ── Helpers ─────────────────────────────────────────────────────────────────

def _extract_entity_texts(entities: dict) -> list[str]:
    """Extract flat list of entity text strings from v2 structured entities."""
    tokens = []
    for category in ["people", "organizations", "countries", "locations",
                     "military_units", "equipment", "weapons",
                     "programs_projects", "media_sources", "other"]:
        for item in entities.get(category, []):
            if isinstance(item, dict):
                text = item.get("text", "")
            else:
                text = str(item)
            if text.strip():
                tokens.append(text.strip())
    return tokens


def _format_date(date_str: str) -> str:
    """Format ISO date to compact 'YYYY-MM-DD HH:MM' format."""
    if not date_str:
        return "?"
    try:
        return date_str[:16].replace("T", " ")
    except Exception:
        return date_str
