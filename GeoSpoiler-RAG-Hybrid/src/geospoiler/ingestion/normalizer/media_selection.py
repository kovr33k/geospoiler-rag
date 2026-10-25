"""Local caption rules shared by Telegram capture and normalization."""

import re

from geospoiler import config


def media_operations(text: str, media_type: str) -> list[str]:
    """Select required operations; attachment presence alone is insufficient."""
    if config.MEDIA_PROCESSING_MODE == "all":
        return {"image": ["vision"], "video": ["vision", "transcribe"],
                "audio": ["transcribe"], "voice": ["transcribe"], "document": ["document_text"]}.get(media_type, [])
    caption = (text or "").casefold()
    image_reference = re.search(
        r"(?:на|в|із|з|on|in)\s+(?:(?:этом|этой|этих|цьому|цій|the|this)\s+)?"
        r"(?:скрин\w*|скрін\w*|изображени\w*|зображенн\w*|фото\w*|карт[аеу]\w*|схем\w*|график\w*|screenshot\w*|image\w*|map\w*)",
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
