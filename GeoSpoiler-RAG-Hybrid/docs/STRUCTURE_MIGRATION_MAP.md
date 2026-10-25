# Карта переноса модулей

Каноническая реализация находится в src/geospoiler. Прежние модули сохраняются как совместимые импорты/точки запуска; функции и состояние не дублируются.

| Прежний путь | Реализация | Совместимость |
|---|---|---|
| `fetcher/state.py` | `src/geospoiler/ingestion/fetcher/state.py` | module identity alias |
| `fetcher/telegram_client.py` | `src/geospoiler/ingestion/fetcher/telegram_client.py` | module identity alias |
| `fetcher/__init__.py` | `src/geospoiler/ingestion/fetcher/__init__.py` | package shell |
| `normalizer/ai_chat_handler.py` | `src/geospoiler/ingestion/normalizer/ai_chat_handler.py` | module identity alias |
| `normalizer/atomic_pair.py` | `src/geospoiler/ingestion/normalizer/atomic_pair.py` | module identity alias |
| `normalizer/image_handler.py` | `src/geospoiler/ingestion/normalizer/image_handler.py` | module identity alias |
| `normalizer/instagram_handler.py` | `src/geospoiler/ingestion/normalizer/instagram_handler.py` | module identity alias |
| `normalizer/media_backfill.py` | `src/geospoiler/ingestion/normalizer/media_backfill.py` | module identity alias |
| `normalizer/media_frames.py` | `src/geospoiler/ingestion/normalizer/media_frames.py` | module identity alias |
| `normalizer/media_paths.py` | `src/geospoiler/ingestion/normalizer/media_paths.py` | module identity alias |
| `normalizer/media_vision.py` | `src/geospoiler/ingestion/normalizer/media_vision.py` | module identity alias |
| `normalizer/pipeline.py` | `src/geospoiler/ingestion/normalizer/pipeline.py` | module identity alias |
| `normalizer/review_queue.py` | `src/geospoiler/ingestion/normalizer/review_queue.py` | module identity alias |
| `normalizer/router.py` | `src/geospoiler/ingestion/normalizer/router.py` | module identity alias |
| `normalizer/text_handler.py` | `src/geospoiler/ingestion/normalizer/text_handler.py` | module identity alias |
| `normalizer/transcription_backfill.py` | `src/geospoiler/ingestion/normalizer/transcription_backfill.py` | module identity alias |
| `normalizer/transcription_handler.py` | `src/geospoiler/ingestion/normalizer/transcription_handler.py` | module identity alias |
| `normalizer/translator.py` | `src/geospoiler/ingestion/normalizer/translator.py` | module identity alias |
| `normalizer/visual_backfill.py` | `src/geospoiler/ingestion/normalizer/visual_backfill.py` | module identity alias |
| `normalizer/web_handler.py` | `src/geospoiler/ingestion/normalizer/web_handler.py` | module identity alias |
| `normalizer/youtube_handler.py` | `src/geospoiler/ingestion/normalizer/youtube_handler.py` | module identity alias |
| `normalizer/__init__.py` | `src/geospoiler/ingestion/normalizer/__init__.py` | package shell |
| `normalizer/instagram/audio.py` | `src/geospoiler/ingestion/normalizer/instagram/audio.py` | module identity alias |
| `normalizer/instagram/builder.py` | `src/geospoiler/ingestion/normalizer/instagram/builder.py` | module identity alias |
| `normalizer/instagram/cache.py` | `src/geospoiler/ingestion/normalizer/instagram/cache.py` | module identity alias |
| `normalizer/instagram/downloader.py` | `src/geospoiler/ingestion/normalizer/instagram/downloader.py` | module identity alias |
| `normalizer/instagram/frames.py` | `src/geospoiler/ingestion/normalizer/instagram/frames.py` | module identity alias |
| `normalizer/instagram/pipeline.py` | `src/geospoiler/ingestion/normalizer/instagram/pipeline.py` | module identity alias |
| `normalizer/instagram/vision.py` | `src/geospoiler/ingestion/normalizer/instagram/vision.py` | module identity alias |
| `normalizer/instagram/__init__.py` | `src/geospoiler/ingestion/normalizer/instagram/__init__.py` | package shell |
| `enricher/chunker.py` | `src/geospoiler/ingestion/enricher/chunker.py` | module identity alias |
| `enricher/content_classifier.py` | `src/geospoiler/ingestion/enricher/content_classifier.py` | module identity alias |
| `enricher/graph_text_builder.py` | `src/geospoiler/ingestion/enricher/graph_text_builder.py` | module identity alias |
| `enricher/llm_enricher.py` | `src/geospoiler/ingestion/enricher/llm_enricher.py` | module identity alias |
| `enricher/pipeline.py` | `src/geospoiler/ingestion/enricher/pipeline.py` | module identity alias |
| `enricher/preprocessor.py` | `src/geospoiler/ingestion/enricher/preprocessor.py` | module identity alias |
| `enricher/repair.py` | `src/geospoiler/ingestion/enricher/repair.py` | module identity alias |
| `enricher/triage.py` | `src/geospoiler/ingestion/enricher/triage.py` | module identity alias |
| `enricher/validator.py` | `src/geospoiler/ingestion/enricher/validator.py` | module identity alias |
| `enricher/youtube_pipeline.py` | `src/geospoiler/ingestion/enricher/youtube_pipeline.py` | module identity alias |
| `enricher/youtube_segmenter.py` | `src/geospoiler/ingestion/enricher/youtube_segmenter.py` | module identity alias |
| `enricher/__init__.py` | `src/geospoiler/ingestion/enricher/__init__.py` | package shell |
| `loader/answer_postprocess.py` | `src/geospoiler/answering/answer_postprocess.py` | module identity alias |
| `loader/card_context.py` | `src/geospoiler/answering/card_context.py` | module identity alias |
| `loader/clients.py` | `src/geospoiler/providers/clients.py` | module identity alias |
| `loader/entity_merge.py` | `src/geospoiler/retrieval/lightrag/entity_merge.py` | module identity alias |
| `loader/extraction.py` | `src/geospoiler/retrieval/lightrag/extraction.py` | module identity alias |
| `loader/factory.py` | `src/geospoiler/retrieval/lightrag/factory.py` | module identity alias |
| `loader/graph_quality.py` | `src/geospoiler/retrieval/lightrag/graph_quality.py` | module identity alias |
| `loader/ingest.py` | `src/geospoiler/retrieval/lightrag/ingest.py` | module identity alias |
| `loader/late_fusion.py` | `src/geospoiler/answering/late_fusion.py` | module identity alias |
| `loader/lightrag_loader.py` | `src/geospoiler/compat/lightrag_loader.py` | module identity alias |
| `loader/manifest_contract.py` | `src/geospoiler/retrieval/lightrag/manifest_contract.py` | module identity alias |
| `loader/profiles.py` | `src/geospoiler/answering/profiles.py` | module identity alias |
| `loader/query.py` | `src/geospoiler/answering/query.py` | module identity alias |
| `loader/reference_hints.py` | `src/geospoiler/answering/reference_hints.py` | module identity alias |
| `loader/runtime.py` | `src/geospoiler/providers/runtime.py` | module identity alias |
| `loader/storage.py` | `src/geospoiler/retrieval/lightrag/storage.py` | module identity alias |
| `retrieval/card_fts.py` | `src/geospoiler/retrieval/cards/card_fts.py` | module identity alias |
| `retrieval/card_text.py` | `src/geospoiler/retrieval/cards/card_text.py` | module identity alias |
| `retrieval/composer.py` | `src/geospoiler/answering/composer.py` | module identity alias |
| `retrieval/query_terms.py` | `src/geospoiler/retrieval/cards/query_terms.py` | module identity alias |
| `retrieval/response_formatter.py` | `src/geospoiler/cli/response_formatter.py` | module identity alias |
| `retrieval/shadow_search.py` | `src/geospoiler/retrieval/cards/shadow_search.py` | module identity alias |
| `retrieval/source_registry.py` | `src/geospoiler/sources/registry.py` | module identity alias |
| `retrieval/wiki/analysis.py` | `src/geospoiler/wiki/analysis.py` | module identity alias |
| `retrieval/wiki/cards.py` | `src/geospoiler/wiki/cards.py` | module identity alias |
| `retrieval/wiki/eligibility.py` | `src/geospoiler/wiki/eligibility.py` | module identity alias |
| `retrieval/wiki/extraction.py` | `src/geospoiler/wiki/extraction.py` | module identity alias |
| `retrieval/wiki/grouping.py` | `src/geospoiler/wiki/grouping.py` | module identity alias |
| `retrieval/wiki/hashing.py` | `src/geospoiler/wiki/hashing.py` | module identity alias |
| `retrieval/wiki/hierarchy.py` | `src/geospoiler/wiki/hierarchy.py` | module identity alias |
| `retrieval/wiki/ingest.py` | `src/geospoiler/wiki/ingest.py` | module identity alias |
| `retrieval/wiki/lifecycle.py` | `src/geospoiler/wiki/lifecycle.py` | module identity alias |
| `retrieval/wiki/projections.py` | `src/geospoiler/wiki/projections.py` | module identity alias |
| `retrieval/wiki/registry.py` | `src/geospoiler/wiki/registry.py` | module identity alias |
| `retrieval/wiki/relations.py` | `src/geospoiler/wiki/relations.py` | module identity alias |
| `retrieval/wiki/schema.py` | `src/geospoiler/wiki/schema.py` | module identity alias |
| `retrieval/wiki/search.py` | `src/geospoiler/wiki/search.py` | module identity alias |
| `retrieval/wiki/service.py` | `src/geospoiler/wiki/service.py` | module identity alias |
| `retrieval/wiki/sidecars.py` | `src/geospoiler/wiki/sidecars.py` | module identity alias |
| `retrieval/wiki/state.py` | `src/geospoiler/wiki/state.py` | module identity alias |
| `retrieval/wiki/__init__.py` | `src/geospoiler/wiki/__init__.py` | package shell |
| `eval/__init__.py` | `src/geospoiler/evaluation/__init__.py` | package shell |
| `eval/model_bakeoff/aggregate_report.py` | `src/geospoiler/evaluation/model_bakeoff/aggregate_report.py` | module identity alias |
| `eval/model_bakeoff/config_loader.py` | `src/geospoiler/evaluation/model_bakeoff/config_loader.py` | module identity alias |
| `eval/model_bakeoff/post_selection.py` | `src/geospoiler/evaluation/model_bakeoff/post_selection.py` | module identity alias |
| `eval/model_bakeoff/providers.py` | `src/geospoiler/evaluation/model_bakeoff/providers.py` | module identity alias |
| `eval/model_bakeoff/rescore_run.py` | `src/geospoiler/evaluation/model_bakeoff/rescore_run.py` | module identity alias |
| `eval/model_bakeoff/run_bakeoff.py` | `src/geospoiler/evaluation/model_bakeoff/run_bakeoff.py` | module identity alias |
| `eval/model_bakeoff/scoring.py` | `src/geospoiler/evaluation/model_bakeoff/scoring.py` | module identity alias |
| `eval/model_bakeoff/select_china_posts.py` | `src/geospoiler/evaluation/model_bakeoff/select_china_posts.py` | module identity alias |
| `eval/model_bakeoff/__init__.py` | `src/geospoiler/evaluation/model_bakeoff/__init__.py` | package shell |
| `eval/model_bakeoff/prompts/__init__.py` | `src/geospoiler/evaluation/model_bakeoff/prompts/__init__.py` | package shell |
| `experiments/__init__.py` | `src/geospoiler/evaluation/experiments/__init__.py` | package shell |
| `scripts/build_release_evidence.py` | `src/geospoiler/operations/build_release_evidence.py` | module identity alias |
| `scripts/build_release_manifest.py` | `src/geospoiler/operations/build_release_manifest.py` | module identity alias |
| `scripts/build_release_risk_register.py` | `src/geospoiler/operations/build_release_risk_register.py` | module identity alias |
| `scripts/build_release_test_evidence.py` | `src/geospoiler/operations/build_release_test_evidence.py` | module identity alias |
| `scripts/build_release_verification.py` | `src/geospoiler/operations/build_release_verification.py` | module identity alias |
| `scripts/cold_graph_check.py` | `src/geospoiler/operations/cold_graph_check.py` | module identity alias |
| `scripts/cold_provenance.py` | `src/geospoiler/operations/cold_provenance.py` | module identity alias |
| `scripts/enriched_v2_pilot.py` | `src/geospoiler/operations/enriched_v2_pilot.py` | module identity alias |
| `scripts/enrichment_bakeoff_testchina.py` | `src/geospoiler/operations/enrichment_bakeoff_testchina.py` | module identity alias |
| `scripts/graph_provenance_reconcile.py` | `src/geospoiler/operations/graph_provenance_reconcile.py` | module identity alias |
| `scripts/instagram_live_check.py` | `src/geospoiler/operations/instagram_live_check.py` | module identity alias |
| `scripts/late_fusion_ab.py` | `src/geospoiler/operations/late_fusion_ab.py` | module identity alias |
| `scripts/late_fusion_activation_smoke.py` | `src/geospoiler/operations/late_fusion_activation_smoke.py` | module identity alias |
| `scripts/luna_reenrich_audit.py` | `src/geospoiler/operations/luna_reenrich_audit.py` | module identity alias |
| `scripts/luna_reenrich_control.py` | `src/geospoiler/operations/luna_reenrich_control.py` | module identity alias |
| `scripts/luna_smoke.py` | `src/geospoiler/operations/luna_smoke.py` | module identity alias |
| `scripts/migrate_portable_paths.py` | `src/geospoiler/operations/migrate_portable_paths.py` | module identity alias |
| `scripts/refresh_youtube_generation_hashes.py` | `src/geospoiler/operations/refresh_youtube_generation_hashes.py` | module identity alias |
| `scripts/release_identity.py` | `src/geospoiler/operations/release_identity.py` | module identity alias |
| `scripts/reranker_compare.py` | `src/geospoiler/operations/reranker_compare.py` | module identity alias |
| `scripts/security_artifact_redaction.py` | `src/geospoiler/operations/security_artifact_redaction.py` | module identity alias |
| `scripts/security_release_scan.py` | `src/geospoiler/operations/security_release_scan.py` | module identity alias |
| `scripts/smoke_contract.py` | `src/geospoiler/operations/smoke_contract.py` | module identity alias |
| `scripts/sync_enrichment_progress.py` | `src/geospoiler/operations/sync_enrichment_progress.py` | module identity alias |
| `scripts/sync_media_contract.py` | `src/geospoiler/operations/sync_media_contract.py` | module identity alias |
| `scripts/translation_bakeoff_testchina.py` | `src/geospoiler/operations/translation_bakeoff_testchina.py` | module identity alias |
| `scripts/validate_backup_restore.py` | `src/geospoiler/operations/validate_backup_restore.py` | module identity alias |
| `config.py` | `src/geospoiler/config.py` | module identity alias |
| `models.py` | `src/geospoiler/contracts/models.py` | module identity alias |
| `data_validation.py` | `src/geospoiler/ingestion/enricher/card_validation.py` | module identity alias |
| `llm_backend.py` | `src/geospoiler/providers/llm_backend.py` | module identity alias |
| `llm_auth.py` | `src/geospoiler/providers/llm_auth.py` | module identity alias |
| `reranker.py` | `src/geospoiler/providers/reranker.py` | module identity alias |
| `reviewer_app.py` | `src/geospoiler/ui/reviewer_app.py` | module identity alias |
| `wiki_reviewer.py` | `src/geospoiler/ui/wiki_reviewer.py` | module identity alias |
| `auth.py` | `src/geospoiler/cli/auth.py` | module identity alias |
| `qr_terminal.py` | `src/geospoiler/cli/qr_terminal.py` | module identity alias |
| `check.py` | `src/geospoiler/cli/check.py` | module identity alias |
| `debug_dialogs.py` | `src/geospoiler/cli/debug_dialogs.py` | module identity alias |
| `baseline_probe.py` | `src/geospoiler/evaluation/baseline_probe.py` | module identity alias |
| `experiment_registry.py` | `src/geospoiler/evaluation/experiment_registry.py` | module identity alias |
| `golden_compare.py` | `src/geospoiler/evaluation/golden_compare.py` | module identity alias |
| `source_selection_golden.py` | `src/geospoiler/evaluation/source_selection_golden.py` | module identity alias |
| `llm_verification_probe.py` | `src/geospoiler/evaluation/llm_verification_probe.py` | module identity alias |
| `cli.py` | `src/geospoiler/cli/compat.py` | module identity alias |
| `testing/no_network.py` | `src/geospoiler/testing_support/no_network.py` | module identity alias |
| `cli_app.py` | `src/geospoiler/cli/app.py` | module identity alias |
| `cli_pipeline.py` | `src/geospoiler/cli/pipeline.py` | module identity alias |
| `cli_query.py` | `src/geospoiler/cli/query.py` | module identity alias |
| `cli_runtime.py` | `src/geospoiler/cli/runtime.py` | module identity alias |
| `cli_tools.py` | `src/geospoiler/cli/tools.py` | module identity alias |
| `cli_wiki.py` | `src/geospoiler/cli/wiki.py` | module identity alias |
