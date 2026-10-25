"""Local text extraction for caption-selected documents."""

from pathlib import Path

from pypdf import PdfReader

from geospoiler.ingestion.normalizer.media_paths import resolve_media_path


def extract_document_text(file_path: str, mime_type: str) -> str:
    path = resolve_media_path(file_path)
    if mime_type == "application/pdf" or path.suffix.lower() == ".pdf":
        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    elif mime_type.startswith("text/") or Path(path).suffix.lower() in {".txt", ".md", ".csv", ".json"}:
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError(f"unsupported_document_type:{mime_type}")
    if not text.strip():
        raise ValueError("document_has_no_extractable_text; image-only documents require review")
    return text.strip()
