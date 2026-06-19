#!/usr/bin/env python3
"""Remove shallow auto-generated wiki pages and rebuild topic hubs."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

from ingest_wiki import (
    LOGS_DIR,
    SOURCE_ORGS,
    SOURCE_ORDER,
    WIKI_DIR,
    build_organisation_page,
    build_topic_page,
    load_pages,
    update_wiki_index,
    wiki_filename,
)
from collections import defaultdict

CONTENT_CATEGORIES = ("procedures", "concepts", "courts", "regulations", "funding")


def is_shallow_wiki_page(text: str) -> bool:
    """True when the page is a link-stub without usable guidance."""
    summary_m = re.search(r"## Summary\n\n(.*?)\n\n## Key Information", text, re.S)
    practical_m = re.search(r"## Practical Guidance\n\n(.*?)\n\n## Related Concepts", text, re.S)
    if not summary_m or not practical_m:
        return True

    summary = summary_m.group(1).strip()
    practical = practical_m.group(1).strip()
    lines = [ln.strip() for ln in practical.splitlines() if ln.startswith("- ") or re.match(r"^\d+\.\s+", ln.strip())]

    if "Take 5 minutes to tell us" in summary or "Help us improve our website" in summary:
        return True

    if summary.endswith("…") or summary.endswith("..."):
        return True

    if "Full step-by-step guidance:" in practical or practical.startswith("- Refer to source"):
        substantive = [
            ln
            for ln in lines
            if "Full step-by-step guidance:" not in ln
            and "Refer to source" not in ln
            and not ln.strip().startswith("- Source:")
            and len(ln) > 100
        ]
        if not substantive:
            return True

    if summary in ("_See source material._", "") and len(lines) <= 2:
        return True

    return False


def remove_shallow_pages() -> tuple[int, list[Path]]:
    removed: list[Path] = []
    for category in CONTENT_CATEGORIES:
        category_dir = WIKI_DIR / category
        if not category_dir.exists():
            continue
        for path in category_dir.glob("*.md"):
            if path.name == "_index.md":
                continue
            if is_shallow_wiki_page(path.read_text(encoding="utf-8")):
                path.unlink()
                removed.append(path)
    return len(removed), removed


def rebuild_topic_hubs() -> int:
    by_topic: dict[str, list] = defaultdict(list)
    for source_key in SOURCE_ORDER:
        for page in load_pages(source_key):
            topic = page.resolved_topic
            if topic and topic != "General":
                by_topic[topic].append(page)

    topics_dir = WIKI_DIR / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for topic, pages in sorted(by_topic.items()):
        org_names = {p.source_name for p in pages}
        path = topics_dir / wiki_filename(topic)
        path.write_text(build_topic_page(topic, pages, org_names), encoding="utf-8")
        count += 1
    return count


def rebuild_organisations() -> None:
    for source_key in SOURCE_ORDER:
        pages = load_pages(source_key)
        org_name = SOURCE_ORGS[source_key]
        path = WIKI_DIR / "organisations" / wiki_filename(org_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_organisation_page(org_name, source_key, pages), encoding="utf-8")


def rebuild_category_indexes() -> None:
    for category in CONTENT_CATEGORIES + ("topics", "organisations"):
        category_dir = WIKI_DIR / category
        if not category_dir.exists():
            continue
        titles = sorted(
            p.stem for p in category_dir.glob("*.md") if p.name != "_index.md"
        )
        title = category.replace("-", " ").title()
        lines = [f"# {title}", "", f"Pages in `wiki/{category}/`.", ""]
        if titles:
            for name in titles:
                lines.append(f"- [[{name}]]")
        else:
            lines.append("_No pages in this category. Use topic hubs and source documents._")
        lines.append("")
        (category_dir / "_index.md").write_text("\n".join(lines), encoding="utf-8")


def append_cleanup_log(removed_count: int, topics_count: int) -> None:
    log_path = LOGS_DIR / "ingestion-log.md"
    row = (
        f"| {date.today().isoformat()} | Wiki cleanup | {removed_count} removed | "
        f"{topics_count} topic hubs | 0 | shallow page purge |"
    )
    content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    if row not in content:
        content += row + "\n"
        log_path.write_text(content, encoding="utf-8")


def main() -> int:
    removed_count, _ = remove_shallow_pages()
    rebuild_topic_hubs()
    rebuild_organisations()
    rebuild_category_indexes()
    update_wiki_index()
    append_cleanup_log(removed_count, len(list((WIKI_DIR / "topics").glob("*.md"))))
    print(f"Removed {removed_count} shallow wiki pages.")
    print(f"Topic hubs: {len(list((WIKI_DIR / 'topics').glob('*.md')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
