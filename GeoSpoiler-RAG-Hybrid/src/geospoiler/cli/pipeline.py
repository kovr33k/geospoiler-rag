"""Pipeline, review, status, and quality CLI command implementations."""

import asyncio
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.cli.runtime import _finalize_rag_safely, logger
from geospoiler.evaluation.baseline_probe import (
    collect_baseline_metadata,
    run_baseline_probe,
    write_baseline_metadata,
    write_probe_report,
)
from geospoiler.ingestion.enricher.pipeline import EnrichmentStats, enrich_all
from geospoiler.ingestion.fetcher.state import get_all_progress, mark_message_processed
from geospoiler.ingestion.fetcher.telegram_client import TelegramFetcher, TelegramMessage
from geospoiler.ingestion.ingest_report import print_problem_posts_line
from geospoiler.ingestion.normalizer.pipeline import (
    NormalizationBatchResult,
    _get_filepath,
    normalize_batch,
    read_normalized_meta,
)
from geospoiler.ingestion.normalizer.review_queue import get_pending_reviews
from geospoiler.ingestion.outcomes import close_outcome, failure_reason, record_open_outcome
from geospoiler.operations.release_identity import safe_resolve_release_path
from geospoiler.providers import llm_backend
from geospoiler.retrieval.cards.card_fts import (
    CardFtsBuildStats,
    YouTubeSegmentFtsBuildStats,
    rebuild_card_index,
    rebuild_youtube_segment_index,
)
from geospoiler.retrieval.lightrag.entity_merge import auto_fix_safe_entity_merges
from geospoiler.retrieval.lightrag.factory import create_rag
from geospoiler.retrieval.lightrag.graph_quality import build_quality_report
from geospoiler.retrieval.lightrag.ingest import load_from_enriched, load_from_manifest
from geospoiler.retrieval.lightrag.storage import rebuild_rag_storage
from geospoiler.sources.registry import SourceRegistryStats, rebuild_source_registry
from geospoiler.wiki.service import WikiPipelineStats, get_wiki_review_counts, run_configured_wiki_pipeline, wiki_status


@dataclass
class LoadStats:
    """Summary of what reached LightRAG during a load step."""

    enriched_cards_seen: int = 0
    graph_texts_loaded: int = 0

    @property
    def total_loaded(self) -> int:
        return self.graph_texts_loaded


@dataclass(frozen=True)
class RetrievalRefreshStats:
    """Result of rebuilding the local indexes derived from enriched cards."""

    card_fts: CardFtsBuildStats | None
    source_registry: SourceRegistryStats | None
    errors: tuple[str, ...] = ()
    youtube_segments: YouTubeSegmentFtsBuildStats | None = None
    wiki: WikiPipelineStats | None = None


class RetrievalRefreshError(RuntimeError):
    """Raised after every local retrieval component was attempted and one failed."""

    def __init__(self, stats: RetrievalRefreshStats):
        self.stats = stats
        super().__init__("Retrieval refresh failed: " + "; ".join(stats.errors))


@dataclass(frozen=True)
class EnrichCommandStats:
    """Enrichment result together with the indexes refreshed from saved cards."""

    enrichment: EnrichmentStats
    retrieval: RetrievalRefreshStats

async def cmd_fetch(limit: int | None = None) -> list[tuple[str, list[TelegramMessage]]]:
    """Fetch new messages from all Telegram channels."""
    logger.info("=== FETCH: Starting Telegram fetch ===")

    fetcher = TelegramFetcher()
    await fetcher.connect()

    try:
        all_messages = await fetcher.fetch_all_channels(limit_per_channel=limit)
        total = sum(len(msgs) for msgs in all_messages.values())
        logger.info(f"=== FETCH complete: {total} messages from {len(all_messages)} channels ===")
        return list(all_messages.items())
    finally:
        await fetcher.disconnect()


