import json
from pathlib import Path

import pytest

from geospoiler.operations import migrate_layout as migration


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "output/normalized/topic").mkdir(parents=True)
    (tmp_path / "output/normalized/topic/1.txt").write_bytes(b"source")
    (tmp_path / ".env").write_text("CUSTOM_VALUE=retained\nWIKI_ENABLED=false\n", encoding="utf-8")
    return tmp_path


def test_stage_activate_and_rollback_preserve_content_and_settings(workspace):
    original = (workspace / ".env").read_bytes()
    assert migration.stage(workspace)["phase"] == "staged"
    assert migration.stage(workspace)["phase"] == "staged"
    assert migration.activate(workspace)["phase"] == "active"
    assert migration.activate(workspace)["phase"] == "active"
    assert not (workspace / "output/normalized").exists()
    assert (workspace / "data/normalized/topic/1.txt").read_bytes() == b"source"
    assert "CUSTOM_VALUE=retained" in (workspace / ".env").read_text()
    assert "HYBRID_QUERY_WIKI_ENABLED=false" in (workspace / ".env").read_text()
    assert migration.rollback(workspace)["phase"] == "rolled_back"
    assert (workspace / ".env").read_bytes() == original
    assert (workspace / "output/normalized/topic/1.txt").read_bytes() == b"source"


@pytest.mark.parametrize("changed", ["existing", "new"])
def test_rollback_refuses_to_discard_new_writes(workspace, changed):
    migration.stage(workspace)
    migration.activate(workspace)
    target = workspace / "data/normalized/topic" / ("1.txt" if changed == "existing" else "2.txt")
    target.write_bytes(b"new material")
    with pytest.raises(ValueError, match="changed|mismatch"):
        migration.rollback(workspace)
    assert target.read_bytes() == b"new material"


def test_rollback_refuses_to_overwrite_new_env_settings(workspace):
    migration.stage(workspace)
    migration.activate(workspace)
    active = (workspace / ".env").read_text(encoding="utf-8")
    (workspace / ".env").write_text(active + "NEW_SETTING=preserve-me\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"\.env changed"):
        migration.rollback(workspace)

    assert "NEW_SETTING=preserve-me" in (workspace / ".env").read_text(encoding="utf-8")


def test_rollback_accepts_legacy_active_journal_without_env_hash(workspace):
    original = (workspace / ".env").read_bytes()
    migration.stage(workspace)
    migration.activate(workspace)
    journal_path = workspace / migration.JOURNAL
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.pop("active_env_sha256")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    assert migration.rollback(workspace)["phase"] == "rolled_back"
    assert (workspace / ".env").read_bytes() == original


def test_source_change_before_activation_is_detected(workspace):
    migration.stage(workspace)
    (workspace / "output/normalized/topic/1.txt").write_bytes(b"changed source")
    with pytest.raises(ValueError, match="mismatch"):
        migration.activate(workspace)
    assert "GEOSPOILER_DATA_LAYOUT" not in (workspace / ".env").read_text()


def test_target_conflict_never_overwrites_files(workspace):
    target = workspace / "data/normalized/topic/1.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unrelated")
    with pytest.raises(ValueError, match="Conflicting"):
        migration.stage(workspace)
    assert target.read_bytes() == b"unrelated"


def test_interrupted_rename_can_be_rolled_back(workspace, monkeypatch):
    migration.stage(workspace)
    original = Path.rename

    def interrupted(source, target):
        original(source, target)
        raise OSError("simulated interruption after rename")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rename", interrupted)
        with pytest.raises(OSError):
            migration.activate(workspace)
    assert migration.rollback(workspace)["phase"] == "rolled_back"
    assert (workspace / "output/normalized/topic/1.txt").is_file()


def test_live_sqlite_sidecar_blocks_snapshot(workspace):
    (workspace / "artifacts").mkdir()
    (workspace / "artifacts/card_fts.sqlite").write_bytes(b"database")
    (workspace / "artifacts/card_fts.sqlite-wal").write_bytes(b"pending changes")
    with pytest.raises(ValueError, match="SQLite"):
        migration.plan(workspace)


def test_journal_cannot_escape_workspace(workspace):
    migration.stage(workspace)
    path = workspace / migration.JOURNAL
    journal = json.loads(path.read_text())
    journal["files"][0]["new"] = "../outside.txt"
    path.write_text(json.dumps(journal))
    with pytest.raises(ValueError):
        migration.activate(workspace)
