"""
GeoSpoiler-RAG Configuration
Loads settings from .env file. All API providers use OpenAI-compatible format
so you can swap between OpenAI, Nvidia NIM, Together AI, etc. by changing URL and key.
"""

import json
import os
import re

from dotenv import load_dotenv

# Load .env from project root
from geospoiler.paths import logical_project_path as logical_project_path
from geospoiler.paths import persisted_identity_path as persisted_identity_path
from geospoiler.paths import project_root
from geospoiler.paths import resolve_project_path as resolve_project_path

PROJECT_ROOT = project_root()
load_dotenv(PROJECT_ROOT / ".env")
DATA_LAYOUT_VERSION = int(os.getenv("GEOSPOILER_DATA_LAYOUT", "1"))


# ───────────────────────── Telegram ─────────────────────────
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
TELEGRAM_FOLDER = os.getenv("TELEGRAM_FOLDER", "GeoSpoiler")  # Telegram folder name to read from

# ───────────────────────── LLM ─────────────────────────
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_AUTH_PROVIDER = os.getenv("LLM_AUTH_PROVIDER", "").strip().lower()
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_MAX_ASYNC = int(os.getenv("LLM_MAX_ASYNC", "1"))
LLM_DELAY_SECONDS = float(os.getenv("LLM_DELAY_SECONDS", "2.0"))

# Text-LLM backend profile. ``current`` preserves the existing HTTP providers;
# ``luna`` routes only text-generation roles through the authenticated Codex CLI.
LLM_PROFILE = os.getenv("LLM_PROFILE", "luna").strip().lower() or "luna"
if LLM_PROFILE not in {"current", "luna"}:
    raise ValueError("LLM_PROFILE must be either 'current' or 'luna'")
CODEX_CLI_PATH = os.getenv("CODEX_CLI_PATH", "codex").strip()
CODEX_LUNA_MODEL = os.getenv("CODEX_LUNA_MODEL", "gpt-6-luna").strip()
CODEX_LUNA_REASONING_EFFORT = (
    os.getenv("CODEX_LUNA_REASONING_EFFORT", "xhigh").strip().lower() or "xhigh"
)
CODEX_LLM_TIMEOUT_SECONDS = float(os.getenv("CODEX_LLM_TIMEOUT_SECONDS", "300"))
CODEX_LLM_MAX_CONCURRENCY = max(1, int(os.getenv("CODEX_LLM_MAX_CONCURRENCY", "3")))
CODEX_FALLBACK_TO_API = os.getenv("CODEX_FALLBACK_TO_API", "false").lower() == "true"
REGENERATE_ON_PROFILE_CHANGE = (
    os.getenv("REGENERATE_ON_PROFILE_CHANGE", "false").lower() == "true"
)
RAG_BUILD_DELAY_SECONDS = float(os.getenv("RAG_BUILD_DELAY_SECONDS", str(LLM_DELAY_SECONDS)))
QUERY_DELAY_SECONDS = float(os.getenv("QUERY_DELAY_SECONDS", "0"))
RAG_INSERT_TIMEOUT_SECONDS = float(os.getenv("RAG_INSERT_TIMEOUT_SECONDS", "600"))
RAG_DELETE_TIMEOUT_SECONDS = float(os.getenv("RAG_DELETE_TIMEOUT_SECONDS", "120"))
QUERY_TIMEOUT_SECONDS = float(os.getenv("QUERY_TIMEOUT_SECONDS", "240"))
FALLBACK_SYNTH_TIMEOUT_SECONDS = float(os.getenv("FALLBACK_SYNTH_TIMEOUT_SECONDS", "45"))
RAG_FINALIZE_TIMEOUT_SECONDS = float(os.getenv("RAG_FINALIZE_TIMEOUT_SECONDS", "30"))
QUERY_MAX_TOKENS = int(os.getenv("QUERY_MAX_TOKENS", "1200"))
FALLBACK_SYNTH_MAX_TOKENS = int(os.getenv("FALLBACK_SYNTH_MAX_TOKENS", "4096"))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
HYBRID_QUERY_CARDS_ENABLED = os.getenv("HYBRID_QUERY_CARDS_ENABLED", "true").lower() == "true"
HYBRID_SYNTH_ENABLED = os.getenv("HYBRID_SYNTH_ENABLED", "true").lower() == "true"
HYBRID_QUERY_CARDS_TOP_K = int(os.getenv("HYBRID_QUERY_CARDS_TOP_K", "3"))
WIKI_ENABLED = os.getenv("WIKI_ENABLED", "false").lower() == "true"
HYBRID_QUERY_WIKI_ENABLED = (
    WIKI_ENABLED
    and os.getenv("HYBRID_QUERY_WIKI_ENABLED", "true").lower() == "true"
)
HYBRID_QUERY_WIKI_TOP_K = max(
    1,
    int(os.getenv("HYBRID_QUERY_WIKI_TOP_K", "2")),
)
AUTO_OPEN_REVIEWER_AFTER_RUN = os.getenv("AUTO_OPEN_REVIEWER_AFTER_RUN", "true").lower() == "true"