def _mark_contiguous_processed(messages: list[TelegramMessage], successful_ids: set[int]) -> int:
    """Advance fetch progress only through the first contiguous successful prefix."""
    marked = 0
    for msg in sorted(messages, key=lambda item: item.message_id):
        if msg.message_id not in successful_ids:
            break
        mark_message_processed(msg.channel_id, msg.channel_name, msg.message_id)
        marked += 1
    return marked


async def cmd_normalize(channel_messages: list[tuple[str, list[TelegramMessage]]]) -> NormalizationBatchResult:
    """Normalize fetched messages into text files."""
    logger.info("=== NORMALIZE: Processing messages ===")

    summary = NormalizationBatchResult()

    for channel_name, messages in channel_messages:
        if not messages:
            continue
        logger.info(f"Processing channel: {channel_name} ({len(messages)} messages)")

        result = normalize_batch(messages)
        _sync_normalization_outcomes(messages, result)
        successful_ids = {int(Path(fp).stem) for fp, _ in result.texts_with_paths}
        marked_count = _mark_contiguous_processed(messages, successful_ids)
        result.processed_messages += marked_count
        sorted_messages = sorted(messages, key=lambda item: item.message_id)
        marked_ids = {msg.message_id for msg in sorted_messages[:marked_count]}

        for msg in messages:
            if msg.message_id not in marked_ids:
                logger.warning(
                    f"  Message {msg.message_id} from '{msg.channel_name}' not marked processed "
                    "(will be retried next run)."
                )

        summary.merge(result)

    logger.info(f"=== NORMALIZE complete: {summary.normalized_messages} texts normalized ===")
    return summary


async def cmd_load(
    manifest_path: str | None = None,
    *,
    reuse_processed: bool = True,
    allow_legacy_manifest: bool = False,
) -> LoadStats:
    """Explicitly load enriched_v2 graph_text into LightRAG."""
    logger.info("=== LOAD: Loading enriched graph text into LightRAG ===")

    resolved_manifest = None
    if manifest_path:
        resolved_manifest = safe_resolve_release_path(
            Path(manifest_path),
            label="accepted_manifest_path",
        )

    rag = await create_rag()
    load_stats = LoadStats()

    try:
        if resolved_manifest is not None:
            load_stats.enriched_cards_seen = 0
            load_stats.graph_texts_loaded = await load_from_manifest(
                rag,
                resolved_manifest,
                reuse_processed=reuse_processed,
                allow_legacy_manifest=allow_legacy_manifest,
            )
        else:
            load_stats.enriched_cards_seen = sum(
                1 for _ in config.ENRICHED_DIR.rglob("*.enriched.json")
            )
            if reuse_processed:
                load_stats.graph_texts_loaded = await load_from_enriched(rag)
            else:
                load_stats.graph_texts_loaded = await load_from_enriched(rag, reuse_processed=False)

        merges = await auto_fix_safe_entity_merges(rag)
        if merges:
            logger.info(f"=== AUTO-FIX complete: {len(merges)} safe entity merge(s) applied ===")
            _print_entity_autofix_summary(merges)

        logger.info(f"=== LOAD complete: {load_stats.total_loaded} texts loaded ===")
        return load_stats
    finally:
        await _finalize_rag_safely(rag)


def cmd_enrich(
    channel_filter: str | None = None,
    force: bool = False,
    *,
    ready_paths: queue.Queue | None = None,
) -> EnrichCommandStats:
    """Enrich normalized posts and refresh indexes derived from saved cards."""
    logger.info("=== ENRICH: Building memory cards ===")

    stats: EnrichmentStats | None = None
    enrichment_error: BaseException | None = None
    try:
        kwargs = {} if ready_paths is None else {"ready_paths": ready_paths}
        stats = enrich_all(channel_filter=channel_filter, force=force, **kwargs)
    except BaseException as exc:
        enrichment_error = exc

    try:
        # A failed batch may still have saved valid cards before the exception.
        refresh_stats = refresh_enriched_retrieval()
    except RetrievalRefreshError as refresh_error:
        if enrichment_error is not None:
            raise BaseExceptionGroup(
                "Enrichment and retrieval refresh failed",
                [enrichment_error, refresh_error],
            ) from refresh_error
        raise

    if enrichment_error is not None:
        raise enrichment_error

    if stats is None:  # pragma: no cover - enrich_all exceptions propagate
        raise RuntimeError("Enrichment did not return statistics")

    _print_enrich_summary(stats)

    logger.info("=== ENRICH complete ===")
    return EnrichCommandStats(enrichment=stats, retrieval=refresh_stats)


