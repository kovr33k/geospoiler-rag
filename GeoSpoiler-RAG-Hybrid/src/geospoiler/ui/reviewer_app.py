"""
Reviewer Web UI — Streamlit-based interface for reviewing pending items.

Launch:
    streamlit run reviewer_app.py

Or automatically via:
    python main.py review --web
"""

import difflib
import json
import logging
import re
import unicodedata
from datetime import datetime
from html import escape as escape_html_attribute
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st

from geospoiler import config  # noqa: E402

# Add project root to path so config can be imported
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.ingestion.normalizer.media_selection import strip_url_tokens
from geospoiler.ingestion.normalizer.review_queue import (
    REVIEW_TYPE_AI_CHAT,
    REVIEW_TYPE_EXTERNAL_LINK,
    REVIEW_TYPE_INSTAGRAM_LONG_REEL,
    REVIEW_TYPE_UNINFORMATIVE,
    mark_reviewed,
    reopen_review,
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
# Failure strings the downloaders return instead of source text. One list so
# save, approve, and extraction all reject the same markers.
_EXTRACTION_FAILURE_MARKERS = (
    "Содержание не удалось извлечь",
    "— ошибка обработки",
    "[YouTube: не удалось",
    "Отправлено в очередь на ручной просмотр",
    "Уже обработано:",
)
_BRACKET_HEADER_LINE = re.compile(r"^\[[^\]\n]*\]$")
# Automatic downloads only. A short note typed by hand is still allowed.
_MIN_AUTOMATIC_DOWNLOAD_LETTERS = 150
_LOGIN_WALL_LETTER_LIMIT = 600
# Ordinary news verbs are not a login control. Applied to casefolded text.
_LOGIN_WALL_RE = re.compile(r"\b(?:log in|sign in|sign up)\b|загружается\.\.\.")
# Author, title, and URL lines from youtube_handler._format_youtube_header.
_YOUTUBE_META_LINE = re.compile(r"^(?:Автор|Название|URL):")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _review_record_key(item: dict) -> str:
    """Stable per-record key. A list index moves when the filter or queue changes."""
    return Path(item["_filepath"]).name


_DRAFT_KEY_PREFIXES = ("text_", "prompt_", "source_", "clean_prompt_", "editing_")


def _clear_review_draft(session_state, record_key: str) -> None:
    """Drop one record's draft. Call before rerun, after the action succeeded."""
    for prefix in _DRAFT_KEY_PREFIXES:
        session_state.pop(f"{prefix}{record_key}", None)


def _collapse_whitespace(text: str) -> str:
    return " ".join(str(text or "").split())


def _failure_marker(text: str) -> str | None:
    for marker in _EXTRACTION_FAILURE_MARKERS:
        if marker in text:
            return marker
    return None


def _is_bracket_header_only(text: str) -> bool:
    """True when every non-empty line is a bracketed header such as ``[Веб: url]``."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(_BRACKET_HEADER_LINE.fullmatch(line) for line in lines)


def _substantive_download_text(text: str) -> str:
    """Text for copy checks and download quality.

    A line that is only ``[...]`` is an extractor header or a status line.
    YouTube also emits unbracketed author, title, and URL lines. Links and
    format characters are removed as well.
    """
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if _BRACKET_HEADER_LINE.fullmatch(stripped) or _YOUTUBE_META_LINE.match(stripped):
            continue
        lines.append(line)
    body = "\n".join(lines)
    body = _MARKDOWN_IMAGE_RE.sub(" ", body)
    body = _MARKDOWN_LINK_RE.sub(r"\1", body)
    body = strip_url_tokens(body)
    return "".join(char for char in body if unicodedata.category(char) != "Cf")


def _comparison_text(text: str) -> str:
    return _collapse_whitespace(_substantive_download_text(text)).casefold()


def _near_copy_of_post(text: str, message_text: str) -> bool:
    """True when the candidate is the post text or a trivial edit of it.

    Headers and status lines are removed first. An empty post does not reject
    anything: there is nothing to copy. A longer extraction that only quotes
    the post stays. autojunk is off: frequent letters are real anchors.
    """
    post = _comparison_text(message_text)
    if not post:
        return False
    candidate = _comparison_text(text)
    if not candidate:
        return False
    if candidate in post:
        return True
    return difflib.SequenceMatcher(None, candidate, post, autojunk=False).ratio() >= 0.9


def download_quality_error(text: str, url: str) -> str | None:
    """Reject a thin automatic download. Hand-typed text does not use this."""
    substantive = _substantive_download_text(text)
    letters = sum(char.isalpha() for char in substantive)
    folded = substantive.casefold()
    login_wall = _LOGIN_WALL_RE.search(folded) is not None
    if letters < _MIN_AUTOMATIC_DOWNLOAD_LETTERS or (
        letters < _LOGIN_WALL_LETTER_LIMIT and login_wall
    ):
        return (
            "Страница почти без текста — вероятно, нужен вход "
            f"или страница грузится скриптом: {url}"
        )
    return None


def validate_review_text(text: str, message_text: str = "") -> str | None:
    """Return a Russian error when the text must not be saved, else None."""
    stripped = str(text or "").strip()
    if not stripped:
        return "Текст пустой. Нажмите «Пропустить», если извлекать нечего."
    message = str(message_text or "")
    if _near_copy_of_post(stripped, message):
        return (
            "Нельзя сохранить текст самого поста. Извлеките текст источника "
            "или нажмите «Пропустить»."
        )
    if not any(char.isalpha() or char.isdigit() for char in strip_url_tokens(stripped)):
        return (
            "После удаления ссылок в тексте не осталось букв или цифр. "
            "Вставьте текст источника или нажмите «Пропустить»."
        )
    if _failure_marker(stripped) or _is_bracket_header_only(stripped):
        return (
            "Это служебное сообщение об ошибке загрузки, а не текст источника. "
            "Исправьте текст или нажмите «Пропустить»."
        )
    return None


def _reject_failed_extraction(text: str, url: str) -> str:
    """Raise when a downloader returned a failure marker instead of source text."""
    if _failure_marker(text) or _is_bracket_header_only(str(text or "").strip()):
        raise RuntimeError(
            f"Не удалось извлечь текст из источника {url}. "
            "Загрузка вернула сообщение об ошибке."
        )
    return text


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

    try:
        if source_kind == "youtube":
            text = _extract_youtube_source(url)
        elif source_kind == "instagram":
            text = _extract_instagram_source(item)
        elif source_kind == "web":
            text = _extract_web_source(url)
        else:
            return str(item.get("message_text") or ""), source_kind
    except Exception as exc:
        raise RuntimeError(
            f"Не удалось извлечь текст из источника {url}. {exc}"
        ) from exc
    text = _reject_failed_extraction(text, url)
    error = download_quality_error(text, url)
    if error:
        raise RuntimeError(error)
    return text, source_kind


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
    record_key: str,
    *,
    extracted: str,
    extraction_source: str,
    clean_prompt: str,
) -> None:
    session_state[f"text_{record_key}"] = extracted
    session_state[f"source_{record_key}"] = extraction_source
    session_state[f"clean_prompt_{record_key}"] = clean_prompt


def _prompt_for_save(session_state, record_key: str, prompt_text: str) -> str:
    return str(
        session_state.get(f"clean_prompt_{record_key}")
        or prompt_text
        or ""
    ).strip()


def _extract_youtube_source(url: str) -> str:
    from geospoiler.ingestion.normalizer.youtube_handler import extract_youtube_text

    return extract_youtube_text(url)


def _extract_instagram_source(item: dict) -> str:
    from geospoiler.ingestion.normalizer.instagram_handler import extract_instagram_text

    url = str(item.get("url") or "")
    try:
        message_id = int(item.get("message_id") or 0)
    except (TypeError, ValueError):
        message_id = 0

    return extract_instagram_text(
        url,
        channel_name=str(item.get("channel") or "reviewer"),
        message_id=message_id,
        message_text=str(item.get("message_text") or ""),
        message_date=_parse_message_datetime(item.get("message_date")),
        queue_failures=False,
    )


def _extract_web_source(url: str) -> str:
    import asyncio

    from geospoiler.ingestion.normalizer.web_handler import extract_web_text

    return asyncio.run(extract_web_text(url))


def _approve_review_item(item: dict) -> str | None:
    """Download a source and save it. Return a Russian error, or None on success."""
    url = str(item.get("url") or "").strip()
    if not url:
        return (
            "У записи нет ссылки на источник. Введите текст через «Редактировать» "
            "или нажмите «Пропустить»."
        )
    try:
        extracted, extraction_source = _extract_source_text_for_prompt(item)
    except Exception as exc:
        return str(exc)
    error = validate_review_text(extracted, str(item.get("message_text") or ""))
    if error:
        return error
    mark_reviewed(
        str(item.get("_filepath") or ""),
        extracted_text=extracted.strip(),
        extraction_source=extraction_source,
    )
    return None


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
    for item in display_items:
        _render_review_card(item)


def _render_review_card(item: dict):
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
    record_key = _review_record_key(item)

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

    extracted_html = ""
    if status == "processed":
        extracted = str(item.get("extracted_text") or "").strip()
        if extracted:
            truncated_extracted = _escape_html(extracted[:500])
            if len(extracted) > 500:
                truncated_extracted += "..."
            extracted_html = (
                '<div class="message-preview"><strong>Извлечённый текст:</strong><br>'
                f"{truncated_extracted}</div>"
            )

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
        f'{extracted_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if status != "pending":
        if st.button(
            "↩️ Вернуть в ожидание",
            key=f"reopen_{record_key}",
            use_container_width=True,
        ):
            reopen_review(filepath)
            _clear_review_draft(st.session_state, record_key)
            st.rerun()
        return

    # Actions for pending items
    col1, col2, col3 = st.columns([1, 1, 2])

    with col1:
        if st.button("⏭️ Пропустить", key=f"skip_{record_key}", use_container_width=True):
            mark_reviewed(filepath, skip=True)
            _clear_review_draft(st.session_state, record_key)
            st.rerun()

    with col2:
        if st.button("✅ Одобрить", key=f"approve_{record_key}", use_container_width=True):
            with st.spinner("Загрузка и разбор источника..."):
                error = _approve_review_item(item)
            if error:
                st.error(error)
            else:
                _clear_review_draft(st.session_state, record_key)
                st.rerun()

    with col3:
        if st.button("✏️ Редактировать", key=f"edit_{record_key}", use_container_width=True):
            st.session_state[f"editing_{record_key}"] = True

    # Edit mode. The text area starts empty: the post text is not an extraction.
    if st.session_state.get(f"editing_{record_key}", False):
        st.markdown("---")
        st.markdown("##### ✏️ Ваша версия текста")

        previous = str(item.get("previous_extracted_text") or "").strip()
        if previous:
            with st.expander("Предыдущий извлечённый текст"):
                st.text(previous)

        if url:
            if st.button("📥 Извлечь весь текст из источника", key=f"auto_{record_key}"):
                with st.spinner("Загрузка и разбор источника..."):
                    try:
                        extracted, extraction_source = _extract_source_text_for_prompt(item)
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        error = validate_review_text(extracted, message_text)
                        if error:
                            st.error(error)
                        else:
                            st.session_state[f"text_{record_key}"] = extracted
                            st.session_state[f"source_{record_key}"] = extraction_source
                            st.rerun()

        prompt_text = st.text_area(
            "Инструкция для извлечения:",
            value=st.session_state.get(
                f"prompt_{record_key}",
                item.get("extraction_prompt") or "",
            ),
            height=90,
            key=f"prompt_{record_key}",
            help=(
                "Например: достать из ролика только информацию про тему X, "
                "без пересказа всего видео."
            ),
        )

        if st.button(
            "🎯 Извлечь по инструкции",
            key=f"prompt_extract_{record_key}",
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
                            error = validate_review_text(extracted, message_text)
                            if error:
                                st.error(error)
                            else:
                                _remember_prompt_extraction_result(
                                    st.session_state,
                                    record_key,
                                    extracted=extracted,
                                    extraction_source=extraction_source,
                                    clean_prompt=clean_prompt,
                                )
                                st.rerun()
                    except Exception as exc:
                        st.error(f"Не удалось извлечь по инструкции: {exc}")

        edited_text = st.text_area(
            "Текст:",
            value=st.session_state.get(f"text_{record_key}", ""),
            height=200,
            key=f"text_{record_key}",
            label_visibility="collapsed",
        )

        uploaded_image = st.file_uploader(
            "📎 Прикрепить изображение (необязательно):",
            type=["jpg", "jpeg", "png", "webp", "gif"],
            key=f"img_{record_key}",
        )

        ecol1, ecol2 = st.columns(2)
        with ecol1:
            if st.button("💾 Сохранить", key=f"save_{record_key}", use_container_width=True, type="primary"):
                error = validate_review_text(edited_text, message_text)
                if error:
                    st.error(error)
                else:
                    img_path = None
                    if uploaded_image:
                        img_path = _save_uploaded_image(item, uploaded_image)

                    extraction_source = st.session_state.get(
                        f"source_{record_key}",
                        item.get("extraction_source"),
                    )
                    extraction_prompt = _prompt_for_save(
                        st.session_state,
                        record_key,
                        prompt_text,
                    ) or None
                    mark_reviewed(
                        filepath,
                        extracted_text=edited_text.strip(),
                        attached_image=img_path,
                        extraction_prompt=extraction_prompt,
                        extraction_source=extraction_source,
                    )
                    _clear_review_draft(st.session_state, record_key)
                    st.rerun()

        with ecol2:
            if st.button("❌ Отмена", key=f"cancel_{record_key}", use_container_width=True):
                _clear_review_draft(st.session_state, record_key)
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