# Minimal Late-Fusion RAG V1. The checked-in template enables the accepted path;
# a missing flag remains fail-closed. All limits are operator-configurable.
LATE_FUSION_ENABLED = os.getenv("LATE_FUSION_ENABLED", "false").lower() == "true"
LATE_FUSION_CARD_TOP_K = int(os.getenv("LATE_FUSION_CARD_TOP_K", "30"))
LATE_FUSION_YOUTUBE_TOP_K = int(os.getenv("LATE_FUSION_YOUTUBE_TOP_K", "15"))
LATE_FUSION_MAX_SOURCES = int(os.getenv("LATE_FUSION_MAX_SOURCES", "20"))
LATE_FUSION_MAX_INPUT_TOKENS = int(os.getenv("LATE_FUSION_MAX_INPUT_TOKENS", "120000"))
LATE_FUSION_RUNTIME_CONTEXT_LIMIT = int(os.getenv("LATE_FUSION_RUNTIME_CONTEXT_LIMIT", "128000"))
LATE_FUSION_FTS_TIMEOUT_SECONDS = float(
    os.getenv("LATE_FUSION_FTS_TIMEOUT_SECONDS", str(QUERY_TIMEOUT_SECONDS))
)
LATE_FUSION_OUTPUT_TOKEN_RESERVE = 8192

if LATE_FUSION_CARD_TOP_K < 1:
    raise ValueError("LATE_FUSION_CARD_TOP_K must be >= 1")
if LATE_FUSION_YOUTUBE_TOP_K < 1:
    raise ValueError("LATE_FUSION_YOUTUBE_TOP_K must be >= 1")
if LATE_FUSION_MAX_SOURCES < 7:
    raise ValueError("LATE_FUSION_MAX_SOURCES must be >= 7 (5 Card FTS + 2 YouTube reserves)")
if LATE_FUSION_MAX_INPUT_TOKENS < 1:
    raise ValueError("LATE_FUSION_MAX_INPUT_TOKENS must be >= 1")
if LATE_FUSION_RUNTIME_CONTEXT_LIMIT <= LATE_FUSION_OUTPUT_TOKEN_RESERVE:
    raise ValueError("LATE_FUSION_RUNTIME_CONTEXT_LIMIT must exceed the output token reserve")
if LATE_FUSION_FTS_TIMEOUT_SECONDS <= 0:
    raise ValueError("LATE_FUSION_FTS_TIMEOUT_SECONDS must be > 0")

# Role-specific chat models. Each role falls back to the main LLM_* settings.
RAG_BUILD_API_KEY = os.getenv("RAG_BUILD_API_KEY", "") or LLM_API_KEY
RAG_BUILD_BASE_URL = os.getenv("RAG_BUILD_BASE_URL", "") or LLM_BASE_URL
RAG_BUILD_MODEL = os.getenv("RAG_BUILD_MODEL", "") or LLM_MODEL

QUERY_API_KEY = os.getenv("QUERY_API_KEY", "") or LLM_API_KEY
QUERY_BASE_URL = os.getenv("QUERY_BASE_URL", "") or LLM_BASE_URL
QUERY_MODEL = os.getenv("QUERY_MODEL", "") or LLM_MODEL

FALLBACK_SYNTH_API_KEY = os.getenv("FALLBACK_SYNTH_API_KEY", "") or QUERY_API_KEY
FALLBACK_SYNTH_BASE_URL = os.getenv("FALLBACK_SYNTH_BASE_URL", "") or QUERY_BASE_URL
FALLBACK_SYNTH_MODEL = os.getenv("FALLBACK_SYNTH_MODEL", "") or QUERY_MODEL