def refresh_enriched_retrieval(
    rebuild_fts: Callable[[], CardFtsBuildStats] | None = None,
    rebuild_registry: Callable[[], SourceRegistryStats] | None = None,
    rebuild_youtube_segments: Callable[[], YouTubeSegmentFtsBuildStats] | None = None,
    refresh_wiki: Callable[[], WikiPipelineStats] | None = None,
) -> RetrievalRefreshStats:
    """Refresh all local indexes derived from current enriched_v2 cards."""
    logger.info("=== RETRIEVAL REFRESH: Rebuilding FTS and registries ===")
    rebuild_fts = rebuild_fts or rebuild_card_index
    rebuild_registry = rebuild_registry or rebuild_source_registry
    rebuild_youtube_segments = rebuild_youtube_segments or rebuild_youtube_segment_index
    refresh_wiki = (
        refresh_wiki or run_configured_wiki_pipeline
        if config.WIKI_ENABLED
        else None
    )
    errors: list[str] = []
    card_stats: CardFtsBuildStats | None = None
    registry_stats: SourceRegistryStats | None = None
    youtube_segment_stats: YouTubeSegmentFtsBuildStats | None = None
    wiki_stats: WikiPipelineStats | None = None

    try:
        card_stats = rebuild_fts()
        logger.info(
            "Card FTS refreshed: seen=%d indexed=%d skipped=%d db=%s",
            card_stats.cards_seen,
            card_stats.cards_indexed,
            card_stats.cards_skipped,
            card_stats.db_path,
        )
    except Exception as exc:
        message = f"card FTS rebuild failed: {exc}"
        errors.append(message)
        logger.exception(message)

    try:
        registry_stats = rebuild_registry()
        logger.info(
            "Source registry refreshed: sources=%d normalized=%d enriched=%d references=%d db=%s",
            registry_stats.sources,
            registry_stats.normalized_docs,
            registry_stats.enriched_cards,
            registry_stats.references,
            registry_stats.db_path,
        )
    except Exception as exc:
        message = f"source registry rebuild failed: {exc}"
        errors.append(message)
        logger.exception(message)

    try:
        youtube_segment_stats = rebuild_youtube_segments()
        logger.info(
            "YouTube segment FTS refreshed: seen=%d indexed=%d skipped=%d db=%s",
            youtube_segment_stats.segments_seen,
            youtube_segment_stats.segments_indexed,
            youtube_segment_stats.segments_skipped,
            youtube_segment_stats.db_path,
        )
    except Exception as exc:
        message = f"YouTube segment FTS rebuild failed: {exc}"
        errors.append(message)
        logger.exception(message)

    if refresh_wiki is None:
        logger.info("Wiki disabled: skipping ingest, derived refresh, and projections")
    else:
        try:
            wiki_stats = refresh_wiki()
            logger.info(
                "Wiki refreshed: concepts_pending=%d hierarchy_pending=%d "
                "ambiguities_pending=%d hubs=%d fts=%d db=%s",
                wiki_stats.review_counts.concepts,
                wiki_stats.review_counts.hierarchy,
                wiki_stats.review_counts.ambiguities,
                wiki_stats.projections.hubs_built,
                wiki_stats.projections.fts_documents,
                wiki_stats.database_path,
            )
        except Exception as exc:
            message = f"Wiki refresh failed: {exc}"
            errors.append(message)
            logger.exception(message)

    logger.info("=== RETRIEVAL REFRESH complete: errors=%d ===", len(errors))
    stats = RetrievalRefreshStats(
        card_fts=card_stats,
        source_registry=registry_stats,
        errors=tuple(errors),
        youtube_segments=youtube_segment_stats,
        wiki=wiki_stats,
    )
    if errors:
        raise RetrievalRefreshError(stats)
    return stats


