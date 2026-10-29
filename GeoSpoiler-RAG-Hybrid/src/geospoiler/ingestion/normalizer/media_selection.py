"""Local caption rules shared by Telegram capture and normalization."""

import re
import unicodedata
from collections.abc import Sequence

from geospoiler import config

# A keycap emoji is a digit plus presentation markers. Drop the whole sequence
# so that digit does not count as a caption.
_KEYCAP_RE = re.compile(r"[0-9#*]\ufe0f?\u20e3")
# Any "#" plus word characters, including tags that start with a digit or "_".
_HASHTAG_RE = re.compile(r"#\w+")
# Same http(s) and scheme-less www tokens as normalization.
_URL_TOKEN_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)


def media_operations(
    text: str,
    media_type: str,
    post_media_types: Sequence[str] | None = None,
) -> list[str]:
    """Select required operations; attachment presence alone is insufficient.

    ``post_media_types`` is every attachment of the post or album. When it is
    omitted, the attachment is judged on its own.
    """
    if config.MEDIA_PROCESSING_MODE == "all":
        return {"image": ["vision"], "video": ["vision", "transcribe"],
                "audio": ["transcribe"], "voice": ["transcribe"], "document": ["document_text"]}.get(media_type, [])
    types = (media_type,) if post_media_types is None else post_media_types
    # An image with no caption is the only source of the post's meaning.
    if media_type == "image" and _is_image_only_post(text, types):
        return ["vision"]
    caption = (text or "").casefold()
    image_reference = re.search(
        r"(?:на|в|із|з|on|in)\s+(?:(?:этом|этой|этих|цьому|цій|the|this)\s+)?"
        r"(?:скрин(?:шот)?(?:е|ах|у|а|ы|ов|ам|ами|ом)?|скрін(?:шот)?(?:і|ах|у|а|и|ів|ам|ами|ом)?|"
        r"изображени\w*|зображенн\w*|фото(?:графи(?:я|и|ю|й|ей|ях|ям|ями))?|"
        r"карт(?:а|е|у|ы|ой|ою|ах|ам|ами)|схем\w*|график\w*|screenshots?|images?|maps?)\b",
        caption,
    )
    recording_reference = re.search(
        r"(?:в|на|из|з|in|on|from)\s+(?:(?:этом|этой|the|this)\s+)?(?:видео|ролик\w*|запис\w*|аудио|голосов\w*|video|recording|audio)",
        caption,
    ) or re.search(r"(?:послушайте|прослушайте|слушайте|listen to|прослухайте)\s+(?:эту\s+)?(?:запис\w*|аудио|голосов\w*)", caption)
    if media_type == "image":
        return ["vision"] if image_reference else []
    if media_type in {"audio", "voice"}:
        return ["transcribe"] if recording_reference else []
    if media_type == "video" and recording_reference:
        operations = []
        if image_reference or re.search(r"видно|показан|показує|демонстр|карта|на экране|shows?|visible", caption):
            operations.append("vision")
        if re.search(r"сказ|говор|реч[ьи]|слов[ао]|послуш|озвуч|заявл|says?|speech|words?|listen", caption):
            operations.append("transcribe")
        return operations
    if media_type == "document" and re.search(r"(?:в|из|у|in|from)\s+(?:этом\s+|the\s+)?(?:документ\w*|файл\w*|таблиц\w*|document|file|pdf)", caption):
        return ["document_text"]
    return []


def _is_image_only_post(text: str, media_types: Sequence[str]) -> bool:
    """True when the post is only images and the caption has no letters or digits."""
    if not media_types or any(kind != "image" for kind in media_types):
        return False
    return not caption_has_letters_or_digits(text)


def strip_url_tokens(text: str) -> str:
    """Remove http(s) and scheme-less www URLs. Shared with normalization."""
    return _URL_TOKEN_RE.sub(" ", text or "")


def caption_has_letters_or_digits(text: str) -> bool:
    """Letters or digits left after URLs, hashtags and emoji are removed."""
    cleaned = strip_url_tokens(text)
    cleaned = _KEYCAP_RE.sub(" ", cleaned)
    cleaned = _HASHTAG_RE.sub(" ", cleaned)
    return any(_is_meaningful_char(ch) for ch in cleaned)


def _is_meaningful_char(ch: str) -> bool:
    """A caption letter or digit. Emoji (So/Sk) and letterlike emoji do not count."""
    if unicodedata.category(ch) in {"So", "Sk"} or "\u2100" <= ch <= "\u214F":
        return False
    return ch.isalpha() or ch.isdigit()
