"""Argument parser and dispatcher for the GeoSpoiler RAG CLI."""

import argparse
import asyncio
import sys
from pathlib import Path

from geospoiler import config
from geospoiler.cli.pipeline import (
    _maybe_launch_reviewer,
    _print_load_summary,
    _print_run_summary,
    cmd_baseline_probe,
    cmd_enrich,
    cmd_fetch,
    cmd_load,
    cmd_normalize,
    cmd_quality,
    cmd_rebuild,
    cmd_rebuild_embedding,
    cmd_review,
    cmd_run,
    cmd_status,
)
from geospoiler.cli.query import _QUERY_MODES, _QUERY_PROFILES, _default_query_mode, cmd_query, cmd_search
from geospoiler.cli.runtime import setup_logging
from geospoiler.cli.tools import (
    cmd_experiments_index,
    cmd_fts_rebuild,
    cmd_fts_search,
    cmd_media_backfill,
    cmd_registry_rebuild,
    cmd_registry_resolve,
    cmd_transcribe_backfill,
    cmd_validate_enriched,
)
from geospoiler.cli.wiki import cmd_wiki_rebuild, cmd_wiki_run, cmd_wiki_search, cmd_wiki_status
from geospoiler.providers import llm_backend

CLI_DESCRIPTION = "Telegram-to-RAG pipeline for GeoSpoiler local memory."