TRANSLATION_API_KEY = os.getenv("TRANSLATION_API_KEY", "") or LLM_API_KEY
TRANSLATION_BASE_URL = os.getenv("TRANSLATION_BASE_URL", "") or LLM_BASE_URL
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL", "") or LLM_MODEL

TRANSCRIPTION_ENABLED = os.getenv("TRANSCRIPTION_ENABLED", "false").lower() == "true"
TRANSCRIPTION_API_KEY = os.getenv("TRANSCRIPTION_API_KEY", "") or LLM_API_KEY
TRANSCRIPTION_BASE_URL = os.getenv("TRANSCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "google/gemini-2.5-flash-lite")
TRANSCRIPTION_STT_MODEL = os.getenv("TRANSCRIPTION_STT_MODEL", "openai/whisper-large-v3")
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "").strip()
TRANSCRIPTION_TIMEOUT_SECONDS = float(os.getenv("TRANSCRIPTION_TIMEOUT_SECONDS", "120"))

# ───────────────────────── Embedding ─────────────────────────
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "4096"))
EMBEDDING_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "120"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
EMBEDDING_MAX_ATTEMPTS = int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "6"))
EMBEDDING_CONCURRENCY = int(os.getenv("EMBEDDING_CONCURRENCY", "1"))

# ───────────────────────── Vision ─────────────────────────
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://openrouter.ai/api/v1")
VISION_MODEL = os.getenv("VISION_MODEL", "google/gemini-2.5-flash-lite")
MEDIA_VISION_BACKEND = os.getenv("MEDIA_VISION_BACKEND", "luna").strip().lower() or "luna"
MEDIA_VISION_TIMEOUT_SECONDS = float(
    os.getenv("MEDIA_VISION_TIMEOUT_SECONDS", str(CODEX_LLM_TIMEOUT_SECONDS))
)
MEDIA_VISION_MAX_IMAGES = int(os.getenv("MEDIA_VISION_MAX_IMAGES", "6"))
MEDIA_VISION_MAX_FRAMES = int(os.getenv("MEDIA_VISION_MAX_FRAMES", "12"))
MEDIA_VISION_FRAME_INTERVAL_SEC = float(
    os.getenv("MEDIA_VISION_FRAME_INTERVAL_SEC", "2.0")
)
MEDIA_VISION_FRAME_BATCH_SIZE = int(os.getenv("MEDIA_VISION_FRAME_BATCH_SIZE", str(MEDIA_VISION_MAX_IMAGES)))
MEDIA_VISION_MAX_FILE_MB = int(os.getenv("MEDIA_VISION_MAX_FILE_MB", "20"))
MEDIA_VISION_PROMPT_VERSION = os.getenv("MEDIA_VISION_PROMPT_VERSION", "media-vision-v1")
MEDIA_VISION_FALLBACK_ENABLED = (
    os.getenv("MEDIA_VISION_FALLBACK_ENABLED", "false").lower() == "true"
)
if MEDIA_VISION_BACKEND not in {"luna", "current"}:
    raise ValueError("MEDIA_VISION_BACKEND must be either 'luna' or 'current'")
if MEDIA_VISION_TIMEOUT_SECONDS <= 0:
    raise ValueError("MEDIA_VISION_TIMEOUT_SECONDS must be positive")
if MEDIA_VISION_FRAME_INTERVAL_SEC <= 0:
    raise ValueError("MEDIA_VISION_FRAME_INTERVAL_SEC must be positive")
if MEDIA_VISION_FRAME_BATCH_SIZE > MEDIA_VISION_MAX_IMAGES:
    raise ValueError("MEDIA_VISION_FRAME_BATCH_SIZE cannot exceed MEDIA_VISION_MAX_IMAGES")
if MEDIA_VISION_MAX_IMAGES <= 0:
    raise ValueError("MEDIA_VISION_MAX_IMAGES must be positive")
if MEDIA_VISION_MAX_FRAMES <= 0:
    raise ValueError("MEDIA_VISION_MAX_FRAMES must be positive")
if MEDIA_VISION_FRAME_BATCH_SIZE <= 0:
    raise ValueError("MEDIA_VISION_FRAME_BATCH_SIZE must be positive")
