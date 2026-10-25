"""
Response Formatter — formats SearchPackage results into a clean markdown document.
"""

from geospoiler.answering.composer import SearchPackage

_CARDS_ONLY_MODES = {"shadow", "cards", "cards-only"}


def format_search_results(package: SearchPackage) -> str:
    """Render a SearchPackage into a readable markdown report."""

    parts = []

    # 1. Header
    parts.append(f"# Итоги поиска: «{package.query}»")
    parts.append(f"**Режим поиска:** {package.mode}\n")

    # 2. Graph/card synthesis note
    if package.mode in _CARDS_ONLY_MODES:
        parts.append("## Поиск по карточкам")
    else:
        parts.append("## Синтез графа (LightRAG)")
    if package.llm_answer:
        parts.append(package.llm_answer.strip())
    else:
        parts.append("*Нет прямого ответа от графа.*")
    parts.append("\n---\n")

    # 3. Primary Sources
    parts.append("## Основные материалы (Прямые совпадения)")
    if package.primary_results:
        for i, res in enumerate(package.primary_results, start=1):
            parts.append(f"### {i}. {res.title}")
            parts.append(f"- **Ссылка:** {res.url}")
            parts.append(f"- **Причина:** {res.relevance_reason}")
            if res.snippets:
                parts.append("- **Фрагменты:**")
                for snip in res.snippets:
                    parts.append(f"  > {snip}")
            if res.segment_hits:
                parts.append("- **Совпавшие фрагменты YouTube:**")
                for hit in res.segment_hits:
                    timing = _format_segment_time(hit.start_seconds, hit.end_seconds)
                    link = f"[{timing}]({hit.url})" if hit.url else timing
                    parts.append(f"  - Сегмент {hit.segment_index + 1}, {link}: {hit.snippet}")
            parts.append("")
    else:
        parts.append("*Прямых совпадений не найдено.*\n")

    # 4. Secondary Sources
    if package.secondary_results:
        parts.append("## Дополнительные материалы (BM25 / Keyword)")
        for i, res in enumerate(package.secondary_results, start=1):
            parts.append(f"### {i}. {res.title}")
            parts.append(f"- **Ссылка:** {res.url}")
            parts.append(f"- **Причина:** {res.relevance_reason}")
            if res.snippets:
                parts.append("- **Фрагменты:**")
                for snip in res.snippets:
                    # truncate long snippets
                    if len(snip) > 150:
                        snip = snip[:150] + "..."
                    parts.append(f"  > {snip}")
            if res.segment_hits:
                parts.append("- **Совпавшие фрагменты YouTube:**")
                for hit in res.segment_hits:
                    timing = _format_segment_time(hit.start_seconds, hit.end_seconds)
                    link = f"[{timing}]({hit.url})" if hit.url else timing
                    parts.append(f"  - Сегмент {hit.segment_index + 1}, {link}: {hit.snippet}")
            parts.append("")

    return "\n".join(parts)


def _format_segment_time(start: float | None, end: float | None) -> str:
    if start is None:
        return "таймкод недоступен"
    return f"{_format_seconds(start)}–{_format_seconds(end)}" if end is not None else _format_seconds(start)


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "?"
    seconds = max(0, int(value))
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{remainder:02d}" if hours else f"{minutes}:{remainder:02d}"