def _add_llm_profile(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--llm-profile",
        choices=("current", "luna"),
        help="Text LLM backend for this command (default: LLM_PROFILE).",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    subparsers = parser.add_subparsers(dest="command")

    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("limit", nargs="?", type=int)

    normalize = _add_llm_profile(subparsers.add_parser("normalize"))
    normalize.add_argument("limit", nargs="?", type=int)

    enrich = _add_llm_profile(subparsers.add_parser("enrich"))
    enrich.add_argument("--channel", dest="channel_filter")
    enrich.add_argument("--force", action="store_true")

    load = _add_llm_profile(subparsers.add_parser("load"))
    load.add_argument(
        "--manifest",
        dest="load_manifest",
        help="Load only accepted Enriched cards listed in a JSON manifest",
    )
    load.add_argument(
        "--reuse-processed",
        action="store_true",
        help="Reuse only processed graph documents whose stored graph_text matches the manifest",
    )
    load.add_argument(
        "--allow-legacy-manifest",
        action="store_true",
        help="Explicitly allow accepted_manifest_v1 (never release-eligible)",
    )

    run = _add_llm_profile(subparsers.add_parser("run"))
    run.add_argument("limit", nargs="?", type=int)

    _add_llm_profile(subparsers.add_parser("rebuild"))

    subparsers.add_parser("rebuild-embedding")

    query = _add_llm_profile(subparsers.add_parser("query"))
    query.add_argument("query_args", nargs="*")

    search = _add_llm_profile(subparsers.add_parser("search"))
    search.add_argument("query", nargs="*")
    search.add_argument("--mode", default="recall")

    baseline = subparsers.add_parser("baseline")
    baseline_sub = baseline.add_subparsers(dest="subcommand")
    baseline_probe = baseline_sub.add_parser("probe")
    baseline_probe.add_argument("limit", nargs="?", type=int, default=3)

    experiments = subparsers.add_parser("experiments")
    experiments_sub = experiments.add_subparsers(dest="subcommand")
    experiments_sub.add_parser("index")

    fts = subparsers.add_parser("fts")
    fts_sub = fts.add_subparsers(dest="subcommand")
    fts_sub.add_parser("rebuild")
    fts_search = fts_sub.add_parser("search")
    fts_search.add_argument("query", nargs="*")
    fts_limit = fts_search.add_mutually_exclusive_group()
    fts_limit.add_argument("--top-k", type=int, default=10)
    fts_limit.add_argument("--all", action="store_true", dest="all_results")
    fts_search.add_argument("--compare-shadow", action="store_true")

    registry = subparsers.add_parser("registry")
    registry_sub = registry.add_subparsers(dest="subcommand")
    registry_sub.add_parser("rebuild")
    registry_resolve = registry_sub.add_parser("resolve")
    registry_resolve.add_argument("source_id", nargs="*")

    media_backfill = subparsers.add_parser("media-backfill")
    selector = media_backfill.add_mutually_exclusive_group(required=True)
    selector.add_argument("--all", action="store_true", dest="all_media")
    selector.add_argument("--limit", type=int)
    selector.add_argument("--message-id", type=int, action="append", dest="message_ids")
    media_backfill.add_argument("--channel")
    execution = media_backfill.add_mutually_exclusive_group()
    execution.add_argument("--dry-run", action="store_true")
    execution.add_argument("--execute-live", action="store_true")
    media_backfill.add_argument("--report", type=Path, default=config.STATE_DIR / "media_backfill_report.json")

    transcribe = subparsers.add_parser("transcribe")
    transcribe_sub = transcribe.add_subparsers(dest="subcommand")
    backfill = transcribe_sub.add_parser("backfill")
    backfill.add_argument("--limit", type=int, default=3)
    backfill.add_argument("--channel")
    backfill.add_argument("--media-type", choices=("video", "audio", "voice"))
    backfill_execution = backfill.add_mutually_exclusive_group()
    backfill_execution.add_argument("--dry-run", action="store_true")
    backfill_execution.add_argument("--execute-live", action="store_true")

    validate = subparsers.add_parser("validate")
    validate_sub = validate.add_subparsers(dest="subcommand")
    validate_enriched = validate_sub.add_parser("enriched")
    validate_enriched.add_argument("--fail-on-error", action="store_true")

    subparsers.add_parser("quality")

    review = _add_llm_profile(subparsers.add_parser("review"))
    review.add_argument("--web", action="store_true")

    wiki = subparsers.add_parser("wiki")
    wiki_sub = wiki.add_subparsers(dest="subcommand")
    wiki_run = _add_llm_profile(wiki_sub.add_parser("run"))
    wiki_run.add_argument("paths", nargs="*")
    wiki_run.add_argument("--no-luna", action="store_true")
    wiki_rebuild = _add_llm_profile(wiki_sub.add_parser("rebuild"))
    wiki_rebuild.add_argument("--no-luna", action="store_true")
    wiki_search = wiki_sub.add_parser("search")
    wiki_search.add_argument("query", nargs="*")
    wiki_search.add_argument("--limit", type=int, default=12)
    wiki_sub.add_parser("status")
    wiki_sub.add_parser("review")

    subparsers.add_parser("status")
    return parser


def _parse_query_tail(query_args: list[str]) -> tuple[str, str, str | None] | None:
    mode = _default_query_mode()
    query_profile = None
    args = list(query_args)
    if args and args[-1].lower() in _QUERY_PROFILES:
        query_profile = args[-1].lower()
        args = args[:-1]
    if args and args[-1].lower() in _QUERY_MODES:
        mode = args[-1].lower()
        args = args[:-1]
    question = " ".join(args).strip()
    if not question:
        return None
    return question, mode, query_profile


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    command = args.command
    if not command:
        parser.print_help()
        return

    selected_profile = getattr(args, "llm_profile", None)
    if selected_profile:
        llm_backend.set_profile(selected_profile)
    luna_disabled_for_wiki = command == "wiki" and getattr(args, "no_luna", False)
    if (
        llm_backend.active_profile() == "luna"
        and not luna_disabled_for_wiki
        and command in {
            "normalize",
            "enrich",
            "load",
            "rebuild",
            "run",
            "query",
            "search",
            "review",
            "wiki",
        }
    ):
        try:
            llm_backend.validate_luna_configuration()
        except llm_backend.LLMBackendError as exc:
            parser.error(str(exc))

    if command == "baseline":
        if args.subcommand == "probe":
            asyncio.run(cmd_baseline_probe(limit=args.limit))
            return
        print("Usage: python main.py baseline probe [N]")
        return

    if command == "experiments":
        if args.subcommand == "index":
            cmd_experiments_index()
            return
        print("Usage: python main.py experiments index")
        return

    setup_logging()

    if command == "fetch":
        asyncio.run(cmd_fetch(args.limit))
        return

    if command == "normalize":
        async def _fetch_and_normalize():
            channel_messages = await cmd_fetch(limit=args.limit)
            total = sum(len(msgs) for _, msgs in channel_messages)
            if total == 0:
                print("No new messages found.")
                return
            normalize_stats = await cmd_normalize(channel_messages)
            print(f"\nDone: {normalize_stats.normalized_messages} texts normalized to output/normalized/")
            _print_run_summary(channel_messages, normalize_stats)
            print("Run 'python main.py enrich' next, then optionally 'python main.py load'.")
            _maybe_launch_reviewer()

        asyncio.run(_fetch_and_normalize())
        return

    if command == "load":
        if args.load_manifest:
            load_stats = asyncio.run(
                cmd_load(
                    manifest_path=args.load_manifest,
                    reuse_processed=args.reuse_processed,
                    allow_legacy_manifest=args.allow_legacy_manifest,
                )
            )
        else:
            # Preserve the zero-argument call contract for existing integrations.
            load_stats = asyncio.run(cmd_load())
        _print_load_summary(load_stats)
        return

    if command == "rebuild":
        asyncio.run(cmd_rebuild())
        return

    if command == "rebuild-embedding":
        asyncio.run(cmd_rebuild_embedding())
        return

    if command == "enrich":
        cmd_enrich(channel_filter=args.channel_filter, force=args.force)
        _maybe_launch_reviewer()
        return

    if command == "run":
        asyncio.run(cmd_run(args.limit))
        _maybe_launch_reviewer()
        return

    if command == "query":
        parsed = _parse_query_tail(args.query_args)
        if parsed is None:
            print('Usage: python main.py query "??? ??????" [mode] [profile]')
            return
        question, mode, query_profile = parsed
        asyncio.run(cmd_query(question, mode, query_profile))
        return

    if command == "search":
        query = " ".join(args.query).strip()
        if not query:
            print('Usage: python main.py search "query" [--mode recall|broll|thesis|entity|shadow]')
            return
        asyncio.run(cmd_search(query, args.mode))
        return

    if command == "fts":
        if args.subcommand == "rebuild":
            cmd_fts_rebuild()
            return
        if args.subcommand == "search":
            query = " ".join(args.query).strip()
            if not query:
                print('Usage: python main.py fts search "query" [--top-k N | --all] [--compare-shadow]')
                return
            top_k = None if args.all_results else args.top_k
            cmd_fts_search(query, top_k=top_k, compare_shadow=args.compare_shadow)
            return
        print(
            'Usage: python main.py fts rebuild | python main.py fts search "query" '
            '[--top-k N | --all] [--compare-shadow]'
        )
        return

    if command == "registry":
        if args.subcommand == "rebuild":
            cmd_registry_rebuild()
            return
        if args.subcommand == "resolve":
            source_id = " ".join(args.source_id).strip()
            if not source_id:
                print("Usage: python main.py registry resolve SOURCE_ID")
                return
            cmd_registry_resolve(source_id)
            return
        print("Usage: python main.py registry rebuild | python main.py registry resolve SOURCE_ID")
        return

    if command == "media-backfill":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        selected_ids = set(args.message_ids or [])
        cmd_media_backfill(
            limit=None if args.all_media or selected_ids else args.limit,
            channel=args.channel,
            message_ids=selected_ids,
            dry_run=not args.execute_live,
            report_path=args.report,
        )
        return

    if command == "transcribe":
        if args.subcommand == "backfill":
            cmd_transcribe_backfill(
                limit=args.limit,
                channel=args.channel,
                media_type=args.media_type,
                dry_run=not args.execute_live,
            )
            return
        print(
            "Usage: python main.py transcribe backfill "
            "[--limit N] [--channel NAME] [--media-type video|audio|voice] "
            "[--dry-run | --execute-live]"
        )
        return

    if command == "validate":
        if args.subcommand == "enriched":
            cmd_validate_enriched(fail_on_error=args.fail_on_error)
            return
        print("Usage: python main.py validate enriched [--fail-on-error]")
        return

    if command == "quality":
        cmd_quality()
        return

    if command == "review":
        cmd_review(web=args.web)
        return

    if command == "wiki":
        if not config.WIKI_ENABLED:
            print(
                "Wiki disabled (WIKI_ENABLED=false). "
                "Set WIKI_ENABLED=true to enable its commands."
            )
            return
        if args.subcommand == "run":
            stats = cmd_wiki_run(
                args.paths,
                use_luna=False if args.no_luna else None,
            )
            if stats.review_counts.total:
                _maybe_launch_reviewer()
            return
        if args.subcommand == "rebuild":
            stats = cmd_wiki_rebuild(
                use_luna=False if args.no_luna else None,
            )
            if stats.review_counts.total:
                _maybe_launch_reviewer()
            return
        if args.subcommand == "search":
            query = " ".join(args.query).strip()
            if not query:
                print('Usage: python main.py wiki search "query" [--limit N]')
                return
            cmd_wiki_search(query, limit=args.limit)
            return
        if args.subcommand == "status":
            cmd_wiki_status()
            return
        if args.subcommand == "review":
            cmd_review(web=True)
            return
        print(
            "Usage: python main.py wiki "
            "run [PATH ...] | rebuild | search QUERY | status | review"
        )
        return

    if command == "status":
        cmd_status()
        return

    print(f"Unknown command: {command}")
    parser.print_help()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    dispatch(args, parser)