if MEDIA_VISION_MAX_FILE_MB <= 0:
    raise ValueError("MEDIA_VISION_MAX_FILE_MB must be positive")

# ───────────────────────── Instagram Deep Extract ─────────────────────────
INSTAGRAM_DEEP_EXTRACT_ENABLED = os.getenv("INSTAGRAM_DEEP_EXTRACT_ENABLED", "false").lower() == "true"
INSTAGRAM_VISION_API_KEY = os.getenv("INSTAGRAM_VISION_API_KEY", "") or VISION_API_KEY or LLM_API_KEY
INSTAGRAM_VISION_BASE_URL = os.getenv("INSTAGRAM_VISION_BASE_URL", "") or VISION_BASE_URL or LLM_BASE_URL
INSTAGRAM_VISION_MODEL = os.getenv("INSTAGRAM_VISION_MODEL", "") or VISION_MODEL
INSTAGRAM_FRAME_INTERVAL_SEC = float(os.getenv("INSTAGRAM_FRAME_INTERVAL_SEC", "2.0"))
INSTAGRAM_FRAME_BATCH_SIZE = int(os.getenv("INSTAGRAM_FRAME_BATCH_SIZE", "5"))
INSTAGRAM_MAX_VIDEO_DURATION_SEC = int(os.getenv("INSTAGRAM_MAX_VIDEO_DURATION_SEC", "180"))
INSTAGRAM_MAX_VIDEO_SIZE_MB = int(os.getenv("INSTAGRAM_MAX_VIDEO_SIZE_MB", "100"))
if INSTAGRAM_FRAME_INTERVAL_SEC <= 0:
    raise ValueError("INSTAGRAM_FRAME_INTERVAL_SEC must be positive")
if INSTAGRAM_FRAME_BATCH_SIZE <= 0:
    raise ValueError("INSTAGRAM_FRAME_BATCH_SIZE must be positive")
if INSTAGRAM_MAX_VIDEO_DURATION_SEC <= 0:
    raise ValueError("INSTAGRAM_MAX_VIDEO_DURATION_SEC must be positive")
if INSTAGRAM_MAX_VIDEO_SIZE_MB <= 0:
    raise ValueError("INSTAGRAM_MAX_VIDEO_SIZE_MB must be positive")
YTDLP_MIN_VERSION = os.getenv("YTDLP_MIN_VERSION", "2026.7.4")
YTDLP_VERSION_CHECK_TIMEOUT_SECONDS = float(
    os.getenv("YTDLP_VERSION_CHECK_TIMEOUT_SECONDS", "10")
)
if YTDLP_VERSION_CHECK_TIMEOUT_SECONDS <= 0:
    raise ValueError("YTDLP_VERSION_CHECK_TIMEOUT_SECONDS must be positive")
YTDLP_PREFLIGHT_TTL_SECONDS = max(
    0.0, float(os.getenv("YTDLP_PREFLIGHT_TTL_SECONDS", "300"))
)
YTDLP_REQUIRE_CURRENT = os.getenv("YTDLP_REQUIRE_CURRENT", "false").lower() == "true"

# ───────────────────────── Reranker ─────────────────────────
# Set RERANKER_ENABLED=true to activate the OpenRouter rerank route.
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_PROVIDER = os.getenv("RERANKER_PROVIDER", "openrouter")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "voyageai/rerank-2.5")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://openrouter.ai/api/v1")
RERANKER_TOP_N = int(os.getenv("RERANKER_TOP_N", "10"))           # Final passages shown to LLM
RERANKER_CANDIDATE_POOL = int(os.getenv("RERANKER_CANDIDATE_POOL", "50"))  # Candidate pool before reranking