async def cmd_run(limit: int | None = None):
    """Automatic pipeline: fetch -> normalize -> enrich -> local index refresh."""
    logger.info("=== FULL PIPELINE START ===")

    channel_messages = await cmd_fetch(limit=limit)

    total_msgs = sum(len(msgs) for _, msgs in channel_messages)
    if total_msgs > 0:
        ready_paths = queue.Queue(maxsize=max(1, config.ENRICHMENT_CONCURRENCY))
        stop = threading.Event()
        consumer_done = threading.Event()

        def consume():
            try:
                return cmd_enrich(ready_paths=ready_paths)
            finally:
                consumer_done.set()
                stop.set()

        producer = asyncio.create_task(asyncio.to_thread(
            _normalize_into_queue, channel_messages, ready_paths, stop, consumer_done))
        consumer = asyncio.create_task(asyncio.to_thread(consume))
        try:
            enrich_stats = await asyncio.shield(consumer)
            normalize_stats = await asyncio.shield(producer)
        finally:
            stop.set()
            # Drain/cancel admission before returning, including caller cancellation.
            # Existing in-flight backend calls retain their normal bounded timeout.
            await asyncio.gather(asyncio.shield(producer), asyncio.shield(consumer), return_exceptions=True)
    else:
        logger.info("No new Telegram messages to normalize.")
        normalize_stats = NormalizationBatchResult()
        enrich_stats = cmd_enrich()
    _print_pipeline_summary(channel_messages, normalize_stats, enrich_stats)
    print_problem_posts_line()

    logger.info("=== FULL PIPELINE COMPLETE ===")


def _normalize_into_queue(channel_messages, ready_paths, stop, consumer_done) -> NormalizationBatchResult:
    summary = NormalizationBatchResult()

    def admit(path):
        while not stop.is_set():
            try:
                ready_paths.put(path, timeout=0.05)
                return True
            except queue.Full:
                pass
        return False

    try:
        incoming = {_get_filepath(msg) for _, messages in channel_messages for msg in messages}
        incoming_ids = {(msg.channel_id, msg.message_id) for _, messages in channel_messages for msg in messages}
        for path in sorted(config.YOUTUBE_NORMALIZED_DIR.rglob("*.youtube.meta.json")):
            try:
                source = json.loads(path.read_text(encoding="utf-8")).get("telegram_source", {})
                if (source.get("channel_id"), source.get("message_id")) in incoming_ids:
                    continue
            except (OSError, ValueError, AttributeError):
                pass  # Let the source owner retain discovery diagnostics.
            if not admit(path):
                return summary
        # Resume saved work even if Telegram's fetch checkpoint has advanced.
        for path in sorted(config.NORMALIZED_DIR.rglob("*.txt")):
            if path not in incoming and not admit(path):
                return summary
        for _, messages in channel_messages:
            successes = set()
            marked = 0
            ordered = sorted(messages, key=lambda item: item.message_id)
            for msg in ordered:
                if stop.is_set():
                    return summary
                result = normalize_batch([msg])
                summary.merge(result)
                _sync_normalization_outcomes([msg], result)
                for filepath, _ in result.texts_with_paths:
                    path = Path(filepath)
                    meta = read_normalized_meta(path)
                    for source in (meta.model_extra or {}).get("youtube_sources", []):
                        metadata_path = source.get("metadata_path")
                        if metadata_path and not admit(config.resolve_project_path(metadata_path)):
                            return summary
                    if not admit(path):
                        return summary
                    successes.add(msg.message_id)
                while marked < len(ordered) and ordered[marked].message_id in successes:
                    completed = ordered[marked]
                    mark_message_processed(completed.channel_id, completed.channel_name, completed.message_id)
                    summary.processed_messages += 1
                    marked += 1
        return summary
    finally:
        while not consumer_done.is_set():
            try:
                ready_paths.put(None, timeout=0.05)
                break
            except queue.Full:
                pass


