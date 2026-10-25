"""
Reviewer Web UI — Streamlit-based interface for reviewing pending items.

Launch:
    streamlit run reviewer_app.py

Or automatically via:
    python main.py review --web
"""

import json
import logging
import re
from datetime import datetime
from html import escape as escape_html_attribute
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st

from geospoiler import config  # noqa: E402

# Add project root to path so config can be imported
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.ingestion.normalizer.review_queue import (
    REVIEW_TYPE_AI_CHAT,
    REVIEW_TYPE_EXTERNAL_LINK,
    REVIEW_TYPE_INSTAGRAM_LONG_REEL,
    REVIEW_TYPE_UNINFORMATIVE,
    mark_reviewed,
)
from geospoiler.ingestion.normalizer.translator import _deepseek_v4_options  # noqa: E402
from geospoiler.providers import llm_backend  # noqa: E402
from geospoiler.providers.llm_auth import auth_headers  # noqa: E402

if config.WIKI_ENABLED:
    from geospoiler.ui.wiki_reviewer import render_wiki_review  # noqa: E402
else:
    render_wiki_review = None

logger = logging.getLogger("geospoiler.reviewer")

# ──────────────────────── Page Config ────────────────────────

st.set_page_config(
    page_title="GeoSpoiler Reviewer",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ──────────────────────── Custom CSS ────────────────────────

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* Global */
    .stApp {
        font-family: 'Inter', sans-serif;
    }

    /* Header */
    .main-header {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        padding: 2rem 2.5rem;
        border-radius: 16px;
        margin-bottom: 2rem;
        color: white;
    }
    .main-header h1 {
        font-size: 2rem;
        font-weight: 700;
        margin: 0;
        color: white;
    }
    .main-header p {
        font-size: 0.95rem;
        color: rgba(255,255,255,0.7);
        margin: 0.5rem 0 0 0;
    }

    /* Stats bar */
    .stats-bar {
        display: flex;
        gap: 1rem;
        margin-bottom: 2rem;
    }
    .stat-card {
        background: linear-gradient(135deg, #1e1e3f, #2a2a5a);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 1rem 1.5rem;
        flex: 1;
        text-align: center;
    }
    .stat-card .number {
        font-size: 2rem;
        font-weight: 700;
        color: #e94560;
    }
    .stat-card .label {
        font-size: 0.8rem;
        color: rgba(255,255,255,0.5);
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    /* Review card */
    .review-card {
        background: linear-gradient(135deg, #1e1e3f, #252550);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 16px;
        padding: 1.5rem 2rem;
        margin-bottom: 1.5rem;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .review-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 32px rgba(0,0,0,0.3);
    }

    /* Type badge */
    .type-badge {
        display: inline-block;
        padding: 0.25rem 0.75rem;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .type-ai-chat {
        background: rgba(155, 89, 182, 0.2);
        color: #bb86fc;
        border: 1px solid rgba(155, 89, 182, 0.3);
    }
    .type-external-link {
        background: rgba(52, 152, 219, 0.2);
        color: #64b5f6;
        border: 1px solid rgba(52, 152, 219, 0.3);
    }
    .type-uninformative {
        background: rgba(241, 196, 15, 0.2);
        color: #ffd54f;
        border: 1px solid rgba(241, 196, 15, 0.3);
    }

    /* Channel info */
    .channel-info {
        color: rgba(255,255,255,0.5);
        font-size: 0.85rem;
        margin: 0.5rem 0;
    }

    /* Message text preview */
    .message-preview {
        background: rgba(0,0,0,0.2);
        border-left: 3px solid #e94560;
        padding: 0.75rem 1rem;
        border-radius: 0 8px 8px 0;
        margin: 0.75rem 0;
        font-size: 0.9rem;
        color: rgba(255,255,255,0.85);
        max-height: 200px;
        overflow-y: auto;
        white-space: pre-wrap;
    }

    /* URL display */
    .url-display {
        background: rgba(0,0,0,0.15);
        padding: 0.5rem 0.75rem;
        border-radius: 8px;
        font-family: monospace;
        font-size: 0.8rem;
        color: #64b5f6;
        word-break: break-all;
        margin: 0.5rem 0;
    }

    /* Reason text */
    .reason-text {
        color: rgba(255,255,255,0.4);
        font-size: 0.8rem;
        font-style: italic;
    }

    /* Empty state */
    .empty-state {
        text-align: center;
        padding: 4rem 2rem;
        color: rgba(255,255,255,0.4);
    }
    .empty-state .emoji {
        font-size: 4rem;
        margin-bottom: 1rem;
    }
    .empty-state h2 {
        font-size: 1.5rem;
        color: rgba(255,255,255,0.6);
        margin-bottom: 0.5rem;
    }

    /* Hide default Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}

    /* Button styling */
    .stButton > button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease;
    }
</style>
""", unsafe_allow_html=True)


# ──────────────────────── Helpers ────────────────────────

TYPE_BADGES = {
    REVIEW_TYPE_AI_CHAT: ("AI Диалог", "type-ai-chat"),
    REVIEW_TYPE_EXTERNAL_LINK: ("Внешняя ссылка", "type-external-link"),
    REVIEW_TYPE_INSTAGRAM_LONG_REEL: ("Длинный Reel", "type-external-link"),
    REVIEW_TYPE_UNINFORMATIVE: ("Малоинформативный", "type-uninformative"),
}

REASON_LABELS = {
    "YouTube link in post with text": "YouTube-ссылка в посте с текстом",
    "Instagram link in post with text": "Instagram/Reels-ссылка в посте с текстом",
    "External web link": "Внешняя web-ссылка",
    "Long Instagram Reel": "Длинный Instagram Reel требует ручной проверки",
}


def _get_type_badge_html(review_type: str) -> str:
    label, css_class = TYPE_BADGES.get(review_type, ("Ревью", "type-external-link"))
    return f'<span class="type-badge {css_class}">{label}</span>'


def _format_review_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    translated = REASON_LABELS.get(reason, reason)
    return f"Причина попадания в ревью: {translated}"


PROMPT_SOURCE_CHAR_LIMIT = 24000
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+|www\.[^\s<>\"]+", re.IGNORECASE)


def _source_kind_for_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return "message"

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower()

    if "youtu.be" in host or "youtube.com" in host:
        return "youtube"
    if "instagram.com" in host or "instagr.am" in host or "kkinstagram.com" in host:
        return "instagram"
    return "web"


def _parse_message_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_source_text_for_prompt(item: dict) -> tuple[str, str]:
    url = str(item.get("url") or "").strip()
    source_kind = _source_kind_for_url(url)

    if source_kind == "youtube":
        return _extract_youtube_source(url), source_kind
    if source_kind == "instagram":
        return _extract_instagram_source(item), source_kind
    if source_kind == "web":
        return _extract_web_source(url), source_kind
    return str(item.get("message_text") or ""), source_kind


def _resolve_prompt_extraction_request(
    item: dict,
    instruction: str,
) -> tuple[str, str, str]:
    source_url, clean_instruction = _extract_url_from_instruction(instruction)
    if source_url:
        source_item = dict(item)
        source_item["url"] = source_url
        source_text, source_label = _extract_source_text_for_prompt(source_item)
        return source_text, source_label, clean_instruction

    source_text, source_label = _extract_source_text_for_prompt(item)
    return source_text, source_label, str(instruction or "").strip()


def _extract_url_from_instruction(instruction: str) -> tuple[str | None, str]:
    instruction = str(instruction or "").strip()
    match = URL_PATTERN.search(instruction)
    if not match:
        return None, instruction

    url = match.group(0).rstrip(").,;!?]}'\"")
    clean_instruction = (
        instruction[: match.start()] + instruction[match.end() :]
    ).strip()
    return url, clean_instruction


def _remember_prompt_extraction_result(
    session_state,
    idx: int,
    *,
    extracted: str,
    extraction_source: str,
    clean_prompt: str,
) -> None:
    session_state[f"text_{idx}"] = extracted
    session_state[f"source_{idx}"] = extraction_source
    session_state[f"clean_prompt_{idx}"] = clean_prompt


def _prompt_for_save(session_state, idx: int, prompt_text: str) -> str:
    return str(
        session_state.get(f"clean_prompt_{idx}")
        or prompt_text
        or ""
    ).strip()


def _extract_youtube_source(url: str) -> str:
    from geospoiler.ingestion.normalizer.youtube_handler import extract_youtube_text

    return extract_youtube_text(url)


def _extract_instagram_source(item: dict) -> str:
    from geospoiler.ingestion.normalizer.instagram_handler import extract_instagram_text

    try:
        message_id = int(item.get("message_id") or 0)
    except (TypeError, ValueError):
        message_id = 0

    return extract_instagram_text(
        str(item.get("url") or ""),
        channel_name=str(item.get("channel") or "reviewer"),
        message_id=message_id,
        message_text=str(item.get("message_text") or ""),
        message_date=_parse_message_datetime(item.get("message_date")),
    )


def _extract_web_source(url: str) -> str:
    import asyncio

    from geospoiler.ingestion.normalizer.web_handler import extract_web_text

    return asyncio.run(extract_web_text(url))


def _build_prompted_extraction_messages(
    source_text: str,
    instruction: str,
    source_label: str,
) -> list[dict[str, str]]:
    source_text = _truncate_source_text(source_text)
    return [
        {
            "role": "system",
            "content": (
                "Ты помогаешь редактору GeoSpoiler извлечь из материала только то, "
                "что запросил пользователь. Не выдумывай факты, сохраняй имена, даты, "
                "места, числа и важные оговорки. Если нужной информации нет в материале, "
                "коротко напиши, что она не найдена. Верни только готовый текст на русском."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Источник: {source_label}\n\n"
                f"Инструкция пользователя:\n{instruction.strip()}\n\n"
                f"Материал:\n{source_text}"
            ),
        },
    ]


def _truncate_source_text(source_text: str) -> str:
    source_text = str(source_text or "").strip()
    if len(source_text) <= PROMPT_SOURCE_CHAR_LIMIT:
        return source_text
    return (
        source_text[:PROMPT_SOURCE_CHAR_LIMIT]
        + "\n\n[Материал был обрезан для лимита контекста.]"
    )


def _extract_with_prompt(source_text: str, instruction: str, source_label: str) -> str:
    source_text = str(source_text or "").strip()
    instruction = str(instruction or "").strip()
    if not source_text:
        raise RuntimeError("исходный материал пустой")
    if not instruction:
        raise RuntimeError("инструкция пустая")

    messages = _build_prompted_extraction_messages(
        source_text,
        instruction,
        source_label,
    )
    if llm_backend.is_luna_role("fallback_synth"):
        try:
            return llm_backend.complete_text_sync(
                messages,
                role="fallback_synth",
                timeout_seconds=config.FALLBACK_SYNTH_TIMEOUT_SECONDS,
            ).strip()
        except llm_backend.LLMBackendError as exc:
            if not config.CODEX_FALLBACK_TO_API:
                raise RuntimeError(f"Codex prompt extraction failed: {exc}") from exc
            logger.warning("Codex prompt extraction failed; explicit API fallback is enabled: %s", exc)

    api_key = config.FALLBACK_SYNTH_API_KEY
    if not api_key or api_key == "your-api-key-here":
        raise RuntimeError("не настроен ключ LLM для FALLBACK_SYNTH/QUERY/LLM")

    payload = {
        "model": config.FALLBACK_SYNTH_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": config.FALLBACK_SYNTH_MAX_TOKENS,
    }
    payload.update(
        _deepseek_v4_options(config.FALLBACK_SYNTH_MODEL, config.FALLBACK_SYNTH_BASE_URL)
    )

    try:
        response = requests.post(
            f"{config.FALLBACK_SYNTH_BASE_URL}/chat/completions",
            headers=auth_headers(api_key, config.FALLBACK_SYNTH_BASE_URL),
            json=payload,
            timeout=config.FALLBACK_SYNTH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.Timeout as exc:
        raise RuntimeError("LLM не ответила вовремя") from exc
    except Exception as exc:
        logger.exception("Prompt extraction failed")
        raise RuntimeError(f"не удалось применить промпт: {exc}") from exc


def _load_all_items() -> list[dict]:
    """Load all review queue items (all statuses)."""
    items = []
    for f in config.REVIEW_QUEUE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filepath"] = str(f)
            items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    # Sort: pending first, then by queued_at descending
    status_order = {"pending": 0, "processed": 1, "skipped": 2}
    items.sort(key=lambda x: (status_order.get(x.get("status", ""), 3), x.get("queued_at", "")))
    return items


def _save_uploaded_image(item: dict, uploaded_file) -> str | None:
    """Save uploaded image next to the normalized file."""
    channel = item.get("channel", "unknown")
    msg_id = item.get("message_id", 0)
    sanitized_channel = "".join(
        c if c.isalnum() or c in " _-" else "_" for c in channel
    ).strip()

    img_dir = config.NORMALIZED_DIR / sanitized_channel
    img_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(uploaded_file.name).suffix or ".jpg"
    img_path = img_dir / f"{msg_id}_review_img{ext}"
    img_path.write_bytes(uploaded_file.getvalue())
    return str(img_path)


# ──────────────────────── Main UI ────────────────────────

def main():
    if not config.WIKI_ENABLED:
        _render_content_review()
        return

    content_tab, wiki_tab = st.tabs(["Контент", "Wiki"])
    with content_tab:
        _render_content_review()
    with wiki_tab:
        assert render_wiki_review is not None
        render_wiki_review()


def _render_content_review():
    # Header
    st.markdown("""
    <div class="main-header">
        <h1>🔍 GeoSpoiler Reviewer</h1>
        <p>Просмотр и обработка элементов очереди ревью</p>
    </div>
    """, unsafe_allow_html=True)

    # Load items
    all_items = _load_all_items()
    pending = [i for i in all_items if i.get("status") == "pending"]
    processed = [i for i in all_items if i.get("status") == "processed"]
    skipped = [i for i in all_items if i.get("status") == "skipped"]

    # Stats bar
    st.markdown(f"""
    <div class="stats-bar">
        <div class="stat-card">
            <div class="number">{len(pending)}</div>
            <div class="label">Ожидают ревью</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(processed)}</div>
            <div class="label">Обработано</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(skipped)}</div>
            <div class="label">Пропущено</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(all_items)}</div>
            <div class="label">Всего</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Filter
    filter_options = [
        "Все ожидающие",
        "AI Диалоги",
        "Внешние ссылки",
        "Длинные Reels",
        "Малоинформативные",
        "Обработанные",
    ]
    selected_filter = st.selectbox("Фильтр:", filter_options, label_visibility="collapsed")

    if selected_filter == "Все ожидающие":
        display_items = pending
    elif selected_filter == "AI Диалоги":
        display_items = [i for i in pending if i.get("review_type") == REVIEW_TYPE_AI_CHAT]
    elif selected_filter == "Внешние ссылки":
        display_items = [i for i in pending if i.get("review_type") == REVIEW_TYPE_EXTERNAL_LINK]
    elif selected_filter == "Длинные Reels":
        display_items = [i for i in pending if i.get("review_type") == REVIEW_TYPE_INSTAGRAM_LONG_REEL]
    elif selected_filter == "Малоинформативные":
        display_items = [i for i in pending if i.get("review_type") == REVIEW_TYPE_UNINFORMATIVE]
    elif selected_filter == "Обработанные":
        display_items = processed + skipped
    else:
        display_items = pending

    # Empty state
    if not display_items:
        st.markdown("""
        <div class="empty-state">
            <div class="emoji">✅</div>
            <h2>Очередь пуста!</h2>
            <p>Нет элементов для ревью. Запустите пайплайн, чтобы появились новые элементы.</p>
        </div>
        """, unsafe_allow_html=True)
        return

    # Render items
    for idx, item in enumerate(display_items):
        _render_review_card(item, idx)


def _render_review_card(item: dict, idx: int):
    """Render a single review card with action buttons."""
    review_type = item.get("review_type", "unknown")
    status = item.get("status", "pending")
    channel = item.get("channel", "?")
    msg_id = item.get("message_id", "?")
    url = item.get("url", "")
    reason = item.get("reason", "")
    message_text = item.get("message_text", "")
    message_date = item.get("message_date", "")
    filepath = item.get("_filepath", "")

    # Card header
    badge_html = _get_type_badge_html(review_type)
    status_emoji = "⏳" if status == "pending" else ("✅" if status == "processed" else "⏭️")

    # Build optional sections
    url_html = ""
    if url:
        displayed_url = _escape_html(url)
        if urlparse(url).scheme.lower() in {"http", "https"}:
            href = escape_html_attribute(url, quote=True)
            displayed_url = f'<a href="{href}" target="_blank" rel="noopener noreferrer">{displayed_url}</a>'
        url_html = f'<div class="url-display">🔗 {displayed_url}</div>'

    reason_html = ""
    if reason:
        reason_html = f'<div class="reason-text">💡 {_escape_html(_format_review_reason(reason))}</div>'

    preview_html = ""
    if message_text:
        truncated = _escape_html(message_text[:500])
        if len(message_text) > 500:
            truncated += "..."
        preview_html = f'<div class="message-preview">{truncated}</div>'

    st.markdown(
        f'<div class="review-card">'
        f'{badge_html}'
        f'<span style="float: right; opacity: 0.5;">{status_emoji} {status}</span>'
        f'<div class="channel-info">'
        f'📌 {_escape_html(str(channel))} &nbsp;|&nbsp; 🆔 {_escape_html(str(msg_id))} '
        f'&nbsp;|&nbsp; 📅 {_escape_html(str(message_date or "?"))}'
        f'</div>'
        f'{url_html}'
        f'{reason_html}'
        f'{preview_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Actions (only for pending items)
    if status == "pending":
        col1, col2, col3 = st.columns([1, 1, 2])

        with col1:
            if st.button("⏭️ Пропустить", key=f"skip_{idx}", use_container_width=True):
                mark_reviewed(filepath, skip=True)
                st.rerun()

        with col2:
            if st.button("✅ Одобрить", key=f"approve_{idx}", use_container_width=True):
                if review_type == REVIEW_TYPE_UNINFORMATIVE and message_text.strip():
                    mark_reviewed(filepath, extracted_text=message_text.strip())
                    st.rerun()
                else:
                    st.warning("Для AI-диалога или внешней ссылки сначала добавьте извлечённый текст через редактирование.")

        with col3:
            if st.button("✏️ Редактировать", key=f"edit_{idx}", use_container_width=True):
                st.session_state[f"editing_{idx}"] = True

        # Edit mode
        if st.session_state.get(f"editing_{idx}", False):
            st.markdown("---")
            st.markdown("##### ✏️ Ваша версия текста")

            if url:
                if st.button("📥 Извлечь весь текст из источника", key=f"auto_{idx}"):
                    with st.spinner("Загрузка и разбор источника..."):
                        extracted, extraction_source = _extract_source_text_for_prompt(item)
                        st.session_state[f"text_{idx}"] = extracted
                        st.session_state[f"source_{idx}"] = extraction_source
                        st.rerun()

            prompt_text = st.text_area(
                "Инструкция для извлечения:",
                value=st.session_state.get(
                    f"prompt_{idx}",
                    item.get("extraction_prompt") or "",
                ),
                height=90,
                key=f"prompt_{idx}",
                help=(
                    "Например: достать из ролика только информацию про тему X, "
                    "без пересказа всего видео."
                ),
            )

            if st.button(
                "🎯 Извлечь по инструкции",
                key=f"prompt_extract_{idx}",
                use_container_width=True,
            ):
                if not prompt_text.strip():
                    st.warning("Сначала напишите инструкцию для извлечения.")
                else:
                    with st.spinner("Извлекаю источник и применяю инструкцию..."):
                        try:
                            source_text, extraction_source, clean_prompt = (
                                _resolve_prompt_extraction_request(item, prompt_text)
                            )
                            if not source_text.strip():
                                st.warning("Не удалось получить исходный материал для извлечения.")
                            elif not clean_prompt.strip():
                                st.warning("После ссылки не осталось инструкции для извлечения.")
                            else:
                                extracted = _extract_with_prompt(
                                    source_text,
                                    clean_prompt,
                                    extraction_source,
                                )
                                _remember_prompt_extraction_result(
                                    st.session_state,
                                    idx,
                                    extracted=extracted,
                                    extraction_source=extraction_source,
                                    clean_prompt=clean_prompt,
                                )
                                st.rerun()
                        except Exception as exc:
                            st.error(f"Не удалось извлечь по инструкции: {exc}")

            edited_text = st.text_area(
                "Текст:",
                value=st.session_state.get(f"text_{idx}", message_text or ""),
                height=200,
                key=f"text_{idx}",
                label_visibility="collapsed",
            )

            uploaded_image = st.file_uploader(
                "📎 Прикрепить изображение (необязательно):",
                type=["jpg", "jpeg", "png", "webp", "gif"],
                key=f"img_{idx}",
            )

            ecol1, ecol2 = st.columns(2)
            with ecol1:
                if st.button("💾 Сохранить", key=f"save_{idx}", use_container_width=True, type="primary"):
                    img_path = None
                    if uploaded_image:
                        img_path = _save_uploaded_image(item, uploaded_image)

                    mark_reviewed(
                        filepath,
                        extracted_text=edited_text.strip() if edited_text.strip() else None,
                        skip=not edited_text.strip(),
                        attached_image=img_path,
                        extraction_prompt=_prompt_for_save(
                            st.session_state,
                            idx,
                            prompt_text,
                        ) or None,
                        extraction_source=st.session_state.get(
                            f"source_{idx}",
                            item.get("extraction_source"),
                        ),
                    )
                    st.session_state[f"editing_{idx}"] = False
                    st.rerun()

            with ecol2:
                if st.button("❌ Отмена", key=f"cancel_{idx}", use_container_width=True):
                    st.session_state[f"editing_{idx}"] = False
                    st.rerun()

        st.markdown("")  # spacer


def _escape_html(text: str) -> str:
    """Basic HTML escaping for safe display."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "<br>")
    )


if __name__ == "__main__":
    main()
