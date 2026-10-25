import json
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_validation import scan_enriched_cards, validate_enriched_card, write_enriched_validation_report  # noqa: E402
from models import EnrichedCardV2, NormalizedMeta  # noqa: E402


def _minimal_v2_card(point_type: str = "reported_statement") -> dict:
    return {
        "schema_version": "enriched_v2",
        "prompt_version": "enriched_prompt_v1",
        "enrichment_model": "test-model",
        "enriched_at": "2026-05-30T00:00:00+00:00",
        "provenance": {
            "source_id": "telegram:3328128766:148",
            "source_type": "telegram_post",
            "channel": "Hungary",
            "date": "2026-04-10T16:41:09+00:00",
            "post_url": "https://t.me/c/3328128766/148",
            "message_id": 148,
            "normalized_path": "output/normalized/Hungary/148.txt",
        },
        "content_type": "telegram_post",
        "language": "ru",
        "summary": "Дональд Трамп поддержал Виктора Орбана.",
        "key_points": [
            {
                "text": "Дональд Трамп поддержал Виктора Орбана перед выборами 2026 года в Венгрии.",
                "type": point_type,
                "importance": "high",
                "evidence": None,
            }
        ],
        "entities": {
            "people": [
                {"text": "Donald Trump", "role": "politician", "salience": "primary"},
                {"text": "Viktor Orban", "role": "politician", "salience": "primary"},
            ],
            "organizations": [],
            "countries": [],
            "locations": [],
            "military_units": [],
            "equipment": [],
            "weapons": [],
            "programs_projects": [],
            "media_sources": [],
            "other": [],
        },
        "topics": [{"label": "Trump-Orban поддержка", "salience": "primary", "type": "diplomatic_topic"}],
        "theses": [],
        "quotes": [],
        "events": [],
        "search_phrases": [{"text": "Trump Orban поддержка", "source": "constructed_from_present_terms"}],
        "source_chain": {
            "original_source": "Hungary",
            "forwarded_from": None,
            "mentioned_sources": [],
            "external_links": [],
        },
        "graph_text": "Дональд Трамп поддержал Виктора Орбана.",
        "search_text": "Дональд Трамп поддержал Виктора Орбана. Donald Trump Viktor Orban.",
        "ignored_blocks": [],
        "quality_flags": [],
    }