# ───────────────────────── Paths ─────────────────────────
OUTPUT_DIR = PROJECT_ROOT / os.getenv("OUTPUT_DIR", "./output")
NORMALIZED_DIR = OUTPUT_DIR / "normalized"
ENRICHED_DIR = OUTPUT_DIR / "enriched"
YOUTUBE_NORMALIZED_DIR = PROJECT_ROOT / os.getenv("YOUTUBE_NORMALIZED_DIR", "./output/normalized_youtube")
YOUTUBE_SEGMENTS_DIR = PROJECT_ROOT / os.getenv("YOUTUBE_SEGMENTS_DIR", "./output/enriched_segments")
REVIEW_QUEUE_DIR = OUTPUT_DIR / "review_queue"
TRANSCRIPTION_DIR = OUTPUT_DIR / "transcripts"
MEDIA_VISUAL_DIR = OUTPUT_DIR / "visuals"
INSTAGRAM_CACHE_DIR = PROJECT_ROOT / os.getenv("INSTAGRAM_CACHE_DIR", str(OUTPUT_DIR / "instagram_cache"))
RAG_STORAGE_DIR = PROJECT_ROOT / os.getenv("RAG_STORAGE_DIR", "./rag_storage")
CARD_FTS_DB_PATH = PROJECT_ROOT / os.getenv("CARD_FTS_DB_PATH", "./artifacts/card_fts.sqlite")
SOURCE_REGISTRY_DB_PATH = PROJECT_ROOT / os.getenv("SOURCE_REGISTRY_DB_PATH", "./artifacts/source_registry.sqlite")
WIKI_STATE_DB_PATH = PROJECT_ROOT / os.getenv(
    "WIKI_STATE_DB_PATH", "./artifacts/wiki_state.sqlite"
)
WIKI_OUTPUT_DIR = PROJECT_ROOT / os.getenv("WIKI_OUTPUT_DIR", "./output/wiki")
WIKI_SIDECAR_DIR = PROJECT_ROOT / os.getenv(
    "WIKI_SIDECAR_DIR", "./wiki_sidecars"
)
STATE_DIR = PROJECT_ROOT / os.getenv("STATE_DIR", "./state")
CODEX_RUNTIME_DIR = PROJECT_ROOT / os.getenv("CODEX_RUNTIME_DIR", "./state/codex_runtime")
YOUTUBE_CHECKPOINT_DIR = STATE_DIR / "youtube_checkpoints"
MEDIA_CACHE_DIR = PROJECT_ROOT / os.getenv("MEDIA_CACHE_DIR", "./media_cache")
MEDIA_PROCESSING_MODE = os.getenv("MEDIA_PROCESSING_MODE", "selective").strip().lower()
if MEDIA_PROCESSING_MODE not in {"selective", "all"}:
    raise ValueError("MEDIA_PROCESSING_MODE must be selective or all")
LOG_DIR = PROJECT_ROOT / os.getenv("LOG_DIR", "./logs")
MEDIA_CAPTURE_ENABLED = os.getenv("MEDIA_CAPTURE_ENABLED", "true").lower() == "true"
MEDIA_CAPTURE_MAX_BYTES = int(os.getenv("MEDIA_CAPTURE_MAX_BYTES", "0"))

# Ensure all directories exist
for d in [
    NORMALIZED_DIR,
    ENRICHED_DIR,
    YOUTUBE_NORMALIZED_DIR,
    YOUTUBE_SEGMENTS_DIR,
    REVIEW_QUEUE_DIR,
    TRANSCRIPTION_DIR,
    MEDIA_VISUAL_DIR,
    INSTAGRAM_CACHE_DIR,
    RAG_STORAGE_DIR,
    CARD_FTS_DB_PATH.parent,
    SOURCE_REGISTRY_DB_PATH.parent,
    STATE_DIR,
    CODEX_RUNTIME_DIR,
    YOUTUBE_CHECKPOINT_DIR,
    MEDIA_CACHE_DIR,
    LOG_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)

if WIKI_ENABLED:
    for d in [WIKI_STATE_DB_PATH.parent, WIKI_OUTPUT_DIR, WIKI_SIDECAR_DIR]:
        d.mkdir(parents=True, exist_ok=True)

