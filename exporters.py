import csv
from dataclasses import is_dataclass
import io
import json
from typing import Any, Sequence

from models import Bookmark, Repository, SearchReport


def export_to_csv(items: Sequence[Repository | Bookmark | dict[str, Any]]) -> str:
    """Exports repositories or bookmarks to CSV string format."""
    if not items:
        return "name,full_name,url,language,stars,forks,quality_score,grade,recommendation,license,description,topics,pushed_at\n"

    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Repository):
            rows.append(item.to_flat_dict())
        elif isinstance(item, Bookmark):
            rows.append(
                {
                    "id": item.repo_id,
                    "name": item.full_name.rsplit("/", 1)[-1],
                    "full_name": item.full_name,
                    "owner": item.full_name.split("/")[0] if "/" in item.full_name else "",
                    "url": item.url,
                    "description": item.description or "",
                    "language": item.language or "Unknown",
                    "stars": item.stars or 0,
                    "forks": item.forks or 0,
                    "open_issues_and_prs": 0,
                    "license": item.license or "None",
                    "quality_score": item.score or "",
                    "grade": item.grade or "",
                    "recommendation": "",
                    "size_kb": 0,
                    "topics": ", ".join(item.tags),
                    "pushed_at": "",
                    "created_at": item.bookmarked_at[:10] if item.bookmarked_at else "",
                    "updated_at": "",
                    "archived": False,
                    "fork": False,
                }
            )
        elif isinstance(item, dict):
            rows.append(item)

    fieldnames = [
        "name",
        "full_name",
        "url",
        "language",
        "stars",
        "forks",
        "open_issues_and_prs",
        "quality_score",
        "grade",
        "recommendation",
        "license",
        "description",
        "topics",
        "pushed_at",
        "created_at",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def export_to_json(
    data: SearchReport | Sequence[Repository | Bookmark] | dict[str, Any],
    indent: int = 2,
) -> str:
    """Exports data to formatted JSON string."""
    if isinstance(data, SearchReport):
        return json.dumps(data.to_dict(), indent=indent, default=str)
    if isinstance(data, (list, tuple)):
        clean_list = []
        for x in data:
            if hasattr(x, "to_dict"):
                clean_list.append(x.to_dict())
            elif is_dataclass(x):
                clean_list.append(asdict(x))
            else:
                clean_list.append(x)
        return json.dumps(clean_list, indent=indent, default=str)
    if hasattr(data, "to_dict"):
        return json.dumps(data.to_dict(), indent=indent, default=str)
    return json.dumps(data, indent=indent, default=str)


def export_to_markdown(
    items: Sequence[Repository | Bookmark],
    title: str = "GitHub Project Hunter Results",
) -> str:
    """Exports items to a clean Markdown table."""
    lines = [f"# {title}", "", f"*Exported {len(items)} repositories*", ""]
    lines.append(
        "| Score | Grade | Repository | Language | Stars | Forks | License | Description |"
    )
    lines.append(
        "| :---: | :---: | :--- | :--- | :---: | :---: | :--- | :--- |"
    )

    for item in items:
        if isinstance(item, Repository):
            score = item.quality_score.overall_score if item.quality_score else "-"
            grade = item.quality_score.grade if item.quality_score else "-"
            name = f"[{item.full_name}]({item.url})"
            lang = item.language or "N/A"
            stars = f"{item.stars:,}" if item.stars is not None else "0"
            forks = f"{item.forks:,}" if item.forks is not None else "0"
            lic = item.license_spdx or item.license_key or "N/A"
            desc = (item.description or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| **{score}** | `{grade}` | {name} | {lang} | {stars} | {forks} | {lic} | {desc} |"
            )
        elif isinstance(item, Bookmark):
            score = item.score or "-"
            grade = item.grade or "-"
            name = f"[{item.full_name}]({item.url})"
            lang = item.language or "N/A"
            stars = f"{item.stars:,}" if item.stars is not None else "0"
            forks = f"{item.forks:,}" if item.forks is not None else "0"
            lic = item.license or "N/A"
            desc = (item.description or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| **{score}** | `{grade}` | {name} | {lang} | {stars} | {forks} | {lic} | {desc} |"
            )

    return "\n".join(lines) + "\n"
