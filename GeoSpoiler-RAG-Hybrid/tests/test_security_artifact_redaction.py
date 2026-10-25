import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from scripts.security_artifact_redaction import (
    apply_remediation,
    count_unredacted_text,
    redact_sensitive_text,
    scan,
)


def test_redact_sensitive_text_preserves_url_and_redacts_value() -> None:
    source = "see https://example.test/a?accessToken=secret&lang=ru now"
    result = redact_sensitive_text(source)
    assert result == "see https://example.test/a?accessToken=%5BREDACTED%5D&lang=ru now"
    assert count_unredacted_text(result) == 0


def test_apply_redacts_text_and_sqlite_and_preserves_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    text_path = root / "sample.json"
    text_path.write_text(
        json.dumps({"url": "https://example.test/x?accessToken=secret"}),
        encoding="utf-8",
    )
    db_path = root / "source_registry.sqlite"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE sources (source_id TEXT PRIMARY KEY, post_url TEXT)")
        conn.execute(
            "INSERT INTO sources VALUES (?, ?)",
            ("source-1", "https://example.test/x?accessToken=secret"),
        )
        conn.commit()

    findings = scan(root)
    assert len(findings) == 2
    assert {item.file_type for item in findings} == {"text", "sqlite"}

    quarantine = tmp_path / "quarantine"
    report_dir = root / "reports"
    report = apply_remediation(root, findings, quarantine, report_dir)
    assert report["status"] == "passed"
    assert report["files_before"] == 2
    assert report["files_after"] == 0
    assert scan(root) == []

    assert "secret" in (quarantine / "sample.json").read_text(encoding="utf-8")
    assert "secret" not in text_path.read_text(encoding="utf-8")
    with closing(sqlite3.connect(db_path)) as conn:
        value = conn.execute("SELECT post_url FROM sources").fetchone()[0]
        assert value == "https://example.test/x?accessToken=%5BREDACTED%5D"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(sqlite3.connect(quarantine / "source_registry.sqlite")) as conn:
        assert "secret" in conn.execute("SELECT post_url FROM sources").fetchone()[0]


def test_apply_requires_external_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    path = root / "sample.txt"
    path.write_text("https://example.test/x?accessToken=secret", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        apply_remediation(root, scan(root), root / "quarantine", root / "reports")
