"""Behavioral contracts for packaging, logical paths and the dormant Wiki."""

import importlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from geospoiler import config
from geospoiler.answering import card_context, composer
from geospoiler.cli import app as cli_app
from geospoiler.ingestion.enricher.youtube_pipeline import _source_fingerprint
from geospoiler.paths import project_root
from geospoiler.retrieval.lightrag.storage import _source_doc_id


@pytest.mark.parametrize("legacy,canonical", [
    ("config", "geospoiler.config"),
    ("loader.query", "geospoiler.answering.query"),
    ("loader.clients", "geospoiler.providers.clients"),
    ("retrieval.card_fts", "geospoiler.retrieval.cards.card_fts"),
    ("normalizer.media_paths", "geospoiler.ingestion.normalizer.media_paths"),
    ("normalizer.instagram_handler", "geospoiler.ingestion.normalizer.instagram.pipeline"),
    ("geospoiler.ingestion.normalizer.instagram_handler", "geospoiler.ingestion.normalizer.instagram.pipeline"),
    ("retrieval.wiki.schema", "geospoiler.wiki.schema"),
])
def test_compatibility_imports_share_module_state(legacy, canonical):
    assert importlib.import_module(legacy) is importlib.import_module(canonical)


@pytest.mark.parametrize("patch_path,call_path", [
    ("normalizer.instagram_handler", "geospoiler.ingestion.normalizer.instagram.pipeline"),
    ("geospoiler.ingestion.normalizer.instagram.pipeline", "normalizer.instagram_handler"),
    ("geospoiler.ingestion.normalizer.instagram_handler", "normalizer.instagram"),
    ("normalizer.instagram.pipeline", "geospoiler.ingestion.normalizer.instagram"),
])
def test_instagram_metadata_patches_reach_all_entrypoints(tmp_path, monkeypatch, patch_path, call_path):
    def unexpected_metadata_call(url):
        pytest.fail("Instagram entrypoint did not use the patched metadata helper")

    for module_path in (
        "geospoiler.ingestion.normalizer.instagram.pipeline",
        "geospoiler.ingestion.normalizer.instagram_handler",
    ):
        monkeypatch.setattr(importlib.import_module(module_path), "_get_info_ytdlp", unexpected_metadata_call)
    monkeypatch.setattr(config, "INSTAGRAM_DEEP_EXTRACT_ENABLED", False)
    monkeypatch.setattr(config, "INSTAGRAM_CACHE_DIR", tmp_path / "instagram-cache")
    monkeypatch.setattr(
        importlib.import_module(patch_path),
        "_get_info_ytdlp",
        lambda url: {"description": "Shared metadata", "uploader": "compat-test"},
    )

    result = importlib.import_module(call_path).extract_instagram_text("https://www.instagram.com/p/COMPAT123/")

    assert "Shared metadata" in result
    assert "@compat-test" in result


