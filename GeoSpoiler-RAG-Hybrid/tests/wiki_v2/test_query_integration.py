from __future__ import annotations

import config
import loader.card_context as card_context
import retrieval.composer as composer
import retrieval.wiki.schema as wiki_schema
from loader.card_context import _card_context_for_query, _wiki_context_for_query
from retrieval.wiki.projections import rebuild_all_projections
from tests.wiki_v2.test_projections import _prepare_approved_wiki


def test_approved_hub_is_available_to_hybrid_query_context(
    wiki_db,
    tmp_path,
    monkeypatch,
) -> None:
    _prepare_approved_wiki(wiki_db)
    rebuild_all_projections(wiki_db)
    database_path = tmp_path / "wiki.sqlite"
    wiki_db.execute("VACUUM INTO ?", (str(database_path),))

    monkeypatch.setattr(config, "WIKI_STATE_DB_PATH", database_path)
    monkeypatch.setattr(config, "WIKI_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_TOP_K", 2)
    monkeypatch.setattr(config, "HYBRID_QUERY_CARDS_ENABLED", False)

    wiki_context = _wiki_context_for_query("КНР ракета")
    assert wiki_context is not None
    assert wiki_context["references"]
    assert "Китай испытал новую ракету" in wiki_context["shadow_context"][0]["facts"][0]

    combined = _card_context_for_query("КНР ракета", "answer")
    assert combined == wiki_context


def test_empty_card_fallback_does_not_hide_approved_wiki_context(
    wiki_db,
    tmp_path,
    monkeypatch,
) -> None:
    _prepare_approved_wiki(wiki_db)
    rebuild_all_projections(wiki_db)
    database_path = tmp_path / "wiki.sqlite"
    wiki_db.execute("VACUUM INTO ?", (str(database_path),))

    monkeypatch.setattr(config, "WIKI_STATE_DB_PATH", database_path)
    monkeypatch.setattr(config, "WIKI_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_TOP_K", 2)
    monkeypatch.setattr(config, "HYBRID_QUERY_CARDS_ENABLED", True)
    monkeypatch.setattr(
        card_context,
        "_shadow_fallback_result",
        lambda *_args, **_kwargs: {"data": {"shadow_context": []}},
    )

    wiki_context = _wiki_context_for_query("КНР ракета")
    assert wiki_context is not None
    assert _card_context_for_query("КНР ракета", "answer") == wiki_context


def test_master_switch_prevents_all_query_time_wiki_database_access(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "WIKI_ENABLED", False)
    monkeypatch.setattr(config, "HYBRID_QUERY_WIKI_ENABLED", True)
    monkeypatch.setattr(
        wiki_schema,
        "connect_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Wiki database must not be opened")
        ),
    )

    assert _wiki_context_for_query("КНР ракета") is None
    assert composer._search_wiki_hubs("КНР ракета") == []