# ───────────────────────── Enrichment ─────────────────────────
# Optional separate model for enrichment (defaults to main LLM_MODEL).
# Use a more capable model here if budget allows (e.g. Claude Opus for reasoning).
ENRICHMENT_API_KEY = os.getenv("ENRICHMENT_API_KEY", "") or LLM_API_KEY
ENRICHMENT_BASE_URL = os.getenv("ENRICHMENT_BASE_URL", "") or LLM_BASE_URL
ENRICHMENT_MODEL = os.getenv("ENRICHMENT_MODEL", "") or LLM_MODEL
ENRICHMENT_SCHEMA_VERSION = "enriched_v2"
ENRICHMENT_PROMPT_VERSION = "enriched_prompt_v2"
YOUTUBE_ENRICHMENT_PROMPT_VERSION = os.getenv(
    "YOUTUBE_ENRICHMENT_PROMPT_VERSION", "youtube_episode_prompt_v2"
)
YOUTUBE_SEGMENTATION_VERSION = "youtube_segmenter_v2"
YOUTUBE_SEGMENT_SEARCH_TOP_K = int(os.getenv("YOUTUBE_SEGMENT_SEARCH_TOP_K", "200"))
YOUTUBE_SEGMENT_HITS_PER_EPISODE = int(os.getenv("YOUTUBE_SEGMENT_HITS_PER_EPISODE", "10"))
YOUTUBE_MERGE_MAX_CHARS = int(os.getenv("YOUTUBE_MERGE_MAX_CHARS", "48000"))
ENRICHMENT_CONCURRENCY = int(os.getenv("ENRICHMENT_CONCURRENCY", "3"))

# ───────────────────────── LightRAG ─────────────────────────
LIGHTRAG_ENTITY_TYPES = [
    "person",           # Политики, военные, журналисты
    "organization",     # НАТО, ООН, ЧВК Вагнер, СБУ, Reuters
    "country",          # Россия, Украина, США, Венгрия
    "military_unit",    # 47-я ОМБр, 1-я танковая армия, батальон Азов
    "event",            # Выборы в США, Курская операция, Саммит мира
    "location",         # Авдеевка, Закарпатье, Сувалкский коридор
    "conflict",         # Российско-украинская война, Конфликт в Газе
    "document",         # Законы, договора, соглашения, санкционные пакеты
                        # (Будапештский меморандум, Минские соглашения,
                        #  Закон о мобилизации, 13-й пакет санкций ЕС)
    "other",            # Fallback for real entities outside the core ontology
]

LIGHTRAG_LANGUAGE = "English"  # Граф строится на английском
RELATION_EXTRACTION_MODE = os.getenv("RELATION_EXTRACTION_MODE", "interpretive").strip().lower()

LIGHTRAG_ENTITY_TYPE_REMAP = {
    "concept": "other",
    "group": "organization",
    "platform": "organization",
    "website": "organization",
    "technology": "other",
    "equipment": "other",
    "artifact": "other",
    "content": "document",
    "data": "document",
    "method": "document",
    "policy": "document",
    "product": "other",
    "category": "other",
    "unknown": "other",
}

_default_aliases = {
    "сша": "United States",
    "usa": "United States",
    "united states": "United States",
    "украина": "Ukraine",
    "ukraine": "Ukraine",
    "россия": "Russia",
    "росія": "Russia",
    "russia": "Russia",
    "германия": "Germany",
    "germany": "Germany",
    "адг": "AfD",
    "afd": "AfD",
    "трамп": "Donald Trump",
    "donald trump": "Donald Trump",
    "дональд трамп": "Donald Trump",
    "ес": "European Union",
    "єс": "European Union",
    "eu": "European Union",
    "european union": "European Union",
    "кремль": "Kremlin",
}

LIGHTRAG_ENTITY_ALIASES = _default_aliases.copy()
_alias_json = os.getenv("LIGHTRAG_ENTITY_ALIASES_JSON", "").strip()
if _alias_json:
    try:
        LIGHTRAG_ENTITY_ALIASES.update(json.loads(_alias_json))
    except json.JSONDecodeError:
        pass

# ───────────────────────── URL Patterns ─────────────────────────
YOUTUBE_PATTERN = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+',
    re.IGNORECASE,
)
INSTAGRAM_PATTERN = re.compile(
    r'^(?:https?://)?(?:www\.)?instagram\.com/(?:reel|p)/[\w-]+/?(?:[?#].*)?$',
    re.IGNORECASE,
)
AI_CHAT_PATTERNS = [
    re.compile(r'(?:https?://)?(?:chat\.openai\.com|chatgpt\.com)/(?:share|c)/[\w-]+', re.IGNORECASE),
    re.compile(r'(?:https?://)?claude\.ai/(?:chat|share)/[\w-]+', re.IGNORECASE),
    re.compile(r'(?:https?://)?gemini\.google\.com/(?:app|share)/[\w-]+', re.IGNORECASE),
]
WEB_URL_PATTERN = re.compile(
    r'https?://[^\s<>"\']+',
    re.IGNORECASE,
)