async def cmd_rebuild():
    """Backup current RAG storage, then rebuild from enriched_v2 graph_text."""
    logger.info("=== REBUILD: Resetting LightRAG storage (source: enriched_v2 graph_text) ===")

    backup_path = rebuild_rag_storage()
    if backup_path:
        logger.info(f"RAG storage backup created at: {backup_path}")
    _clear_lightrag_query_cache()

    logger.info("=== REBUILD: Loading enriched_v2 graph text into fresh LightRAG storage ===")
    load_stats = await cmd_load()
    _print_load_summary(load_stats, heading="REBUILD SUMMARY")
    logger.info("=== REBUILD COMPLETE ===")


async def cmd_rebuild_embedding():
    """Backup current RAG storage, preserve LLM cache, then rebuild embeddings."""
    logger.info("=== REBUILD EMBEDDING: Resetting storage but preserving LLM cache ===")

    backup_path = rebuild_rag_storage(preserve_llm_cache=True)
    if backup_path:
        logger.info(f"RAG storage backup created at: {backup_path}")

    logger.info("=== REBUILD EMBEDDING: Loading sources to regenerate embeddings ===")
    load_stats = await cmd_load()
    _print_load_summary(load_stats, heading="REBUILD EMBEDDING SUMMARY")
    logger.info("=== REBUILD EMBEDDING COMPLETE ===")


def _clear_lightrag_query_cache() -> None:
    """Remove stale LLM query cache after storage reset/rebuild."""
    cache_path = config.RAG_STORAGE_DIR / "kv_store_llm_response_cache.json"
    if not cache_path.exists():
        return
    try:
        cache_path.unlink()
        logger.info("Cleared LightRAG LLM response cache for rebuild.")
    except OSError as exc:
        logger.warning(f"Could not clear LightRAG LLM response cache: {exc}")


def _sync_normalization_outcomes(
    messages: list[TelegramMessage],
    result: NormalizationBatchResult,
) -> None:
    """Record normalization failures and close outcomes of normalized posts.

    The channel cursor is updated by the caller and is left unchanged here.
    """
    for failure in result.failures:
        msg = failure.message
        record_open_outcome(
            channel_id=msg.channel_id,
            channel_title=msg.channel_name,
            channel_username=msg.channel_username,
            message_id=msg.message_id,
            post_url=msg.post_url,
            stage="normalize",
            outcome="failed",
            reason=failure_reason(failure.error),
        )
    successful_ids = {int(Path(path).stem) for path, _ in result.texts_with_paths}
    for msg in messages:
        if msg.message_id in successful_ids:
            close_outcome(msg.channel_id, msg.message_id)


def _print_run_summary(
    channel_messages: list[tuple[str, list[TelegramMessage]]],
    normalize_stats: NormalizationBatchResult,
) -> None:
    """Print an end-of-run summary with content breakdown and final delivery counts."""
    total_channels = len(channel_messages)
    channels_with_posts = sum(1 for _, msgs in channel_messages if msgs)

    print("\n📈 RUN SUMMARY")
    print("═" * 60)
    print(f"Каналов проверено: {total_channels}")
    print(f"Каналов с новыми постами: {channels_with_posts}")
    print(f"Новых постов найдено: {normalize_stats.messages_total}")
    print()
    print("Контент по новым постам:")
    print(f"  С текстом: {normalize_stats.messages_with_text}")
    print(
        f"  С изображениями: {normalize_stats.messages_with_images} постов / "
        f"{normalize_stats.images_total} изображений"
    )
    print(f"  С Telegram-видео: {normalize_stats.messages_with_native_video}")
    print(
        f"  С YouTube: {normalize_stats.messages_with_youtube} постов / "
        f"{normalize_stats.youtube_links_total} ссылок"
    )
    print(
        f"  С Instagram Reels: {normalize_stats.messages_with_instagram_reels} постов / "
        f"{normalize_stats.instagram_reel_links_total} ссылок"
    )
    print(
        f"  С Instagram posts: {normalize_stats.messages_with_instagram_posts} постов / "
        f"{normalize_stats.instagram_post_links_total} ссылок"
    )
    print(
        f"  С AI chat links: {normalize_stats.messages_with_ai_chat} постов / "
        f"{normalize_stats.ai_chat_links_total} ссылок"
    )
    print(
        f"  С web-ссылками: {normalize_stats.messages_with_web} постов / "
        f"{normalize_stats.web_links_total} ссылок"
    )
    print()
    print("Нормализация:")
    print(f"  Успешно нормализовано: {normalize_stats.normalized_messages}")
    print(f"  Пустых/пропущено: {normalize_stats.skipped_messages}")
    print(f"  Ошибок: {normalize_stats.failed_messages}")
    print(f"  Отмечено processed в state: {normalize_stats.processed_messages}")
    print()
    print("Review queue:")
    print(f"  AI чатов отправлено на review: {normalize_stats.ai_review_created}")
    print(f"  Внешних ссылок отправлено на review: {normalize_stats.link_review_created}")
    print(f"  Малоинформативных постов на review: {normalize_stats.uninformative_review_created}")
    print(f"  AI ссылок уже обработано ранее: {normalize_stats.ai_review_already_reviewed}")
    print("═" * 60)


