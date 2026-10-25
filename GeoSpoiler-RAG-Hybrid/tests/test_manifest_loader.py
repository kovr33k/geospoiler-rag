import hashlib
import json
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import llm_backend
from loader import ingest as ingest_module
from loader.ingest import load_from_manifest
from scripts.release_identity import config_identity_sha256


class ManifestLoaderTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(self, root: Path, *, quality_flags: list[str] | None = None) -> tuple[Path, dict]:
        enriched_dir = root / "output" / "enriched" / "topic"
        normalized_dir = root / "output" / "normalized" / "topic"
        enriched_dir.mkdir(parents=True, exist_ok=True)
        normalized_dir.mkdir(parents=True, exist_ok=True)
        normalized_path = normalized_dir / "1.txt"
        normalized_path.write_text("[Channel: test]\nsource text\n", encoding="utf-8")

        graph_text = "Entity: test\nRelation: source"
        card = {
            "schema_version": "enriched_v2",
            "graph_text": graph_text,
            "quality_flags": quality_flags or [],
            "provenance": {
                "source_id": "telegram:123:1",
                "normalized_path": "output/normalized/topic/1.txt",
            },
        }
        card_path = enriched_dir / "1.enriched.json"
        card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

        entry = {
            "accepted": True,
            "source_id": "telegram:123:1",
            "normalized_path": "output/normalized/topic/1.txt",
            "card_path": "output/enriched/topic/1.enriched.json",
            "normalized_sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
            "enriched_sha256": hashlib.sha256(card_path.read_bytes()).hexdigest(),
            "graph_text_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
        }
        manifest = {
            "schema_version": "accepted_manifest_v2",
            "run_id": "run-1",
            "pipeline_identity": {
                "llm_profile": config.LLM_PROFILE,
                "enrichment_model": llm_backend.active_model_for("enrichment"),
                "media_contract_version": "media-contract-v2",
                "late_fusion_enabled": config.LATE_FUSION_ENABLED,
                "reranker_enabled": config.RERANKER_ENABLED,
                "wiki_enabled": config.WIKI_ENABLED,
                "expected_cards": 1,
                "config_identity_sha256": config_identity_sha256(),
            },
            "sources": [entry],
        }
        manifest_path = root / "accepted.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest_path, manifest

    def _patch_config(self, root: Path):
        return (
            patch.object(config, "PROJECT_ROOT", root),
            patch.object(config, "NORMALIZED_DIR", root / "output" / "normalized"),
            patch.object(config, "ENRICHED_DIR", root / "output" / "enriched"),
        )

    async def test_valid_manifest_preflights_and_loads_exactly_all_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            loader = AsyncMock(return_value=1)
            rag = object()
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=loader),
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
            ):
                loaded = await load_from_manifest(rag, manifest_path)

            normalized_path = str((root / manifest["sources"][0]["normalized_path"]).resolve())
            self.assertEqual(loaded, 1)
            loader.assert_awaited_once_with(
                rag,
                [(normalized_path, "Entity: test\nRelation: source")],
                reuse_processed=False,
            )
            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "accepted")
            self.assertEqual(report["expected"], 1)
            self.assertEqual(report["inserted"], 1)
            self.assertEqual(
                report["loaded_sources"],
                [
                    {
                        "source_id": manifest["sources"][0]["source_id"],
                        "graph_text_sha256": manifest["sources"][0]["graph_text_sha256"],
                    }
                ],
            )

    async def test_manifest_requires_root_and_entry_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            manifest.pop("pipeline_identity")
            for field in (
                "accepted",
                "source_id",
                "normalized_path",
                "card_path",
                "normalized_sha256",
                "enriched_sha256",
                "graph_text_sha256",
            ):
                entry = manifest["sources"][0]
                entry.pop(field, None)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "required field"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_manifest_rejects_card_and_normalized_paths_outside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            manifest["sources"][0]["normalized_path"] = str(outside)
            manifest["sources"][0]["card_path"] = str(outside)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2]:
                with self.assertRaisesRegex(RuntimeError, "outside allowed directory"):
                    await load_from_manifest(object(), manifest_path)

    async def test_manifest_rejects_each_hash_mismatch_before_loader(self):
        for hash_field in ("normalized_sha256", "enriched_sha256", "graph_text_sha256"):
            with self.subTest(hash_field=hash_field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest_path, manifest = self._fixture(root)
                manifest["sources"][0][hash_field] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                patches = self._patch_config(root)
                with patches[0], patches[1], patches[2], patch(
                    "loader.ingest.load_texts", new=AsyncMock()
                ) as loader:
                    with self.assertRaisesRegex(RuntimeError, hash_field + " mismatch"):
                        await load_from_manifest(object(), manifest_path)
                loader.assert_not_awaited()

    async def test_manifest_rejects_provenance_mismatch_and_blocked_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root, quality_flags=["partial_segment_failure"])
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "blocked quality flag"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

            manifest_path, manifest = self._fixture(root)
            manifest["sources"][0]["source_id"] = "telegram:wrong:1"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "source_id mismatch"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_manifest_rejects_duplicate_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            manifest["sources"].append(dict(manifest["sources"][0]))
            manifest["pipeline_identity"]["expected_cards"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "duplicate source_id"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_manifest_rejects_unknown_schema_and_v2_identity_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            manifest["schema_version"] = "accepted_manifest_v99"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "schema_version must be one of"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

            manifest_path, manifest = self._fixture(root)
            manifest["schema_version"] = "accepted_manifest_v2"
            manifest["pipeline_identity"] = {"llm_profile": "current"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "pipeline_identity missing required"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_partial_load_is_failed_and_writes_machine_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = self._fixture(root)
            loader = AsyncMock(return_value=0)
            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=loader
            ):
                with self.assertRaisesRegex(RuntimeError, "inserted=0, expected=1"):
                    await load_from_manifest(object(), manifest_path)

            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["expected"], 1)
            self.assertEqual(report["inserted"], 0)
            self.assertEqual(report["skipped"], 1)
            self.assertEqual(report["failed"], 1)

    async def test_manifest_accepts_absolute_paths_inside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            entry = manifest["sources"][0]
            entry["normalized_path"] = str((root / "output" / "normalized" / "topic" / "1.txt").resolve())
            entry["card_path"] = str((root / "output" / "enriched" / "topic" / "1.enriched.json").resolve())
            card_path = Path(entry["card_path"])
            card = json.loads(card_path.read_text(encoding="utf-8"))
            card["provenance"]["normalized_path"] = entry["normalized_path"]
            card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            entry["enriched_sha256"] = hashlib.sha256(card_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=AsyncMock(return_value=1)) as loader,
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
            ):
                self.assertEqual(await load_from_manifest(object(), manifest_path), 1)
            loader.assert_awaited_once()

    async def test_manifest_rejects_card_snapshot_changed_before_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            original_load_manifest_file = ingest_module.load_manifest_file

            def validate_then_change_card(path, *, allow_legacy_manifest=False):
                validation = original_load_manifest_file(
                    path,
                    allow_legacy_manifest=allow_legacy_manifest,
                )
                card_path = root / manifest["sources"][0]["card_path"]
                card = json.loads(card_path.read_text(encoding="utf-8"))
                card["graph_text"] = "Entity: changed\nRelation: after validation"
                card_path.write_text(json.dumps(card), encoding="utf-8")
                return validation

            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch.object(ingest_module, "load_manifest_file", side_effect=validate_then_change_card),
                patch("loader.ingest.load_texts", new=AsyncMock()) as loader,
            ):
                with self.assertRaisesRegex(RuntimeError, "card bytes changed after validation"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_manifest_rejects_normalized_snapshot_changed_before_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            original_load_manifest_file = ingest_module.load_manifest_file

            def validate_then_change_normalized(path, *, allow_legacy_manifest=False):
                validation = original_load_manifest_file(
                    path,
                    allow_legacy_manifest=allow_legacy_manifest,
                )
                normalized_path = root / manifest["sources"][0]["normalized_path"]
                normalized_path.write_text("changed after validation", encoding="utf-8")
                return validation

            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch.object(ingest_module, "load_manifest_file", side_effect=validate_then_change_normalized),
                patch("loader.ingest.load_texts", new=AsyncMock()) as loader,
            ):
                with self.assertRaisesRegex(RuntimeError, "normalized bytes changed after validation"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()

    async def test_v2_manifest_accepts_youtube_normalized_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "output" / "normalized_youtube" / "channel" / "episode.txt"
            card_path = root / "output" / "enriched" / "channel" / "episode.enriched.json"
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            card_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_path.write_text("[YouTube]\ntranscript\n", encoding="utf-8")
            graph_text = "Entity: episode\nRelation: source"
            card = {
                "schema_version": "enriched_v2",
                "graph_text": graph_text,
                "quality_flags": [],
                "provenance": {
                    "source_id": "telegram:123:7:youtube:episode",
                    "normalized_path": "output\\normalized_youtube\\channel\\episode.txt",
                },
            }
            card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
            entry = {
                "accepted": True,
                "document_kind": "youtube_episode",
                "source_id": "telegram:123:7:youtube:episode",
                "normalized_path": "output\\normalized_youtube\\channel\\episode.txt",
                "card_path": "output\\enriched\\channel\\episode.enriched.json",
                "normalized_sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
                "enriched_sha256": hashlib.sha256(card_path.read_bytes()).hexdigest(),
                "graph_text_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
            }
            manifest_path = root / "accepted-v2.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "accepted_manifest_v2",
                        "run_id": "v2",
                        "pipeline_identity": {
                            "llm_profile": config.LLM_PROFILE,
                            "enrichment_model": llm_backend.active_model_for("enrichment"),
                            "media_contract_version": "media-contract-v2",
                            "late_fusion_enabled": config.LATE_FUSION_ENABLED,
                            "reranker_enabled": config.RERANKER_ENABLED,
                            "wiki_enabled": config.WIKI_ENABLED,
                            "expected_cards": 1,
                            "config_identity_sha256": config_identity_sha256(),
                        },
                        "sources": [entry],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            loader = AsyncMock(return_value=1)
            rag = object()
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch.object(config, "YOUTUBE_NORMALIZED_DIR", root / "output" / "normalized_youtube"),
                patch("loader.ingest.load_texts", new=loader),
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
            ):
                loaded = await load_from_manifest(rag, manifest_path)

            self.assertEqual(loaded, 1)
            loader.assert_awaited_once_with(
                rag,
                [(str(normalized_path.resolve()), graph_text)],
                reuse_processed=False,
            )

    async def test_v1_manifest_requires_explicit_legacy_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, manifest = self._fixture(root)
            manifest["schema_version"] = "accepted_manifest_v1"
            manifest["pipeline_identity"] = {"profile": config.LLM_PROFILE}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self._patch_config(root)
            with patches[0], patches[1], patches[2], patch(
                "loader.ingest.load_texts", new=AsyncMock()
            ) as loader:
                with self.assertRaisesRegex(RuntimeError, "legacy_identity_not_release_eligible"):
                    await load_from_manifest(object(), manifest_path)
            loader.assert_not_awaited()
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=AsyncMock(return_value=1)) as loader,
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
            ):
                self.assertEqual(
                    await load_from_manifest(
                        object(), manifest_path, allow_legacy_manifest=True
                    ),
                    1,
                )
            loader.assert_awaited_once()

    async def test_accepted_load_fails_when_release_identity_preparation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = self._fixture(root)
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=AsyncMock(return_value=1)) as loader,
                patch(
                    "scripts.release_identity.build_identity",
                    side_effect=RuntimeError("identity details must stay out of reports"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "release report failed"):
                    await load_from_manifest(object(), manifest_path)

            loader.assert_awaited_once()
            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("identity details", json.dumps(report))

    async def test_accepted_load_fails_when_report_stamping_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = self._fixture(root)
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=AsyncMock(return_value=1)) as loader,
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
                patch(
                    "scripts.release_identity.stamp_report",
                    side_effect=RuntimeError("stamp details must stay out of reports"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "release report failed"):
                    await load_from_manifest(object(), manifest_path)

            loader.assert_awaited_once()
            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("stamp details", json.dumps(report))

    async def test_accepted_load_fails_when_atomic_report_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = self._fixture(root)
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch("loader.ingest.load_texts", new=AsyncMock(return_value=1)) as loader,
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
                patch(
                    "loader.ingest.atomic_write_json",
                    side_effect=OSError("disk details must stay out of reports"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "release report failed"):
                    await load_from_manifest(object(), manifest_path)

            loader.assert_awaited_once()
            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("disk details", json.dumps(report))

    async def test_load_exception_remains_primary_and_report_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path, _ = self._fixture(root)
            patches = self._patch_config(root)
            with (
                patches[0],
                patches[1],
                patches[2],
                patch(
                    "loader.ingest.load_texts",
                    new=AsyncMock(side_effect=ValueError("load details must stay out of reports")),
                ) as loader,
                patch("scripts.release_identity.git_commit", return_value="commit"),
                patch("scripts.release_identity.git_tree_sha256", return_value="tree"),
                patch("scripts.release_identity.working_tree_sha256", return_value="working"),
            ):
                with self.assertRaisesRegex(RuntimeError, "accepted manifest load failed before completion") as caught:
                    await load_from_manifest(object(), manifest_path)

            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(
                "load details must stay out of reports",
                "".join(traceback.format_exception(caught.exception)),
            )
            loader.assert_awaited_once()
            report = json.loads(
                (root / "artifacts" / "accepted_manifest_load_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("load details", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