class DataContractTests(unittest.TestCase):
    def test_enriched_card_v2_parses(self):
        card = EnrichedCardV2.model_validate(_minimal_v2_card())

        self.assertEqual(card.source_id.value, "telegram:3328128766:148")
        self.assertEqual(card.key_points[0].type, "reported_statement")
        self.assertEqual(card.content_type, "telegram_post")
        self.assertEqual(card.extraction_issues, [])
        self.assertNotIn("chunks", card.model_dump())

        with_chunks = _minimal_v2_card()
        with_chunks["chunks"] = []
        with self.assertRaises(ValidationError):
            EnrichedCardV2.model_validate(with_chunks)

    def test_normalized_meta_contract_derives_source_id(self):
        meta = NormalizedMeta.model_validate(
            {
                "channel_name": "Hungary",
                "channel_id": 3328128766,
                "message_id": 148,
                "post_url": "https://t.me/c/3328128766/148",
                "has_text": True,
                "youtube_urls": None,
            }
        )

        self.assertEqual(meta.source_id.value, "telegram:3328128766:148")
        self.assertEqual(meta.youtube_urls, [])

    def test_validate_enriched_card_rejects_unknown_point_type(self):
        card_data = _minimal_v2_card(point_type="bogus_type")
        card, issues = validate_enriched_card(card_data, "card.enriched.json")

        self.assertIsNone(card)
        self.assertTrue(any(issue.severity == "error" for issue in issues))
        self.assertTrue(any(issue.field == "key_points.0.type" for issue in issues))

    def test_enriched_card_rejects_wrong_schema_and_missing_source_id(self):
        wrong_schema = _minimal_v2_card()
        wrong_schema["schema_version"] = "enriched_v3"
        with self.assertRaises(ValidationError):
            EnrichedCardV2.model_validate(wrong_schema)

        missing_source_id = _minimal_v2_card()
        missing_source_id["provenance"].pop("source_id")
        with self.assertRaises(ValidationError):
            EnrichedCardV2.model_validate(missing_source_id)

    def test_enriched_card_rejects_invalid_enums(self):
        mutations = (
            ("key_points.0.importance", lambda card: card["key_points"][0].update(importance="urgent")),
            ("entities.people.0.salience", lambda card: card["entities"]["people"][0].update(salience="central")),
            ("topics.0.salience", lambda card: card["topics"][0].update(salience="central")),
            ("topics.0.type", lambda card: card["topics"][0].update(type="news")),
            ("theses.0.stance", lambda card: card.update(theses=[{"text": "x", "stance": "hostile"}])),
            ("events.0.event_type", lambda card: card.update(events=[{"event_type": "speech", "description": "x"}])),
            ("search_phrases.0.source", lambda card: card["search_phrases"][0].update(source="invented")),
            ("ignored_blocks.0.type", lambda card: card.update(ignored_blocks=[{"type": "document", "text": "x"}])),
            ("quality_flags.0", lambda card: card.update(quality_flags=["propaganda"])),
            ("content_type", lambda card: card.update(content_type="news")),
        )
        for field, mutate in mutations:
            with self.subTest(field=field):
                card = _minimal_v2_card()
                mutate(card)
                with self.assertRaises(ValidationError):
                    EnrichedCardV2.model_validate(card)

    def test_validate_enriched_card_errors_on_missing_provenance(self):
        bad_card = _minimal_v2_card()
        bad_card.pop("provenance")

        card, issues = validate_enriched_card(bad_card, "bad.enriched.json")

        self.assertIsNone(card)
        self.assertTrue(any(issue.severity == "error" for issue in issues))

    def test_validate_enriched_card_rejects_non_russian_semantic_field(self):
        card_data = _minimal_v2_card()
        card_data["summary"] = "Україна ухвалила важливе рішення."

        card, issues = validate_enriched_card(card_data, "card.enriched.json")

        self.assertIsNotNone(card)
        self.assertTrue(any(issue.code == "non_russian_semantic_field" for issue in issues))

    def test_validate_enriched_card_allows_non_russian_quote_outside_graph_text(self):
        card_data = _minimal_v2_card()
        card_data["quotes"] = [{"speaker": "Президент", "text": "Ми ухвалили це рішення"}]
        card_data["search_text"] += " Ми ухвалили це рішення"

        card, issues = validate_enriched_card(card_data, "card.enriched.json")

        self.assertIsNotNone(card)
        self.assertFalse(any(issue.severity == "error" for issue in issues), issues)

    def test_validate_enriched_card_rejects_non_russian_quote_in_graph_text(self):
        card_data = _minimal_v2_card()
        quote = "Ми ухвалили це важливе рішення"
        card_data["quotes"] = [{"speaker": "Президент", "text": quote}]
        card_data["graph_text"] += f" {quote}"

        card, issues = validate_enriched_card(card_data, "card.enriched.json")

        self.assertIsNotNone(card)
        self.assertTrue(any(issue.code == "non_russian_quote_in_graph_text" for issue in issues))

    def test_scan_enriched_cards_and_write_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched_dir = Path(tmpdir) / "enriched"
            enriched_dir.mkdir()
            (enriched_dir / "ok.enriched.json").write_text(
                json.dumps(_minimal_v2_card(), ensure_ascii=False),
                encoding="utf-8",
            )
            warning_card = _minimal_v2_card()
            warning_card["summary"] = ""
            warning_card["key_points"] = []
            (enriched_dir / "warn.enriched.json").write_text(
                json.dumps(warning_card, ensure_ascii=False),
                encoding="utf-8",
            )
            (enriched_dir / "bad.enriched.json").write_text("{bad json", encoding="utf-8")

            report = scan_enriched_cards(enriched_dir)
            report_path = write_enriched_validation_report(report, Path(tmpdir) / "report.md")

        self.assertEqual(report.cards_seen, 3)
        self.assertEqual(report.cards_valid, 2)
        self.assertEqual(report.cards_invalid, 1)
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.warning_count, 1)
        self.assertTrue(report_path.name.endswith(".md"))


if __name__ == "__main__":
    unittest.main()