def _print_pipeline_summary(
    channel_messages: list[tuple[str, list[TelegramMessage]]],
    normalize_stats: NormalizationBatchResult,
    enrich_stats: EnrichCommandStats,
) -> None:
    """Print the automatic pipeline result without implying LightRAG work."""
    total_messages = sum(len(messages) for _, messages in channel_messages)
    enrichment = enrich_stats.enrichment
    retrieval = enrich_stats.retrieval

    print("\nPIPELINE SUMMARY")
    print("=" * 60)
    print(f"Fetched messages: {total_messages}")
    print(f"Normalized texts: {normalize_stats.normalized_messages}")
    print(f"Enriched cards written: {enrichment.enriched}")
    print(f"Enrichment failures: {enrichment.failed}")
    if retrieval.card_fts:
        print(
            "Card FTS: "
            f"{retrieval.card_fts.cards_indexed}/{retrieval.card_fts.cards_seen} indexed "
            f"({retrieval.card_fts.cards_skipped} skipped)"
        )
    if retrieval.source_registry:
        print(
            "Source registry: "
            f"{retrieval.source_registry.sources} sources, "
            f"{retrieval.source_registry.enriched_cards} enriched cards"
        )
    print(f"Index refresh errors: {len(retrieval.errors)}")
    print("=" * 60)


def _print_load_summary(load_stats: LoadStats, heading: str = "LOAD SUMMARY") -> None:
    """Print a compact summary for load/rebuild commands."""
    print(f"\n📦 {heading}")
    print("═" * 60)
    print(f"Enriched cards scanned: {load_stats.enriched_cards_seen}")
    print(f"enriched_v2 graph texts loaded: {load_stats.graph_texts_loaded}")
    print(f"Итого загружено в LightRAG: {load_stats.total_loaded}")
    print("═" * 60)


def _print_enrich_summary(stats: EnrichmentStats) -> None:
    """Print a compact summary for the enrich command."""
    print("\n🧠 ENRICH SUMMARY")
    print("═" * 60)
    print(f"Просканировано постов: {stats.scanned}")
    print(f"Обогащено полностью: {stats.enriched}")
    if stats.partial:
        print(f"⚠️  Частично (LLM не ответил, будет повтор): {stats.partial}")
    if hasattr(stats, "skipped_review") and stats.skipped_review:
        print(f"Отправлено на review: {stats.skipped_review}")
    print(f"Пропущено (актуальных): {stats.skipped_up_to_date}")
    print(f"Пропущено (без meta.json): {stats.skipped_no_meta}")
    if getattr(stats, "youtube_sources", 0):
        print(
            f"YouTube: {stats.youtube_episodes} episode cards, "
            f"{stats.youtube_segments} segments"
        )
        if getattr(stats, "youtube_skipped", 0):
            print(f"YouTube skipped (already current): {stats.youtube_skipped}")
        if stats.youtube_partial:
            print(f"⚠️  YouTube с частичными сегментами: {stats.youtube_partial}")
        if stats.youtube_failed:
            print(f"⚠️  YouTube с ошибкой: {stats.youtube_failed}")
    print(f"Ошибок: {stats.failed}")
    if stats.partial_posts:
        print()
        print("⚠️  Посты для повтора (запустите enrich ещё раз):")
        for post in stats.partial_posts:
            print(f"  - {post}")
    if stats.by_content_type:
        print()
        print("По типу контента:")
        for ct, count in sorted(stats.by_content_type.items(), key=lambda x: -x[1]):
            print(f"  {ct}: {count}")
    if stats.repaired:
        print(f"\nОтремонтировано: {stats.repaired}")
    print("═" * 60)