@pytest.mark.parametrize("master,hybrid", [(None, None), ("false", "true"), ("false", "false")])
def test_fresh_process_keeps_wiki_off_outside_checkout(tmp_path, master, hybrid):
    root = tmp_path / "проект с пробелами"
    root.mkdir()
    env = os.environ.copy()
    env["GEOSPOILER_PROJECT_ROOT"] = str(root)
    for key in ("WIKI_ENABLED", "HYBRID_QUERY_WIKI_ENABLED"):
        env.pop(key, None)
    if master is not None:
        env["WIKI_ENABLED"] = master
    if hybrid is not None:
        env["HYBRID_QUERY_WIKI_ENABLED"] = hybrid
    # Do not inherit an integration runner's working paths.
    for key in ("OUTPUT_DIR", "YOUTUBE_NORMALIZED_DIR", "YOUTUBE_SEGMENTS_DIR", "RAG_STORAGE_DIR",
                "CARD_FTS_DB_PATH", "SOURCE_REGISTRY_DB_PATH", "WIKI_STATE_DB_PATH", "WIKI_OUTPUT_DIR",
                "WIKI_SIDECAR_DIR", "STATE_DIR", "CODEX_RUNTIME_DIR", "MEDIA_CACHE_DIR", "LOG_DIR"):
        env.pop(key, None)
    code = """
import json
import config
from geospoiler import config as canonical
print(json.dumps({'same': config is canonical, 'root': str(config.PROJECT_ROOT),
                 'wiki': config.WIKI_ENABLED, 'hybrid': config.HYBRID_QUERY_WIKI_ENABLED,
                 'db': config.WIKI_STATE_DB_PATH.exists(), 'wiki_dir': config.WIKI_OUTPUT_DIR.exists()}))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    assert data == {"same": True, "root": str(root), "wiki": False, "hybrid": False,
                    "db": False, "wiki_dir": False}


def test_relative_explicit_project_root_is_rejected(monkeypatch):
    monkeypatch.setenv("GEOSPOILER_PROJECT_ROOT", "relative-directory")
    with pytest.raises(ValueError, match="absolute"):
        project_root()


def test_data_move_preserves_doc_id_fingerprint_and_readability(tmp_path, monkeypatch):
    root = tmp_path
    legacy = root / "output/normalized_youtube/channel/video.txt"
    current = root / "data/normalized_youtube/channel/video.txt"
    current.parent.mkdir(parents=True)
    current.write_text("source material", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.setattr(config, "DATA_LAYOUT_VERSION", 1)
    monkeypatch.setattr(config, "YOUTUBE_NORMALIZED_DIR", root / "data/normalized_youtube")
    before_id = _source_doc_id(str(legacy))
    before_fingerprint = _source_fingerprint({"text_path": str(legacy)}, "same transcript")
    (root / "data/.layout-v2.json").write_text(json.dumps({"version": 2, "identity_root": str(root)}))
    monkeypatch.setattr(config, "DATA_LAYOUT_VERSION", 2)
    assert config.resolve_project_path(legacy) == current
    assert config.resolve_project_path("output/normalized_youtube/channel/video.txt") == current
    assert config.resolve_project_path(legacy).read_text(encoding="utf-8") == "source material"
    assert _source_doc_id(str(current)) == before_id
    assert _source_fingerprint({"text_path": str(current)}, "same transcript") == before_fingerprint
    assert config.resolve_project_path(current) == current


def test_alias_resolution_does_not_hide_symlink_components(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_LAYOUT_VERSION", 2)
    current = tmp_path / "data/normalized"
    current.mkdir(parents=True)
    monkeypatch.setattr(config, "NORMALIZED_DIR", current)
    (tmp_path / "data/.layout-v2.json").write_text(json.dumps({"version": 2, "identity_root": str(tmp_path)}))
    # No actual symlink privilege is needed to assert that lexical components
    # remain available to the existing strict symlink validator.
    result = config.resolve_project_path("output/normalized/link/file.txt")
    assert result == current / "link/file.txt"


def test_no_network_socketpair_contract_survives_legacy_module_alias():
    from geospoiler.testing_support import no_network

    assert importlib.import_module("testing.no_network") is no_network


@pytest.mark.parametrize("existing", [False, True])
def test_wiki_off_commands_and_search_do_not_open_or_create_state(tmp_path, monkeypatch, existing):
    db = tmp_path / "wiki-state.sqlite"
    if existing:
        with sqlite3.connect(db) as connection:
            connection.execute("create table manual_decisions (value text)")
            connection.execute("insert into manual_decisions values ('preserve')")
    before = db.read_bytes() if existing else None
    monkeypatch.setattr(config, "WIKI_ENABLED", False)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_ENABLED", True)
    monkeypatch.setattr(config, "WIKI_STATE_DB_PATH", db)
    monkeypatch.setattr(config, "WIKI_OUTPUT_DIR", tmp_path / "wiki-projections")
    monkeypatch.setattr(config, "WIKI_SIDECAR_DIR", tmp_path / "wiki-sidecars")
    monkeypatch.setattr(cli_app, "setup_logging", lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Wiki-off route opened a database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert composer._search_wiki_hubs("query") == []
    assert card_context._wiki_context_for_query("query") is None
    for arguments in (["wiki", "run", "unused.json"], ["wiki", "rebuild"],
                      ["wiki", "search", "query"], ["wiki", "status"], ["wiki", "review"]):
        cli_app.main(arguments)
    assert (db.read_bytes() if db.exists() else None) == before
    assert not config.WIKI_OUTPUT_DIR.exists()
    assert not config.WIKI_SIDECAR_DIR.exists()
