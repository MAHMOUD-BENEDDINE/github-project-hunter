import argparse
from datetime import date
import json
from pathlib import Path
import sys

from config import Settings
from exporters import export_to_csv, export_to_json, export_to_markdown
from github_client import GitHubAPIError, GitHubClient
from search_engine import SearchEngine, SearchFilters, build_query
from storage import StorageManager


def make_parser():
    parser = argparse.ArgumentParser(
        description="Discover public GitHub repositories for a project idea with quality scores."
    )
    parser.add_argument("idea", nargs="?", default="", help="Project description or search idea")
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=[],
        help="Override derived terms; quote multiword phrases.",
    )
    parser.add_argument("--language", help="Primary programming language (e.g. Python, Java, Rust)")
    parser.add_argument("--min-stars", type=int, default=0, help="Minimum stars")
    parser.add_argument("--max-stars", type=int, help="Maximum stars")
    parser.add_argument("--min-forks", type=int, default=0, help="Minimum forks")
    parser.add_argument("--license", dest="license_key", help="SPDX license key (e.g. mit, apache-2.0)")
    parser.add_argument("--topic", action="append", default=[], help="Required topic tag (repeatable)")
    parser.add_argument("--include-archived", action="store_true", help="Include archived repositories")
    parser.add_argument("--include-forks", action="store_true", help="Include forked repositories")
    parser.add_argument("--max-results", type=int, default=20, help="Maximum matching repositories to return")
    parser.add_argument("--scan-limit", type=int, default=200, help="Candidate inspection budget (max 1000)")
    parser.add_argument(
        "--sort",
        choices=["best-match", "stars", "forks", "updated", "quality-score"],
        default="best-match",
        help="Result ordering criteria",
    )
    parser.add_argument(
        "--format",
        choices=["json", "csv", "table"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Save output to file instead of stdout",
    )
    parser.add_argument(
        "--rate-limit",
        action="store_true",
        help="Display current GitHub API rate limit quotas and exit",
    )
    parser.add_argument(
        "--bookmarks",
        action="store_true",
        help="List saved repository bookmarks and exit",
    )
    parser.add_argument(
        "--enrich-readme",
        action="store_true",
        help="Fetch and attach README previews for discovered repositories",
    )
    for prefix in ("created", "pushed", "updated"):
        for suffix in ("after", "before"):
            parser.add_argument(
                f"--{prefix}-{suffix}",
                type=date.fromisoformat,
                metavar="YYYY-MM-DD",
                help=f"Filter repositories {prefix} {suffix} date",
            )
    parser.add_argument(
        "--show-query",
        action="store_true",
        help="Print the generated GitHub query without making API requests.",
    )
    return parser


def format_table(repositories) -> str:
    if not repositories:
        return "No matching repositories found."
    lines = [
        f"{'Score':<6} {'Grade':<6} {'Stars':<8} {'Forks':<8} {'Language':<12} {'Repository':<35} {'URL'}",
        "-" * 105,
    ]
    for repo in repositories:
        score_val = str(repo.quality_score.overall_score) if repo.quality_score else "-"
        grade_val = repo.quality_score.grade if repo.quality_score else "-"
        stars_val = f"{repo.stars:,}" if repo.stars is not None else "0"
        forks_val = f"{repo.forks:,}" if repo.forks is not None else "0"
        lang_val = (repo.language or "Unknown")[:11]
        name_val = (repo.full_name)[:34]
        lines.append(
            f"{score_val:<6} {grade_val:<6} {stars_val:<8} {forks_val:<8} {lang_val:<12} {name_val:<35} {repo.url}"
        )
    return "\n".join(lines)


def main(argv=None):
    args = make_parser().parse_args(argv)
    settings = Settings.from_env()
    storage = StorageManager(data_dir=settings.data_dir)

    # Handle --rate-limit command
    if args.rate_limit:
        with GitHubClient(settings) as client:
            rate_info = client.get_rate_limit()
            print(json.dumps(rate_info, indent=2))
        return 0

    # Handle --bookmarks command
    if args.bookmarks:
        bookmarks = storage.list_bookmarks()
        if args.format == "csv":
            content = export_to_csv(bookmarks)
        elif args.format == "table":
            if not bookmarks:
                content = "No bookmarks found."
            else:
                lines = [f"{'Stars':<8} {'Score':<6} {'Grade':<6} {'Repository':<35} {'Notes'}", "-" * 80]
                for b in bookmarks:
                    lines.append(f"{b.stars or 0:<8} {b.score or '-':<6} {b.grade or '-':<6} {b.full_name:<35} {b.notes}")
                content = "\n".join(lines)
        else:
            content = export_to_json(bookmarks)
        print(content)
        return 0

    if not args.idea:
        print("Error: A project idea or command flag (--rate-limit, --bookmarks) is required.", file=sys.stderr)
        return 1

    try:
        filters = SearchFilters(
            language=args.language,
            min_stars=args.min_stars,
            max_stars=args.max_stars,
            min_forks=args.min_forks,
            license_key=args.license_key,
            topics=tuple(args.topic),
            include_archived=args.include_archived,
            include_forks=args.include_forks,
            **{
                f"{prefix}_{suffix}": getattr(args, f"{prefix}_{suffix}")
                for prefix in ("created", "pushed", "updated")
                for suffix in ("after", "before")
            },
        )

        if args.show_query:
            query, terms = build_query(args.idea, filters, args.keywords)
            result = json.dumps({"query": query, "search_terms": terms}, indent=2)
            if args.output:
                args.output.write_text(result, encoding="utf-8")
            else:
                print(result)
            return 0

        with GitHubClient(settings) as client:
            engine = SearchEngine(client)
            report = engine.search(
                args.idea,
                filters,
                keywords=args.keywords,
                max_results=args.max_results,
                scan_limit=args.scan_limit,
                sort=args.sort,
                enrich_readme=args.enrich_readme,
            )

        # Save to search history
        try:
            storage.add_history_entry(
                idea=args.idea,
                keywords=tuple(args.keywords),
                filters={"language": args.language, "min_stars": args.min_stars, "sort": args.sort},
                result_count=len(report.repositories),
                total_count=report.total_count,
            )
        except Exception:
            pass

        # Format output
        if args.format == "csv":
            formatted = export_to_csv(report.repositories)
        elif args.format == "table":
            formatted = format_table(report.repositories)
            if report.warnings:
                formatted += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in report.warnings)
        else:
            formatted = export_to_json(report)

        if args.output:
            args.output.write_text(formatted, encoding="utf-8")
            print(f"Results successfully written to {args.output}")
        else:
            print(formatted)

        return 2 if report.stop_reason == "api_error" else 0

    except (ValueError, GitHubAPIError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