def launch_reviewer_web() -> None:
    """Launch the Streamlit reviewer web UI and open it in the default browser."""
    import subprocess
    import time
    import webbrowser
    from importlib.util import find_spec

    app_path = Path(find_spec("geospoiler.ui.reviewer_app").origin)
    if not app_path.exists():
        print(f"Reviewer app not found at {app_path}")
        return

    port = 8501
    print(f"\n🌐 Запускаю Reviewer Web UI на http://localhost:{port}")
    print("Нажмите Ctrl+C в терминале, чтобы остановить.\n")

    # Open browser after a short delay
    def _open_browser():
        time.sleep(2)
        webbrowser.open(f"http://localhost:{port}")

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    child_env = os.environ.copy()
    child_env.update(
        {
            "LLM_PROFILE": llm_backend.active_profile(),
            "CODEX_CLI_PATH": config.CODEX_CLI_PATH,
            "CODEX_LUNA_MODEL": config.CODEX_LUNA_MODEL,
            "CODEX_LUNA_REASONING_EFFORT": config.CODEX_LUNA_REASONING_EFFORT,
            "CODEX_LLM_TIMEOUT_SECONDS": str(config.CODEX_LLM_TIMEOUT_SECONDS),
            "CODEX_LLM_MAX_CONCURRENCY": str(config.CODEX_LLM_MAX_CONCURRENCY),
            "CODEX_FALLBACK_TO_API": str(config.CODEX_FALLBACK_TO_API).lower(),
            "REGENERATE_ON_PROFILE_CHANGE": str(config.REGENERATE_ON_PROFILE_CHANGE).lower(),
        }
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
        ],
        cwd=str(config.PROJECT_ROOT),
        env=child_env,
    )


def _maybe_launch_reviewer() -> None:
    """Check for pending reviews and offer to launch the web UI."""
    content_pending = get_pending_reviews()
    wiki_pending = None
    if config.WIKI_ENABLED:
        try:
            wiki_pending = get_wiki_review_counts()
        except Exception as exc:
            logger.warning("Could not count Wiki review items: %s", exc)
    wiki_total = 0 if wiki_pending is None else wiki_pending.total
    review_total = len(content_pending) + wiki_total
    if review_total:
        print(f"\nReview required: {review_total} item(s).")
        print(f"  Content queue: {len(content_pending)}")
        if wiki_pending is not None:
            print(
                "  Wiki: "
                f"{wiki_pending.concepts} concepts/aliases, "
                f"{wiki_pending.hierarchy} hierarchy, "
                f"{wiki_pending.ambiguities} ambiguities"
            )
        if config.AUTO_OPEN_REVIEWER_AFTER_RUN:
            launch_reviewer_web()
        else:
            print("  Run: python main.py review --web")


def cmd_review(web: bool = False):
    """Show pending review items or launch the web UI."""
    if web:
        launch_reviewer_web()
        return

    wiki_pending = None
    if config.WIKI_ENABLED:
        try:
            wiki_pending = get_wiki_review_counts()
        except Exception as exc:
            logger.warning("Could not count Wiki review items: %s", exc)
    if wiki_pending is not None and wiki_pending.total:
        print(
            "\nWiki review: "
            f"{wiki_pending.concepts} concepts/aliases, "
            f"{wiki_pending.hierarchy} hierarchy, "
            f"{wiki_pending.ambiguities} ambiguities."
        )
        print("Open all queues with: python main.py review --web")

    items = get_pending_reviews()
    if not items and (wiki_pending is None or wiki_pending.total == 0):
        print("✅ Нет элементов в очереди на проверку.")
        print("\nДля веб-интерфейса: python main.py review --web")
        return
    if not items:
        return

    print(f"\n📋 Pending review items: {len(items)}\n")
    for i, item in enumerate(items, 1):
        review_type = item.get('review_type', 'unknown')
        type_label = {
            'ai_chat': 'AI Chat',
            'external_link': 'Link',
            'uninformative': 'Low-info',
            'instagram_long_reel': 'Long Reel',
        }
        badge = type_label.get(review_type, review_type)
        print(f"  {i}. [{badge}] [{item.get('channel', '?')}] msg_id={item.get('message_id', '?')}")
        url = item.get('url', '')
        if url:
            print(f"     URL: {url}")
        reason = item.get('reason', '')
        if reason and not url:
            print(f"     Reason: {reason}")
        if item.get("message_text"):
            preview = item["message_text"][:80]
            print(f"     Text: {preview}...")
        print(f"     File: {item['_filepath']}")
        print()

    print(f"Всего: {len(items)} элемент(ов) ожидают ревью.")
    print("Для веб-интерфейса: python main.py review --web")
    print("Или отредактируй JSON файл вручную и запусти: python main.py load")


def cmd_status():
    """Show current pipeline status."""
    progress = get_all_progress()

    print("\n📊 Pipeline Status")
    print("═" * 50)
    print(f"Last run: {progress.get('last_run', 'Never')}")
    print()

    channels = progress.get("channels", {})
    if not channels:
        print("  No channels processed yet.")
    else:
        for ch_id, data in sorted(channels.items()):
            display_name = data.get("title", ch_id)
            last_id = data.get("last_message_id", 0)
            updated = data.get("updated_at", "?")
            print(f"  📌 {display_name}")
            print(f"     Last message ID: {last_id}")
            print(f"     Updated: {updated}")

    txt_count = sum(1 for _ in config.NORMALIZED_DIR.rglob("*.txt"))
    print(f"\n  📄 Normalized files: {txt_count}")

    review_count = len(get_pending_reviews())
    if not config.WIKI_ENABLED:
        print("  Wiki: disabled (WIKI_ENABLED=false)")
    else:
        try:
            current_wiki = wiki_status()
        except Exception as exc:
            print(f"  Wiki: unavailable ({exc})")
        else:
            print(
                "  Wiki: "
                f"{current_wiki['approved_concepts']} approved concepts, "
                f"{current_wiki['pending_proposals']} concept proposals, "
                f"{current_wiki['pending_hierarchy']} hierarchy proposals, "
                f"{current_wiki['pending_ambiguities']} ambiguities"
            )
    print(f"  🔍 Pending reviews: {review_count}")


def cmd_quality():
    """Show graph quality diagnostics."""
    print()
    print(build_quality_report())


async def cmd_baseline_probe(limit: int = 3) -> None:
    metadata_path = write_baseline_metadata(collect_baseline_metadata())
    report = await run_baseline_probe(limit=limit)
    metadata_path, report_path = write_probe_report(report)

    print("Baseline model probe complete.")
    print(f"  Query model: {report.query_model}")
    print(f"  Query base URL: {report.query_base_url}")
    print(f"  Mode: {report.mode}")
    print(f"  Stable cases: {report.stable_count}/{len(report.results)}")
    print(f"  Metadata: {metadata_path}")
    print(f"  Report: {report_path}")

def _print_entity_autofix_summary(merges: list[dict[str, Any]]) -> None:
    """Print the entity alias fixes applied after load/rebuild."""
    print()
    print("Автофикс alias-дублей:")
    for merge in merges:
        print(f"  {', '.join(merge['sources'])} -> {merge['target']}")
